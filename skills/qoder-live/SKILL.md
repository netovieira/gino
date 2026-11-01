---
name: qoder-live
description: Documentação técnica do sidecar Qoder Live (sessão única, protocolo JSONL, retomada). Não é carregada no prompt ao vivo.
---

# Qoder Live

## Contrato

- Processo Node em `qoder_live/index.mjs`, filho do JARVIS, protocolo JSONL por stdin/stdout; logs em stderr.
- Entrada: `{"type":"ask","id","text","speaker?","priority?":"now|next|later"}`, `{"type":"interrupt"}`, `{"type":"close"}`.
- Saída: `ready`, `session`, `text`, `tool`, `task`, `done`, `error`, `closed`.
- Uma única `query({prompt: channel, options})` mantém a conversa inteira viva. `ask` não espera a resposta.
- `persistSession: true` + `resume: JARVIS_RESUME_SESSION` continuam a mesma sessão após reiniciar o sidecar/app.
- Execução pesada: `Agent` com `run_in_background=true`, `subagent_type="executor"`, modelo `DeepSeek-Flash`. A conversa não espera.
- O turno ao vivo só roda leitura rápida (Read/Glob/Grep/Bash readonly) e ferramentas MCP do JARVIS. Edição/comando longo é negado fora de um subagente.
- Skills de política ficam em `skills/{conversation,execution,actions,memory,voice}/SKILL.md` e são lidas no boot.

## Testes

- `node --check qoder_live/index.mjs`
- `node qoder_live/index.mjs --selftest`
- `pytest tests/test_qoder_live.py`
