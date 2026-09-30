"""Persistent storage for the Oráculo learning loop.

Uses PostgreSQL when DATABASE_URL is configured (recommended for Render).
Falls back to local SQLite for development. SQLite on Render Free is only a
fallback/demo because the filesystem is not persistent there.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    select,
    and_,
    desc,
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

engine: Engine = create_engine(
    DB_URL,
    future=True,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
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


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def ensure_schema() -> None:
    metadata.create_all(engine)


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
            draw_date = str(row.get("date") or "")[:10]
            draw_time = str(row.get("draw_time") or "")[:5]
            prize = int(row.get("prize") or 0)
            number = str(row.get("number") or "").strip()
            group_code = str(row.get("group") or "").zfill(2)
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
            existing = conn.execute(stmt).first()
            if existing:
                continue
            conn.execute(results.insert().values(
                lottery=lottery,
                draw_date=draw_date,
                draw_time=draw_time,
                prize=prize,
                number=number,
                group_code=group_code,
                source=row.get("source"),
                raw_json=json_dumps(row),
                created_at=now_utc(),
            ))
            inserted += 1
    return inserted


def get_results(lottery: str, cutoff_iso: Optional[str] = None, limit: int = 1000) -> list[dict]:
    ensure_schema()
    with engine.connect() as conn:
        stmt = select(results).where(results.c.lottery == lottery).order_by(
            desc(results.c.draw_date), desc(results.c.draw_time), results.c.prize
        ).limit(limit)
        rows = conn.execute(stmt).mappings().all()
    out = [dict(r) for r in rows]
    out.reverse()  # chronological for model building
    if cutoff_iso:
        out = [r for r in out if f"{r['draw_date']}T{r['draw_time']}:00" < cutoff_iso]
    return out


def get_all_results(cutoff_iso: Optional[str] = None, limit: int = 10000) -> list[dict]:
    ensure_schema()
    with engine.connect() as conn:
        stmt = select(results).order_by(results.c.draw_date, results.c.draw_time, results.c.prize).limit(limit)
        rows = conn.execute(stmt).mappings().all()
    out = [dict(r) for r in rows]
    if cutoff_iso:
        out = [r for r in out if f"{r['draw_date']}T{r['draw_time']}:00" < cutoff_iso]
    return out


def save_prediction(payload: dict) -> int:
    ensure_schema()
    with engine.begin() as conn:
        # One frozen prediction per lottery/date/time/modality.
        where = and_(
            predictions.c.lottery == payload["lottery"],
            predictions.c.draw_date == payload["draw_date"],
            predictions.c.draw_time == payload["draw_time"],
            predictions.c.modality == payload["modality"],
        )
        existing = conn.execute(select(predictions.c.id).where(where).limit(1)).scalar_one_or_none()
        values = {
            "lottery": payload["lottery"],
            "draw_date": payload["draw_date"],
            "draw_time": payload["draw_time"],
            "modality": payload["modality"],
            "prediction": payload["prediction"],
            "candidates_json": json_dumps(payload.get("candidates", [])),
            "cutoff_iso": payload["cutoff_iso"],
            "model_name": payload.get("model_name"),
            "model_json": json_dumps(payload.get("model", {})),
            "context_json": json_dumps(payload.get("context", {})),
            "created_at": now_utc(),
        }
        if existing:
            conn.execute(predictions.update().where(predictions.c.id == existing).values(**values))
            return int(existing)
        return int(conn.execute(predictions.insert().values(**values).returning(predictions.c.id)).scalar_one())


def get_prediction(lottery: str, draw_date: str, draw_time: str, modality: str) -> Optional[dict]:
    ensure_schema()
    with engine.connect() as conn:
        row = conn.execute(select(predictions).where(and_(
            predictions.c.lottery == lottery,
            predictions.c.draw_date == draw_date,
            predictions.c.draw_time == draw_time,
            predictions.c.modality == modality,
        )).limit(1)).mappings().first()
    if not row:
        return None
    out = dict(row)
    for key in ("candidates_json", "model_json", "context_json", "evaluation_json"):
        raw = out.get(key)
        out[key] = json.loads(raw) if raw else None
    return out


def pending_predictions(lottery: Optional[str] = None, limit: int = 500) -> list[dict]:
    ensure_schema()
    with engine.connect() as conn:
        stmt = select(predictions).where(predictions.c.evaluated == False).order_by(predictions.c.created_at).limit(limit)  # noqa: E712
        if lottery:
            stmt = stmt.where(predictions.c.lottery == lottery)
        rows = conn.execute(stmt).mappings().all()
    out = []
    for row in rows:
        item = dict(row)
        for key in ("candidates_json", "model_json", "context_json", "evaluation_json"):
            raw = item.get(key)
            item[key] = json.loads(raw) if raw else None
        out.append(item)
    return out


def mark_prediction_evaluated(prediction_id: int, evaluation: dict) -> None:
    ensure_schema()
    with engine.begin() as conn:
        conn.execute(predictions.update().where(predictions.c.id == prediction_id).values(
            evaluated=True,
            evaluation_json=json_dumps(evaluation),
        ))


def save_model(lottery: str, modality: str, model_name: str, weights: dict, score: float, cases: int) -> None:
    ensure_schema()
    with engine.begin() as conn:
        where = and_(models.c.lottery == lottery, models.c.modality == modality)
        existing = conn.execute(select(models.c.id).where(where).limit(1)).scalar_one_or_none()
        values = {
            "lottery": lottery,
            "modality": modality,
            "model_name": model_name,
            "weights_json": json_dumps(weights),
            "score": float(score),
            "cases": int(cases),
            "trained_at": now_utc(),
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
            draw_date=draw_date,
            lotteries_json=json_dumps(lotteries),
            inserted=int(inserted),
            errors_json=json_dumps(errors),
            created_at=now_utc(),
        ))
