import os, re, time, hashlib
from datetime import date, datetime
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

APP_NAME = "Oráculo Results Bridge"
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "600"))
SOURCE_URL = os.getenv("SOURCE_URL", "https://www.ojogodobicho.com/deu_no_poste.html")
ALLOWED_HOSTS = {
    host.strip().lower()
    for host in os.getenv("ALLOWED_SOURCE_HOSTS", "www.ojogodobicho.com,ojogodobicho.com").split(",")
    if host.strip()
}
CACHE = {}

app = FastAPI(title=APP_NAME, version="0.1.0")

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
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(400, "A fonte precisa usar HTTPS.")
    if parsed.hostname.lower() not in ALLOWED_HOSTS:
        raise HTTPException(400, "Host não autorizado. Configure ALLOWED_SOURCE_HOSTS.")
    return url

async def fetch_html(url: str) -> str:
    validate_source_url(url)
    key = hashlib.sha256(url.encode()).hexdigest()
    now = time.time()
    cached = CACHE.get(key)
    if cached and now - cached["at"] < CACHE_TTL:
        return cached["html"]
    headers = {
        "User-Agent": "OraculoResultsBridge/0.1 (personal research app)",
        "Accept": "text/html,application/xhtml+xml"
    }
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Não foi possível consultar a fonte configurada: {type(exc).__name__}")
    html = response.text
    CACHE[key] = {"at": now, "html": html}
    return html

def extract_tables(html: str):
    soup = BeautifulSoup(html, "html.parser")
    tables = []
    for ti, table in enumerate(soup.find_all("table")):
        rows = []
        for tr in table.find_all("tr"):
            cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        if rows:
            tables.append({"table_index": ti, "rows": rows})
    return tables

@app.get("/")
def root():
    return {
        "service": APP_NAME,
        "status": "online",
        "configured_source": SOURCE_URL,
        "note": "A extração genérica de tabelas não identifica por si só loteria/horário. Use adaptadores validados antes de tratar registros como resultados oficiais."
    }

@app.get("/health")
def health():
    return {"ok": True, "service": APP_NAME, "version": "0.1.0"}

@app.get("/api/source/tables")
async def source_tables(url: Optional[str] = Query(default=None, description="URL HTTPS da fonte allowlisted")):
    target = validate_source_url(url or SOURCE_URL)
    html = await fetch_html(target)
    tables = extract_tables(html)
    return {
        "source": target,
        "fetched_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "table_count": len(tables),
        "tables": tables,
        "warning": "Dados brutos de tabelas. Não são resultados validados ou classificados por extração."
    }

@app.get("/api/results")
async def results(
    lottery: str = Query(..., min_length=1, max_length=60),
    draw_time: str = Query(..., pattern=r"^\d{2}:\d{2}$"),
    draw_date: date = Query(default_factory=date.today),
):
    # Intentionally fail closed until a source-specific parser is configured and verified.
    # This prevents accidentally assigning one extraction's result to another lottery/time.
    raise HTTPException(
        501,
        detail={
            "status": "adapter_not_configured",
            "lottery": lottery,
            "draw_time": draw_time,
            "draw_date": draw_date.isoformat(),
            "message": "Este endpoint só deve retornar resultados após configurar e validar um adaptador específico para a fonte, loteria e horário. Consulte /api/source/tables para inspecionar tabelas brutas."
        }
    )
