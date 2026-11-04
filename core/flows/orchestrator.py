"""Orquestrador — dono do estado e do despacho.

Recebe a transcrição (inclusive parcial) antes de qualquer modelo, fecha a
rota por código e diz quem faz o quê. Ele não pensa conteúdo, não fala e não é
modelo: chama o roteador, o executor, a boca e o supervisor.

Fluxo de um turno:

    partial --> pré-aquece rota/contexto/modelo (não fala)
    final  --> rota --> brief (facts=[]) --> boca
                    \\-> tasks --> executor --> fatos --> boca complementar
    cancel --> aborta tarefas e devolve fato de cancelamento para a boca

O fallback só é chamado quando o roteador não fecha por regra. Ele é remoto
por contrato (:class:`core.flows.router.classify_remote`); sem hook remoto, a
boca pede esclarecimento em vez de inventar ação.
"""
from __future__ import annotations

import concurrent.futures
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import partial as _partial
from .config import FALLBACK_REMOTE, FlowsConfig
from .executor import Executor, ExecutorResult
from .mouth import Mouth, MouthResult
from .router import Decision, Router, TRUST_OWNER
from .supervisor import (KIND_EXECUTOR, KIND_MEDIA_MONITOR, KIND_MONITOR,
                         KIND_REASONING, Supervisor)

CHAT_REASONING = "chat_raciocinio"
CHAT_EXECUTOR = "chat_ferramentas"
CHAT_WHATSAPP = "chat_whatsapp"
CHAT_EMAIL = "chat_email"
CHAT_MEDIA = "monitor_multimidia"


@dataclass
class TurnPlan:
    turn_id: str
    text: str
    decision: Decision = field(default_factory=Decision)
    active: bool = True
    blocked: bool = False
    reason: str = ""
    route_ms: float = 0.0
    fallback_ms: float = 0.0
    used_fallback: bool = False
    context: str = ""
    prewarmed_from_partial: bool = False
    spawned: list[str] = field(default_factory=list)

    @property
    def route(self) -> str:
        return self.decision.route


@dataclass
class _TurnState:
    plan: TurnPlan
    cancel_event: threading.Event = field(default_factory=threading.Event)
    cancelled: bool = False
    answered: bool = False
    brief_spoken: bool = False
    partial_text: str = ""
    partial_first_at: float = 0.0
    partial_changed_at: float = 0.0
    partial_released: bool = False
    prewarm_count: int = 0


class Orchestrator:
    def __init__(self, config: FlowsConfig,
                 *, mouth: Mouth | None = None,
                 executor: Executor | None = None,
                 supervisor: Supervisor | None = None,
                 fallback: Callable[[str, int], dict[str, Any] | None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] | None = None,
                 on_event: Callable[[dict[str, Any]], None] | None = None,
                 assistant_name: str = "Gino",
                 history: Callable[[], list[dict[str, Any]]] | None = None):
        self.config = config
        self.clock = clock
        self.log = log or (lambda _msg: None)
        self.on_event = on_event
        self.assistant_name = assistant_name
        self.router = Router()
        self.mouth = mouth or Mouth(config, history=history)
        self.history = history
        self.executor = executor or Executor(
            timeout_ms=config.executor_timeout_ms,
            enabled=config.executor_enabled)
        self.supervisor = supervisor or Supervisor(clock=clock)
        self.fallback = fallback
        self._turns: dict[str, _TurnState] = {}
        self._lock = threading.RLock()
        self._register_defaults()

    # ── registro ────────────────────────────────────────────────────────────
    def _register_defaults(self) -> None:
        self.supervisor.ensure(CHAT_REASONING, KIND_REASONING,
                               self.config.reasoning)
        self.supervisor.ensure(CHAT_EXECUTOR, KIND_EXECUTOR,
                               self.config.executor)
        self.supervisor.ensure(CHAT_WHATSAPP, KIND_MONITOR,
                               self.config.monitor)
        self.supervisor.ensure(CHAT_EMAIL, KIND_MONITOR,
                               self.config.monitor)

    # ── turno ───────────────────────────────────────────────────────────────
    def begin(self, turn_id: str, text: str, *, trust: str = TRUST_OWNER,
              speak: bool = True, source: str = "owner") -> TurnPlan:
        started = self.clock()
        if not self.config.enabled:
            plan = TurnPlan(turn_id=str(turn_id), text=str(text),
                            active=False, reason="flows desligados")
            self._event("turn_disabled", plan.turn_id)
            return plan

        if str(trust or TRUST_OWNER) != TRUST_OWNER:
            decision = self.router.route(text, trust=trust)
            plan = TurnPlan(turn_id=str(turn_id), text=str(text),
                            decision=decision, blocked=True,
                            reason="untrusted", active=False)
            self._event("turn_blocked", plan.turn_id, trust=trust)
            return plan

        self.supervisor.ensure(CHAT_REASONING, KIND_REASONING,
                               self.config.reasoning)
        self.supervisor.touch(CHAT_REASONING)

        route_started = self.clock()
        decision = self.router.parse(text)
        fallback_ms = 0.0
        used_fallback = False
        if decision.needs_fallback and self.config.fallback == FALLBACK_REMOTE:
            used_fallback = True
            fallback_ms = self._apply_remote_fallback(decision, text)
        route_ms = (self.clock() - route_started) * 1000.0

        context = self._context_for(decision)
        plan = TurnPlan(
            turn_id=str(turn_id), text=str(text), decision=decision,
            route_ms=route_ms, fallback_ms=fallback_ms,
            used_fallback=used_fallback, context=context)

        state = _TurnState(plan=plan)
        with self._lock:
            previous = self._turns.get(plan.turn_id)
            if previous is not None:
                state.partial_text = previous.partial_text
                state.partial_first_at = previous.partial_first_at
                state.partial_changed_at = previous.partial_changed_at
                plan.prewarmed_from_partial = bool(previous.prewarm_count)
            self._turns[plan.turn_id] = state

        if decision.cancel:
            state.cancel_event.set()
            state.cancelled = True

        if any(a.tool == "media_control" for a in decision.actions):
            self.supervisor.register(CHAT_MEDIA, KIND_MEDIA_MONITOR,
                                     self.config.media_monitor)
            plan.spawned.append(CHAT_MEDIA)
        self._event("turn", plan.turn_id, route=plan.route,
                    actions=[a.tool for a in decision.actions],
                    lookups=[lk.kind for lk in decision.lookups],
                    fallback=used_fallback, ms=round(route_ms, 3))
        return plan

    def _apply_remote_fallback(self, decision: Decision, text: str) -> float:
        if self.fallback is None:
            self.router.fallback_calls += 1
            self.router.apply_fallback(decision, None, source="remote")
            self._event("fallback_unavailable", text[:80])
            return 0.0
        started = self.clock()
        payload: dict[str, Any] | None = None
        try:
            pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="flows-fallback")
            future = pool.submit(self.fallback, text,
                                 self.config.fallback_timeout_ms)
            try:
                payload = future.result(
                    timeout=self.config.fallback_timeout_ms / 1000.0)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        except concurrent.futures.TimeoutError:
            self._event("fallback_timeout", text[:80])
            payload = None
        except Exception as exc:
            self._event("fallback_error", repr(exc))
            payload = None
        self.router.fallback_calls += 1
        self.router.apply_fallback(decision, payload, source="remote")
        return (self.clock() - started) * 1000.0

    @staticmethod
    def _context_for(decision: Decision) -> str:
        targets = ", ".join(a.tool for a in decision.actions)
        lookups = ", ".join(lk.kind for lk in decision.lookups)
        return (f"rota={decision.route}; alvo={decision.target}; "
                f"ferramentas=[{targets}]; consultas=[{lookups}]")

    # ── parcial ─────────────────────────────────────────────────────────────
    def partial(self, turn_id: str, delta: str,
                *, now: float | None = None) -> _partial.PartialDecision | None:
        if not (self.config.enabled and self.config.partial_prewarm):
            return None
        now = self.clock() if now is None else float(now)
        delta = str(delta or "")
        with self._lock:
            state = self._turns.get(str(turn_id))
            if state is None:
                return None
            if not state.partial_first_at:
                state.partial_first_at = now
                state.partial_changed_at = now
            if delta:
                state.partial_text += delta
                state.partial_changed_at = now
                state.prewarm_count += 1
                # Rota/contexto: fechar por regra durante o parcial. Não
                # despacha tarefa; só aquece a decisão do turno final.
                decision = self.router.parse(state.partial_text)
                state.plan.context = self._context_for(decision)
                self._event("prewarm", state.plan.turn_id,
                            route=decision.route,
                            chars=len(state.partial_text))
            self.supervisor.touch(CHAT_REASONING, now=now)
        decision = self._partial_release(state, now)
        return decision

    def release_if_stable(self, turn_id: str,
                          *, now: float | None = None
                          ) -> _partial.PartialDecision | None:
        if not (self.config.enabled and self.config.partial_speech):
            return None
        now = self.clock() if now is None else float(now)
        with self._lock:
            state = self._turns.get(str(turn_id))
            if state is None:
                return None
        return self._partial_release(state, now)

    def _partial_release(self, state: _TurnState, now: float
                         ) -> _partial.PartialDecision | None:
        if not self.config.partial_speech:
            return None
        if state.answered or state.brief_spoken or state.cancelled:
            return None
        stable = (now - state.partial_changed_at
                  >= self.config.partial_stability_ms / 1000.0)
        decision = _partial.decide(
            state.partial_text, stable=stable,
            already_released=state.partial_released,
            min_chars=self.config.partial_min_chars,
            min_words=self.config.partial_min_words,
            assistant_name=self.assistant_name)
        if decision.release:
            state.partial_released = True
            state.brief_spoken = True
            self._event("partial_speech", state.plan.turn_id,
                        phrase=decision.phrase, reason=decision.reason)
        return decision

    def mark_answered(self, turn_id: str) -> None:
        """O raciocínio já falou: parcial não pode mais abrir a boca."""
        with self._lock:
            state = self._turns.get(str(turn_id))
            if state is not None:
                state.answered = True

    # ── boca ────────────────────────────────────────────────────────────────
    def brief(self, plan: TurnPlan,
              cancel_event: threading.Event | None = None) -> MouthResult:
        with self._lock:
            state = self._turns.get(plan.turn_id)
            if state is not None and (state.brief_spoken or state.cancelled):
                reason = "turno cancelado" if state.cancelled else "brief já falado"
                self._event("brief_suppressed", plan.turn_id, reason=reason)
                return MouthResult(sentence="", fixed=True,
                                   raw=f"[brief suprimido: {reason}]")
        result = self.mouth.brief(plan.text, plan.decision, cancel_event,
                                  allow_model=self.config.mouth_enabled,
                                  context=plan.context)
        with self._lock:
            state = self._turns.get(plan.turn_id)
            if state is not None:
                state.brief_spoken = True
        self._event("brief", plan.turn_id, sentence=result.sentence,
                    ms=round(result.ms, 1),
                    guard=result.guard_tripped, fixed=result.fixed)
        return result

    def complement(self, plan: TurnPlan, facts: list[dict[str, Any]],
                   cancel_event: threading.Event | None = None) -> MouthResult:
        result = self.mouth.complement(plan.text, plan.decision, facts,
                                       cancel_event)
        self._event("complement", plan.turn_id, sentence=result.sentence,
                    ms=round(result.ms, 1),
                    guard=result.guard_tripped, fixed=result.fixed)
        return result

    # ── executor ────────────────────────────────────────────────────────────
    def run_plan(self, plan: TurnPlan,
                 cancel_event: threading.Event | None = None
                 ) -> list[ExecutorResult]:
        """Executa as ações do plano no executor (nunca no orquestrador)."""
        with self._lock:
            state = self._turns.get(plan.turn_id)
        event = cancel_event or (state.cancel_event if state else None)
        actions = list(plan.decision.actions)
        if not actions:
            return []
        if event is not None and event.is_set():
            return [self.executor.run(f"k{i}", a.tool, a.args,
                                      requires_confirm=a.requires_confirm,
                                      cancel_event=event)
                    for i, a in enumerate(actions, 1)]
        results: list[ExecutorResult] = []
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(actions)),
            thread_name_prefix="flows-plan")
        try:
            futures = [
                pool.submit(self.executor.run, f"k{i}", a.tool, a.args,
                            requires_confirm=a.requires_confirm,
                            cancel_event=event)
                for i, a in enumerate(actions, 1)
            ]
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append(ExecutorResult(
                        task_id="?", error=f"{type(exc).__name__}: {exc}"))
        finally:
            pool.shutdown(wait=True)
        self.supervisor.add_usage(CHAT_EXECUTOR, 256)
        self._event("results", plan.turn_id,
                    facts=[f for r in results for f in r.facts])
        return results

    # ── cancelamento ────────────────────────────────────────────────────────
    def cancel(self, turn_id: str, reason: str = "cancelado") -> bool:
        with self._lock:
            state = self._turns.get(str(turn_id))
            if state is None:
                return False
            if state.cancelled:
                return False
            state.cancelled = True
            state.cancel_event.set()
        self._event("cancel", str(turn_id), reason=str(reason))
        return True

    def finish(self, turn_id: str) -> None:
        with self._lock:
            self._turns.pop(str(turn_id), None)
        self.supervisor.touch(CHAT_REASONING)

    # ── monitores ───────────────────────────────────────────────────────────
    def monitor_event(self, source: str, text: str,
                      trust: str = "untrusted") -> dict[str, Any]:
        """Lei (c): evento externo vira proposta, nunca ordem/task."""
        untrusted = str(trust or "untrusted") != TRUST_OWNER
        decision = "blocked" if untrusted else "proposal"
        result = {"source": str(source), "trust": str(trust or "untrusted"),
                  "decision": decision, "text": str(text), "task": None}
        self._event("monitor_event", source=str(source), trust=str(trust),
                    decision=decision, text=str(text)[:200])
        return result

    # ── orçamento/prazo ─────────────────────────────────────────────────────
    def expire(self, *, now: float | None = None) -> list[tuple[str, str]]:
        return self.supervisor.expire(now=now)

    def usage(self, chat: str, tokens: int) -> str:
        return self.supervisor.add_usage(chat, tokens)

    # ── métricas ────────────────────────────────────────────────────────────
    def metrics(self) -> dict[str, Any]:
        with self._lock:
            turns = len(self._turns)
        return {
            "router": {
                "rule_decisions": self.router.rule_decisions,
                "fallback_decisions": self.router.fallback_decisions,
                "fallback_calls": self.router.fallback_calls,
                "fallback_last": dict(self.router.fallback_last),
            },
            "mouth": {"calls": self.mouth.calls,
                      "guard_trips": self.mouth.guard_trips,
                      "failures": self.mouth.failures},
            "supervisor": self.supervisor.snapshot(),
            "active_turns": turns,
        }

    def _event(self, kind: str, *args: Any, **fields: Any) -> None:
        if not self.config.log_events:
            return
        payload = {"type": kind, "args": list(args), **fields}
        try:
            self.log(f"[Fluxos] {payload}")
        except Exception:
            pass
        if self.on_event is not None:
            try:
                self.on_event(payload)
            except Exception:
                pass
