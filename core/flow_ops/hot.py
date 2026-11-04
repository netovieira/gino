"""Despachante quente: roteia e entrega, só isso.

O quente fica no caminho da primeira frase. Ele lê a tabela de regras (dado),
casa o texto, monta o evento do contrato e entrega ao destino (fila, boca,
executor). Não guarda prazo, não faz retentativa, não espera confirmação, não
abre thread e não rastreia vivacidade: se precisar de qualquer uma dessas
coisas, quem cuida é o :mod:`core.flow_ops.cold`.

Estado próprio: contadores inteiros e a tabela de regras. Nada de dicionário de
turnos. A fotografia de ciclo de vida, quando vier, é recebida por parâmetro e
apenas anexada ao evento — o quente nunca pergunta "esse fluxo está vivo?".

Evento de chat alheio (``trust != owner``) nunca vira ``task``: sai como
``monitor_event`` com decisão ``proposal`` e não é entregue ao executor.

Medição de latência: :meth:`HotDispatcher.dispatch` devolve ``elapsed_ms``
cronometrado com ``clock`` injetável. O alvo declarado (< 5 ms) é verificado em
teste com medição real; se a máquina estiver carregada, o número aparece no
teste em vez de ser escondido.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .envelope import Envelope, make
from .registry import LifecycleSnapshot
from .rules import Rule, RulesTable

TRUST_OWNER = "owner"


@dataclass(frozen=True)
class HotDecision:
    turn_id: str
    text: str
    flow: str
    action: str
    deadline_ms: int
    requires_confirm: bool
    trust: str
    source: str
    rule_id: str = ""
    origin: str = "default"
    envelope: Envelope | None = None
    delivered: bool = False
    error: str = ""
    elapsed_ms: float = 0.0
    blocked: bool = False
    flow_alive: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id, "text": self.text, "flow": self.flow,
            "action": self.action, "deadline_ms": self.deadline_ms,
            "requires_confirm": self.requires_confirm,
            "trust": self.trust, "source": self.source,
            "rule_id": self.rule_id, "origin": self.origin,
            "delivered": self.delivered, "error": self.error,
            "elapsed_ms": round(self.elapsed_ms, 4),
            "blocked": self.blocked, "flow_alive": self.flow_alive,
        }


class HotDispatcher:
    """Roteador + entrega sem estado de turno. Alvo declarado: < 5 ms."""

    def __init__(self, rules: RulesTable, sink: Callable[[dict[str, Any]], Any]
                 | None = None, *, clock: Callable[[], float] = time.perf_counter,
                 default_flow: str | None = None,
                 default_action: str | None = None,
                 default_deadline_ms: int | None = None):
        self.rules = rules
        self.sink = sink
        self.clock = clock
        self.default_flow = default_flow or rules.default_flow
        self.default_action = default_action or rules.default_action
        self.default_deadline_ms = int(default_deadline_ms
                                       or rules.default_deadline_ms)
        self.dispatched = 0
        self.blocked = 0
        self.errors = 0
        self.last_ms = 0.0
        self.max_ms = 0.0

    def refresh_rules(self) -> bool:
        """Recarrega o dado do dono. Chamado pelo frio/supervisor, não no turno."""
        return self.rules.reload_if_changed()

    def dispatch(self, text: str, *, turn_id: str, route: str = "",
                 trust: str = TRUST_OWNER, source: str = TRUST_OWNER,
                 snapshot: LifecycleSnapshot | None = None
                 ) -> HotDecision:
        started = self.clock()
        trust = str(trust or TRUST_OWNER)
        if trust != TRUST_OWNER:
            decision = self._untrusted(text, turn_id=turn_id, trust=trust,
                                       source=source)
            return self._finish(decision, started)

        hit = self.rules.match(text, route=route, trust=trust, source=source,
                               turn_id=turn_id)
        if hit is not None:
            rule: Rule = hit.rule
            flow, action = rule.flow, rule.action
            deadline_ms = rule.deadline_ms
            requires_confirm = rule.requires_confirm
            rule_id, origin = rule.id, hit.origin
        else:
            flow, action = self.default_flow, self.default_action
            deadline_ms = self.default_deadline_ms
            requires_confirm = False
            rule_id, origin = "", "default"

        denied = action == "deny"
        flow_alive = None
        if snapshot is not None:
            flow_alive = snapshot.alive(flow)
        envelope = make(
            "monitor_event" if denied else "brief", id=str(turn_id),
            flow=flow, action=action, deadline_ms=int(deadline_ms),
            requires_confirm=bool(requires_confirm), must_not_assert=True,
            facts=[], source=source, trust=trust,
            rule=rule_id, origin=origin, snapshot_at=(snapshot.taken_at
                                                      if snapshot else None))
        decision = HotDecision(
            turn_id=str(turn_id), text=str(text), flow=flow, action=action,
            deadline_ms=int(deadline_ms),
            requires_confirm=bool(requires_confirm), trust=trust,
            source=source, rule_id=rule_id, origin=origin,
            envelope=envelope, blocked=denied, flow_alive=flow_alive)
        if denied:
            self.blocked += 1
            return self._finish(decision, started)
        return self._finish(self._deliver(decision), started)

    def _untrusted(self, text: str, *, turn_id: str, trust: str,
                   source: str) -> HotDecision:
        envelope = make("monitor_event", id=str(turn_id), trigger=source,
                        trust="untrusted", text=str(text)[:300],
                        decision="proposal")
        self.blocked += 1
        return HotDecision(
            turn_id=str(turn_id), text=str(text), flow="none",
            action="proposal", deadline_ms=0, requires_confirm=False,
            trust=trust, source=source, rule_id="", origin="untrusted",
            envelope=envelope, blocked=True, delivered=False)

    def _deliver(self, decision: HotDecision) -> HotDecision:
        if self.sink is None:
            return HotDecision(**{**decision.__dict__,
                                  "error": "sem destino (sink) configurado"})
        try:
            self.sink(decision.envelope.to_dict() if decision.envelope else {})
            return HotDecision(**{**decision.__dict__, "delivered": True})
        except Exception as exc:
            self.errors += 1
            return HotDecision(**{**decision.__dict__,
                                  "error": f"{type(exc).__name__}: {exc}"})

    def _finish(self, decision: HotDecision, started: float) -> HotDecision:
        elapsed = (self.clock() - started) * 1000.0
        self.dispatched += 1
        self.last_ms = elapsed
        self.max_ms = max(self.max_ms, elapsed)
        return HotDecision(**{**decision.__dict__, "elapsed_ms": elapsed})

    def stats(self) -> dict[str, Any]:
        return {"dispatched": self.dispatched, "blocked": self.blocked,
                "errors": self.errors, "last_ms": round(self.last_ms, 4),
                "max_ms": round(self.max_ms, 4),
                "rules": len(self.rules), "sink": self.sink is not None}
