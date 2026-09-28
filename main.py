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
VERSION = "0.7.0"
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
AGGREGATOR_BASES = [
    # Archive provider referenced in the app's research notes; keep a fallback
    # to the previous provider so one missing date route does not immediately fail.
    "https://resultadosorte.com/arquivo",
    "https://ojogodobiicho.com/resultados-anteriores",
]
AGGREGATOR_HOSTS = {"resultadosorte.com", "www.resultadosorte.com", "ojogodobiicho.com", "www.ojogodobiicho.com"}

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
        "message": "Nenhuma das fontes de arquivo respondeu com uma página para esta data. Isso não confirma ausência de sorteio.",
        "attempts": errors,
    })

def parse_aggregator_page(html: str, requested_day: date):
    """Parse the date-specific board from Ojogodobiicho.

    The source's board format shows each bank as a heading and each draw as a
    table: prize ranks are rows, draw times are columns, and cells commonly
    contain a six-digit concatenation (four-digit milhar + two-digit group)
    followed by the animal name. We only accept that explicit pattern.
    """
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
            # Prefer the longest matching alias to avoid matching a short name
            # inside another bank label.
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
                # Board cells in the aggregator expose milhar+group as six digits,
                # e.g. 783108 · Camelo. Require a known animal label too.
                m = re.search(r"(?<!\d)(\d{6})(?!\d)", cell_text)
                if not m:
                    continue
                six = m.group(1)
                number, group_from_digits = six[:4], int(six[4:])
                animal_match = re.search(r"[·•\-]\s*([A-Za-zÀ-ÿ]+)", cell_text)
                group = group_from_digits if 1 <= group_from_digits <= 25 else None
                if animal_match:
                    animal = _normalize_key(animal_match.group(1)).replace(" ", "")
                    animal_group = animal_to_group.get(animal)
                    if animal_group and group and animal_group != group:
                        # Source's animal and encoded group disagree: do not guess.
                        continue
                    if animal_group:
                        group = animal_group
                if not group or not 1 <= group <= 25:
                    continue
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
            raise HTTPException(502, detail={
                "status": "aggregator_conflict", "date": requested_day.isoformat(),
                "lottery": row["lottery"], "draw_time": row["draw_time"],
                "prize": row["prize"],
                "message": "A fonte retornou valores conflitantes; os dados foram bloqueados."
            })
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


# Integração isolada do PT-RIO pela página solicitada. Não altera o parser/rota LOOK.
RIO_URL = "https://www.ojogodobicho.com/deu_no_poste.htm"
RIO_HOSTS = {"www.ojogodobicho.com", "ojogodobicho.com", "ojogodobiicho.com"}
RIO_TIMES = {"PPT":"09:20", "PTM":"11:20", "PT":"14:20", "PTV":"16:20", "PTN":"18:20", "COR":"21:20"}

def parse_rio_page(html: str, requested_day: date):
    soup = BeautifulSoup(html, "html.parser")
    # O quadro Rio é reconhecido pelo cabeçalho com as seis siglas, em ordem.
    wanted = ["PPT", "PTM", "PT", "PTV", "PTN", "COR"]
    out = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2: continue
        header_idx = None
        for i, tr in enumerate(rows[:4]):
            labels = [_clean(c.get_text(" ", strip=True)).upper() for c in tr.find_all(["th", "td"])]
            joined = " ".join(labels)
            if all(re.search(rf"\b{x}\b", joined) for x in wanted):
                header_idx = i; break
        if header_idx is None: continue
        header_cells = rows[header_idx].find_all(["th", "td"])
        col_map = {}
        for idx, cell in enumerate(header_cells):
            label = _clean(cell.get_text(" ", strip=True)).upper()
            for sigla in wanted:
                if re.search(rf"\b{sigla}\b", label): col_map[idx] = sigla
        if len(col_map) < 6: continue
        for tr in rows[header_idx+1:]:
            cells = tr.find_all(["th", "td"])
            if not cells: continue
            rankm = re.fullmatch(r"\s*([1-7])\s*(?:º|°)?\s*", _clean(cells[0].get_text(" ", strip=True)))
            if not rankm: continue
            prize = int(rankm.group(1))
            for idx, sigla in col_map.items():
                if idx >= len(cells): continue
                val = _clean(cells[idx].get_text(" ", strip=True))
                m = re.fullmatch(r"(\d{1,4})\s*[-–]\s*(\d{1,2})", val)
                if not m: continue
                num, group = m.groups(); g=int(group)
                if not 1 <= g <= 25: continue
                out.append({"date":requested_day.isoformat(),"lottery":"PT-RIO","draw_time":RIO_TIMES[sigla],"draw_code":sigla,"prize":prize,"number":num.zfill(4 if prize <= 6 else 3),"group":f"{g:02d}","source":"ojogodobicho.com"})
        if out: break
    # bloqueia retorno vazio; não inventa dados nem reaproveita outra data
    unique={(r['draw_time'],r['prize']):r for r in out}
    return sorted(unique.values(), key=lambda r:(r['draw_time'],r['prize']))

# Arquivo diário separado para o Rio. A fonte de arquivo publica os boards fechados por data.
RIO_ARCHIVE_URL = "https://ojogodobiicho.com/resultados-anteriores"
RIO_ARCHIVE_TIMES = {
    "PPT": "09:20", "PTM": "11:20", "PT": "14:20",
    "PTV": "16:20", "PTN": "18:20", "COR": "21:20",
    "FED": "20:00"
}

async def fetch_rio_archive(day: date) -> str:
    # O arquivo PT-RIO usa URL amigável por data, não o parâmetro ?date=.
    # Exemplo documentado: /resultados-anteriores/2026/07/21
    url = f"{RIO_ARCHIVE_URL}/{day.year:04d}/{day.month:02d}/{day.day:02d}"
    parsed_host = urlparse(url).hostname or ""
    if parsed_host not in RIO_HOSTS:
        raise HTTPException(400, "Fonte histórica do Rio não autorizada.")
    return await fetch_html(url)

def parse_rio_archive(html: str, requested_day: date):
    soup = BeautifulSoup(html, "html.parser")
    text = _clean(soup.get_text(" ", strip=True))
    # Se a página explicita uma data única diferente, falha fechado.
    date_candidates = set()
    for dd, mm, yyyy in re.findall(r"\b(\d{2})/(\d{2})/(\d{4})\b", text):
        try: date_candidates.add(date(int(yyyy), int(mm), int(dd)))
        except ValueError: pass
    if len(date_candidates) == 1 and requested_day not in date_candidates:
        return []

    out = []
    # A página arquivada organiza o bloco da banca PT-RIO e cada sorteio em uma linha.
    # Aceita células contendo milhar e grupo separados por hífen ou ponto médio.
    for table in soup.find_all("table"):
        preceding = table.find_previous(['h1','h2','h3','h4','caption'])
        context = _clean((preceding.get_text(" ", strip=True) if preceding else "") + " " + table.get_text(" ", strip=True)).upper()
        if "PT-RIO" not in context and "RIO DE JANEIRO" not in context:
            continue
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if len(cells) < 8: continue
            rowtext = [_clean(c.get_text(" ", strip=True)) for c in cells]
            # Layout: horário/sigla seguido dos sete prêmios.
            first = rowtext[0].upper().replace(" ", "")
            code = next((k for k in RIO_ARCHIVE_TIMES if first.startswith(k)), None)
            if not code:
                code = next((k for k in RIO_ARCHIVE_TIMES if k in first), None)
            if not code: continue
            for prize, val in enumerate(rowtext[1:8], 1):
                m = re.search(r"(\d{3,6})\s*(?:[-–·|/]\s*)?(\d{1,2})?", val)
                if not m: continue
                raw, group = m.groups()
                # Algumas tabelas juntam milhar e grupo (6 dígitos); outras usam milhar-grupo.
                if len(raw) == 6 and not group:
                    number, group = raw[:4], raw[4:]
                else:
                    number = raw[-4:].zfill(4)
                if not group:
                    group = str(((int(number[-2:]) - 1) // 4) + 1)
                g = int(group)
                if not 1 <= g <= 25: continue
                out.append({"date":requested_day.isoformat(),"lottery":"PT-RIO","draw_time":RIO_ARCHIVE_TIMES[code],"draw_code":code,"prize":prize,"number":number,"group":f"{g:02d}","source":RIO_ARCHIVE_URL})
    unique={(r["draw_code"],r["prize"]):r for r in out}
    return sorted(unique.values(), key=lambda r:(r["draw_time"],r["prize"]))

@app.get("/api/rio-results")
async def rio_results(draw_date: Optional[date] = Query(default=None)):
    requested_day = draw_date or brazil_today()
    if requested_day == brazil_today():
        source_url = RIO_URL
        html = await fetch_html(source_url)
        rows = parse_rio_page(html, requested_day)
        now_local = datetime.now(BRAZIL_TZ)
        rows = [r for r in rows if datetime.combine(requested_day, datetime.strptime(r["draw_time"], "%H:%M").time(), tzinfo=BRAZIL_TZ) <= now_local]
    else:
        source_url = f"{RIO_ARCHIVE_URL}/{requested_day.year:04d}/{requested_day.month:02d}/{requested_day.day:02d}"
        html = await fetch_rio_archive(requested_day)
        rows = parse_rio_archive(html, requested_day)
    if not rows:
        raise HTTPException(404, detail={"status":"rio_date_not_found_or_unparsed","date":requested_day.isoformat(),"source":source_url,"message":"A fonte histórica não retornou um quadro PT-RIO reconhecível para esta data. Nenhum resultado de outra data foi usado."})
    return {"status":"ok","lottery":"PT-RIO","date":requested_day.isoformat(),"source":source_url,"count":len(rows),"results":rows,"note":"Resultados extraídos da fonte indicada para a data solicitada; confira a fonte original."}

@app.get("/api/sources")
def sources():
    return {
        "version": VERSION,
        "existing": ["LOOK Goiás via ojogodobicho.com", "PT-RIO via deu_no_poste.htm (integração separada)"],
        "experimental": ["Arquivo diário multi-bancas com fallback entre resultadosorte.com e ojogodobiicho.com; cobertura depende dos nomes e formatos publicados pela fonte"],
        "requested_lotteries": list(LOTTERY_ALIASES.keys()),
        "note": "A presença de um nome na lista não confirma que a fonte publica resultados para ele."
    }

@app.get("/")
def root():
    return {
        "service": APP_NAME,
        "status": "online",
        "version": VERSION,
        "supported_source": "LOOK Goiás via fonte original; outras bancas tentam arquivo diário multi-fonte e só retornam registros extraídos explicitamente",
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
