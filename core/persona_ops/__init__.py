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
* :mod:`core.persona_ops.limits` — tetos de concorrência (4 personas, 4 tasks)
  e a contagem viva das vagas.
* :mod:`core.persona_ops.delegation` — plano de delegação: de quem é o assunto.

Importar este pacote não muda o assistente vivo: a ligação é chamada pelo
``main.py`` depois do reinício.
"""
from __future__ import annotations

from .autonomy import (AutonomyError, AutonomyTable, DEFAULT_AUTONOMY_PATH,
                       PersonaGrant, ToolDecision, load_autonomy, tool_key)
from .delivery import (Addressee, AuditLog, DeliveryDecision, SpeakerArbiter,
                       SpeechTicket, decide_delivery, parse_addressee)
from .delegation import DelegationPlan, plan, score_personas, subject_words
from .limits import (LimitReached, PersonaSlots, end_task, max_concurrent_personas,
                    max_tasks_per_persona, read_limits, release, request,
                    save_limits, slots, start_task, working)
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
    "DelegationPlan",
    "DeliveryDecision",
    "LimitReached",
    "MeasureError",
    "MemoryHit",
    "OwnerRelay",
    "PersonaAddress",
    "PersonaGrant",
    "PersonaMemory",
    "PersonaOps",
    "PersonaSession",
    "PersonaSlots",
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
    "end_task",
    "find_addressed_persona",
    "load_autonomy",
    "max_concurrent_personas",
    "max_tasks_per_persona",
    "parse_addressee",
    "parse_jsonl",
    "partition_of",
    "plan",
    "read_limits",
    "release",
    "request",
    "safe_slug",
    "save_limits",
    "score_personas",
    "slots",
    "start_task",
    "subject_words",
    "summarize",
    "tool_key",
    "working",
]
