---
name: tool-bridge
description: Documentação técnica da ponte HTTP local entre o Qoder e as ações do JARVIS. Não é carregada no prompt ao vivo.
---

# Tool Bridge

- `core/tool_bridge.py` sobe FastAPI em `127.0.0.1` (porta configurada, fallback livre) com token Bearer por processo.
- Endpoints: `/tools`, `/call`, `/memory/read`, `/memory/search`, `/memory/note`, `/voice/list`, `/voice/authorize`, `/restart/assistant`, `/restart/pc`.
- Toda chamada de ação roda em worker thread; o loop de áudio/HUD nunca bloqueia.
- Ações de risco passam pela confirmação do dono (`core/actiongate.py`) — voz dele ou senha digitada verificada, os dois fatores plenos da decisão de 06/11/2026; sem janela aberta por um desses fatores, a ponte devolve `confirmation_required`.
- Auditoria JSONL em `logs/tool_bridge.log`, com redação de chaves sensíveis.
- Execução pesada não passa por aqui: o Qoder usa `Agent` em segundo plano.

## Testes

- `pytest tests/test_tool_bridge.py tests/test_action_consent_voice.py`
