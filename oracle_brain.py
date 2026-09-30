"""Production Oráculo brain.

The brain is deliberately temporal: a prediction is built only from rows strictly
before its cutoff. It mixes frequency, recency, overdue tails/groups, digit
position statistics, prize-position weights, draw-to-draw transitions, calendar
bicho and the immediately preceding draw. Calibration is walk-forward and kept
separate by lottery/modality.

This is a statistical forecasting engine, not a guarantee of future lottery or
jogo do bicho results.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional

BICHOS = {
    1:"Avestruz", 2:"Águia", 3:"Burro", 4:"Borboleta", 5:"Cachorro",
    6:"Cabra", 7:"Carneiro", 8:"Camelo", 9:"Cobra", 10:"Coelho",
    11:"Cavalo", 12:"Elefante", 13:"Galo", 14:"Gato", 15:"Jacaré",
    16:"Leão", 17:"Macaco", 18:"Porco", 19:"Pavão", 20:"Peru",
    21:"Touro", 22:"Tigre", 23:"Urso", 24:"Veado", 25:"Vaca",
}

MODEL_FAMILIES = {
    "balanced": {"freq": .24, "recent": .20, "overdue": .10, "digits": .14, "transition": .14, "group": .08, "prize1": .10},
    "hot": {"freq": .46, "recent": .28, "overdue": .03, "digits": .07, "transition": .06, "group": .04, "prize1": .06},
    "overdue": {"freq": .08, "recent": .10, "overdue": .58, "digits": .08, "transition": .06, "group": .06, "prize1": .04},
    "recent": {"freq": .16, "recent": .52, "overdue": .04, "digits": .12, "transition": .08, "group": .04, "prize1": .04},
    "transition": {"freq": .10, "recent": .15, "overdue": .05, "digits": .18, "transition": .38, "group": .08, "prize1": .06},
    "group": {"freq": .12, "recent": .14, "overdue": .08, "digits": .10, "transition": .12, "group": .38, "prize1": .06},
}

ORACLE_SCHEDULES = {
    "PT-RIO": ["09:20", "11:20", "14:20", "16:20", "18:20", "21:20"],
    "Para Todos-SP": ["08:20", "10:20", "12:20", "13:20", "15:20", "17:20", "19:20", "20:20"],
    "LOOK Goiás": ["07:20", "09:20", "11:20", "14:20", "16:20", "18:20", "21:20", "23:20"],
    "LNS Nacional": ["02:00", "08:00", "10:00", "12:00", "15:00", "17:00", "21:00", "23:00"],
}

MODALITY_ALIASES = {
    "duque_dezena": "duque_dezena",
    "duque": "duque_grupo",
    "duque_grupo": "duque_grupo",
    "terno_dezena": "terno_dezena",
    "terno": "terno_grupo",
    "terno_grupo": "terno_grupo",
    "passe_vai": "passe_vai",
    "passe": "passe_vai",
    "palpitao": "palpitao",
}


def parse_dt(row: dict) -> datetime:
    return datetime.fromisoformat(f"{row['draw_date'] if 'draw_date' in row else row['date']}T{row['draw_time']}:00-03:00")


def row_number(row: dict) -> str:
    return str(row.get("number") or "").zfill(4)[-4:]


def group_of(number: str) -> int:
    tail = int(str(number).zfill(4)[-2:])
    if tail == 0:
        return 25
    return min(25, ((tail - 1) // 4) + 1)


def calendar_group(day: date) -> int:
    # User-defined calendar mapping: 1-25 are their same-number groups,
    # 26-28 = Carneiro (7), 29-31 = Camelo (8).
    if day.day <= 25:
        return day.day
    if day.day <= 28:
        return 7
    return 8


def deterministic_rng(seed_text: str):
    state = 2166136261
    for ch in seed_text:
        state ^= ord(ch)
        state = (state * 16777619) & 0xFFFFFFFF
    def rnd():
        nonlocal state
        state = (state + 0x6D2B79F5) & 0xFFFFFFFF
        t = state
        t = (t ^ (t >> 15)) * (t | 1) & 0xFFFFFFFF
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & 0xFFFFFFFF)) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296.0
    return rnd


def group_draws(rows: Iterable[dict]) -> list[dict]:
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        buckets[(str(row.get("draw_date") or row.get("date")), str(row.get("draw_time"))[:5])].append(row)
    out = []
    for (d, t), vals in buckets.items():
        vals = sorted(vals, key=lambda r: int(r.get("prize") or 999))
        if vals:
            out.append({"date": d, "time": t, "dt": parse_dt({"draw_date": d, "draw_time": t}), "rows": vals})
    return sorted(out, key=lambda x: x["dt"])


def _age_weight(draw_dt: datetime, latest_dt: datetime) -> float:
    days = max(0.0, (latest_dt - draw_dt).total_seconds() / 86400.0)
    return math.exp(-days / 12.0)


def build_features(rows: list[dict], reference_date: Optional[date] = None, special_signal: Optional[dict] = None) -> dict:
    rows = sorted(rows, key=parse_dt)
    draws = group_draws(rows)
    latest_dt = draws[-1]["dt"] if draws else None
    full = Counter(); tails = Counter(); hundreds = Counter(); groups = Counter()
    prize_full = {p: Counter() for p in range(1, 11)}
    prize_tails = {p: Counter() for p in range(1, 11)}
    digit_pos = [Counter() for _ in range(4)]
    tail_transition: dict[str, Counter] = defaultdict(Counter)
    group_transition: dict[int, Counter] = defaultdict(Counter)
    group_last_seen: dict[int, int] = {}
    tail_last_seen: dict[str, int] = {}
    day_window = Counter()
    recent_2day = Counter()

    for idx, row in enumerate(rows):
        n = row_number(row)
        p = int(row.get("prize") or 0)
        g = int(row.get("group") or row.get("group_code") or group_of(n))
        wt = (3.2 if p == 1 else 1.9 if p == 2 else 1.4 if p <= 5 else 1.0)
        if latest_dt:
            wt *= _age_weight(parse_dt(row), latest_dt)
        full[n] += wt
        tails[n[-2:]] += wt
        hundreds[n[-3:]] += wt
        groups[g] += wt
        if 1 <= p <= 10:
            prize_full[p][n] += wt
            prize_tails[p][n[-2:]] += wt
        for pos, ch in enumerate(n):
            digit_pos[pos][int(ch)] += wt
        group_last_seen.setdefault(g, idx)
        tail_last_seen.setdefault(n[-2:], idx)
        if idx:
            prev = row_number(rows[idx - 1])
            tail_transition[prev[-2:]][n[-2:]] += wt
            group_transition[group_of(prev)][g] += wt
        if reference_date:
            d = parse_dt(row).date()
            age = (reference_date - d).days
            if 0 <= age <= 2:
                mult = 3.0 if age == 0 else 1.7 if age == 1 else 1.0
                recent_2day[n] += mult * (3 if p == 1 else 1)

    latest_draw = draws[-1] if draws else None
    latest_rows = latest_draw["rows"][:5] if latest_draw else []
    moment_groups = Counter()
    moment_tails = Counter()
    for row in latest_rows:
        n = row_number(row); p = int(row.get("prize") or 0)
        g = int(row.get("group") or row.get("group_code") or group_of(n))
        w = 3.5 if p == 1 else 1.2
        moment_groups[g] += w; moment_tails[n[-2:]] += w

    return {
        "rows": rows, "draws": draws, "full": full, "tails": tails, "hundreds": hundreds,
        "groups": groups, "prize_full": prize_full, "prize_tails": prize_tails,
        "digit_pos": digit_pos, "tail_transition": tail_transition, "group_transition": group_transition,
        "tail_last_seen": tail_last_seen, "group_last_seen": group_last_seen,
        "recent_2day": recent_2day, "latest_draw": latest_draw, "latest_rows": latest_rows,
        "moment_groups": moment_groups, "moment_tails": moment_tails,
        "calendar_group": calendar_group(reference_date) if reference_date else None,
        "special_signal": special_signal or {},
    }


def _norm(counter: Counter, key) -> float:
    if not counter:
        return 0.0
    mx = max(counter.values()) or 1.0
    return math.log1p(counter.get(key, 0.0)) / math.log1p(mx)


def _overdue(counter: Counter, key, total_items: int, last_seen: dict) -> float:
    idx = last_seen.get(key)
    if idx is None:
        return 1.0
    gap = max(0, total_items - 1 - idx)
    return min(1.0, gap / 40.0)


def score_number(number: str, f: dict, weights: dict, modality: str, previous_tail: Optional[str] = None) -> float:
    n = str(number).zfill(4)[-4:]
    tail = n[-2:]; hundred = n[-3:]; g = group_of(n)
    freq = _norm(f["full"], n) * 0.58 + _norm(f["tails"], tail) * 0.27 + _norm(f["hundreds"], hundred) * 0.15
    recent = _norm(f["recent_2day"], n) * 0.65 + _norm(f["recent_2day"], tail) * 0.35
    # If recent_2day does not contain a bare tail key, the second part is zero; that is fine.
    overdue = _overdue(f["tails"], tail, len(f["rows"]), f["tail_last_seen"])
    digits = sum(_norm(f["digit_pos"][pos], int(ch)) for pos, ch in enumerate(n)) / 4.0
    ref_tail = previous_tail or (row_number(f["latest_rows"][0])[-2:] if f["latest_rows"] else None)
    trans = 0.0
    if ref_tail:
        trans += _norm(f["tail_transition"].get(ref_tail, Counter()), tail) * 0.65
        ref_group = group_of("00" + ref_tail)
        trans += _norm(f["group_transition"].get(ref_group, Counter()), g) * 0.35
    group_score = _norm(f["groups"], g)
    prize1 = _norm(f["prize_full"].get(1, Counter()), n) * 0.65 + _norm(f["prize_tails"].get(1, Counter()), tail) * 0.35
    calendar = 0.14 if g == f.get("calendar_group") else 0.0
    moment = 0.18 * _norm(f.get("moment_groups", Counter()), g) + 0.08 * _norm(f.get("moment_tails", Counter()), tail)
    special = 0.0
    sig = f.get("special_signal") or {}
    if sig.get("first_group"):
        special += 0.12 if g == sig["first_group"] else 0.0
    if sig.get("first_tail"):
        special += 0.10 * _norm({sig["first_tail"]: 1, tail: 0}, sig["first_tail"]) if tail == sig["first_tail"] else 0.0
    return (
        weights["freq"] * freq + weights["recent"] * recent + weights["overdue"] * overdue +
        weights["digits"] * digits + weights["transition"] * trans + weights["group"] * group_score +
        weights["prize1"] * prize1 + calendar + moment + special
    )


def group_scores(f: dict, weights: dict, amount: int = 25) -> list[tuple[float, int]]:
    scored=[]
    for g in range(1,26):
        s = weights["group"] * _norm(f["groups"], g)
        s += weights["overdue"] * _overdue(f["groups"], g, len(f["rows"]), f["group_last_seen"])
        s += weights["transition"] * _norm(f["group_transition"].get(group_of(row_number(f["latest_rows"][0])) if f["latest_rows"] else 0, Counter()), g)
        s += 0.12 * (g == f.get("calendar_group"))
        s += 0.20 * _norm(f.get("moment_groups", Counter()), g)
        if (f.get("special_signal") or {}).get("first_group") == g:
            s += 0.12
        scored.append((s,g))
    return sorted(scored, key=lambda x:(-x[0],x[1]))[:amount]


def rank_numbers(f: dict, modality: str, weights: dict, limit: int = 100, previous_tail: Optional[str] = None) -> list[str]:
    modality = MODALITY_ALIASES.get(modality, modality)
    if modality in {"grupo", "duque_grupo", "terno_grupo", "passe_vai"}:
        return [f"{g:02d}" for _,g in group_scores(f, weights, min(limit,25))]
    if modality == "milhar":
        pool=(f"{i:04d}" for i in range(10000))
    elif modality == "centena":
        pool=(f"{i:04d}" for i in range(1000))
    else:
        pool=(f"{i:04d}" for i in range(100))
    scored=[]
    for n in pool:
        scored.append((score_number(n, f, weights, modality, previous_tail), n))
    scored.sort(key=lambda x:(-x[0],x[1]))
    width={"milhar":4,"centena":3,"dezena":2,"palpitao":2}.get(modality,4)
    return [n[-width:] for _,n in scored[:limit]]


def _weighted_choice(items: list[str], salt: str, top: int = 30) -> str:
    items = items[:max(1, min(top, len(items)))]
    rnd = deterministic_rng(salt)
    weights = [math.exp(-i / max(2.5, len(items)/7)) for i in range(len(items))]
    total = sum(weights); r = rnd() * total
    acc = 0.0
    for item,w in zip(items,weights):
        acc += w
        if r <= acc:
            return item
    return items[0]


def _pick_unique(ranked: list[str], amount: int, salt: str, blocked: Optional[set[str]] = None) -> list[str]:
    blocked = blocked or set()
    pool=[x for x in ranked if x not in blocked]
    out=[]; used=set(blocked); rnd=deterministic_rng(salt)
    # Deterministic weighted sweep with small controlled skips for diversity.
    for idx,x in enumerate(pool):
        if len(out)>=amount: break
        keep = idx < 12 or rnd() < max(0.08, 0.55 - idx/len(pool))
        if keep and x not in used:
            out.append(x); used.add(x)
    for x in pool:
        if len(out)>=amount: break
        if x not in used:
            out.append(x); used.add(x)
    return out[:amount]


def generate_portfolio(rows: list[dict], lottery: str, mode: str, target_date: date, draw_time: Optional[str],
                       model_name: str, weights: dict, special_signal: Optional[dict] = None,
                       context_seed: str = "") -> dict:
    reference = target_date
    f = build_features(rows, reference_date=reference, special_signal=special_signal)
    previous_tail = row_number(f["latest_rows"][0])[-2:] if f["latest_rows"] else None
    base_salt=f"{lottery}|{mode}|{target_date.isoformat()}|{draw_time or '00:00'}|{model_name}|{context_seed}"
    ranked_m=rank_numbers(f,"milhar",weights,150,previous_tail)
    ranked_c=rank_numbers(f,"centena",weights,120,previous_tail)
    ranked_d=rank_numbers(f,"dezena",weights,100,previous_tail)
    ranked_g=rank_numbers(f,"grupo",weights,25,previous_tail)

    milhar=_weighted_choice(ranked_m,base_salt+"|milhar",35)
    centena=_weighted_choice(ranked_c,base_salt+"|centena",35)
    dezena=_weighted_choice(ranked_d,base_salt+"|dezena",35)
    group=_weighted_choice(ranked_g,base_salt+"|grupo",18)

    duque_d=_pick_unique(ranked_d,2,base_salt+"|duque_dezena")
    terno_d=_pick_unique(ranked_d,3,base_salt+"|terno_dezena",set(duque_d[:1]))
    duque_g=_pick_unique(ranked_g,2,base_salt+"|duque_grupo")
    terno_g=_pick_unique(ranked_g,3,base_salt+"|terno_grupo",set(duque_g[:1]))
    passe=_pick_unique(ranked_g,2,base_salt+"|passe_vai")
    palpitao=_pick_unique(ranked_d,20,base_salt+"|palpitao")

    return {
        "mode": mode, "lottery": lottery, "date": target_date.isoformat(), "time": draw_time,
        "model_name": model_name, "model_weights": weights,
        "portfolio": {
            "milhar": milhar,
            "centena": centena,
            "dezena": dezena,
            "grupo": group,
            "duque_dezena": duque_d,
            "terno_dezena": terno_d,
            "duque_grupo": duque_g,
            "terno_grupo": terno_g,
            "passe_vai": passe,
            "palpitao": palpitao,
        },
        "rankings": {"milhar": ranked_m[:50], "centena": ranked_c[:50], "dezena": ranked_d[:50], "grupo": ranked_g},
        "features": {
            "history_rows": len(rows), "latest_draw": f.get("latest_draw"),
            "calendar_group": f.get("calendar_group"), "moment_groups": dict(f.get("moment_groups", Counter())),
            "special_signal": f.get("special_signal", {}),
        }
    }


def _prediction_primary(portfolio: dict, modality: str):
    m=MODALITY_ALIASES.get(modality,modality)
    p=portfolio["portfolio"]
    if m in {"duque_dezena","terno_dezena","duque_grupo","terno_grupo","passe_vai","palpitao"}:
        return ", ".join(map(str,p[m]))
    return str(p[m])


def _extract_values(prediction: str, modality: str):
    m=MODALITY_ALIASES.get(modality,modality)
    import re
    vals=re.findall(r"\d+", str(prediction))
    if m=="milhar": return [v.zfill(4)[-4:] for v in vals[:1]]
    if m=="centena": return [v.zfill(3)[-3:] for v in vals[:1]]
    if m=="dezena": return [v.zfill(2)[-2:] for v in vals]
    if m in {"grupo","duque_grupo","terno_grupo","passe_vai"}: return [v.zfill(2)[-2:] for v in vals]
    if m in {"duque_dezena","terno_dezena","palpitao"}: return [v.zfill(2)[-2:] for v in vals]
    return vals


def evaluate_forecast(prediction: str, actual_rows: list[dict], modality: str, candidates: list[str] | None=None) -> dict:
    nums=[row_number(r) for r in sorted(actual_rows,key=lambda r:int(r.get("prize") or 999))]
    groups=[int(r.get("group") or r.get("group_code") or group_of(row_number(r))) for r in actual_rows]
    vals=_extract_values(prediction,modality)
    m=MODALITY_ALIASES.get(modality,modality)
    actual_tails=[n[-2:] for n in nums]
    exact=False; group_hit=False; tail_hit=False; topk=None
    if m=="milhar":
        exact=bool(vals and vals[0] in nums)
        tail_hit=bool(vals and vals[0][-2:] in actual_tails)
        group_hit=bool(vals and group_of(vals[0]) in groups)
        topk=(candidates.index(vals[0])+1) if candidates and vals and vals[0] in candidates else None
    elif m=="centena":
        exact=bool(vals and vals[0][-3:] in [n[-3:] for n in nums])
        tail_hit=bool(vals and vals[0][-2:] in actual_tails)
        group_hit=bool(vals and group_of(vals[0]) in groups)
        topk=(candidates.index(vals[0])+1) if candidates and vals and vals[0] in candidates else None
    elif m=="dezena":
        exact=any(v in actual_tails for v in vals); tail_hit=exact
        group_hit=any(group_of(v) in groups for v in vals)
        topk=(min([candidates.index(v)+1 for v in vals if candidates and v in candidates]) if candidates and any(v in candidates for v in vals) else None)
    elif m=="grupo":
        gs=[int(v) for v in vals]
        exact=any(g in groups for g in gs); group_hit=exact
        topk=(min([candidates.index(v)+1 for v in vals if candidates and v in candidates]) if candidates and any(v in candidates for v in vals) else None)
    else:
        # Combination modalities are scored by any component group/tail hitting;
        # exact means every requested component appeared in the observed five.
        if m in {"duque_dezena","terno_dezena","palpitao"}:
            exact=all(v in actual_tails for v in vals) if vals else False
            tail_hit=any(v in actual_tails for v in vals)
            group_hit=any(group_of(v) in groups for v in vals)
        else:
            gs=[int(v) for v in vals]
            exact=all(g in groups for g in gs) if gs else False
            group_hit=any(g in groups for g in gs)
    score=(2.0 if exact else 0.0)+(0.55 if tail_hit else 0.0)+(0.8 if group_hit else 0.0)
    if topk:
        score += 0.8 if topk<=10 else 0.45 if topk<=25 else 0.2 if topk<=50 else 0
    return {"exact":exact,"tail":tail_hit,"group":group_hit,"topk_rank":topk,"score":round(score,4),"actual_prizes":nums[:10]}


def calibrate(rows: list[dict], modality: str, max_cases: int=60) -> dict:
    draws=group_draws(rows)
    if len(draws)<10:
        return {"model_name":"balanced","weights":MODEL_FAMILIES["balanced"],"score":0.0,"cases":0,"metrics":{}}
    # Use recent cases while preserving enough history before each target.
    targets=draws[-max_cases:]
    results=[]
    for name,weights in MODEL_FAMILIES.items():
        scores=[]; exact=tail=group=top10=top25=0
        for target in targets:
            hist=[r for r in rows if parse_dt(r) < target["dt"]]
            if len(hist)<18: continue
            f=build_features(hist,reference_date=target["dt"].date())
            prev_tail=row_number(f["latest_rows"][0])[-2:] if f["latest_rows"] else None
            ranked=rank_numbers(f,MODALITY_ALIASES.get(modality,modality),weights,60,prev_tail)
            pred=ranked[0] if ranked else ("0000" if modality=="milhar" else "000")
            ev=evaluate_forecast(pred,target["rows"][:5],modality,ranked)
            scores.append(ev["score"]); exact+=int(ev["exact"]); tail+=int(ev["tail"]); group+=int(ev["group"])
            if ev.get("topk_rank") and ev["topk_rank"]<=10: top10+=1
            if ev.get("topk_rank") and ev["topk_rank"]<=25: top25+=1
        cases=len(scores)
        avg=sum(scores)/cases if cases else 0.0
        results.append({"model_name":name,"weights":weights,"score":avg,"cases":cases,
                        "metrics":{"exact_rate":exact/max(1,cases),"top10_rate":top10/max(1,cases),"top25_rate":top25/max(1,cases),"group_rate":group/max(1,cases)}})
    results.sort(key=lambda x:(-x["score"],-x["metrics"]["group_rate"],-x["metrics"]["top25_rate"],x["model_name"]))
    best=results[0] if results else {"model_name":"balanced","weights":MODEL_FAMILIES["balanced"],"score":0.0,"cases":0,"metrics":{}}
    return {**best,"alternatives":results[:6]}
