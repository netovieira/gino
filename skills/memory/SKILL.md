---
name: memory
description: Hierarquia de memória em níveis. Use para decidir quando responder de cabeça e quando buscar nos arquivos.
---

# Memória em níveis

- **Nível 0 — sessão viva**: o histórico desta conversa. Use para o que acabou de ser dito; não precisa de ferramenta.
- **Nível 1 — últimas horas**: bloco quente do índice/memória. Use para o fio recente.
- **Nível 2 — fio atual**: tópico atual, threads abertas e notas. Use para saber o que ficou pendente.
- **Nível 3 — horas, dias e tópicos**: use `memory_search` e depois `memory_read` no arquivo certo. Um "deixa eu lembrar" antes é natural.
- **Nível 4 — longo prazo**: memória permanente (`recall_memory`/arquivo de longo prazo), só quando os níveis anteriores não bastarem.
- **Nível sono**: o bloco `[SONO — memória compactada]` é o dia anterior resumido pelo Qoder; use para lembrar ontem, mas se precisar do detalhe exato abra o arquivo daquele dia.
- **Nível 5 — não existe**: se não estiver registrado, diga que não lembra. Nunca reconstrua um detalhe por dedução.

Regras:
- Antes de responder sobre algo antigo, busque. Não invente nomes, datas, arquivos ou decisões.
- Grave decisões, preferências e threads abertas com `memory_note`, sem narrar a gravação.
- Quando o dono disser "encerra esse assunto" ou "muda de assunto", trate como fechamento do tópico atual.
- Um resumo não substitui o registro exato: detalhes vivem em `hours/`, `days/` e `topics/`.
- **Teia de índices**: use `memory_index action=query` para cruzar assunto, hora, persona e projeto ("o que a Fulana fez no projeto X?", "o que aconteceu no assunto Y?"). Use `action=tag` ao começar um projeto/assunto para os próximos turnos herdarem o foco, e `action=open` para abrir a teia no Obsidian. `action=link` liga dois nós quando a relação não for óbvia.
- Ao responder "quem cuidou disso" ou "quando foi", consulte a teia antes de inventar; se não houver registro, diga que não lembra.
