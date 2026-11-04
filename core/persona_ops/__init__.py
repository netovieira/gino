"""Autonomia, memória, entrega e medição de personas — sem segundo orquestrador.

O orquestrador da casa continua único: dono do relógio, do estado e do
alto-falante. Estes módulos são dados e decisões que ele consulta:

* :mod:`core.persona_ops.autonomy` — tabela declarativa em
  ``config/personas_autonomy.json`` (``RulesTable`` + override do dono).
* :mod:`core.persona_ops.session` — partição de memória em
  ``memory/personas/<slug>/`` e orçamento/prazo via ``LifecycleRegistry``.
* :mod:`core.persona_ops.delivery` — arbítrio dono/Gino e fila única de fala.
* :mod:`core.persona_ops.relay` — recado do dono para a persona sem a boca do
  Gino.
* :mod:`core.persona_ops.measure` — comparação de latência/custo antes e
  depois da paralelização.
* :mod:`core.persona_ops.wiring` — montagem e mapa dos pontos de ligação.

Importar este pacote não muda o assistente vivo: a ligação é chamada pelo
``main.py`` depois do reinício.
"""
from __future__ import annotations

from .autonomy import (AutonomyError, AutonomyTable, DEFAULT_AUTONOMY_PATH,
                       PersonaGrant, ToolDecision, load_autonomy, tool_key)
from .delivery import (Addressee, AuditLog, DeliveryDecision, SpeakerArbiter,
                       SpeechTicket, decide_delivery, parse_addressee)
from .measure import (MeasureError, TurnSample, compare, demo_samples,
                      parse_jsonl, summarize)
from .relay import (OwnerRelay, PersonaAddress, RelayPlan, RelayResult,
                    classify_owner_relay, find_addressed_persona)
from .session import (BudgetState, MemoryHit, PersonaMemory, PersonaSession,
                      partition_of, safe_slug)
from .wiring import ATTACHMENT_POINTS, PersonaOps, build

__all__ = [
    "ATTACHMENT_POINTS",
    "Addressee",
    "AuditLog",
    "AutonomyError",
    "AutonomyTable",
    "BudgetState",
    "DEFAULT_AUTONOMY_PATH",
    "DeliveryDecision",
    "MeasureError",
    "MemoryHit",
    "OwnerRelay",
    "PersonaAddress",
    "PersonaGrant",
    "PersonaMemory",
    "PersonaOps",
    "PersonaSession",
    "RelayPlan",
    "RelayResult",
    "SpeakerArbiter",
    "SpeechTicket",
    "ToolDecision",
    "TurnSample",
    "build",
    "classify_owner_relay",
    "compare",
    "decide_delivery",
    "demo_samples",
    "find_addressed_persona",
    "load_autonomy",
    "parse_addressee",
    "parse_jsonl",
    "partition_of",
    "safe_slug",
    "summarize",
    "tool_key",
]
