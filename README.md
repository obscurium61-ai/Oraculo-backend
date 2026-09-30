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
- Ciclo automático a cada 10 minutos pelo GitHub Actions: sincroniza, avalia previsões disponíveis, registra aprendizado, recalibra e pré-aquece o próximo sorteio elegível.
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

## Observação importante

O motor é um sistema estatístico com validação temporal. Ele não garante acertos futuros nem transforma resultados aleatórios em algo previsível. O objetivo do aprendizado é medir o que funcionou no histórico sem permitir vazamento do resultado que estava sendo previsto.
