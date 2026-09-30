"""Walk-forward calibration and prediction engine.

The engine never sees the target draw while constructing its prediction.
Historical cases are built chronologically: for each target draw, the feature
set contains only records strictly before that draw's timestamp.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Iterable, Optional

BRAZIL_GROUPS = {
    1:"Avestruz",2:"Águia",3:"Burro",4:"Borboleta",5:"Cachorro",6:"Cabra",7:"Carneiro",8:"Camelo",
    9:"Cobra",10:"Coelho",11:"Cavalo",12:"Elefante",13:"Galo",14:"Gato",15:"Jacaré",16:"Leão",
    17:"Macaco",18:"Porco",19:"Pavão",20:"Peru",21:"Touro",22:"Tigre",23:"Urso",24:"Veado",25:"Vaca"
}

MODEL_FAMILIES = {
    "hot": {"freq": 0.55, "recent": 0.25, "overdue": 0.05, "digits": 0.10, "transition": 0.05},
    "overdue": {"freq": 0.10, "recent": 0.10, "overdue": 0.65, "digits": 0.10, "transition": 0.05},
    "recent": {"freq": 0.15, "recent": 0.65, "overdue": 0.05, "digits": 0.10, "transition": 0.05},
    "transition": {"freq": 0.10, "recent": 0.20, "overdue": 0.05, "digits": 0.20, "transition": 0.45},
    "hybrid": {"freq": 0.30, "recent": 0.30, "overdue": 0.10, "digits": 0.15, "transition": 0.15},
}

@dataclass
class CalibrationResult:
    model_name: str
    weights: dict
    score: float
    cases: int
    metrics: dict


def parse_dt(row: dict) -> datetime:
    return datetime.fromisoformat(f"{row['draw_date']}T{row['draw_time']}:00-03:00")


def row_number(row: dict) -> str:
    return str(row.get("number") or "").zfill(4)[-4:]


def group_of(number: str) -> int:
    tail = int(number[-2:])
    if tail == 0:
        return 25
    group = (tail - 1) // 4 + 1
    return 25 if group > 25 else group


def calendar_group(day: date) -> int:
    d = day.day
    if d <= 25:
        return d
    if d <= 28:
        return 25
    return 24


def grouped_draws(rows: Iterable[dict]) -> list[dict]:
    buckets = defaultdict(list)
    for row in rows:
        buckets[(row["draw_date"], row["draw_time"])].append(row)
    draws = []
    for (d, t), values in buckets.items():
        ordered = sorted(values, key=lambda r: int(r.get("prize") or 99))
        if not ordered:
            continue
        draws.append({
            "date": d,
            "time": t,
            "dt": parse_dt({"draw_date": d, "draw_time": t}),
            "rows": ordered,
        })
    return sorted(draws, key=lambda x: x["dt"])


def build_features(rows: list[dict], cutoff: Optional[datetime] = None, reference_date: Optional[date] = None) -> dict:
    if cutoff:
        rows = [r for r in rows if parse_dt(r) < cutoff]
    rows = sorted(rows, key=parse_dt)
    draws = grouped_draws(rows)
    full_numbers = Counter()
    tails = Counter()
    hundreds = Counter()
    groups = Counter()
    digit_pos = [Counter() for _ in range(4)]
    first_prize_numbers = Counter()
    recent_rows = rows[-80:]
    last_seen_tail = {}
    tail_transition = defaultdict(Counter)
    group_transition = defaultdict(Counter)
    for idx, row in enumerate(rows):
        n = row_number(row)
        p = int(row.get("prize") or 0)
        w = 3.0 if p == 1 else 1.5 if p <= 3 else 1.0
        full_numbers[n] += w
        tails[n[-2:]] += w
        hundreds[n[-3:]] += w
        g = int(row.get("group") or group_of(n))
        groups[g] += w
        for pos, ch in enumerate(n):
            digit_pos[pos][int(ch)] += w
        if p == 1:
            first_prize_numbers[n] += 1
        if idx > 0:
            prev = row_number(rows[idx - 1])
            tail_transition[prev[-2:]][n[-2:]] += w
            group_transition[group_of(prev)][g] += w
        last_seen_tail[n[-2:]] = idx

    total = max(1.0, sum(full_numbers.values()))
    latest_draw = draws[-1] if draws else None
    latest_first = row_number(latest_draw["rows"][0]) if latest_draw else None
    moment_groups = Counter()
    if latest_draw:
        for row in latest_draw["rows"][:5]:
            g = int(row.get("group") or group_of(row_number(row)))
            moment_groups[g] += 3 if int(row.get("prize") or 0) == 1 else 1

    def norm(counter: Counter, key, total_override=None):
        total_n = total_override if total_override is not None else sum(counter.values()) or 1.0
        return (counter.get(key, 0.0) + 0.25) / (total_n + 0.25 * max(1, len(counter) or 1))

    return {
        "rows": rows,
        "draws": draws,
        "full_numbers": full_numbers,
        "tails": tails,
        "hundreds": hundreds,
        "groups": groups,
        "digit_pos": digit_pos,
        "first_prize_numbers": first_prize_numbers,
        "tail_transition": tail_transition,
        "group_transition": group_transition,
        "last_seen_tail": last_seen_tail,
        "recent_rows": recent_rows,
        "latest_first": latest_first,
        "moment_groups": moment_groups,
        "calendar_group": calendar_group(reference_date) if reference_date else (calendar_group(parse_dt(rows[-1]).date()) if rows else None),
        "total_weight": total,
        "norm": norm,
    }


def _scaled(counter: Counter, key) -> float:
    if not counter:
        return 0.0
    mx = max(counter.values()) or 1.0
    return math.log1p(counter.get(key, 0.0)) / math.log1p(mx)


def _recency(counter: Counter, key, rows: list[dict], window=50) -> float:
    recent = rows[-window:]
    if not recent:
        return 0.0
    score = 0.0
    denom = 0.0
    for i, row in enumerate(recent):
        age = len(recent) - 1 - i
        w = 0.5 ** (age / 12.0)
        n = row_number(row)
        if n.endswith(str(key)) if len(str(key)) <= 2 else n == str(key):
            score += w
        denom += w
    return score / max(1e-9, denom)


def candidate_score(number: str, features: dict, model_weights: dict, modality: str) -> float:
    n = number.zfill(4)[-4:]
    tail = n[-2:]
    hundred = n[-3:]
    group = group_of(n)
    freq = _scaled(features["full_numbers"], n)
    recent = _recency(features["full_numbers"], n, features["rows"]) * 0.7 + _scaled(features["tails"], tail) * 0.3
    last_idx = features["last_seen_tail"].get(tail)
    overdue = 1.0 if last_idx is None else min(1.0, max(0, len(features["rows"]) - 1 - last_idx) / 60.0)
    digit_score = 0.0
    for pos, ch in enumerate(n):
        cnt = features["digit_pos"][pos]
        digit_score += _scaled(cnt, int(ch)) / 4.0
    trans = 0.0
    latest_first = features.get("latest_first")
    if latest_first:
        trans += _scaled(features["tail_transition"].get(latest_first[-2:], Counter()), tail)
        trans += _scaled(features["group_transition"].get(group_of(latest_first), Counter()), group)
        trans /= 2.0
    calendar = 0.25 if group == features.get("calendar_group") else 0.0
    moment = _scaled(features.get("moment_groups", Counter()), group)
    weights = model_weights
    base = (
        weights["freq"] * freq +
        weights["recent"] * recent +
        weights["overdue"] * overdue +
        weights["digits"] * digit_score +
        weights["transition"] * trans
    )
    # Calendar and moment are intentionally secondary; they do not override data.
    base += 0.08 * calendar + 0.14 * moment
    if modality == "milhar":
        base += 0.10 * _scaled(features["first_prize_numbers"], n)
    elif modality == "centena":
        base += 0.10 * _scaled(features["hundreds"], hundred)
    elif modality == "dezena":
        base += 0.10 * _scaled(features["tails"], tail)
    return base


def rank_candidates(features: dict, modality: str, model_weights: dict, limit: int = 100) -> list[str]:
    if modality == "milhar":
        pool = (f"{i:04d}" for i in range(10000))
    elif modality == "centena":
        pool = (f"{i:04d}" for i in range(1000))
    elif modality == "dezena":
        pool = (f"{i:04d}" for i in range(100))
    elif modality == "grupo":
        scores = []
        for g in range(1, 26):
            s = _scaled(features["groups"], g)
            s += 0.15 * _scaled(features.get("moment_groups", Counter()), g)
            if g == features.get("calendar_group"):
                s += 0.08
            scores.append((s, g))
        scores.sort(reverse=True)
        return [f"{g:02d}" for s, g in scores[:limit]]
    else:
        pool = (f"{i:04d}" for i in range(100))
    scored = []
    for n in pool:
        scored.append((candidate_score(n, features, model_weights, modality), n))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [n[-(4 if modality == "milhar" else 3 if modality == "centena" else 2):] for _, n in scored[:limit]]


def evaluate_prediction(prediction: str, actual_rows: list[dict], modality: str, candidates: Optional[list[str]] = None) -> dict:
    actual_nums = [row_number(r) for r in actual_rows]
    actual_groups = [int(r.get("group") or group_of(row_number(r))) for r in actual_rows]
    p = prediction.zfill(4)
    if modality == "milhar":
        exact = p in actual_nums
        topk = candidates.index(p) + 1 if candidates and p in candidates else None
        return {"exact": exact, "topk_rank": topk, "tail": p[-2:] in [n[-2:] for n in actual_nums], "group": group_of(p) in actual_groups}
    if modality == "centena":
        c = p[-3:]
        exact = c in [n[-3:] for n in actual_nums]
        topk = candidates.index(c) + 1 if candidates and c in candidates else None
        return {"exact": exact, "topk_rank": topk, "tail": c[-2:] in [n[-2:] for n in actual_nums], "group": group_of(c) in actual_groups}
    if modality == "dezena":
        d = p[-2:]
        exact = d in [n[-2:] for n in actual_nums]
        topk = candidates.index(d) + 1 if candidates and d in candidates else None
        return {"exact": exact, "topk_rank": topk, "group": group_of(d) in actual_groups}
    g = int(prediction)
    return {"exact": g in actual_groups, "topk_rank": candidates.index(prediction) + 1 if candidates and prediction in candidates else None}


def score_evaluation(ev: dict) -> float:
    score = 0.0
    if ev.get("exact"): score += 1.0
    rank = ev.get("topk_rank")
    if rank:
        if rank <= 10: score += 0.75
        elif rank <= 25: score += 0.45
        elif rank <= 50: score += 0.20
    if ev.get("tail"): score += 0.30
    if ev.get("group"): score += 0.20
    return score


def model_backtest(rows: list[dict], modality: str, model_name: str, max_cases: int = 40) -> CalibrationResult:
    weights = MODEL_FAMILIES[model_name]
    draws = grouped_draws(rows)
    # Keep enough history to build meaningful features. Skip very early cases.
    usable = draws[max(12, len(draws) - max_cases):]
    if not usable:
        return CalibrationResult(model_name, weights, 0.0, 0, {})
    scores = []
    exact = 0
    top10 = 0
    top25 = 0
    group_hits = 0
    for target in usable:
        history_rows = [r for r in rows if parse_dt(r) < target["dt"]]
        if len(history_rows) < 20:
            continue
        features = build_features(history_rows, reference_date=target["dt"].date())
        ranked = rank_candidates(features, modality, weights, limit=50)
        prediction = ranked[0] if ranked else ("00" if modality != "milhar" else "0000")
        actual = target["rows"][:5]
        ev = evaluate_prediction(prediction, actual, modality, ranked)
        scores.append(score_evaluation(ev))
        exact += int(bool(ev.get("exact")))
        rank = ev.get("topk_rank")
        top10 += int(bool(rank and rank <= 10))
        top25 += int(bool(rank and rank <= 25))
        group_hits += int(bool(ev.get("group")))
    avg = sum(scores) / len(scores) if scores else 0.0
    metrics = {
        "exact_rate": exact / max(1, len(scores)),
        "top10_rate": top10 / max(1, len(scores)),
        "top25_rate": top25 / max(1, len(scores)),
        "group_rate": group_hits / max(1, len(scores)),
    }
    return CalibrationResult(model_name, weights, avg, len(scores), metrics)


def calibrate(rows: list[dict], modality: str, max_cases: int = 40) -> CalibrationResult:
    candidates = [model_backtest(rows, modality, name, max_cases=max_cases) for name in MODEL_FAMILIES]
    candidates.sort(key=lambda x: (-x.score, -x.metrics.get("top25_rate", 0), x.model_name))
    return candidates[0] if candidates else CalibrationResult("hybrid", MODEL_FAMILIES["hybrid"], 0.0, 0, {})


def predict(rows: list[dict], modality: str, model_name: str = "hybrid", limit: int = 50, target_date: Optional[date] = None) -> dict:
    features = build_features(rows, reference_date=target_date)
    weights = MODEL_FAMILIES.get(model_name, MODEL_FAMILIES["hybrid"])
    ranked = rank_candidates(features, modality, weights, limit=limit)
    prediction = ranked[0] if ranked else ("0000" if modality == "milhar" else "000" if modality == "centena" else "00")
    return {
        "prediction": prediction,
        "candidates": ranked,
        "model_name": model_name,
        "weights": weights,
        "history_rows": len(rows),
        "latest_draw": features.get("draws", [])[-1] if features.get("draws") else None,
        "calendar_group": features.get("calendar_group"),
        "moment_groups": dict(features.get("moment_groups", {})),
    }
