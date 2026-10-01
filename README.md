# Mago & Oráculo — V13 Definitiva

- Motor estatístico local no navegador; sem Supabase, sem aprendizado persistente e sem tarefas em segundo plano.
- O servidor Render somente consulta e normaliza resultados externos.
- Sorte do Dia usa somente histórico anterior ao dia e fica congelada durante o dia; reinicia automaticamente às 00:00.
- Super Palpitão gera 100 milhares ou 50 centenas e reinicia automaticamente às 00:00.
- Por Sorteio: um único portfólio independente por loteria + horário; libera somente depois que o sorteio imediatamente anterior tiver resultado.
- Para o primeiro sorteio do dia, usa o último sorteio disponível do dia anterior.
- Modalidades são calculadas como carteira independente no mesmo contexto: Milhar, Centena e Dezena não são subpartes umas das outras.
- Repetições exatas dos números do sorteio imediatamente anterior são evitadas na Milhar; combinações com os mesmos sufixos também recebem bloqueios de diversificação entre modalidades.
- Foco estatístico em 1º prêmio e cobertura do 1º ao 5º, usando frequência, recência, atraso, intervalos, dígitos, transições, grupos, calendário, Viradinha, bicho do momento e coocorrência.
- Viradinha dos dias 01–31 segue a regra fechada: inverter o dia; 01–25 = grupo direto; acima de 25 = localizar o número dentro das faixas dos bichos.
- Snapshot namespace V13 impede reutilização de palpites salvos pelas versões anteriores.
