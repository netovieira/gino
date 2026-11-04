"""Onda 2 da arquitetura de fluxos: tirar responsabilidade de cima do orquestrador.

Cinco peças, cada uma num módulo próprio e sem dependência de ``main.py``,
``qoder_live`` ou de :mod:`core.flows` (a onda 1 ainda está em integração; este
pacote é importável sozinho):

* :mod:`core.flow_ops.hot` — despachante quente: só roteia e entrega, sem estado
  de prazo, retentativa ou confirmação pendente. Alvo declarado: < 5 ms.
* :mod:`core.flow_ops.cold` — coordenador frio: prazo, retentativa, retrabalho,
  confirmação pendente e timer. Pode atrasar.
* :mod:`core.flow_ops.registry` — ciclo de vida fora do orquestrador: cada fluxo
  bate ponto no próprio heartbeat; o supervisor expira/mata; o orquestrador lê
  apenas :class:`~core.flow_ops.registry.LifecycleSnapshot`.
* :mod:`core.flow_ops.rules` — tabela declarativa de roteamento em dados
  (``config/flows_rules.json``), com override do dono de precedência máxima.
* :mod:`core.flow_ops.durable` — fila durável append-only com replay idempotente.
* :mod:`core.flow_ops.mouth_guard` — guarda antiasserção no ponto de escrita da
  boca, com template determinístico.
* :mod:`core.flow_ops.envelope` — contrato JSONL versionado (schema 2), com
  receptor que recusa versão incompatível com erro claro.
* :mod:`core.flow_ops.wiring` — funções de ligação explícitas; nada é aplicado
  ao pipeline ao vivo só por importar o pacote.
"""
from __future__ import annotations

from .cold import ColdCoordinator, ColdTask
from .durable import DurableQueue, ReplayReport, TurnRecord
from .envelope import Envelope, SchemaError, SCHEMA_VERSION, decode, encode, make
from .hot import HotDecision, HotDispatcher
from .mouth_guard import MouthSpeech, MouthWriter, speak_guarded
from .registry import (Expiry, FlowHeartbeat, LifecycleRegistry,
                       LifecycleSnapshot)
from .rules import Rule, RuleHit, RulesError, RulesTable, load_rules

__all__ = [
    "ColdCoordinator",
    "ColdTask",
    "DurableQueue",
    "Envelope",
    "Expiry",
    "FlowHeartbeat",
    "HotDecision",
    "HotDispatcher",
    "LifecycleRegistry",
    "LifecycleSnapshot",
    "MouthSpeech",
    "MouthWriter",
    "ReplayReport",
    "Rule",
    "RuleHit",
    "RulesError",
    "RulesTable",
    "SCHEMA_VERSION",
    "SchemaError",
    "TurnRecord",
    "decode",
    "encode",
    "load_rules",
    "make",
    "speak_guarded",
]
