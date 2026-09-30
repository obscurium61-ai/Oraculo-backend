"""Oráculo service layer: persistence, synchronization, calibration and API routes."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Query

from oracle_engine import calibrate, predict, evaluate_prediction
from oracle_store import (
    ensure_schema, database_info, upsert_results, get_results, get_prediction,
    save_prediction, save_model, load_model, pending_predictions,
    mark_prediction_evaluated, log_sync,
)

ORACLE_LOTTERIES = ["PT-RIO", "Para Todos-SP", "LOOK Goiás", "LNS Nacional"]
ORACLE_MODALITIES = ["milhar", "centena", "dezena", "grupo", "duque", "terno", "passe"]


def _target_cutoff(draw_date: str, draw_time: str) -> str:
    return f"{draw_date}T{draw_time}:00-03:00"


def register_oracle_routes(
    app,
    fetch_look: Callable,
    fetch_rio: Callable,
    fetch_aggregated: Callable,
):
    router = APIRouter(prefix="/api/oracle", tags=["oracle"])

    ensure_schema()

    async def sync_one(lottery: str, day: date) -> tuple[int, str]:
        if lottery == "LOOK Goiás":
            rows = await fetch_look(day)
            return upsert_results(rows), "LOOK"
        if lottery == "PT-RIO":
            rows = await fetch_rio(day)
            return upsert_results(rows), "PT-RIO"
        if lottery in {"Para Todos-SP", "LNS Nacional"}:
            rows = await fetch_aggregated(day, lottery)
            return upsert_results(rows), lottery
        raise HTTPException(400, f"Loteria não suportada: {lottery}")

    @router.get("/status")
    def status():
        return {
            "ok": True,
            "database": database_info(),
            "lotteries": ORACLE_LOTTERIES,
            "modalities": ORACLE_MODALITIES,
            "message": "Motor histórico e calibração disponíveis.",
        }

    @router.get("/sync")
    async def sync(
        draw_date: Optional[date] = Query(default=None),
        lottery: Optional[str] = Query(default=None),
    ):
        day = draw_date or datetime.now(ZoneInfo("America/Sao_Paulo")).date()
        lots = [lottery] if lottery else list(ORACLE_LOTTERIES)
        inserted = 0
        errors: list[str] = []
        for lot in lots:
            try:
                n, _ = await sync_one(lot, day)
                inserted += n
            except Exception as exc:
                errors.append(f"{lot}: {type(exc).__name__}: {exc}")
        log_sync(day.isoformat(), lots, inserted, errors)
        return {"status": "ok", "date": day.isoformat(), "lotteries": lots, "inserted": inserted, "errors": errors}

    @router.get("/backfill")
    async def backfill(
        days: int = Query(default=7, ge=1, le=90),
        lottery: Optional[str] = Query(default=None),
    ):
        today = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
        lots = [lottery] if lottery else list(ORACLE_LOTTERIES)
        summary = {lot: {"days": 0, "inserted": 0, "errors": 0} for lot in lots}
        all_errors = []
        for offset in range(days - 1, -1, -1):
            day = today - timedelta(days=offset)
            for lot in lots:
                try:
                    n, _ = await sync_one(lot, day)
                    summary[lot]["days"] += 1
                    summary[lot]["inserted"] += n
                except Exception as exc:
                    summary[lot]["errors"] += 1
                    all_errors.append(f"{day.isoformat()} {lot}: {type(exc).__name__}: {exc}")
        return {"status": "ok", "days": days, "summary": summary, "errors": all_errors[-50:]}

    @router.get("/calibrate")
    def calibrate_route(
        lottery: str = Query(...),
        modality: str = Query("milhar"),
        max_cases: int = Query(40, ge=10, le=100),
    ):
        if lottery not in ORACLE_LOTTERIES:
            raise HTTPException(400, "Loteria não suportada")
        if modality not in ORACLE_MODALITIES:
            raise HTTPException(400, "Modalidade não suportada")
        rows = get_results(lottery, limit=3000)
        if len(rows) < 25:
            raise HTTPException(409, f"Histórico insuficiente: {len(rows)} registros. Faça um backfill primeiro.")
        result = calibrate(rows, modality, max_cases=max_cases)
        save_model(lottery, modality, result.model_name, result.weights, result.score, result.cases)
        return {
            "status": "ok", "lottery": lottery, "modality": modality,
            "model": result.model_name, "weights": result.weights,
            "score": result.score, "cases": result.cases, "metrics": result.metrics,
            "warning": "A calibração mede aderência histórica; não garante acertos futuros.",
        }

    @router.get("/predict")
    def predict_route(
        lottery: str = Query(...),
        draw_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
        draw_time: str = Query(..., pattern=r"^\d{2}:\d{2}$"),
        modality: str = Query("milhar"),
        force_retrain: bool = Query(False),
    ):
        if lottery not in ORACLE_LOTTERIES:
            raise HTTPException(400, "Loteria não suportada")
        if modality not in ORACLE_MODALITIES:
            raise HTTPException(400, "Modalidade não suportada")
        frozen = get_prediction(lottery, draw_date, draw_time, modality)
        if frozen and not force_retrain:
            return {"status": "frozen", "prediction": frozen["prediction"], "record": frozen}

        cutoff = _target_cutoff(draw_date, draw_time)
        # Never create a brand-new "prediction" after the target result is
        # already stored. That would make a historical result look like a
        # forecast. Historical testing belongs to /calibrate, not /predict.
        target_rows = get_results(lottery, limit=3000)
        target_exists = any(r["draw_date"] == draw_date and r["draw_time"] == draw_time for r in target_rows)
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
        return {"status": "calculated", "prediction_id": prediction_id, **pack, "cutoff_iso": cutoff}

    @router.get("/evaluate-pending")
    def evaluate_pending(lottery: Optional[str] = None):
        pending = pending_predictions(lottery=lottery)
        evaluated = 0
        for p in pending:
            rows = get_results(p["lottery"], limit=3000)
            target_dt = f"{p['draw_date']}T{p['draw_time']}:00"
            actual = [r for r in rows if r["draw_date"] == p["draw_date"] and r["draw_time"] == p["draw_time"]]
            # Use only published rows; evaluation naturally occurs after the draw exists.
            if not actual:
                continue
            candidates = p.get("candidates_json") or []
            ev = evaluate_prediction(p["prediction"], actual, p["modality"], candidates)
            ev["score"] = round(float(ev.get("exact", False)) + 0.3 * float(ev.get("tail", False)) + 0.2 * float(ev.get("group", False)), 4)
            ev["target"] = target_dt
            mark_prediction_evaluated(int(p["id"]), ev)
            evaluated += 1
        return {"status": "ok", "evaluated": evaluated, "pending_before": len(pending)}

    app.include_router(router)
    return router
