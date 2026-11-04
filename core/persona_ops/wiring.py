"""Montagem das peças de persona e pontos de ligação ao pipeline vivo.

Nada aqui é instalado por importar o pacote. O ``main.py`` chama
:func:`build` uma vez (lazy), guarda o :class:`PersonaOps` e usa três métodos:

* :meth:`PersonaOps.relay_plan` — no início do turno, para saber se o dono
  chamou uma persona pelo nome (então o Gino não fala);
* :meth:`PersonaOps.note_ask` — no ``on_tool`` do ``persona_ask``, para gravar
  o destinatário daquele pedido;
* :meth:`PersonaOps.deliver` — no ponto em que a persona fala, para o arbítrio
  dono/Gino e a fila única do alto-falante.

O orquestrador continua único: este módulo não abre processo, não inicia
thread e não substitui o ``FlowsRuntime``.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .autonomy import AutonomyTable, PersonaGrant, load_autonomy
from .delivery import (Addressee, AuditLog, DeliveryDecision, SpeakerArbiter,
                       decide_delivery, parse_addressee)
from .relay import RelayPlan, classify_owner_relay, find_addressed_persona
from .session import PersonaMemory, PersonaSession

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OPS_DIR = PROJECT_ROOT / "logs" / "personas"
DEFAULT_PERSONA_MEMORY_ROOT = PROJECT_ROOT / "memory" / "personas"
ENV_OPS_DIR = "JARVIS_PERSONA_OPS_DIR"

#: Onde cada peça encaixa no ``main.py`` vivo. Nomes de função, não de linha,
#: para o mapa não envelhecer a cada edição do outro agente.
ATTACHMENT_POINTS: dict[str, str] = {
    "autonomy": (
        "main.py:JarvisLive._qoder_on_tool — ao ver persona_ask, chamar "
        "ops.note_ask(slug, turn_id, addressee, channel). O addressee é "
        "'owner' quando meta['persona_relay'] é verdadeiro, senão 'gino'."
    ),
    "session": (
        "main.py:JarvisLive._qoder_on_tool / _persona_say — abrir "
        "ops.new_session(slug, registry) no primeiro pedido da persona e "
        "cobrar charge(tokens) no fim do turno. O registro é do orquestrador "
        "único; a persona não abre processo."
    ),
    "delivery": (
        "main.py:JarvisLive._persona_say — trocar o _say_now direto por "
        "ops.deliver(slug, text, addressee=..., channel=...). A boca do Gino "
        "não é chamada quando o destino é interno."
    ),
    "relay": (
        "main.py:JarvisLive._qoder_turn — antes de montar o meta, chamar "
        "ops.relay_plan(text, _personas.list_personas()); se is_relay, gravar "
        "meta['persona_relay']=True e meta['speak']=False. O Gino encaminha "
        "sem falar; a persona fala pela boca dela."
    ),
    "measure": (
        "scripts/measure_persona_turn.py — roda DEPOIS do reinício, lendo as "
        "amostras reais em logs/personas/turns.jsonl; sem amostras ele "
        "declara 'não medido' em vez de inventar ganho."
    ),
}


@dataclass
class PersonaOps:
    autonomy: AutonomyTable
    arbiter: SpeakerArbiter
    audit: AuditLog
    memory_root: Path = DEFAULT_PERSONA_MEMORY_ROOT
    log: Callable[[str], None] = print
    _asks: dict[str, deque] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock,
                                   repr=False)

    # ── autonomia ───────────────────────────────────────────────────────────
    def grant(self, persona: str) -> PersonaGrant:
        return self.autonomy.grant(persona)

    def check_tool(self, persona: str, tool: str, *, path: str = "",
                   confirm: bool = False):
        return self.grant(persona).check_tool(tool, path=path, confirm=confirm)

    # ── destinatário do pedido ──────────────────────────────────────────────
    def note_ask(self, persona: str, turn_id: str, addressee: Any, *,
                 channel: str = "voice",
                 name: str = "") -> None:
        slug = str(persona or "").strip()
        if not slug:
            return
        who = parse_addressee(addressee)
        entry = {
            "persona": slug,
            "turn_id": str(turn_id or ""),
            "addressee": who.value,
            "channel": str(channel or "voice"),
            "name": str(name or ""),
            "at": time.time(),
        }
        with self._lock:
            self._asks.setdefault(slug, deque()).append(entry)
        self.audit({"event": "persona_ask", **entry})

    def current_ask(self, persona: str) -> dict[str, Any] | None:
        with self._lock:
            queue = self._asks.get(str(persona or ""))
            return dict(queue[0]) if queue else None

    def finish_ask(self, persona: str) -> dict[str, Any] | None:
        with self._lock:
            queue = self._asks.get(str(persona or ""))
            if not queue:
                return None
            entry = queue.popleft()
            if not queue:
                self._asks.pop(str(persona or ""), None)
            return dict(entry)

    def clear_asks(self, persona: str | None = None) -> None:
        with self._lock:
            if persona is None:
                self._asks.clear()
            else:
                self._asks.pop(str(persona or ""), None)

    def addressee_for(self, persona: str, *,
                      default: str = "owner") -> str:
        entry = self.current_ask(persona)
        if entry is None:
            return default
        return str(entry.get("addressee") or default)

    def channel_for(self, persona: str, *,
                    default: str = "voice") -> str:
        entry = self.current_ask(persona)
        if entry is None:
            return default
        return str(entry.get("channel") or default)

    # ── entrega ─────────────────────────────────────────────────────────────
    def decide(self, persona: str, addressee: Any, *,
               channel: str = "voice") -> DeliveryDecision:
        return decide_delivery(persona, addressee, channel=channel)

    def deliver(self, persona: str, text: str, *, addressee: Any,
                channel: str = "voice",
                priority: int = 10) -> DeliveryDecision:
        decision = self.decide(persona, addressee, channel=channel)
        payload = {
            "event": "persona_delivery",
            **decision.as_dict(),
            "text": str(text or "")[:240],
        }
        self.audit(payload)
        if decision.spoken and str(text or "").strip():
            self.arbiter.submit(
                text, owner="persona", persona=persona, priority=priority,
                decision=decision.as_dict())
        return decision

    # ── relay do dono ───────────────────────────────────────────────────────
    def relay_plan(self, text: str,
                   personas: Iterable[Mapping[str, Any]]) -> RelayPlan:
        return classify_owner_relay(text, personas)

    def find_persona(self, text: str,
                     personas: Iterable[Mapping[str, Any]]):
        return find_addressed_persona(text, personas)

    # ── sessão e memória ────────────────────────────────────────────────────
    def memory(self, persona: str) -> PersonaMemory:
        return PersonaMemory(self.memory_root, persona)

    def new_session(self, persona: str, registry) -> PersonaSession:
        return PersonaSession(persona, self.grant(persona), registry,
                              memory_root=self.memory_root)

    # ── leitura ─────────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            asks = {slug: list(queue) for slug, queue in self._asks.items()}
        return {
            "autonomy": self.autonomy.describe(),
            "speaker": self.arbiter.snapshot(),
            "asks": asks,
        }


def build(root: str | Path | None = None, *,
          autonomy_path: str | Path | None = None,
          audit_path: str | Path | None = None,
          speak: Callable[[Any], Any] | None = None,
          memory_root: str | Path | None = None,
          log: Callable[[str], None] | None = None) -> PersonaOps:
    """Monta as peças sem ligar nada: o chamador decide quando usar."""
    base = Path(root or os.environ.get(ENV_OPS_DIR) or DEFAULT_OPS_DIR)
    base.mkdir(parents=True, exist_ok=True)
    table = load_autonomy(autonomy_path, missing_ok=True)
    audit = AuditLog(audit_path or (base / "audit.jsonl"))
    arbiter = SpeakerArbiter(speak or (lambda _ticket: None),
                             audit=audit, log=log)
    return PersonaOps(
        autonomy=table, arbiter=arbiter, audit=audit,
        memory_root=Path(memory_root or DEFAULT_PERSONA_MEMORY_ROOT),
        log=log or print)


def attachment_points() -> dict[str, str]:
    return dict(ATTACHMENT_POINTS)
