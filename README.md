# Oráculo Results Bridge — v0.2

Backend FastAPI para consultar resultados publicados pela página da LOOK Goiás no ojogodobicho.com.

## Rotas
- `GET /health` — estado do serviço.
- `GET /api/results?lottery=LOOK%20Goi%C3%A1s&draw_date=2026-09-25` — resultados do dia.
- `GET /api/results?lottery=LOOK%20Goi%C3%A1s&draw_date=2026-09-25&draw_time=07:20` — uma extração.

O parser tenta ler os cabeçalhos de horário e as linhas de prêmio, e retorna erro se não encontrar registros reconhecíveis. Não inventa resultados. A primeira versão suporta somente LOOK Goiás; outras bancas/loterias precisam de adaptadores específicos e validação própria.

## Render
Build: `pip install -r requirements.txt`
Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`

Confirme sempre os resultados no site de origem. A estatística histórica não prevê nem garante resultados futuros.

## V2
See README_V2.md for the persistent learning layer.
