# Mago & Oráculo — versão final integrada

Este repositório mantém o backend de resultados do projeto e adiciona o motor persistente do Oráculo.

## O que já está integrado

- PT-RIO, Para Todos-SP, LOOK Goiás e LNS Nacional.
- Banco PostgreSQL via `DATABASE_URL` (Supabase no Render).
- Previsão congelada: uma previsão já criada para um sorteio não é substituída depois que o resultado aparece.
- Sorte do Dia: um portfólio diário por loteria, sempre calculado com corte em 00:00 do dia e separado do Por Sorteio.
- Por Sorteio: uma previsão para cada horário. Para sorteios posteriores ao primeiro, o servidor exige o resultado do sorteio imediatamente anterior da mesma loteria antes de criar a previsão.
- Primeiro sorteio do dia: usa o último sorteio disponível do dia anterior.
- Nacional 02:00: pode usar o primeiro prêmio do LOOK 23:20 do dia anterior como sinal especial.
- Bicho do momento: usa os cinco prêmios do sorteio anterior, com peso maior para o 1º prêmio.
- Bicho do calendário: 1–25 seguem os grupos homônimos; dias 26–28 = Carneiro (7); 29–31 = Camelo (8).
- Palpitão normal: 20 dezenas.
- Super Palpitão: 100 milhares ou 50 centenas por ciclo diário, agregando as quatro loterias do Oráculo.
- Milhar, centena, dezena, grupo, duque, terno e passe usam carteiras independentes, com sementes e rankings próprios.
- Calibração walk-forward por loteria/modalidade.
- Ciclo automático a cada 10 minutos pelo GitHub Actions: sincroniza resultados, avalia previsões congeladas e recalibra o modelo. A geração pesada de carteiras não é feita em lote no ciclo para não saturar o Render Free; o palpite pedido pelo usuário é gerado sob demanda usando o último modelo aprendido.
- Interface final disponível pelo próprio Render em `/app`.

## Publicação

O serviço Render continua usando:

```text
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port $PORT
```

A variável `DATABASE_URL` deve apontar para o PostgreSQL persistente já criado no Supabase.

Depois do deploy, a interface pode ser aberta em:

```text
https://SEU-RENDER.onrender.com/app
```

No projeto atual do usuário, o frontend já está configurado para consultar:

```text
https://oraculo-backend-mor3.onrender.com
```

## Endpoints principais

```text
GET /api/oracle/status
GET /api/oracle/portfolio
GET /api/oracle/super
GET /api/oracle/results
GET /api/oracle/cycle
GET /api/oracle/learn-now
GET /api/oracle/prewarm
GET /api/oracle/backfill?days=14
GET /api/oracle/diagnose
```

## Por que o ciclo não gera dezenas de carteiras em segundo plano

O Render Free usa apenas 0,1 CPU e 512 MB e pode desligar o serviço após 15 minutos sem tráfego. O GitHub Actions chama o ciclo periodicamente para manter o modelo atualizado enquanto o serviço estiver disponível. Em cada ciclo, o Oráculo aprende com os resultados novos e guarda os modelos no PostgreSQL/Supabase. Quando alguém pede um palpite, o servidor usa esse modelo já aprendido e gera apenas a modalidade solicitada. Isso evita recalcular dezenas de carteiras em paralelo e deixa o app responsivo.

## Observação importante

O motor é um sistema estatístico com validação temporal. Ele não garante acertos futuros nem transforma resultados aleatórios em algo previsível. O objetivo do aprendizado é medir o que funcionou no histórico sem permitir vazamento do resultado que estava sendo previsto.


## V10 — pré-cálculo automático e correções

- `oracle_predictions.prediction` passa a ser `TEXT` (corrige HTTP 500 em Palpitão/Super).
- `/api/oracle/cycle` agora sincroniza, avalia, aprende e **precalcula** a Sorte do Dia + o próximo sorteio elegível de cada loteria.
- A previsão fica congelada no PostgreSQL antes do resultado e só é avaliada depois que o resultado chega.
- Sorte do Dia usa somente os 2 dias imediatamente anteriores.
- Super Palpitão não faz calibração pesada no clique; usa modelo salvo/baseline e é congelado no banco.
- O registro de teste explícito `PT-RIO / 2026-09-30 / 11:20 / milhar = 1980` é removido automaticamente.
- Resultados exibem `Bicho · Grupo XX`.
