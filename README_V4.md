# Oráculo Backend V4 — Learning Stable

Esta versão mantém as rotas existentes de resultados e acrescenta um ciclo de aprendizado mais robusto.

## O que mudou

- PostgreSQL continua sendo o banco de produção via `DATABASE_URL`.
- Previsões são **imutáveis** por `loteria + data + horário + modalidade`.
- `/api/oracle/predict` nunca substitui uma previsão congelada.
- `/api/oracle/learn-now` sincroniza os resultados do dia antes de avaliar previsões.
- A rotina de aprendizado não depende exclusivamente de `evaluated=false`; ela inspeciona as previsões recentes e repara registros inconsistentes.
- Depois de avaliar uma previsão nova, recalibra a modalidade correspondente.
- `/api/oracle/diagnose` mostra contagem do banco, previsões recentes e resultados por loteria sem expor senha.
- Horários do PT-RIO continuam normalizados para o calendário do aplicativo: 09:20, 11:20, 14:20, 16:20, 18:20 e 21:20.

## Fluxo recomendado depois do deploy

1. Confira `/api/oracle/status`.
2. Execute `/api/oracle/diagnose`.
3. Para um ciclo único de sincronização + avaliação + recalibração, execute `/api/oracle/learn-now`.

Não é necessário limpar o banco do Supabase. O V4 reutiliza as tabelas existentes.
