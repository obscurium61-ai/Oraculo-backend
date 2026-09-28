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
VERSION = "1.2.0"
BRAZIL_TZ = ZoneInfo("America/Sao_Paulo")
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "120"))
ARCHIVE_URL = "https://www.ojogodobicho.com/look/resultados-anteriores.htm"
ALLOWED_HOSTS = {"www.ojogodobicho.com", "ojogodobicho.com", "ojogodobiicho.com", "www.ojogodobiicho.com"}
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
    return f"{ARCHIVE_URL}?d={day.isoformat()}"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_look_page(html: str, requested_day: date):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    page_text = _clean(soup.get_text(" ", strip=True))

    date_candidates = re.findall(r"\b(\d{2})/(\d{2})/(\d{4})\b", page_text)
    iso_candidates = re.findall(r"\b(20\d{2})-(\d{2})-(\d{2})\b", page_text)
    if date_candidates:
        parsed_dates = set()
        for dd, mm, yyyy in date_candidates:
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
                continue
            prize = int(rank_match.group(1))

            for col_idx, draw_time in time_by_col.items():
                if col_idx >= len(cells):
                    continue
                cell_text = _clean(cells[col_idx].get_text(" ", strip=True))
                match = re.fullmatch(r"(\d{1,4})\s*[-–]\s*(\d{1,2})", cell_text)
                if not match:
                    continue
                number_raw, group_raw = match.groups()
                group = int(group_raw)
                if not 1 <= group <= 25:
                    continue
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
            "message": "A fonte retornou valores conflitantes. Resultados bloqueados."
        })

    return sorted(unique.values(), key=lambda r: (r["draw_time"], r["prize"]))


def brazil_today() -> date:
    return datetime.now(BRAZIL_TZ).date()


AGGREGATOR_BASES = [
    "https://resultadosorte.com/arquivo",
    "https://ojogodobiicho.com/resultados-anteriores",
]
AGGREGATOR_HOSTS = {"resultadosorte.com", "www.resultadosorte.com", "ojogodobiicho.com", "www.ojogodobiicho.com"}

LOTTERY_ALIASES = {
    "PT-RIO": ["PT Rio", "PT-RIO", "PT Rio RJ"],
    "Bahia-BA": ["Bahia", "Bahia (Maluca)", "Maluca Bahia"],
    "Para Todos-SP": ["PT-SP", "PT SP", "Loteria Paulista", "Para Todos SP", "Bicho SP", "São Paulo"],
    "LNS Nacional": ["LNS Nacional", "Nacional", "Loteria Nacional"],
    "LOOK Goiás": ["LOOK", "LOOK Goiás", "Look Loterias"],
    "Lotep-PB": ["LOTEP", "Lotep-PB", "Lotep PB"],
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
    urls = [
        f"{AGGREGATOR_BASES[0]}/{day.isoformat()}/",
        f"{AGGREGATOR_BASES[1]}/{day.year:04d}/{day.month:02d}/{day.day:02d}",
        f"{AGGREGATOR_BASES[1]}/{day.year:04d}/{day.month:02d}/{day.day:02d}/",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }
    errors = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
        for url in urls:
            validate_aggregator_url(url)
            key = hashlib.sha256(url.encode()).hexdigest()
            cached = CACHE.get(key)
            if cached and time.time() - cached["at"] < CACHE_TTL:
                return cached["html"], url
            try:
                response = await client.get(url)
                if response.status_code == 404:
                    errors.append(f"{url}: arquivo não encontrado")
                    continue
                response.raise_for_status()
                body = response.text or ""
                if len(body.strip()) < 300:
                    errors.append(f"{url}: resposta vazia/incompleta")
                    continue
                CACHE[key] = {"at": time.time(), "html": body}
                return body, str(response.url)
            except httpx.HTTPError as exc:
                errors.append(f"{url}: {type(exc).__name__}")
                continue
    raise HTTPException(404, detail={
        "status": "archive_date_unavailable",
        "date": day.isoformat(),
        "message": "Nenhuma das fontes de arquivo respondeu para esta data.",
        "attempts": errors,
    })

def parse_aggregator_page(html: str, requested_day: date):
    soup = BeautifulSoup(html, "html.parser")
    parsed = []
    current_bank = None
    animal_to_group = {
        "avestruz": 1, "aguia": 2, "burro": 3, "borboleta": 4,
        "cachorro": 5, "cabra": 6, "carneiro": 7, "camelo": 8,
        "cobra": 9, "coelho": 10, "cavalo": 11, "elefante": 12,
        "galo": 13, "gato": 14, "jacare": 15, "leao": 16,
        "macaco": 17, "porco": 18, "pavao": 19, "peru": 20,
        "touro": 21, "tigre": 22, "urso": 23, "veado": 24,
        "vaca": 25,
    }

    for node in soup.find_all(["h2", "h3", "h4", "table"]):
        if node.name in {"h2", "h3", "h4"}:
            heading = _clean(node.get_text(" ", strip=True))
            normalized_heading = _normalize_key(heading)
            current_bank = None
            for alias_norm, canonical in sorted(_ALIAS_LOOKUP.items(), key=lambda x: len(x[0]), reverse=True):
                if alias_norm and (normalized_heading == alias_norm or normalized_heading.startswith(alias_norm + " ")):
                    current_bank = canonical
                    break
            continue

        if node.name != "table" or not current_bank:
            continue
        rows = node.find_all("tr")
        if len(rows) < 2:
            continue
        header_idx = None
        draw_columns = {}
        for ridx, tr in enumerate(rows[:3]):
            cells = tr.find_all(["th", "td"])
            found = {}
            for idx, cell in enumerate(cells):
                label = _clean(cell.get_text(" ", strip=True))
                tm = re.search(r"(?<!\d)(\d{1,2}:\d{2})(?!\d)", label)
                if tm:
                    hh, mm = tm.group(1).split(":")
                    found[idx] = f"{int(hh):02d}:{mm}"
            if found:
                header_idx, draw_columns = ridx, found
                break
        if header_idx is None:
            continue

        for tr in rows[header_idx + 1:]:
            cells = tr.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            rank_text = _clean(cells[0].get_text(" ", strip=True))
            rank_match = re.search(r"^\s*([1-7])\s*(?:º|°|o)?\s*$", rank_text, re.I)
            if not rank_match:
                continue
            prize = int(rank_match.group(1))
            for col_idx, draw_time in draw_columns.items():
                if col_idx >= len(cells):
                    continue
                cell_text = _clean(cells[col_idx].get_text(" ", strip=True))
                m = re.search(r"(?<!\d)(\d{3,6})(?!\d)", cell_text)
                if not m:
                    continue
                raw_num = m.group(1)
                if len(raw_num) == 6:
                    number, group_from_digits = raw_num[:4], int(raw_num[4:])
                elif len(raw_num) in (3, 4):
                    number = raw_num.zfill(4 if prize <= 6 else 3)
                    group_from_digits = None
                else:
                    continue

                group = group_from_digits if (group_from_digits and 1 <= group_from_digits <= 25) else None
                animal_match = re.search(r"[·•\-]\s*([A-Za-zÀ-ÿ]+)", cell_text)
                if animal_match:
                    animal = _normalize_key(animal_match.group(1)).replace(" ", "")
                    animal_group = animal_to_group.get(animal)
                    if animal_group:
                        group = animal_group

                if not group or not 1 <= group <= 25:
                    # Se não veio bicho nem grupo na dezena, calcula o grupo pelas dezenas finais do milhar
                    dezena = int(number[-2:])
                    if dezena == 0:
                        group = 25
                    else:
                        group = (dezena - 1) // 4 + 1

                parsed.append({
                    "date": requested_day.isoformat(),
                    "lottery": current_bank,
                    "draw_time": draw_time,
                    "prize": prize,
                    "number": number,
                    "group": f"{group:02d}",
                    "source": "ojogodobiicho.com",
                    "validation": "aggregator_board_extracted",
                })

    unique = {}
    for row in parsed:
        key = (row["lottery"], row["draw_time"], row["prize"])
        old = unique.get(key)
        if old and (old["number"] != row["number"] or old["group"] != row["group"]):
            continue
        unique[key] = row
    return sorted(unique.values(), key=lambda r: (r["lottery"], r["draw_time"], r["prize"]))


DNP_NACIONAL_BASE = "https://deunopostenacional.com.br"
DNP_NACIONAL_HOSTS = {"deunopostenacional.com.br", "www.deunopostenacional.com.br"}
DNP_PAGES = {
    "Para Todos-SP": "/jogo-do-bicho-sao-paulo/",
    "LNS Nacional": "/loteria-nacional/",
}

def validate_dnp_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in DNP_NACIONAL_HOSTS:
        raise HTTPException(400, "Fonte Deu no Poste Nacional HTTPS não autorizada.")
    return url

async def fetch_dnp_html(url: str) -> str:
    validate_dnp_url(url)
    key = hashlib.sha256(url.encode()).hexdigest()
    cached = CACHE.get(key)
    if cached and time.time() - cached["at"] < CACHE_TTL:
        return cached["html"]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pt-BR,pt;q=0.9",
    }
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Falha ao consultar Deu no Poste Nacional: {type(exc).__name__}")
    CACHE[key] = {"at": time.time(), "html": response.text}
    return response.text

def parse_dnp_nacional_page(html: str, requested_day: date, lottery: str):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    cards = soup.select("div.dnp-qb-card")
    if not cards:
        cards = soup.select("section.dnp-qb-section")
    
    selected = []
    for card in cards:
        heading = card.find(["h3", "h4"])
        if not heading:
            continue
        title = _clean(heading.get_text(" ", strip=True))
        tm = re.search(r"\((\d{1,2})\s*h\s*(\d{1,2})\s*min", title, re.I)
        if not tm:
            tm = re.search(r"\b(\d{1,2})\s*:\s*(\d{2})\b", title)
        if not tm:
            continue
        draw_time = f"{int(tm.group(1)):02d}:{tm.group(2)}"
        table = card.find("table", class_=re.compile(r"resultado-tabela"))
        if not table:
            continue
        rows = table.find_all("tr")
        parsed_rows = []
        for tr in rows[1:]:
            cells = tr.find_all(["th", "td"])
            if len(cells) < 3:
                continue
            rankm = re.fullmatch(r"\s*(10|[1-9])\D*", _clean(cells[0].get_text(" ", strip=True)), re.I)
            numberm = re.search(r"(?<!\d)(\d{4})(?!\d)", _clean(cells[1].get_text(" ", strip=True)))
            groupm = re.search(r"\((\d{1,2})\)", _clean(cells[2].get_text(" ", strip=True)))
            if not rankm or not numberm or not groupm:
                continue
            prize = int(rankm.group(1))
            group = int(groupm.group(1))
            if not 1 <= prize <= 10 or not 1 <= group <= 25:
                continue
            parsed_rows.append({
                "date": requested_day.isoformat(), "lottery": lottery,
                "draw_time": draw_time, "prize": prize,
                "number": numberm.group(1), "group": f"{group:02d}",
                "source": "deunopostenacional.com.br",
            })
        if parsed_rows:
            is_full = len(parsed_rows) >= 8 or ("1" in title and "10" in title)
            selected.append((draw_time, is_full, parsed_rows))

    by_time = {}
    for draw_time, is_full, rows in selected:
        old = by_time.get(draw_time)
        if old is None or (is_full and not old[0]) or len(rows) > len(old[1]):
            by_time[draw_time] = (is_full, rows)
    for _, rows in by_time.values():
        out.extend(rows)

    unique = {}
    for row in out:
        key = (row["draw_time"], row["prize"])
        unique[key] = row
    return sorted(unique.values(), key=lambda r:(r["draw_time"], r["prize"]))


@app.get("/api/aggregated-results")
async def aggregated_results(
    draw_date: Optional[date] = Query(default=None),
    lottery: Optional[str] = Query(default=None, max_length=60),
):
    requested_day = draw_date or brazil_today()
    requested_key = _normalize_key(lottery or "")
    dnp_lottery = None
    if requested_key in {"para todos sp", "pt sp", "bicho sp", "sao paulo", "loteria paulista"}:
        dnp_lottery = "Para Todos-SP"
    elif requested_key in {"lns nacional", "nacional", "loteria nacional"}:
        dnp_lottery = "LNS Nacional"

    if dnp_lottery and requested_day == brazil_today():
        url = DNP_NACIONAL_BASE + DNP_PAGES[dnp_lottery]
        try:
            html = await fetch_dnp_html(url)
            rows = parse_dnp_nacional_page(html, requested_day, dnp_lottery)
            if rows:
                return {
                    "status": "ok", "date": requested_day.isoformat(),
                    "lottery": dnp_lottery, "source": url,
                    "count": len(rows), "results": rows,
                    "note": "Resultados extraídos da página atual do Deu no Poste Nacional."
                }
        except Exception:
            pass

    html, url = await fetch_aggregator_html(requested_day)
    rows = parse_aggregator_page(html, requested_day)
    source_host = urlparse(url).hostname or "fonte-externa"
    for row in rows:
        row["source"] = source_host
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
            "message": "Nenhum resultado foi extraído para esta banca e data."
        })
    return {
        "status": "ok",
        "date": requested_day.isoformat(),
        "source": url,
        "count": len(rows),
        "lotteries_found": sorted({r["lottery"] for r in rows}),
        "results": rows,
        "note": "Resultados extraídos via fonte agregadora."
    }


# Integração PT-RIO (Limite mantido do 1º ao 7º prêmio)
RIO_URL = "https://www.ojogodobicho.com/deu_no_poste.htm"
RIO_HOSTS = {"www.ojogodobicho.com", "ojogodobicho.com"}
RIO_ARCHIVE_URL = "https://www.ojogodobicho.com/resultado"

RIO_TIMES = {
    "PPT": "09:20", "PTM": "11:20", "PT": "14:30",
    "PTV": "16:30", "PTN": "18:20", "COR": "21:30",
    "FED": "11:30",
}

def rio_schedule_for_day(day: date):
    if day.weekday() == 6:
        return {"FED": "11:30", "PT": "14:30", "PTV": "16:30"}
    if day.weekday() == 2:
        return {"PPT": "09:30", "PTM": "11:30", "PT": "14:30",
                "PTV": "16:30", "FED": "20:00", "COR": "21:30"}
    schedule = dict(RIO_TIMES)
    if day.weekday() == 5:
        schedule["PTN"] = "19:30"
    return schedule

def parse_rio_page(html: str, requested_day: date):
    soup = BeautifulSoup(html, "html.parser")
    wanted = ["PPT", "PTM", "PT", "PTV", "PTN", "COR", "FED"]
    out = []
    schedule = rio_schedule_for_day(requested_day)
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_idx = None
        col_map = {}
        for i, tr in enumerate(rows[:5]):
            labels = [_clean(c.get_text(" ", strip=True)).upper()
                      for c in tr.find_all(["th", "td"])]
            found = {}
            for idx, label in enumerate(labels):
                for sigla in wanted:
                    if re.fullmatch(rf"{sigla}", label):
                        found[idx] = sigla
                        break
            if len(set(found.values())) >= 3 and "PT" in found.values():
                header_idx, col_map = i, found
                break
        if header_idx is None:
            continue

        for tr in rows[header_idx + 1:]:
            cells = tr.find_all(["th", "td"])
            if not cells:
                continue
            rank_text = _clean(cells[0].get_text(" ", strip=True))
            rankm = re.fullmatch(r"([1-7])(?:º|°)?", rank_text, re.I) # Limita do 1º ao 7º prêmio
            if not rankm:
                continue
            prize = int(rankm.group(1))
            for idx, sigla in col_map.items():
                if idx >= len(cells) or sigla not in schedule:
                    continue
                val = _clean(cells[idx].get_text(" ", strip=True))
                m = re.fullmatch(r"(\d{3,4})\s*[-–]\s*(\d{1,2})", val)
                if not m:
                    continue
                number, group = m.groups()
                if set(number) == {"0"}:
                    continue
                g = int(group)
                if not 1 <= g <= 25:
                    continue
                out.append({
                    "date": requested_day.isoformat(),
                    "lottery": "PT-RIO",
                    "draw_time": schedule[sigla],
                    "draw_code": sigla,
                    "prize": prize,
                    "number": number,
                    "group": f"{g:02d}",
                    "source": "ojogodobicho.com",
                })

    unique = {}
    for row in out:
        key = (row["draw_code"], row["prize"])
        unique[key] = row
    return sorted(unique.values(), key=lambda r: (r["draw_time"], r["prize"]))

async def fetch_rio_archive(day: date) -> str:
    url = f"{RIO_ARCHIVE_URL}/{day.year:04d}/{day.month:02d}/{day.day:02d}/"
    return await fetch_html(url)

def parse_rio_archive(html: str, requested_day: date):
    soup = BeautifulSoup(html, "html.parser")
    schedule = rio_schedule_for_day(requested_day)
    code_aliases = {
        "FEDERAL": "FED", "FED": "FED", "PPT": "PPT", "PTM": "PTM", "CORUJINHA": "COR", "CORUJA": "COR",
        "PT": "PT", "PTV": "PTV", "PTN": "PTN", "COR": "COR",
    }
    rows_out = []
    for wrap in soup.select("div.table-wrap"):
        table = wrap.find("table")
        if not table:
            continue
        wrap_text = _clean(wrap.get_text(" ", strip=True))
        label_match = re.match(r"\s*(FEDERAL|PPT|PTM|PTV|PTN|PT|CORUJINHA|CORUJA|COR)\b(?:\s*\((\d{1,2}:\d{2})\))?", wrap_text, re.I)
        if not label_match:
            continue
        raw_code = label_match.group(1).upper()
        draw_code = code_aliases.get(raw_code)
        if not draw_code:
            continue
        draw_time = label_match.group(2) or schedule.get(draw_code)
        if not draw_time:
            continue

        for tr in table.find_all("tr"):
            cells = [_clean(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if len(cells) < 4:
                continue
            rank_match = re.fullmatch(r"\s*([1-7])\D*", cells[0], re.I) # Limita do 1º ao 7º
            if not rank_match:
                continue
            prize = int(rank_match.group(1))
            milhar = re.fullmatch(r"\d{4}", cells[1])
            centena = re.fullmatch(r"\d{3}", cells[2])
            group_match = re.fullmatch(r"\d{1,2}", cells[3])
            if not (milhar and group_match):
                continue
            number = milhar.group(0)
            group = int(group_match.group(0))
            if not 1 <= group <= 25:
                continue
            rows_out.append({
                "date": requested_day.isoformat(),
                "lottery": "PT-RIO",
                "draw_time": draw_time,
                "draw_code": draw_code,
                "prize": prize,
                "number": number,
                "group": f"{group:02d}",
                "source": "ojogodobicho.com",
            })

    unique = {}
    for row in rows_out:
        key = (row["draw_code"], row["prize"])
        unique[key] = row
    return sorted(unique.values(), key=lambda r: (r["draw_time"], r["prize"]))

@app.get("/api/rio-results")
async def rio_results(draw_date: Optional[date] = Query(default=None)):
    requested_day = draw_date or brazil_today()
    if requested_day == brazil_today():
        source_url = RIO_URL
        html = await fetch_html(source_url)
        rows = parse_rio_page(html, requested_day)
    else:
        source_url = f"{RIO_ARCHIVE_URL}/{requested_day.year:04d}/{requested_day.month:02d}/{requested_day.day:02d}/"
        html = await fetch_rio_archive(requested_day)
        rows = parse_rio_archive(html, requested_day)
    
    if not rows:
        raise HTTPException(404, detail={
            "status": "rio_date_not_found",
            "date": requested_day.isoformat(),
            "source": source_url,
            "message": "Nenhum resultado encontrado para a data do Rio."
        })
    return {
        "status": "ok", "lottery": "PT-RIO", "date": requested_day.isoformat(),
        "source": source_url, "count": len(rows), "results": rows,
    }

@app.get("/health")
def health():
    return {"ok": True, "service": APP_NAME, "version": VERSION}
