import os
import re
import time
import hashlib
from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from zoneinfo import ZoneInfo

APP_NAME = "Oráculo Results Bridge"
VERSION = "0.4.0"
BRAZIL_TZ = ZoneInfo("America/Sao_Paulo")
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "120"))
ARCHIVE_URL = "https://www.ojogodobicho.com/look/resultados-anteriores.htm"
ALLOWED_HOSTS = {"www.ojogodobicho.com", "ojogodobicho.com"}
CACHE = {}
LOOK_TIMES = {"07:20", "09:20", "11:20", "14:20", "16:20", "18:20", "21:20", "23:20"}

app = FastAPI(title=APP_NAME, version=VERSION)
origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "*").split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins != ["*"] else ["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def validate_source_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in ALLOWED_HOSTS:
        raise HTTPException(400, "Fonte HTTPS não autorizada.")
    return url


async def fetch_html(url: str) -> str:
    validate_source_url(url)
    now = time.time()
    key = hashlib.sha256(url.encode()).hexdigest()
    cached = CACHE.get(key)
    if cached and now - cached["at"] < CACHE_TTL:
        return cached["html"]

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; OraculoResultsBridge/0.3; +results parser)",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Falha ao consultar a fonte: {type(exc).__name__}")

    CACHE[key] = {"at": now, "html": response.text}
    return response.text


def source_url_for(day: date) -> str:
    # O arquivo histórico recebe a data explicitamente, inclusive para hoje.
    return f"{ARCHIVE_URL}?d={day.isoformat()}"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_look_page(html: str, requested_day: date):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    page_text = _clean(soup.get_text(" ", strip=True))

    # Fail closed if the archive page itself explicitly identifies another date.
    # Expected heading format includes DD/MM/YYYY or YYYY-MM-DD.
    date_candidates = re.findall(r"\b(\d{2})/(\d{2})/(\d{4})\b", page_text)
    iso_candidates = re.findall(r"\b(20\d{2})-(\d{2})-(\d{2})\b", page_text)
    if date_candidates:
        parsed_dates = set()
        for dd, mm, yyyy in date_candidates:
            try:
                parsed_dates.add(date(int(yyyy), int(mm), int(dd)))
            except ValueError:
                pass
        # Only reject when a single explicit date is present and it differs.
        if len(parsed_dates) == 1 and requested_day not in parsed_dates:
            raise HTTPException(502, detail={
                "status": "source_date_mismatch",
                "requested_date": requested_day.isoformat(),
                "page_dates": [d.isoformat() for d in parsed_dates],
                "message": "A página retornada identifica outra data; nenhum resultado foi devolvido."
            })
    elif iso_candidates:
        parsed_dates = set()
        for yyyy, mm, dd in iso_candidates:
            try:
                parsed_dates.add(date(int(yyyy), int(mm), int(dd)))
            except ValueError:
                pass
        if len(parsed_dates) == 1 and requested_day not in parsed_dates:
            raise HTTPException(502, detail={
                "status": "source_date_mismatch",
                "requested_date": requested_day.isoformat(),
                "page_dates": [d.isoformat() for d in parsed_dates],
                "message": "A página retornada identifica outra data; nenhum resultado foi devolvido."
            })

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        # Find the header row containing draw times. Preserve each cell's true
        # column index, including the first "prize" column.
        header_idx = None
        time_by_col = {}
        for ridx, tr in enumerate(rows[:4]):
            cells = tr.find_all(["th", "td"])
            found = {}
            for cidx, cell in enumerate(cells):
                txt = _clean(cell.get_text(" ", strip=True))
                mt = re.search(r"(?<!\d)(\d{2}:\d{2})(?!\d)", txt)
                if mt and mt.group(1) in LOOK_TIMES:
                    found[cidx] = mt.group(1)
            if len(found) >= 2:
                header_idx, time_by_col = ridx, found
                break
        if header_idx is None:
            continue

        for tr in rows[header_idx + 1:]:
            cells = tr.find_all(["th", "td"])
            if not cells:
                continue
            first = _clean(cells[0].get_text(" ", strip=True))
            rank_match = re.search(r"^\s*([1-7])\s*(?:º|°|o)?\s*$", first, re.I)
            if not rank_match:
                # Some tables put rank text in a separate first cell or row label.
                continue
            prize = int(rank_match.group(1))

            for col_idx, draw_time in time_by_col.items():
                if col_idx >= len(cells):
                    continue
                cell_text = _clean(cells[col_idx].get_text(" ", strip=True))
                # Expected cell form is four-digit result, dash, group 01-25.
                # Do not infer a result from unrelated numbers or nearby cells.
                match = re.fullmatch(r"(\d{1,4})\s*[-–]\s*(\d{1,2})", cell_text)
                if not match:
                    continue
                number_raw, group_raw = match.groups()
                group = int(group_raw)
                if not 1 <= group <= 25:
                    continue
                # Prizes 1-6 are four-digit derived/result numbers; prize 7 is
                # three-digit derived. Keep leading zeroes in the source.
                number = number_raw.zfill(4 if prize <= 6 else 3)
                results.append({
                    "date": requested_day.isoformat(),
                    "lottery": "LOOK Goiás",
                    "draw_time": draw_time,
                    "prize": prize,
                    "number": number,
                    "group": f"{group:02d}",
                    "source": "ojogodobicho.com",
                })

    # Deduplicate only identical records; never overwrite conflicting numbers
    # for the same date/time/prize silently.
    unique = {}
    conflicts = []
    for item in results:
        key = (item["draw_time"], item["prize"])
        previous = unique.get(key)
        if previous is None:
            unique[key] = item
        elif previous["number"] != item["number"] or previous["group"] != item["group"]:
            conflicts.append({
                "draw_time": item["draw_time"],
                "prize": item["prize"],
                "values": [previous["number"], item["number"]],
            })
    if conflicts:
        raise HTTPException(502, detail={
            "status": "conflicting_source_rows",
            "date": requested_day.isoformat(),
            "conflicts": conflicts,
            "message": "A fonte retornou valores conflitantes para o mesmo horário e prêmio. Resultados bloqueados."
        })

    return sorted(unique.values(), key=lambda r: (r["draw_time"], r["prize"]))


def brazil_today() -> date:
    return datetime.now(BRAZIL_TZ).date()



# ---------------------------------------------------------------------------
# Fonte agregadora experimental: Resultado Sorte
# Mantém a integração original da LOOK intacta. Esta rota é separada para
# validar o formato do agregador antes de substituir qualquer integração.
# ---------------------------------------------------------------------------
AGGREGATOR_BASE = "https://resultadosorte.com/arquivo"
AGGREGATOR_HOSTS = {"resultadosorte.com", "www.resultadosorte.com"}

# Aliases intencionais: só associe um nome se houver correspondência conhecida.
# Os nomes não encontrados continuam aparecendo como ausentes, nunca inventados.
LOTTERY_ALIASES = {
    "PT-RIO": ["PT Rio", "PT-RIO", "PT Rio RJ"],
    "Bahia-BA": ["Bahia", "Bahia (Maluca)", "Maluca Bahia"],
    "Para Todos-SP": ["PT-SP", "PT SP", "Loteria Paulista"],
    "LNS Nacional": ["LNS Nacional", "Nacional", "Loteria Nacional"],
    "LOOK Goiás": ["LOOK", "LOOK Goiás", "Look Loterias"],
    "Lotep-PB": ["LOTEP", "Lotep-PB"],
    "Minas-MG": ["Minas", "Minas MG", "Alvorada / Minas Gerais"],
    "LOTECE-CE": ["LOTECE", "Lotece Loteria dos Sonhos", "Paratodos CE"],
    "Para Todos-PB": ["Para Todos PB", "Paratodos PB"],
    "AVAL-PE": ["AVAL Pernambuco", "AVAL-PE"],
    "Tradicional-GO": ["Tradicional-GO", "Loteria Tradicional"],
    "Coruja": ["Coruja", "Corujinha", "Malukinha Rio"],
    "Federal": ["Federal", "Loteria Federal"],
    "Capital-SC": ["Capital-SC", "Capital SC"],
    "Sorte-RS": ["Sorte-RS", "Sorte RS", "Bicho RS"],
}

def _normalize_key(value: str) -> str:
    import unicodedata
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

_ALIAS_LOOKUP = {
    _normalize_key(alias): canonical
    for canonical, aliases in LOTTERY_ALIASES.items()
    for alias in aliases
}

def validate_aggregator_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in AGGREGATOR_HOSTS:
        raise HTTPException(400, "Fonte agregadora HTTPS não autorizada.")
    return url

async def fetch_aggregator_html(day: date) -> tuple[str, str]:
    url = f"{AGGREGATOR_BASE}/{day.isoformat()}/"
    validate_aggregator_url(url)
    key = hashlib.sha256(url.encode()).hexdigest()
    now = time.time()
    cached = CACHE.get(key)
    if cached and now - cached["at"] < CACHE_TTL:
        return cached["html"], url
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; OraculoResultsBridge/0.4; results parser)",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
            response = await client.get(url)
            if response.status_code == 404:
                raise HTTPException(404, detail={
                    "status": "archive_date_unavailable",
                    "date": day.isoformat(),
                    "message": "A fonte não possui arquivo publicado para esta data."
                })
            response.raise_for_status()
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Falha ao consultar agregador: {type(exc).__name__}")
    CACHE[key] = {"at": now, "html": response.text}
    return response.text, url

def parse_aggregator_page(html: str, requested_day: date):
    soup = BeautifulSoup(html, "html.parser")
    parsed = []
    current_bank = None

    # Walk headings and tables in document order so each table inherits the
    # immediately preceding bank heading. Only explicitly labelled results pass.
    for node in soup.find_all(["h2", "h3", "h4", "table"]):
        if node.name in {"h2", "h3", "h4"}:
            heading = _clean(node.get_text(" ", strip=True))
            match = re.search(r"(.+?)(?:\s*[—-]\s*|\s*\()", heading)
            candidate = match.group(1).strip() if match else heading
            normalized_heading = _normalize_key(heading)
            current_bank = None
            for alias_norm, canonical in _ALIAS_LOOKUP.items():
                if alias_norm and (alias_norm == normalized_heading or alias_norm in normalized_heading):
                    current_bank = canonical
                    break
            continue

        if node.name != "table" or not current_bank:
            continue
        rows = node.find_all("tr")
        if len(rows) < 2:
            continue
        header_cells = rows[0].find_all(["th", "td"])
        draw_columns = {}
        for idx, cell in enumerate(header_cells):
            label = _clean(cell.get_text(" ", strip=True))
            tm = re.search(r"(\d{1,2}:\d{2})", label)
            if tm:
                hh, mm = tm.group(1).split(":")
                draw_columns[idx] = f"{int(hh):02d}:{mm}"

        if not draw_columns:
            continue

        for tr in rows[1:]:
            cells = tr.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            prize_text = _clean(cells[0].get_text(" ", strip=True))
            pm = re.search(r"\b([1-7])\s*º?\b", prize_text, re.I)
            if not pm:
                continue
            prize = int(pm.group(1))
            for col_idx, draw_time in draw_columns.items():
                if col_idx >= len(cells):
                    continue
                cell_text = _clean(cells[col_idx].get_text(" ", strip=True))
                # Common archive cell: "4395 24 Veado" or "4395-24".
                nm = re.search(r"(?<!\d)(\d{3,4})\s*(?:[-– ]\s*)?(?:G\s*)?0?(\d{1,2})(?:\s|$)", cell_text, re.I)
                if not nm:
                    continue
                number_raw, group_raw = nm.groups()
                group = int(group_raw)
                if not 1 <= group <= 25:
                    continue
                number = number_raw.zfill(4 if prize <= 5 else 3)
                parsed.append({
                    "date": requested_day.isoformat(),
                    "lottery": current_bank,
                    "draw_time": draw_time,
                    "prize": prize,
                    "number": number,
                    "group": f"{group:02d}",
                    "source": "resultadosorte.com",
                    "validation": "unverified_aggregator",
                })

    # Avoid silently overwriting conflicting data from duplicate table renderings.
    unique = {}
    for row in parsed:
        key = (row["lottery"], row["draw_time"], row["prize"])
        if key in unique:
            old = unique[key]
            if old["number"] != row["number"] or old["group"] != row["group"]:
                raise HTTPException(502, detail={
                    "status": "aggregator_conflict",
                    "date": requested_day.isoformat(),
                    "lottery": row["lottery"],
                    "draw_time": row["draw_time"],
                    "prize": row["prize"],
                    "message": "Foram encontrados valores conflitantes; os dados foram bloqueados."
                })
        else:
            unique[key] = row
    return sorted(unique.values(), key=lambda r: (r["lottery"], r["draw_time"], r["prize"]))

@app.get("/api/aggregated-results")
async def aggregated_results(
    draw_date: Optional[date] = Query(default=None),
    lottery: Optional[str] = Query(default=None, max_length=60),
):
    requested_day = draw_date or brazil_today()
    html, url = await fetch_aggregator_html(requested_day)
    rows = parse_aggregator_page(html, requested_day)
    if lottery:
        target = _normalize_key(lottery)
        rows = [
            row for row in rows
            if _normalize_key(row["lottery"]) == target
            or target in [_normalize_key(a) for a in LOTTERY_ALIASES.get(row["lottery"], [])]
        ]
    if not rows:
        raise HTTPException(404, detail={
            "status": "no_mapped_results",
            "date": requested_day.isoformat(),
            "lottery": lottery,
            "source": url,
            "message": "Nenhum resultado foi extraído com correspondência explícita. Isso não significa que a banca não sorteou; a fonte pode usar outro nome ou estrutura."
        })
    return {
        "status": "ok",
        "date": requested_day.isoformat(),
        "source": url,
        "count": len(rows),
        "lotteries_found": sorted({r["lottery"] for r in rows}),
        "results": rows,
        "note": "Fonte agregadora experimental. Confira a banca, horário e status na fonte original antes de usar como histórico oficial."
    }

@app.get("/api/sources")
def sources():
    return {
        "version": VERSION,
        "existing": ["LOOK Goiás via ojogodobicho.com"],
        "experimental": ["Resultado Sorte via resultadosorte.com"],
        "requested_lotteries": list(LOTTERY_ALIASES.keys()),
        "note": "A presença de um nome na lista não confirma que a fonte publica resultados para ele."
    }

@app.get("/")
def root():
    return {
        "service": APP_NAME,
        "status": "online",
        "version": VERSION,
        "supported_source": "LOOK Goiás validada; agregador Resultado Sorte experimental",
        "supported_times": sorted(LOOK_TIMES),
        "note": "Parser histórico por data; confirme sempre na fonte. Sem garantia de palpites ou ganhos."
    }


@app.get("/health")
def health():
    return {"ok": True, "service": APP_NAME, "version": VERSION}


@app.get("/api/results")
async def results(
    lottery: str = Query(default="LOOK Goiás", min_length=1, max_length=60),
    draw_date: Optional[date] = Query(default=None),
    draw_time: Optional[str] = Query(default=None, pattern=r"^\d{2}:\d{2}$"),
):
    normalized = lottery.strip().lower()
    if normalized not in {"look goiás", "look goias", "look"}:
        raise HTTPException(400, detail={"error": "lottery_not_supported", "supported": ["LOOK Goiás"]})

    requested_day = draw_date or brazil_today()
    if draw_time and draw_time not in LOOK_TIMES:
        raise HTTPException(400, detail={"error": "draw_time_not_supported", "supported_times": sorted(LOOK_TIMES)})

    url = validate_source_url(source_url_for(requested_day))
    html = await fetch_html(url)
    parsed = parse_look_page(html, requested_day)

    # Do not return draws in the future according to Brazil local time when
    # requesting today's date. Past-date archive records are unaffected.
    if requested_day == brazil_today():
        now_local = datetime.now(BRAZIL_TZ)
        parsed = [
            r for r in parsed
            if datetime.combine(requested_day, datetime.strptime(r["draw_time"], "%H:%M").time(), tzinfo=BRAZIL_TZ) <= now_local
        ]

    if draw_time:
        parsed = [r for r in parsed if r["draw_time"] == draw_time]

    if not parsed:
        raise HTTPException(404, detail={
            "status": "no_published_results",
            "lottery": "LOOK Goiás",
            "date": requested_day.isoformat(),
            "draw_time": draw_time,
            "source": url,
            "message": "Nenhum resultado publicado e validado foi encontrado para essa data/horário. Nenhum dado de outro dia foi reaproveitado."
        })

    return {
        "status": "ok",
        "lottery": "LOOK Goiás",
        "date": requested_day.isoformat(),
        "source": url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(parsed),
        "results": parsed,
        "note": "Confira os dados na fonte original. A extração depende da estrutura e disponibilidade do site."
    }
