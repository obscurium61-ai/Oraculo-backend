"""Persistent storage for the Oráculo learning loop.

PostgreSQL is the production store (DATABASE_URL on Render). SQLite is kept only
as a local-development fallback.

This version deliberately treats predictions as immutable facts: once a
prediction exists for a lottery/date/time/modality, a later request cannot
silently replace it. That prevents the system from rewriting a forecast after
the result is known.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, MetaData, String, Table, Text,
    and_, create_engine, desc, select, update, or_, func,
)
from sqlalchemy.engine import Engine

DB_URL = os.getenv("DATABASE_URL", "sqlite:///./oraculo_local.sqlite3")
if DB_URL.startswith("postgres://"):
    DB_URL = "postgresql+psycopg://" + DB_URL[len("postgres://"):]
elif DB_URL.startswith("postgresql://"):
    DB_URL = "postgresql+psycopg://" + DB_URL[len("postgresql://"):]

if DB_URL.startswith("sqlite:///"):
    db_path = DB_URL.replace("sqlite:///", "", 1)
    if db_path and db_path not in {":memory:", "./oraculo_local.sqlite3"}:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

if DB_URL.startswith("sqlite"):
    engine: Engine = create_engine(
        DB_URL,
        future=True,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False},
    )
else:
    # O banco remoto nunca deve conseguir travar o servidor do Render
    # indefinidamente. Limites curtos evitam que conexões presas ocupem
    # todos os workers enquanto o /health continua disponível.
    engine = create_engine(
        DB_URL,
        future=True,
        pool_pre_ping=True,
        pool_size=3,
        max_overflow=0,
        pool_timeout=5,
        pool_recycle=300,
        connect_args={
            "connect_timeout": 5,
            "options": "-c statement_timeout=8000",
        },
    )

metadata = MetaData()

results = Table(
    "oracle_results", metadata,
    Column("id", Integer, primary_key=True),
    Column("lottery", String(80), nullable=False),
    Column("draw_date", String(10), nullable=False),
    Column("draw_time", String(5), nullable=False),
    Column("prize", Integer, nullable=False),
    Column("number", String(8), nullable=False),
    Column("group_code", String(2), nullable=False),
    Column("source", String(255)),
    Column("raw_json", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

predictions = Table(
    "oracle_predictions", metadata,
    Column("id", Integer, primary_key=True),
    Column("lottery", String(80), nullable=False),
    Column("draw_date", String(10), nullable=False),
    Column("draw_time", String(5), nullable=False),
    Column("modality", String(30), nullable=False),
    Column("prediction", String(20), nullable=False),
    Column("candidates_json", Text),
    Column("cutoff_iso", String(40), nullable=False),
    Column("model_name", String(80)),
    Column("model_json", Text),
    Column("context_json", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("evaluated", Boolean, nullable=False, default=False),
    Column("evaluation_json", Text),
)

models = Table(
    "oracle_models", metadata,
    Column("id", Integer, primary_key=True),
    Column("lottery", String(80), nullable=False),
    Column("modality", String(30), nullable=False),
    Column("model_name", String(80), nullable=False),
    Column("weights_json", Text, nullable=False),
    Column("score", Float, nullable=False, default=0.0),
    Column("cases", Integer, nullable=False, default=0),
    Column("trained_at", DateTime(timezone=True), nullable=False),
)

sync_runs = Table(
    "oracle_sync_runs", metadata,
    Column("id", Integer, primary_key=True),
    Column("draw_date", String(10), nullable=False),
    Column("lotteries_json", Text),
    Column("inserted", Integer, nullable=False, default=0),
    Column("errors_json", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

prediction_events = Table(
    "oracle_prediction_events", metadata,
    Column("id", Integer, primary_key=True),
    Column("prediction_id", Integer, nullable=False),
    Column("event_type", String(40), nullable=False),
    Column("payload_json", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

learning_runs = Table(
    "oracle_learning_runs", metadata,
    Column("id", Integer, primary_key=True),
    Column("trigger", String(80), nullable=False),
    Column("synced_rows", Integer, nullable=False, default=0),
    Column("evaluated", Integer, nullable=False, default=0),
    Column("pending_seen", Integer, nullable=False, default=0),
    Column("errors_json", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


PT_RIO_CANONICAL_BY_CODE = {
    "PPT": "09:20", "PTM": "11:20", "PT": "14:20",
    "PTV": "16:20", "PTN": "18:20", "COR": "21:20",
}
PT_RIO_TIME_ALIASES = {
    "09:30": "09:20", "11:30": "11:20", "14:30": "14:20",
    "16:30": "16:20", "18:30": "18:20", "21:30": "21:20",
}


def canonical_oracle_draw_time(lottery: str, draw_time: str, draw_code: str | None = None) -> str:
    if lottery != "PT-RIO":
        return str(draw_time or "")[:5]
    code = str(draw_code or "").strip().upper() or None
    if code in PT_RIO_CANONICAL_BY_CODE:
        return PT_RIO_CANONICAL_BY_CODE[code]
    raw = str(draw_time or "")[:5]
    return PT_RIO_TIME_ALIASES.get(raw, raw)


def _row_draw_code(row: dict) -> Optional[str]:
    raw = row.get("raw_json")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        return payload.get("draw_code")
    except Exception:
        return None


def normalize_existing_pt_rio_times() -> int:
    changed = 0
    with engine.begin() as conn:
        rows = conn.execute(select(results).where(results.c.lottery == "PT-RIO")).mappings().all()
        for row in rows:
            target_time = canonical_oracle_draw_time("PT-RIO", row["draw_time"], _row_draw_code(dict(row)))
            if target_time and target_time != row["draw_time"]:
                conn.execute(update(results).where(results.c.id == row["id"]).values(draw_time=target_time))
                changed += 1
    return changed


_SCHEMA_READY = False

def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    metadata.create_all(engine)
    try:
        normalize_existing_pt_rio_times()
    except Exception:
        # A normalização é apenas de manutenção; uma falha não deve impedir
        # o uso das tabelas já existentes.
        pass
    _SCHEMA_READY = True


def database_info() -> dict:
    is_sqlite = DB_URL.startswith("sqlite")
    return {
        "configured_url": bool(os.getenv("DATABASE_URL")),
        "dialect": "sqlite" if is_sqlite else "postgresql",
        "persistent_on_render_free": not is_sqlite,
        "database_url_source": "DATABASE_URL" if os.getenv("DATABASE_URL") else "local fallback",
    }


def upsert_results(rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    ensure_schema()
    inserted = 0
    with engine.begin() as conn:
        for row in rows:
            lottery = str(row.get("lottery") or "").strip()
            draw_date = str(row.get("date") or row.get("draw_date") or "")[:10]
            draw_time = canonical_oracle_draw_time(lottery, str(row.get("draw_time") or "")[:5], row.get("draw_code"))
            try:
                prize = int(row.get("prize") or 0)
            except Exception:
                prize = 0
            number = str(row.get("number") or "").strip().zfill(4)
            group_code = str(row.get("group") or row.get("group_code") or "").zfill(2)
            if not (lottery and draw_date and draw_time and prize and number and group_code):
                continue
            stmt = select(results.c.id).where(and_(
                results.c.lottery == lottery,
                results.c.draw_date == draw_date,
                results.c.draw_time == draw_time,
                results.c.prize == prize,
                results.c.number == number,
                results.c.group_code == group_code,
            )).limit(1)
            if conn.execute(stmt).first():
                continue
            conn.execute(results.insert().values(
                lottery=lottery, draw_date=draw_date, draw_time=draw_time,
                prize=prize, number=number, group_code=group_code,
                source=row.get("source"), raw_json=json_dumps(row), created_at=now_utc(),
            ))
            inserted += 1
    return inserted


def _row_to_dict(row) -> dict:
    return dict(row)


def get_results(lottery: str, cutoff_iso: Optional[str] = None, limit: int = 3000) -> list[dict]:
    ensure_schema()
    with engine.connect() as conn:
        stmt = select(results).where(results.c.lottery == lottery).order_by(
            results.c.draw_date.desc(), results.c.draw_time.desc(), results.c.prize.asc()
        ).limit(limit)
        rows = conn.execute(stmt).mappings().all()
    out = [_row_to_dict(r) for r in rows]
    out.reverse()
    if cutoff_iso:
        out = [r for r in out if f"{r['draw_date']}T{r['draw_time']}:00" < cutoff_iso]
    return out


def get_all_results(cutoff_iso: Optional[str] = None, limit: int = 10000) -> list[dict]:
    ensure_schema()
    with engine.connect() as conn:
        stmt = select(results).order_by(results.c.draw_date, results.c.draw_time, results.c.prize).limit(limit)
        rows = conn.execute(stmt).mappings().all()
    out = [_row_to_dict(r) for r in rows]
    if cutoff_iso:
        out = [r for r in out if f"{r['draw_date']}T{r['draw_time']}:00" < cutoff_iso]
    return out


def _decode_json_fields(item: dict) -> dict:
    out = dict(item)
    for key in ("candidates_json", "model_json", "context_json", "evaluation_json"):
        raw = out.get(key)
        out[key] = json.loads(raw) if raw else None
    return out


def save_prediction(payload: dict) -> int:
    """Create an immutable forecast record. Existing forecasts are never replaced."""
    ensure_schema()
    key_filter = and_(
        predictions.c.lottery == payload["lottery"],
        predictions.c.draw_date == payload["draw_date"],
        predictions.c.draw_time == payload["draw_time"],
        predictions.c.modality == payload["modality"],
    )
    with engine.begin() as conn:
        existing = conn.execute(select(predictions).where(key_filter).limit(1)).mappings().first()
        if existing:
            return int(existing["id"])
        result = conn.execute(predictions.insert().values(
            lottery=payload["lottery"], draw_date=payload["draw_date"], draw_time=payload["draw_time"],
            modality=payload["modality"], prediction=payload["prediction"],
            candidates_json=json_dumps(payload.get("candidates", [])),
            cutoff_iso=payload["cutoff_iso"], model_name=payload.get("model_name"),
            model_json=json_dumps(payload.get("model", {})),
            context_json=json_dumps(payload.get("context", {})),
            created_at=now_utc(), evaluated=False,
        ).returning(predictions.c.id))
        pid = int(result.scalar_one())
        conn.execute(prediction_events.insert().values(
            prediction_id=pid, event_type="created", payload_json=json_dumps({
                "prediction": payload["prediction"], "cutoff_iso": payload["cutoff_iso"],
                "model_name": payload.get("model_name"),
            }), created_at=now_utc(),
        ))
        return pid


def get_prediction(lottery: str, draw_date: str, draw_time: str, modality: str) -> Optional[dict]:
    ensure_schema()
    with engine.connect() as conn:
        row = conn.execute(select(predictions).where(and_(
            predictions.c.lottery == lottery, predictions.c.draw_date == draw_date,
            predictions.c.draw_time == draw_time, predictions.c.modality == modality,
        )).limit(1)).mappings().first()
    return _decode_json_fields(dict(row)) if row else None


def list_predictions(
    lottery: Optional[str] = None,
    draw_date: Optional[str] = None,
    evaluated: Optional[bool] = None,
    limit: int = 500,
) -> list[dict]:
    ensure_schema()
    clauses = []
    if lottery:
        clauses.append(predictions.c.lottery == lottery)
    if draw_date:
        clauses.append(predictions.c.draw_date == draw_date)
    if evaluated is not None:
        clauses.append(predictions.c.evaluated == evaluated)
    with engine.connect() as conn:
        stmt = select(predictions).order_by(predictions.c.created_at.desc()).limit(limit)
        if clauses:
            stmt = stmt.where(and_(*clauses))
        rows = conn.execute(stmt).mappings().all()
    return [_decode_json_fields(dict(r)) for r in rows]


def pending_predictions(lottery: Optional[str] = None, limit: int = 500) -> list[dict]:
    return list_predictions(lottery=lottery, evaluated=False, limit=limit)


def prediction_counts() -> dict:
    ensure_schema()
    with engine.connect() as conn:
        total = int(conn.execute(select(func.count()).select_from(predictions)).scalar_one())
        pending = int(conn.execute(select(func.count()).select_from(predictions).where(predictions.c.evaluated == False)).scalar_one())  # noqa: E712
        evaluated = total - pending
    return {"total": total, "pending": pending, "evaluated": evaluated}


def result_counts() -> dict:
    ensure_schema()
    with engine.connect() as conn:
        total = int(conn.execute(select(func.count()).select_from(results)).scalar_one())
        per = conn.execute(select(results.c.lottery, func.count()).group_by(results.c.lottery)).all()
    return {"total": total, "by_lottery": {str(k): int(v) for k, v in per}}


def mark_prediction_evaluated(prediction_id: int, evaluation: dict) -> None:
    ensure_schema()
    with engine.begin() as conn:
        conn.execute(predictions.update().where(predictions.c.id == prediction_id).values(
            evaluated=True, evaluation_json=json_dumps(evaluation),
        ))
        conn.execute(prediction_events.insert().values(
            prediction_id=prediction_id, event_type="evaluated", payload_json=json_dumps(evaluation), created_at=now_utc(),
        ))




def prediction_has_event(prediction_id: int, event_type: str) -> bool:
    """Return True when a prediction already has a lifecycle event of event_type."""
    ensure_schema()
    with engine.connect() as conn:
        row = conn.execute(select(prediction_events.c.id).where(and_(
            prediction_events.c.prediction_id == prediction_id,
            prediction_events.c.event_type == event_type,
        )).limit(1)).first()
    return row is not None


def add_prediction_event(prediction_id: int, event_type: str, payload: dict) -> None:
    """Append a prediction lifecycle event once; duplicates are harmlessly ignored."""
    ensure_schema()
    with engine.begin() as conn:
        exists = conn.execute(select(prediction_events.c.id).where(and_(
            prediction_events.c.prediction_id == prediction_id,
            prediction_events.c.event_type == event_type,
        )).limit(1)).first()
        if exists:
            return
        conn.execute(prediction_events.insert().values(
            prediction_id=prediction_id, event_type=event_type,
            payload_json=json_dumps(payload), created_at=now_utc(),
        ))

def save_model(lottery: str, modality: str, model_name: str, weights: dict, score: float, cases: int) -> None:
    ensure_schema()
    with engine.begin() as conn:
        where = and_(models.c.lottery == lottery, models.c.modality == modality)
        existing = conn.execute(select(models.c.id).where(where).limit(1)).scalar_one_or_none()
        values = {
            "lottery": lottery, "modality": modality, "model_name": model_name,
            "weights_json": json_dumps(weights), "score": float(score), "cases": int(cases), "trained_at": now_utc(),
        }
        if existing:
            conn.execute(models.update().where(models.c.id == existing).values(**values))
        else:
            conn.execute(models.insert().values(**values))


def load_model(lottery: str, modality: str) -> Optional[dict]:
    ensure_schema()
    with engine.connect() as conn:
        row = conn.execute(select(models).where(and_(models.c.lottery == lottery, models.c.modality == modality)).limit(1)).mappings().first()
    if not row:
        return None
    out = dict(row)
    out["weights_json"] = json.loads(out["weights_json"] or "{}")
    return out


def log_sync(draw_date: str, lotteries: list[str], inserted: int, errors: list[str]) -> None:
    ensure_schema()
    with engine.begin() as conn:
        conn.execute(sync_runs.insert().values(
            draw_date=draw_date, lotteries_json=json_dumps(lotteries), inserted=int(inserted),
            errors_json=json_dumps(errors), created_at=now_utc(),
        ))


def log_learning_run(trigger: str, synced_rows: int, evaluated: int, pending_seen: int, errors: list[str]) -> None:
    ensure_schema()
    with engine.begin() as conn:
        conn.execute(learning_runs.insert().values(
            trigger=trigger, synced_rows=int(synced_rows), evaluated=int(evaluated),
            pending_seen=int(pending_seen), errors_json=json_dumps(errors), created_at=now_utc(),
        ))
