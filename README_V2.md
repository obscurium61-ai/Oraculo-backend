# Oráculo Results Bridge — V2 Learning Backend

Esta versão preserva os parsers e rotas existentes e acrescenta uma camada persistente para o Oráculo:

- histórico de resultados por loteria, data, horário e prêmio;
- previsões congeladas por alvo (lottery/data/horário/modalidade);
- avaliação da previsão apenas depois que o resultado existe;
- calibração walk-forward (sem usar o resultado futuro ao formar a previsão histórica);
- modelos separados por loteria/modalidade;
- endpoints para sincronização, backfill, calibração, previsão e avaliação;
- workflow opcional do GitHub Actions para sincronizar e recalibrar automaticamente.

## 1. Banco persistente recomendado

No Render Free, o filesystem local não deve ser usado como banco permanente. Configure `DATABASE_URL` apontando para um PostgreSQL persistente (por exemplo, o projeto Supabase do usuário).

O serviço cria as tabelas automaticamente na primeira inicialização.

Sem `DATABASE_URL`, o projeto cai para SQLite local (`oraculo_local.sqlite3`) apenas para desenvolvimento/testes.

## 2. Teste local

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Depois:

```text
GET /api/oracle/status
GET /api/oracle/backfill?days=3
GET /api/oracle/calibrate?lottery=PT-RIO&modality=milhar
GET /api/oracle/predict?lottery=PT-RIO&draw_date=2026-09-30&draw_time=09:20&modality=milhar
GET /api/oracle/evaluate-pending
```

## 3. GitHub Actions

Crie no repositório o secret `ORACULO_API_URL` com a URL pública do Render, por exemplo:

```text
https://SEU-SERVICO.onrender.com
```

O workflow `oraculo-learning.yml` roda a cada 30 minutos, sincroniza resultados, avalia previsões congeladas e recalibra os quatro modelos centrais.

## 4. Integração no HTML

O HTML pode trocar a geração local do Oráculo por chamadas a:

```text
GET /api/oracle/predict?lottery=...&draw_date=...&draw_time=...&modality=...
```

O backend devolve `prediction`, `candidates`, `model_name`, `cutoff_iso` e contexto básico. O alvo permanece congelado para evitar que um resultado publicado depois altere retroativamente a previsão.

## Importante

A calibração é uma avaliação histórica, não uma garantia de acerto. O motor usa walk-forward: cada caso histórico recebe somente dados anteriores ao horário do alvo.
