"""Oráculo service layer: persistence, synchronization, calibration and learning loop."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Query

from oracle_engine import calibrate, predict, evaluate_prediction
from oracle_store import (
    ensure_schema, database_info, upsert_results, get_results, get_prediction,
    save_prediction, save_model, load_model, pending_predictions,
    list_predictions, prediction_counts, result_counts, mark_prediction_evaluated,
    log_sync, log_learning_run, canonical_oracle_draw_time,
)

ORACLE_LOTTERIES = ["PT-RIO", "Para Todos-SP", "LOOK Goiás", "LNS Nacional"]
ORACLE_MODALITIES = ["milhar", "centena", "dezena", "grupo", "duque", "terno", "passe"]
BRAZIL_TZ = ZoneInfo("America/Sao_Paulo")


def _target_cutoff(draw_date: str, draw_time: str) -> str:
    return f"{draw_date}T{draw_time}:00-03:00"


def register_oracle_routes(app, fetch_look: Callable, fetch_rio: Callable, fetch_aggregated: Callable):
    router = APIRouter(prefix="/api/oracle", tags=["oracle"])
    ensure_schema()

    async def sync_one(lottery: str, day: date) -> tuple[int, str]:
        if lottery == "LOOK Goiás":
            rows = await fetch_look(day)
            return upsert_results(rows), "LOOK"
        if lottery == "PT-RIO":
            rows = await fetch_rio(day)
            for row in rows:
                row["draw_time"] = canonical_oracle_draw_time("PT-RIO", row.get("draw_time", ""), row.get("draw_code"))
            return upsert_results(rows), "PT-RIO"
        if lottery in {"Para Todos-SP", "LNS Nacional"}:
            rows = await fetch_aggregated(day, lottery)
            return upsert_results(rows), lottery
        raise HTTPException(400, f"Loteria não suportada: {lottery}")

    async def sync_today_all() -> tuple[int, list[str]]:
        today = datetime.now(BRAZIL_TZ).date()
        total = 0
        errors: list[str] = []
        for lot in ORACLE_LOTTERIES:
            try:
                n, _ = await sync_one(lot, today)
                total += n
            except Exception as exc:
                errors.append(f"{today.isoformat()} {lot}: {type(exc).__name__}: {exc}")
        return total, errors

    async def learning_cycle(trigger: str = "manual") -> dict:
        # 1) Always sync today first. Do not depend on the pending flag to decide
        # whether a just-published result should enter the database.
        synced_rows, sync_errors = await sync_today_all()

        # 2) Look at ALL recent predictions, not only pending=True. This makes the
        # loop resilient to legacy/inconsistent evaluation flags after a deploy.
        predictions = list_predictions(limit=500)
        pending_seen = sum(1 for p in predictions if not bool(p.get("evaluated")))
        evaluated = 0
        learned = []
        errors = list(sync_errors)
        touched = set()

        for p in predictions:
            target_time = canonical_oracle_draw_time(p["lottery"], p["draw_time"])
            rows = get_results(p["lottery"], limit=3000)
            actual = [
                r for r in rows
                if r["draw_date"] == p["draw_date"]
                and canonical_oracle_draw_time(p["lottery"], r["draw_time"], r.get("draw_code")) == target_time
            ]
            if not actual:
                continue

            # Do not re-score a completed evaluation unless its JSON is missing.
            if p.get("evaluated") and p.get("evaluation_json"):
                continue

            try:
                candidates = p.get("candidates_json") or []
                ev = evaluate_prediction(p["prediction"], actual, p["modality"], candidates)
                ev["score"] = round(
                    float(bool(ev.get("exact")))
                    + 0.3 * float(bool(ev.get("tail")))
                    + 0.2 * float(bool(ev.get("group"))), 4
                )
                ev["target"] = f"{p['draw_date']}T{target_time}:00"
                ev["prediction_id"] = p["id"]
                ev["prediction"] = p["prediction"]
                ev["actual_prizes"] = [str(r["number"]).zfill(4) for r in actual[:10]]
                mark_prediction_evaluated(int(p["id"]), ev)
                evaluated += 1
                touched.add((p["lottery"], p["modality"]))
            except Exception as exc:
                errors.append(f"evaluate #{p.get('id')}: {type(exc).__name__}: {exc}")

        # 3) Recalibrate only modalities that actually received a new evaluation.
        for lot, modality in sorted(touched):
            try:
                rows = get_results(lot, limit=3000)
                if len(rows) >= 25:
                    result = calibrate(rows, modality, max_cases=40)
                    save_model(lot, modality, result.model_name, result.weights, result.score, result.cases)
                    learned.append({
                        "lottery": lot, "modality": modality, "model": result.model_name,
                        "score": result.score, "cases": result.cases, "metrics": result.metrics,
                    })
            except Exception as exc:
                errors.append(f"calibration {lot}/{modality}: {type(exc).__name__}: {exc}")

        log_learning_run(trigger, synced_rows, evaluated, pending_seen, errors)
        counts = prediction_counts()
        return {
            "status": "ok", "trigger": trigger, "synced_rows": synced_rows,
            "evaluated": evaluated, "pending_seen": pending_seen,
            "predictions": counts, "learned": learned, "errors": errors,
        }

    @router.get("/status")
    def status():
        return {
            "ok": True,
            "database": database_info(),
            "lotteries": ORACLE_LOTTERIES,
            "modalities": ORACLE_MODALITIES,
            "predictions": prediction_counts(),
            "results": result_counts(),
            "message": "Motor histórico e calibração disponíveis.",
        }

    @router.get("/diagnose")
    def diagnose(limit: int = Query(10, ge=1, le=50)):
        recent = list_predictions(limit=limit)
        compact = []
        for p in recent:
            compact.append({
                "id": p["id"], "lottery": p["lottery"], "draw_date": p["draw_date"],
                "draw_time": p["draw_time"], "modality": p["modality"],
                "prediction": p["prediction"], "evaluated": bool(p.get("evaluated")),
                "created_at": str(p.get("created_at")),
                "has_evaluation": bool(p.get("evaluation_json")),
            })
        return {
            "status": "ok", "database": database_info(), "predictions": prediction_counts(),
            "results": result_counts(), "recent_predictions": compact,
        }

    @router.get("/sync")
    async def sync(draw_date: Optional[date] = Query(default=None), lottery: Optional[str] = Query(default=None)):
        day = draw_date or datetime.now(BRAZIL_TZ).date()
        lots = [lottery] if lottery else list(ORACLE_LOTTERIES)
        inserted = 0; errors: list[str] = []
        for lot in lots:
            try:
                n, _ = await sync_one(lot, day); inserted += n
            except Exception as exc:
                errors.append(f"{lot}: {type(exc).__name__}: {exc}")
        log_sync(day.isoformat(), lots, inserted, errors)
        return {"status": "ok", "date": day.isoformat(), "lotteries": lots, "inserted": inserted, "errors": errors}

    @router.get("/backfill")
    async def backfill(days: int = Query(default=7, ge=1, le=90), lottery: Optional[str] = Query(default=None)):
        today = datetime.now(BRAZIL_TZ).date()
        lots = [lottery] if lottery else list(ORACLE_LOTTERIES)
        summary = {lot: {"days": 0, "inserted": 0, "errors": 0} for lot in lots}
        all_errors = []
        for offset in range(days - 1, -1, -1):
            day = today - timedelta(days=offset)
            for lot in lots:
                try:
                    n, _ = await sync_one(lot, day)
                    summary[lot]["days"] += 1; summary[lot]["inserted"] += n
                except Exception as exc:
                    summary[lot]["errors"] += 1
                    all_errors.append(f"{day.isoformat()} {lot}: {type(exc).__name__}: {exc}")
        return {"status": "ok", "days": days, "summary": summary, "errors": all_errors[-50:]}

    @router.get("/calibrate")
    def calibrate_route(lottery: str = Query(...), modality: str = Query("milhar"), max_cases: int = Query(40, ge=10, le=100)):
        if lottery not in ORACLE_LOTTERIES: raise HTTPException(400, "Loteria não suportada")
        if modality not in ORACLE_MODALITIES: raise HTTPException(400, "Modalidade não suportada")
        rows = get_results(lottery, limit=3000)
        if len(rows) < 25: raise HTTPException(409, f"Histórico insuficiente: {len(rows)} registros. Faça um backfill primeiro.")
        result = calibrate(rows, modality, max_cases=max_cases)
        save_model(lottery, modality, result.model_name, result.weights, result.score, result.cases)
        return {
            "status": "ok", "lottery": lottery, "modality": modality, "model": result.model_name,
            "weights": result.weights, "score": result.score, "cases": result.cases, "metrics": result.metrics,
            "warning": "A calibração mede aderência histórica; não garante acertos futuros.",
        }

    @router.get("/predict")
    def predict_route(
        lottery: str = Query(...), draw_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
        draw_time: str = Query(..., pattern=r"^\d{2}:\d{2}$"), modality: str = Query("milhar"),
        force_retrain: bool = Query(False),
    ):
        if lottery not in ORACLE_LOTTERIES: raise HTTPException(400, "Loteria não suportada")
        if modality not in ORACLE_MODALITIES: raise HTTPException(400, "Modalidade não suportada")
        draw_time = canonical_oracle_draw_time(lottery, draw_time)

        # Immutable frozen record wins over any later request, including force_retrain.
        frozen = get_prediction(lottery, draw_date, draw_time, modality)
        if frozen:
            return {"status": "frozen", "prediction": frozen["prediction"], "record": frozen}

        cutoff = _target_cutoff(draw_date, draw_time)
        target_rows = get_results(lottery, limit=3000)
        target_exists = any(
            r["draw_date"] == draw_date
            and canonical_oracle_draw_time(lottery, r["draw_time"], r.get("draw_code")) == draw_time
            for r in target_rows
        )
        if target_exists:
            raise HTTPException(409, "O sorteio alvo já possui resultado armazenado e não tinha previsão congelada. Não é permitida previsão retroativa.")

        rows = get_results(lottery, cutoff_iso=cutoff, limit=3000)
        if len(rows) < 20:
            raise HTTPException(409, f"Histórico insuficiente antes do corte: {len(rows)} registros.")
        model = load_model(lottery, modality)
        if not model or force_retrain:
            cal = calibrate(rows, modality, max_cases=40)
            model_name, weights, score, cases = cal.model_name, cal.weights, cal.score, cal.cases
            save_model(lottery, modality, model_name, weights, score, cases)
        else:
            model_name, weights = model["model_name"], model["weights_json"]
        pack = predict(rows, modality, model_name, limit=50, target_date=date.fromisoformat(draw_date))
        payload = {
            "lottery": lottery, "draw_date": draw_date, "draw_time": draw_time, "modality": modality,
            "prediction": pack["prediction"], "candidates": pack["candidates"], "cutoff_iso": cutoff,
            "model_name": model_name, "model": weights,
            "context": {"history_rows": len(rows), "latest_draw": pack["latest_draw"], "calendar_group": pack["calendar_group"], "moment_groups": pack["moment_groups"]},
        }
        prediction_id = save_prediction(payload)
        frozen = get_prediction(lottery, draw_date, draw_time, modality)
        return {"status": "calculated", "prediction_id": prediction_id, **pack, "cutoff_iso": cutoff, "frozen_created_at": str(frozen.get("created_at")) if frozen else None}

    @router.get("/evaluate-pending")
    async def evaluate_pending(lottery: Optional[str] = None):
        # Backwards-compatible endpoint name; the new learning loop is broader and safer.
        result = await learning_cycle("evaluate-pending")
        if lottery:
            # Keep response shape useful for existing callers; actual filtering is done by the full loop.
            result["lottery_filter"] = lottery
        return result

    @router.get("/learn-now")
    async def learn_now():
        return await learning_cycle("learn-now")

    app.include_router(router)
    return router
