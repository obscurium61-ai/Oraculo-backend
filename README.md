# Mago & Oráculo — versão estatística stateless

Esta versão separa completamente o gerador estatístico de qualquer banco de aprendizado.

## Arquitetura
- `main.py`: ponte FastAPI que consulta as fontes externas e entrega resultados ao HTML.
- `frontend/Mago_Oraculo_FINAL.html`: interface + motor estatístico executado no navegador.
- Não há Supabase, SQLAlchemy, psycopg, modelos persistidos ou tarefas de aprendizado.
- O navegador congela localmente os palpites já gerados para impedir recálculo retroativo no mesmo dispositivo.

## Estatísticas do motor
Frequência, recência, atraso, distância entre aparições, posição dos dígitos, pares/trincas de dígitos, transições entre sorteios, transições de grupo, coocorrência de grupos, histórico dos 1º–5º prêmios, bicho do momento, bicho do dia, Viradinha, dia da semana e diversificação.

A pontuação usa um pequeno ensemble determinístico com maior peso para o 1º prêmio e cobertura do 1º ao 5º prêmio, sem alegar garantia de acerto.

## Viradinha
O dia é invertido de 01 a 31.
- Virada 01–25: o número virado é interpretado como número do grupo.
- Virada acima de 25: o número é procurado nas faixas de dezenas dos bichos.

Exemplos: 01→10 Coelho/G10; 02→20 Peru/G20; 03→30 Camelo/G8; 26→62 Leão/G16; 30→03 Burro/G3; 31→13 Galo/G13.

## Modos
- Sorte do Dia: usa somente os dois dias anteriores e congela localmente.
- Por Sorteio: usa apenas informação anterior ao sorteio alvo. O primeiro horário usa o último sorteio do dia anterior; os seguintes exigem o sorteio imediatamente anterior da mesma loteria.
- Super Palpitão: agrega as quatro loterias selecionadas e usa apenas dias anteriores.

## Execução
`uvicorn main:app --host 0.0.0.0 --port 8000`
