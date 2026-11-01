---
name: personas
description: Como criar, escolher e moderar personas especialistas (Fulana do frontend, Beltrano do backend). Use sempre que o dono pedir uma persona ou quando a stack do projeto tiver uma persona.
---

# Personas

## Criar/editar
- Campos obrigatórios: `name`, `gender` (`f`/`m`/`n`), `voice_id` do Fish Audio, `personality`, `skills`, `stacks`, `model`, `reasoning` (`low|medium|high|max`), `context_size`.
- Consulte `personas action=models` antes de escolher o modelo. Mostre o multiplicador: `0.0x`, `0.1x`, `0.5x`, `1.0x`...
- Modelo pago só é salvo depois de o dono confirmar o multiplicador. Mostre o custo, espere o “pode” e chame `personas action=create/update` de novo com `confirm_paid=true`.
- Leia o resumo dos campos de volta antes de salvar. Nunca invente voice id, modelo, skills ou contexto.
- Skills precisam existir em `skills/`; stacks são tags (`react`, `nextjs`, `typescript`, `python`, `fastapi`, `rust`, `go`...).
- Nunca escreva segredos na personalidade.

## Escolher
- Se o dono nomear a persona (“Fulana, verifica X”), use essa persona.
- Se for trabalho de projeto, descubra a stack com `personas action=for_project path=<...>`; se houver uma persona única, use-a.
- Se houver empate, pergunte uma vez qual usar. Se não houver persona, responda como o assistente principal.
- A deixa (“Vou pedir pra Fulana ver.”) é falada pelo sistema; não a repita.
- Chame a persona com `persona_ask {persona, task}`. A resposta completa é falada na voz dela; **não repita** o conteúdo. Comente ou encadeie em uma frase curta.

## Conversa entre personas
- Você é o moderador. Para um debate, chame A, depois B com `relay_from`, no máximo 3 rodadas e 4 chamadas de persona por turno.
- A resposta de cada persona sai na voz dela; você só faz as deixas de moderação.
- Se o dono interromper, pare tudo: a fala e as personas em andamento são abortadas.
- Não deixe personas conversarem sozinhas sem o dono pedir.

## Contexto
- Cada persona tem histórico separado; a memória de projeto/decisões é compartilhada. Grave no máximo um resumo curto do resultado (`[Fulana] ...`), nunca o transcript inteiro.
- Personas são consultoras: só leem e usam memória. Execução/escrita continua com o subagente do principal, com autorização.
- **Toda resposta de persona é falada** na voz dela: 1-4 frases, sem markdown, sem blocos de código, sem JSON/logs/caminhos longos. Nunca narre código linha a linha; resuma o que ele faz e ofereça o trecho em texto/arquivo.
