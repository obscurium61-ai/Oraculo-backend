import os, re, time, hashlib
from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

APP_NAME = "Oráculo Results Bridge"
VERSION = "0.2.0"
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "600"))
SOURCE_URL = os.getenv("SOURCE_URL", "https://www.ojogodobicho.com/look/deu-no-poste.htm")
ALLOWED_HOSTS = {h.strip().lower() for h in os.getenv("ALLOWED_SOURCE_HOSTS", "www.ojogodobicho.com,ojogodobicho.com").split(",") if h.strip()}
CACHE = {}
LOOK_TIMES = {"07:20", "09:20", "11:20", "14:20", "16:20", "18:20", "21:20", "23:20"}

app = FastAPI(title=APP_NAME, version=VERSION)
origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "*").split(",") if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins if origins != ["*"] else ["*"], allow_credentials=False, allow_methods=["GET"], allow_headers=["*"])


def validate_source_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in ALLOWED_HOSTS:
        raise HTTPException(400, "A fonte precisa ser HTTPS e pertencer a um host autorizado.")
    return url


async def fetch_html(url: str) -> str:
    validate_source_url(url)
    key, now = hashlib.sha256(url.encode()).hexdigest(), time.time()
    cached = CACHE.get(key)
    if cached and now - cached["at"] < CACHE_TTL:
        return cached["html"]
    headers = {"User-Agent": "Mozilla/5.0 (compatible; OraculoResultsBridge/0.2; results parser)", "Accept": "text/html,application/xhtml+xml"}
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=headers) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Falha ao consultar a fonte: {type(exc).__name__}")
    CACHE[key] = {"at": now, "html": response.text}
    return response.text


def source_url_for(day: date) -> str:
    # The publisher provides a dedicated live page and a date-query archive page.
    if day == date.today():
        return "https://www.ojogodobicho.com/look/deu-no-poste.htm"
    return f"https://www.ojogodobicho.com/look/resultados-anteriores.htm?d={day.isoformat()}"


def parse_look_page(html: str, day: date):
    soup = BeautifulSoup(html, "html.parser")
    # Preserve line boundaries from individual text nodes; this works with the archive's
    # draw headings and prize rows, while refusing to guess a draw/time if absent.
    lines = [re.sub(r"\s+", " ", s).strip() for s in soup.stripped_strings]
    current_time = None
    results = []
    for line in lines:
        mtime = re.search(r"LOOK\s+(\d{2}:\d{2})", line, re.I)
        if mtime:
            candidate = mtime.group(1)
            current_time = candidate if candidate in LOOK_TIMES else None
            continue
        if not current_time:
            continue
        # Rows typically contain rank, four-digit/three-digit result, and group label.
        # Example text nodes: "1º", "1403", "Avestruz Grupo 01".
        rank_match = re.fullmatch(r"([1-7])\s*[º°.]?", line)
        if rank_match:
            continue
        # Some pages expose each row as a single text node; parse only explicit group tags.
        row = re.search(r"(?:^|\b)([1-7])\s*[º°.]?\s*(\d{3,4})\s*(?:[-–]\s*)?.{0,80}?Grupo\s*0?(\d{1,2})\b", line, re.I)
        if row:
            prize, number, group = int(row.group(1)), row.group(2), int(row.group(3))
            if 1 <= group <= 25:
                results.append({"date": day.isoformat(), "lottery": "LOOK Goiás", "draw_time": current_time, "prize": prize, "number": number.zfill(4) if prize <= 6 else number.zfill(3), "group": f"{group:02d}"})
    # Fallback for table-based archive: use time header columns and cells formatted "0000- 00".
    if not results:
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            header = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
            times = []
            for cell in header:
                mt = re.search(r"\b(\d{2}:\d{2})\b", cell)
                times.append(mt.group(1) if mt and mt.group(1) in LOOK_TIMES else None)
            if not any(times):
                continue
            for tr in rows[1:]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                if not cells:
                    continue
                rankm = re.search(r"([1-7])", cells[0])
                if not rankm:
                    continue
                prize = int(rankm.group(1))
                for idx, cell in enumerate(cells[1:]):
                    if idx >= len(times) or not times[idx]:
                        continue
                    val = re.search(r"(\d{3,4})\s*[-–]\s*0?(\d{1,2})", cell)
                    if val:
                        group = int(val.group(2))
                        if 1 <= group <= 25:
                            number = val.group(1)
                            results.append({"date": day.isoformat(), "lottery": "LOOK Goiás", "draw_time": times[idx], "prize": prize, "number": number.zfill(4) if prize <= 6 else number.zfill(3), "group": f"{group:02d}"})
    # De-duplicate in case both text and table representations were present.
    unique = {(r["draw_time"], r["prize"]): r for r in results}
    return sorted(unique.values(), key=lambda r: (r["draw_time"], r["prize"]))


@app.get("/")
def root():
    return {"service": APP_NAME, "status": "online", "version": VERSION, "supported_source": "LOOK Goiás (ojogodobicho.com)", "supported_times": sorted(LOOK_TIMES), "note": "Parser inicial de uma fonte. Confirme resultados com a fonte original; não há garantia de palpites ou ganhos."}


@app.get("/health")
def health():
    return {"ok": True, "service": APP_NAME, "version": VERSION}


@app.get("/api/results")
async def results(
    lottery: str = Query(default="LOOK Goiás", min_length=1, max_length=60),
    draw_date: date = Query(default_factory=date.today),
    draw_time: Optional[str] = Query(default=None, pattern=r"^\d{2}:\d{2}$"),
):
    normalized = lottery.strip().lower()
    if normalized not in {"look goiás", "look goias", "look"}:
        raise HTTPException(400, detail={"error": "lottery_not_supported", "supported": ["LOOK Goiás"]})
    if draw_time and draw_time not in LOOK_TIMES:
        raise HTTPException(400, detail={"error": "draw_time_not_supported", "supported_times": sorted(LOOK_TIMES)})
    url = validate_source_url(source_url_for(draw_date))
    html = await fetch_html(url)
    parsed = parse_look_page(html, draw_date)
    if draw_time:
        parsed = [r for r in parsed if r["draw_time"] == draw_time]
    if not parsed:
        raise HTTPException(502, detail={"status": "no_validated_rows", "lottery": "LOOK Goiás", "date": draw_date.isoformat(), "source": url, "message": "A página foi consultada, mas o parser não encontrou linhas no formato esperado. Não foram inventados resultados."})
    return {"status": "ok", "lottery": "LOOK Goiás", "date": draw_date.isoformat(), "source": url, "fetched_at": datetime.now(timezone.utc).isoformat(), "count": len(parsed), "results": parsed, "note": "Confira os dados na fonte original. A coleta pode depender do formato e da disponibilidade do site."}
