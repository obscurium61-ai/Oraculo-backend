"""Production Oracle API.

The module keeps the existing result collectors and adds one stable interface
for the app: frozen daily forecasts, frozen per-draw forecasts, Super Palpitão,
walk-forward learning, and an automatic cycle.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from typing import Callable, Optional
import asyncio

from fastapi import APIRouter, HTTPException, Query

from oracle_brain import (
    ORACLE_SCHEDULES, MODALITY_ALIASES, MODEL_FAMILIES,
    generate_portfolio, calibrate, evaluate_forecast, row_number, group_of,
)
from oracle_store import (
    ensure_schema, database_info, upsert_results, get_results, get_prediction,
    save_prediction, save_model, load_model, list_predictions, prediction_counts,
    result_counts, mark_prediction_evaluated, prediction_has_event,
    add_prediction_event, log_sync, log_learning_run, canonical_oracle_draw_time,
)

ORACLE_LOTTERIES = ["PT-RIO", "Para Todos-SP", "LOOK Goiás", "LNS Nacional"]
ORACLE_MODALITIES = [
    "milhar", "palpitao", "centena", "dezena", "grupo",
    "duque_dezena", "terno_dezena", "terno_grupo", "duque_grupo", "passe_vai"
]
BRAZIL_TZ = ZoneInfo("America/Sao_Paulo")


def _now_local() -> datetime:
    return datetime.now(BRAZIL_TZ)


def _cutoff_iso(draw_date: str, draw_time: str) -> str:
    return f"{draw_date}T{draw_time}:00-03:00"


def _daily_cutoff(day: date) -> str:
    return f"{day.isoformat()}T00:00:00-03:00"


def _canonical(lottery: str, draw_time: str) -> str:
    return canonical_oracle_draw_time(lottery, draw_time)


def _schedule(lottery: str) -> list[str]:
    return ORACLE_SCHEDULES[lottery]


def _target_exists(lottery: str, draw_date: str, draw_time: str) -> bool:
    rows = get_results(lottery, limit=5000)
    target = _canonical(lottery, draw_time)
    return any(r["draw_date"] == draw_date and _canonical(lottery, r["draw_time"]) == target for r in rows)


def _draw_rows(lottery: str, draw_date: str, draw_time: str) -> list[dict]:
    target = _canonical(lottery, draw_time)
    rows = get_results(lottery, limit=5000)
    return [r for r in rows if r["draw_date"] == draw_date and _canonical(lottery, r["draw_time"]) == target]


def _daily_history(rows: list[dict], target_date: date, days: int = 2) -> list[dict]:
    """Return only the immediately preceding calendar days for Sorte do Dia."""
    allowed = {(target_date - timedelta(days=i)).isoformat() for i in range(1, days + 1)}
    out = [r for r in rows if str(r.get("draw_date"))[:10] in allowed]
    out.sort(key=lambda r: (r.get("draw_date", ""), r.get("draw_time", ""), int(r.get("prize") or 999)))
    return out


def _previous_draw_requirement(lottery: str, draw_date: date, draw_time: str) -> dict | None:
    times = _schedule(lottery)
    target = _canonical(lottery, draw_time)
    if target not in times:
        raise HTTPException(400, f"Horário {draw_time} não pertence à programação de {lottery}.")
    idx = times.index(target)
    if idx == 0:
        prev_date = draw_date - timedelta(days=1)
        prev_time = times[-1]
    else:
        prev_date = draw_date
        prev_time = times[idx-1]
    rows = _draw_rows(lottery, prev_date.isoformat(), prev_time)
    if not rows:
        return None
    ordered = sorted(rows, key=lambda r: int(r.get("prize") or 999))
    return {"date": prev_date.isoformat(), "time": prev_time, "rows": ordered}


def _moment_from_draw(draw: dict | None) -> dict:
    if not draw:
        return {"groups": {}, "first_group": None, "first_tail": None, "name": None}
    groups: dict[str, float] = {}
    ordered = sorted(draw["rows"], key=lambda r: int(r.get("prize") or 999))[:5]
    for r in ordered:
        n = row_number(r)
        g = int(r.get("group") or r.get("group_code") or group_of(n))
        w = 3.5 if int(r.get("prize") or 0) == 1 else 1.0
        groups[str(g)] = groups.get(str(g), 0) + w
    first = ordered[0] if ordered else None
    return {
        "groups": groups,
        "first_group": int(first.get("group") or first.get("group_code") or group_of(row_number(first))) if first else None,
        "first_tail": row_number(first)[-2:] if first else None,
        "name": None,
    }


def _model_for(lottery: str, modality: str, rows: list[dict]) -> dict:
    # Modelos antigos do banco podem ter a estrutura V2/V3 (frequency/recency/delay)
    # e quebrariam a V5 com KeyError ao pontuar. Detectamos esse legado e fazemos
    # uma migração segura para o modelo balanceado da V5, sem travar a primeira
    # previsão com uma calibração pesada. O ciclo de aprendizado posterior pode
    # recalibrar e substituir esse modelo com a estrutura atual.
    required = {"freq", "recent", "overdue", "digits", "transition", "group", "prize1"}
    stored = load_model(lottery, modality)
    if stored:
        weights = stored.get("weights_json") or {}
        if isinstance(weights, dict) and required.issubset(weights.keys()):
            model_name = stored.get("model_name") if stored.get("model_name") in MODEL_FAMILIES else "balanced"
            return {
                "model_name": model_name,
                "weights": weights,
                "score": float(stored.get("score") or 0.0),
                "cases": int(stored.get("cases") or 0),
            }
        # Legacy/incompatible model: use a valid V5 baseline in memory.
        # A migração permanente fica para o ciclo de aprendizado, evitando uma
        # escrita no banco logo na primeira consulta do usuário.
        fallback = MODEL_FAMILIES["balanced"]
        return {"model_name": "balanced", "weights": fallback, "score": 0.0, "cases": 0, "migrated_legacy": True}

    # Cold start: use um baseline V5 em memória imediatamente. A calibração pesada
    # é feita por /api/oracle/learn-now ou /api/oracle/cycle, não no primeiro acesso.
    fallback = MODEL_FAMILIES["balanced"]
    return {"model_name": "balanced", "weights": fallback, "score": 0.0, "cases": 0, "cold_start": True}


def _mode_description(mode: str, lottery: str, target_date: date, draw_time: str | None, rows: list[dict], model: dict, prev: dict | None, special: dict | None) -> str:
    if mode == "dia":
        return f"Sorte do Dia congelada para {target_date.strftime('%d/%m/%Y')}; histórico cortado exatamente às 00:00. {len(rows)} registros históricos usados."
    parts = [f"Por Sorteio {draw_time}; dados limitados ao que existia antes de {draw_time}."]
    if prev:
        parts.append(f"Último sorteio usado: {prev['date']} {prev['time']}.")
        m = _moment_from_draw(prev)
        if m.get("first_group"):
            parts.append(f"Bicho do momento: grupo {m['first_group']}.")
    if special:
        parts.append("Sinal especial do LOOK 23:20 anterior aplicado ao Nacional 02:00.")
    parts.append(f"Modelo atual: {model['model_name']}.")
    return " ".join(parts)


def register_oracle_routes(app, fetch_look: Callable, fetch_rio: Callable, fetch_aggregated: Callable):
    router = APIRouter(prefix="/api/oracle", tags=["oracle"])
    # Não toque no banco durante a inicialização do processo. O Render deve
    # conseguir subir e responder /health mesmo quando o Postgres estiver
    # temporariamente indisponível; o schema será preparado sob demanda.

    def _dedupe_rows(rows: list[dict]) -> list[dict]:
        # Fontes históricas às vezes repetem uma mesma linha (especialmente o 7º/8º prêmio).
        # Persistimos somente uma ocorrência por sorteio+prêmio+número.
        seen = set()
        out = []
        for row in rows or []:
            key = (
                str(row.get("lottery") or ""),
                str(row.get("draw_date") or row.get("date") or ""),
                _canonical(str(row.get("lottery") or ""), str(row.get("draw_time") or "")) if row.get("draw_time") else "",
                int(row.get("prize") or 0),
                str(row.get("number") or "").zfill(4)[-4:],
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
        return out

    async def sync_one(lottery: str, day: date) -> tuple[int, str]:
        if lottery == "LOOK Goiás":
            rows = _dedupe_rows(await fetch_look(day))
            return upsert_results(rows), "LOOK"
        if lottery == "PT-RIO":
            rows = await fetch_rio(day)
            for row in rows:
                row["draw_time"] = _canonical("PT-RIO", row.get("draw_time", ""))
            rows = _dedupe_rows(rows)
            return upsert_results(rows), "PT-RIO"
        if lottery in {"Para Todos-SP", "LNS Nacional"}:
            rows = _dedupe_rows(await fetch_aggregated(day, lottery))
            return upsert_results(rows), lottery
        raise HTTPException(400, f"Loteria não suportada: {lottery}")

    async def sync_day(day: date, lotteries: Optional[list[str]] = None) -> tuple[int, list[str]]:
        total = 0; errors=[]
        for lot in lotteries or ORACLE_LOTTERIES:
            try:
                n,_ = await sync_one(lot, day); total += n
            except Exception as exc:
                errors.append(f"{day.isoformat()} {lot}: {type(exc).__name__}: {exc}")
        if lotteries is None:
            log_sync(day.isoformat(), ORACLE_LOTTERIES, total, errors)
        return total, errors

    async def sync_today_all():
        return await sync_day(_now_local().date())

    async def _ensure_special_nacional_0200(target_date: date) -> dict | None:
        if target_date != _now_local().date() and target_date > date.today():
            return None
        previous_day = target_date - timedelta(days=1)
        try:
            rows = get_results("LOOK Goiás", limit=5000)
            look_rows = [r for r in rows if r["draw_date"] == previous_day.isoformat() and _canonical("LOOK Goiás", r["draw_time"]) == "23:20"]
            if not look_rows:
                try:
                    await sync_one("LOOK Goiás", previous_day)
                except Exception:
                    pass
                rows = get_results("LOOK Goiás", limit=5000)
                look_rows = [r for r in rows if r["draw_date"] == previous_day.isoformat() and _canonical("LOOK Goiás", r["draw_time"]) == "23:20"]
            if not look_rows:
                return None
            first = sorted(look_rows, key=lambda r:int(r.get("prize") or 999))[0]
            n=row_number(first); g=int(first.get("group") or first.get("group_code") or group_of(n))
            return {"first_number": n, "first_group": g, "first_tail": n[-2:], "source": "LOOK_23:20_previous_day"}
        except Exception:
            return None

    def _build_prediction(lottery: str, mode: str, target_date: date, draw_time: str | None, modality: str,
                          rows: list[dict], prev_draw: dict | None, special: dict | None, context_seed: str) -> dict:
        model = _model_for(lottery, modality, rows)
        portfolio = generate_portfolio(rows, lottery, mode, target_date, draw_time, model["model_name"], model["weights"], special_signal=special, context_seed=context_seed)
        p = portfolio["portfolio"][modality]
        if isinstance(p, list):
            prediction = ", ".join(p)
            candidates = p[:] if len(p) <= 30 else portfolio["rankings"].get("dezena",[])[:50]
        else:
            prediction = str(p)
            if modality == "milhar": candidates=portfolio["rankings"]["milhar"]
            elif modality == "centena": candidates=portfolio["rankings"]["centena"]
            elif modality == "grupo": candidates=portfolio["rankings"]["grupo"]
            else: candidates=portfolio["rankings"]["dezena"]
        return portfolio, prediction, candidates, model

    async def ensure_forecast(lottery: str, mode: str, target_date: date, draw_time: str | None,
                              modality: str, force_retrain: bool=False) -> dict:
        mode = mode.lower()
        if modality not in ORACLE_MODALITIES:
            raise HTTPException(400, f"Modalidade não suportada: {modality}")
        modality = MODALITY_ALIASES.get(modality, modality)
        if mode not in {"dia", "sorteio"}:
            raise HTTPException(400, "Modo deve ser dia ou sorteio")
        canonical_time = "00:00" if mode == "dia" else _canonical(lottery, draw_time or "")
        existing = get_prediction(lottery, target_date.isoformat(), canonical_time, modality)
        if existing:
            return {"status":"frozen", "prediction": existing["prediction"], "record": existing}

        now = _now_local()
        if mode == "sorteio":
            if _target_exists(lottery, target_date.isoformat(), canonical_time):
                raise HTTPException(409, "O sorteio alvo já possui resultado armazenado; não é permitida previsão retroativa.")
            if target_date == now.date() and now.time() >= dtime.fromisoformat(canonical_time):
                # A previsão pode existir only if it had been frozen before the draw.
                raise HTTPException(409, "O horário alvo já passou e ainda não existe previsão congelada para ele.")
            prev = _previous_draw_requirement(lottery, target_date, canonical_time)
            if prev is None:
                raise HTTPException(409, f"A previsão de {canonical_time} aguarda o resultado do sorteio imediatamente anterior.")
            cutoff=_cutoff_iso(target_date.isoformat(), canonical_time)
            rows=get_results(lottery, cutoff_iso=cutoff, limit=5000)
            special=None
            if lottery == "LNS Nacional" and canonical_time == "02:00":
                special = await _ensure_special_nacional_0200(target_date)
        else:
            prev=None; special=None
            cutoff=_daily_cutoff(target_date)
            rows=get_results(lottery, cutoff_iso=cutoff, limit=5000)
        if len(rows) < 20:
            raise HTTPException(409, f"Histórico insuficiente antes do corte: {len(rows)} registros.")
        if force_retrain:
            cal=calibrate(rows, modality, max_cases=60)
            save_model(lottery, modality, cal["model_name"], cal["weights"], cal["score"], cal["cases"])
        portfolio, prediction, candidates, model=_build_prediction(lottery,mode,target_date,canonical_time,modality,rows,prev,special,f"{mode}|{target_date}|{canonical_time}")
        context={
            "mode":mode, "history_rows":len(rows), "previous_draw":prev, "special_signal":special,
            "calendar_group":portfolio["features"].get("calendar_group"), "moment_groups":portfolio["features"].get("moment_groups"),
            "portfolio":portfolio["portfolio"], "rankings":portfolio["rankings"],
        }
        pid=save_prediction({"lottery":lottery,"draw_date":target_date.isoformat(),"draw_time":canonical_time,
                             "modality":modality,"prediction":prediction,"candidates":candidates,"cutoff_iso":cutoff,
                             "model_name":model["model_name"],"model":model["weights"],"context":context})
        rec=get_prediction(lottery,target_date.isoformat(),canonical_time,modality)
        return {
            "status":"calculated", "prediction_id":pid, "prediction":prediction, "candidates":candidates,
            "portfolio":portfolio["portfolio"], "rankings":portfolio["rankings"], "cutoff_iso":cutoff,
            "history_rows":len(rows), "model":model, "message":_mode_description(mode,lottery,target_date,canonical_time,rows,model,prev,special),
            "record":rec,
        }

    async def ensure_super(super_type: str, target_date: date) -> dict:
        modality = "super_milhar" if super_type == "milhar" else "super_centena"
        existing=get_prediction("SUPER",target_date.isoformat(),"00:00",modality)
        if existing:
            vals=existing["prediction"].split(",") if existing["prediction"] else []
            return {"status":"frozen","type":super_type,"items":vals,"record":existing}
        cutoff=_daily_cutoff(target_date)
        all_rows=[]
        for lot in ORACLE_LOTTERIES:
            all_rows.extend(get_results(lot,cutoff_iso=cutoff,limit=5000))
        if len(all_rows)<60:
            raise HTTPException(409,f"Histórico agregado insuficiente antes do corte: {len(all_rows)} registros.")
        # Keep Super Palpitão cheap enough for the free Render instance.
        # The regular learning cycle updates stored models separately.
        base_mod = "milhar" if super_type == "milhar" else "centena"
        model = _model_for("SUPER", base_mod, all_rows)
        from oracle_brain import build_features, rank_numbers, _pick_unique
        f=build_features(all_rows,reference_date=target_date)
        ranked=rank_numbers(f,base_mod,model["weights"],400)
        amount=100 if super_type=="milhar" else 50
        items=_pick_unique(ranked,amount,f"SUPER|{target_date.isoformat()}|{super_type}")
        save_prediction({"lottery":"SUPER","draw_date":target_date.isoformat(),"draw_time":"00:00","modality":modality,
                         "prediction":", ".join(items),"candidates":ranked[:100],"cutoff_iso":cutoff,"model_name":model["model_name"],
                         "model":model["weights"],"context":{"mode":"super","type":super_type,"history_rows":len(all_rows),"lotteries":ORACLE_LOTTERIES}})
        return {"status":"calculated","type":super_type,"items":items,"model":model,"cutoff_iso":cutoff,"history_rows":len(all_rows)}

    async def _evaluate_one(p: dict) -> tuple[bool,bool,dict|None]:
        lottery=p["lottery"]; modality=p["modality"]; draw_date=p["draw_date"]; draw_time=p["draw_time"]
        ctx=p.get("context_json") or {}
        mode=ctx.get("mode") or ("dia" if draw_time=="00:00" else "sorteio")
        if lottery == "SUPER":
            if mode != "super": return False,False,None
            target=date.fromisoformat(draw_date); now=_now_local()
            last_done=max(x[-1] for x in ORACLE_SCHEDULES.values())
            if target == now.date() and now.time() <= dtime.fromisoformat(last_done): return False,False,None
            actual=[]
            for lot in ORACLE_LOTTERIES:
                actual.extend([r for r in get_results(lot,limit=5000) if r["draw_date"]==draw_date])
            if not actual: return False,False,None
            ev=evaluate_forecast(p["prediction"],actual, "milhar" if modality.endswith("milhar") else "centena", p.get("candidates") or [])
        elif mode=="dia":
            target=date.fromisoformat(draw_date); now=_now_local()
            if target == now.date() and now.time() <= dtime.fromisoformat(ORACLE_SCHEDULES[lottery][-1]): return False,False,None
            actual=[r for r in get_results(lottery,limit=5000) if r["draw_date"]==draw_date]
            if not actual: return False,False,None
            ev=evaluate_forecast(p["prediction"],actual,modality,p.get("candidates") or [])
        else:
            actual=_draw_rows(lottery,draw_date,draw_time)
            if not actual: return False,False,None
            ev=evaluate_forecast(p["prediction"],actual,modality,p.get("candidates") or [])
        is_new=not bool(p.get("evaluated")) or not p.get("evaluation_json")
        if is_new:
            ev["target"]=f"{draw_date}T{draw_time}:00"; ev["prediction_id"]=p["id"]; ev["prediction"]=p["prediction"]
            mark_prediction_evaluated(int(p["id"]),ev)
        else:
            ev=p["evaluation_json"]
        return True,is_new,ev

    async def learning_cycle(trigger: str="cycle") -> dict:
        synced, sync_errors = await sync_today_all()
        predictions=list_predictions(limit=250)
        pending_before=sum(1 for p in predictions if not p.get("evaluated"))
        evaluated=0; relearned=0; learned=[]; errors=list(sync_errors)
        targets={}
        for p in predictions:
            try:
                found,is_new,ev=await _evaluate_one(p)
                if not found or ev is None: continue
                if is_new: evaluated+=1
                if prediction_has_event(int(p["id"]),"learned"): continue
                key=(p["lottery"],p["modality"])
                if p["lottery"]=="SUPER":
                    add_prediction_event(int(p["id"]),"learned",{"model":"aggregate","score":ev.get("score",0),"reason":"daily evaluation"})
                    learned.append({"lottery":"SUPER","modality":p["modality"],"model":"aggregate","score":ev.get("score",0),"cases":1,"predictions_learned":[p["id"]]})
                    continue
                targets.setdefault(key,[]).append(p)
                if not is_new: relearned+=1
            except Exception as exc:
                errors.append(f"evaluate #{p.get('id')}: {type(exc).__name__}: {exc}")
        for (lot,mod), plist in list(sorted(targets.items()))[:2]:
            try:
                rows=get_results(lot,limit=5000)
                if len(rows)<25: continue
                cal=calibrate(rows,mod,max_cases=30)
                save_model(lot,mod,cal["model_name"],cal["weights"],cal["score"],cal["cases"])
                for p in plist:
                    add_prediction_event(int(p["id"]),"learned",{"lottery":lot,"modality":mod,"model":cal["model_name"],"score":cal["score"],"cases":cal["cases"],"metrics":cal.get("metrics",{})})
                learned.append({"lottery":lot,"modality":mod,"model":cal["model_name"],"score":cal["score"],"cases":cal["cases"],"metrics":cal.get("metrics",{}),"predictions_learned":[p["id"] for p in plist]})
            except Exception as exc:
                errors.append(f"calibration {lot}/{mod}: {type(exc).__name__}: {exc}")
        log_learning_run(trigger,synced,evaluated,pending_before,errors)
        return {"status":"ok","trigger":trigger,"synced_rows":synced,"evaluated":evaluated,"relearned_existing":relearned,"pending_seen":pending_before,"predictions":prediction_counts(),"learned":learned,"errors":errors}

    async def _prepare_context(lottery: str, mode: str, target_date: date, draw_time: str | None,
                               rows: list[dict], prev: dict | None, special: dict | None) -> dict:
        """Freeze all modalities for one context using three shared model families."""
        canonical_time = "00:00" if mode == "dia" else _canonical(lottery, draw_time or "")
        cutoff = _daily_cutoff(target_date) if mode == "dia" else _cutoff_iso(target_date.isoformat(), canonical_time)
        key_date=target_date.isoformat()
        families={
            "milhar":["milhar"],
            "centena":["centena"],
            "dezena":["dezena","duque_dezena","terno_dezena","palpitao"],
            "grupo":["grupo","duque_grupo","terno_grupo","passe_vai"],
        }
        generated=[]
        for base_mod, modalities in families.items():
            missing=[m for m in modalities if not get_prediction(lottery,key_date,canonical_time,m)]
            if not missing:
                continue
            model=_model_for(lottery,base_mod,rows)
            portfolio=generate_portfolio(rows, lottery, mode, target_date, canonical_time, model["model_name"], model["weights"], special_signal=special, context_seed=f"PREWARM|{mode}|{key_date}|{canonical_time}|{base_mod}")
            context_base={"mode":mode,"history_rows":len(rows),"previous_draw":prev,"special_signal":special,
                          "calendar_group":portfolio["features"].get("calendar_group"),"moment_groups":portfolio["features"].get("moment_groups"),"prewarmed":True,"base_model_modality":base_mod}
            for mod in missing:
                value=portfolio["portfolio"][mod]
                if isinstance(value,list):
                    prediction=", ".join(map(str,value))
                    if mod in {"palpitao","duque_dezena","terno_dezena","duque_grupo","terno_grupo","passe_vai"}:
                        candidates=list(value)
                    elif mod=="grupo":
                        candidates=portfolio["rankings"].get("grupo",[])[:25]
                    else:
                        candidates=portfolio["rankings"].get("dezena",[])[:50]
                else:
                    prediction=str(value)
                    key={"milhar":"milhar","centena":"centena","dezena":"dezena","grupo":"grupo"}.get(mod,"dezena")
                    candidates=portfolio["rankings"].get(key,[])
                save_prediction({"lottery":lottery,"draw_date":key_date,"draw_time":canonical_time,"modality":mod,
                                 "prediction":prediction,"candidates":candidates,"cutoff_iso":cutoff,"model_name":model["model_name"],
                                 "model":model["weights"],"context":{**context_base,"portfolio":portfolio["portfolio"],"rankings":portfolio["rankings"]}})
                generated.append({"mode":mode,"lottery":lottery,"draw_time":canonical_time,"modality":mod,"model":model["model_name"]})
        return {"generated":generated}

    async def prewarm_current_day() -> dict:
        """Background preparation: daily snapshot + next eligible draw per lottery."""
        today=_now_local().date(); now=_now_local(); generated=[]; skipped=[]; errors=[]
        for lot in ORACLE_LOTTERIES:
            try:
                rows=_daily_history(get_results(lot,cutoff_iso=_daily_cutoff(today),limit=5000),today,days=2)
                if len(rows)<20:
                    skipped.append({"mode":"dia","lottery":lot,"reason":f"histórico insuficiente: {len(rows)} registros nos 2 dias anteriores"})
                    continue
                out=await _prepare_context(lot,"dia",today,None,rows,None,None)
                generated.extend(out["generated"])
            except Exception as exc:
                skipped.append({"mode":"dia","lottery":lot,"reason":f"{type(exc).__name__}: {exc}"})
        for lot,times in ORACLE_SCHEDULES.items():
            next_time=next((t for t in times if now.time() < dtime.fromisoformat(t)),None)
            if not next_time:
                continue
            try:
                prev=_previous_draw_requirement(lot,today,next_time)
                if prev is None and next_time==times[0]:
                    try: await sync_one(lot,today-timedelta(days=1))
                    except Exception: pass
                    prev=_previous_draw_requirement(lot,today,next_time)
                if prev is None:
                    skipped.append({"mode":"sorteio","lottery":lot,"draw_time":next_time,"reason":"aguardando resultado do sorteio imediatamente anterior"})
                    continue
                canonical_time=_canonical(lot,next_time)
                if _target_exists(lot,today.isoformat(),canonical_time):
                    skipped.append({"mode":"sorteio","lottery":lot,"draw_time":canonical_time,"reason":"sorteio já possui resultado; não cria previsão retroativa"})
                    continue
                rows=get_results(lot,cutoff_iso=_cutoff_iso(today.isoformat(),canonical_time),limit=5000)
                special=await _ensure_special_nacional_0200(today) if lot=="LNS Nacional" and canonical_time=="02:00" else None
                if len(rows)<20:
                    skipped.append({"mode":"sorteio","lottery":lot,"draw_time":canonical_time,"reason":f"histórico insuficiente: {len(rows)}"})
                    continue
                out=await _prepare_context(lot,"sorteio",today,canonical_time,rows,prev,special)
                generated.extend(out["generated"])
            except Exception as exc:
                errors.append(f"sorteio {lot}/{next_time}: {type(exc).__name__}: {exc}")
        for st in ("milhar","centena"):
            try:
                out=await ensure_super(st,today)
                generated.append({"mode":"super","type":st,"status":out.get("status")})
            except Exception as exc:
                errors.append(f"super {st}: {type(exc).__name__}: {exc}")
        return {"generated":generated[-160:],"skipped":skipped[-160:],"errors":errors[-80:]}

    @router.get("/status")
    def status():
        return {"ok":True,"database":database_info(),"lotteries":ORACLE_LOTTERIES,"schedules":ORACLE_SCHEDULES,"modalities":ORACLE_MODALITIES,"predictions":prediction_counts(),"results":result_counts(),"message":"Oráculo persistente, congelado e preparado para ciclo automático."}

    @router.get("/diagnose")
    def diagnose(limit:int=Query(10,ge=1,le=100)):
        recent=list_predictions(limit=limit)
        return {"status":"ok","database":database_info(),"predictions":prediction_counts(),"results":result_counts(),"recent_predictions":[{
            "id":p["id"],"lottery":p["lottery"],"draw_date":p["draw_date"],"draw_time":p["draw_time"],"modality":p["modality"],"prediction":p["prediction"],"evaluated":bool(p.get("evaluated")),"created_at":str(p.get("created_at"))
        } for p in recent]}

    @router.get("/sync")
    async def sync(draw_date: Optional[date]=Query(None), lottery: Optional[str]=Query(None)):
        day=draw_date or _now_local().date(); lots=[lottery] if lottery else ORACLE_LOTTERIES
        inserted,errors=await sync_day(day,lots); log_sync(day.isoformat(),lots,inserted,errors)
        return {"status":"ok","date":day.isoformat(),"lotteries":lots,"inserted":inserted,"errors":errors}

    @router.get("/backfill")
    async def backfill(days:int=Query(14,ge=1,le=90), lottery:Optional[str]=Query(None)):
        today=_now_local().date(); lots=[lottery] if lottery else ORACLE_LOTTERIES
        summary={lot:{"days":0,"inserted":0,"errors":0} for lot in lots}; errors=[]
        for offset in range(days-1,-1,-1):
            day=today-timedelta(days=offset)
            for lot in lots:
                try:
                    n,_=await sync_one(lot,day)
                    summary[lot]["days"]+=1
                    summary[lot]["inserted"]+=n
                except Exception as exc:
                    summary[lot]["errors"]+=1
                    msg=f"{day.isoformat()} {lot}: {type(exc).__name__}: {exc}"
                    errors.append(msg)
        return {"status":"ok","days":days,"summary":summary,"errors":errors[-50:]}

    @router.get("/calibrate")
    def calibrate_route(lottery:str=Query(...),modality:str=Query("milhar"),max_cases:int=Query(60,ge=10,le=120)):
        modality=MODALITY_ALIASES.get(modality,modality)
        if lottery not in ORACLE_LOTTERIES or modality not in ORACLE_MODALITIES: raise HTTPException(400,"Loteria ou modalidade não suportada")
        rows=get_results(lottery,limit=5000)
        if len(rows)<25: raise HTTPException(409,f"Histórico insuficiente: {len(rows)} registros.")
        cal=calibrate(rows,modality,max_cases=max_cases); save_model(lottery,modality,cal["model_name"],cal["weights"],cal["score"],cal["cases"])
        return {"status":"ok","lottery":lottery,"modality":modality,**cal,"warning":"A calibração mede aderência histórica; não garante acertos futuros."}

    @router.get("/portfolio")
    async def portfolio(lottery:str=Query(...),mode:str=Query("dia"),draw_date:str=Query(...),draw_time:Optional[str]=Query(None),modality:str=Query("milhar"),force_retrain:bool=Query(False)):
        if lottery not in ORACLE_LOTTERIES: raise HTTPException(400,"Loteria não suportada")
        if modality not in ORACLE_MODALITIES: raise HTTPException(400,"Modalidade não suportada")
        target=date.fromisoformat(draw_date)
        return await ensure_forecast(lottery,mode,target,draw_time,modality,force_retrain)

    @router.get("/results")
    async def stored_results(lottery: str = Query(...), draw_date: Optional[str] = Query(None), sync: bool = Query(True)):
        if lottery not in ORACLE_LOTTERIES:
            raise HTTPException(400, "Loteria não suportada")
        day = date.fromisoformat(draw_date) if draw_date else _now_local().date()
        sync_error = None
        if sync:
            try:
                await sync_one(lottery, day)
            except Exception as exc:
                sync_error = f"{type(exc).__name__}: {exc}"
        rows = get_results(lottery, limit=5000)
        rows = [r for r in rows if r["draw_date"] == day.isoformat()]
        rows.sort(key=lambda r: (r["draw_time"], int(r.get("prize") or 999)))
        unique = []
        seen = set()
        for r in rows:
            key = (r["draw_time"], int(r.get("prize") or 0), str(r.get("number") or "").zfill(4)[-4:])
            if key in seen:
                continue
            seen.add(key)
            unique.append(r)
        return {"status":"ok","lottery":lottery,"date":day.isoformat(),"count":len(unique),"results":unique,"sync_error":sync_error}

    @router.get("/super")
    async def super_endpoint(type:str=Query("milhar"),draw_date:str=Query(...)):
        if type not in {"milhar","centena"}: raise HTTPException(400,"type deve ser milhar ou centena")
        return await ensure_super(type,date.fromisoformat(draw_date))

    @router.get("/predict")
    async def predict_compat(lottery:str=Query(...),draw_date:str=Query(...),draw_time:str=Query(...),modality:str=Query("milhar")):
        return await ensure_forecast(lottery,"sorteio",date.fromisoformat(draw_date),draw_time,modality)

    @router.get("/evaluate-pending")
    async def evaluate_pending():
        return await learning_cycle("evaluate-pending")

    @router.get("/learn-now")
    async def learn_now():
        return await learning_cycle("learn-now")

    @router.get("/cycle")
    async def cycle():
        learning=await learning_cycle("cycle")
        prewarm=await prewarm_current_day()
        return {"status":"ok","learning":learning,"prewarm":prewarm,"message":"Ciclo concluído: resultados sincronizados, aprendizado atualizado e próximos palpites congelados."}

    @router.get("/prewarm")
    async def prewarm():
        return {"status":"ok",**(await prewarm_current_day())}

    app.include_router(router)
    return router
