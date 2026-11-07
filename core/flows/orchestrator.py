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
import datetime as dt
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from core import owner_law as _owner_law

from . import partial as _partial
from . import router as _router
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
    # Motivo pelo qual a CONVERSA CURTA pode sair da boca local sem passar
    # pelo cérebro remoto ('' = o turno pertence ao raciocínio). É uma
    # sugestão conservadora do roteador; a frase final ainda passa pelo guard.
    local_chat: str = ""
    # UC-1: consulta de memória executada de verdade (FALA passo 3 / RESPOSTA
    # passo 1). ``memory_wait`` trava até o carregamento terminar ou estourar
    # o teto; ``memory_context`` é o bloco injetado no prompt do cérebro;
    # ``lookups_run`` é a prova do que foi consultado (nunca deduzida da
    # rota). Nunca entra no contexto da boca: a boca fala com os 15 min
    # históricos e o portão de <1 s é dela.
    memory_context: str = ""
    memory_topics: list[str] = field(default_factory=list)
    memory_hits: int = 0
    memory_ms: float = 0.0
    memory_failed: str = ""
    lookups_run: list[str] = field(default_factory=list)
    memory_wait: threading.Event = field(default_factory=threading.Event)
    # UC-5: reclamação/ordem do dono abre defeito no mesmo turno. ``defect``
    # é o registro escrito; ``owner_law`` é a linha de lei que vai ao prompt
    # do cérebro (e só a ele: a boca não comenta a ordem, cumpre).
    defect: dict[str, Any] = field(default_factory=dict)
    defect_wait: threading.Event = field(default_factory=threading.Event)
    owner_law: str = ""

    @property
    def route(self) -> str:
        return self.decision.route

    @property
    def memory_loaded(self) -> bool:
        return self.memory_wait.is_set()


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
    prewarm_last_route: str = ""
    prewarm_logged_at: float = 0.0
    memory_thread: threading.Thread | None = None
    defect_thread: threading.Thread | None = None


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
                 history: Callable[[], list[dict[str, Any]]] | None = None,
                 attention: Any | None = None,
                 memory: Callable[..., dict[str, Any]] | None = None):
        self.config = config
        self.clock = clock
        self.log = log or (lambda _msg: None)
        self.on_event = on_event
        self.assistant_name = assistant_name
        self.router = Router()
        self.mouth = mouth or Mouth(config, history=history)
        self.history = history
        # UC-1: provedor de consulta ``(query, *, hours, limit) -> dict``.
        # Quem injeta é o processo vivo (main.py), porque só ele tem a
        # ChatMemory e o índice em mãos. Sem provedor, nada é fingido: o
        # turno segue sem contexto e o log diz ``memory_loaded ... skipped``.
        self.memory = memory
        self.executor = executor or Executor(
            timeout_ms=config.executor_timeout_ms,
            enabled=config.executor_enabled)
        self.supervisor = supervisor or Supervisor(clock=clock)
        self.fallback = fallback
        # FASE4-TASK5: monitor de atenção por webcam (opcional).
        # Quando injetado, o orquestrador registra eventos de atenção
        # e pode silenciar notificações quando o usuário não está olhando.
        self.attention = attention
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
        local_chat = ""
        if (speak and self.config.mouth_enabled
                and self.config.local_chat_enabled):
            local_chat = _router.short_chat_reason(
                text, decision, name=self.assistant_name)
        plan = TurnPlan(
            turn_id=str(turn_id), text=str(text), decision=decision,
            route_ms=route_ms, fallback_ms=fallback_ms,
            used_fallback=used_fallback, context=context,
            local_chat=local_chat)

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
        # UC-1 (FALA passo 3): os lookups declarados pela rota e o contexto
        # automático de memória são EXECUTADOS aqui — antes só eram nomeados
        # no ``context``. Roda em thread própria: a boca do passo 2 (<1 s)
        # nunca espera por isso, e o cérebro espera no máximo
        # ``memory_timeout_ms``. Turno bloqueado/cancelado não consulta.
        if not plan.blocked and not state.cancelled:
            self._start_memory_worker(plan)
            self._open_owner_defect(plan)
        return plan

    # ── lei do dono (UC-5) ──────────────────────────────────────────────────
    def _open_owner_defect(self, plan: TurnPlan) -> None:
        """Reclamação/ordem do dono vira defeito aberto, nunca verificação.

        A ordem de 2026-11-05 22:27 é literal: o que ele diz não se audita.
        A linha de lei é posta aqui (é string pronta) e a gravação do defeito
        vai para um fio: ``begin()`` segura o brief da boca (passo 2, <1 s)
        e disco nenhum pode entrar nesse caminho. O evento só sai quando o
        registro existe — nunca antes.
        """
        if not plan.decision.owner_complaint:
            return
        plan.owner_law = _owner_law.law_line()
        worker = threading.Thread(
            target=self._record_owner_defect, args=(plan.turn_id,),
            name="flows-owner-law", daemon=True)
        with self._lock:
            state = self._turns.get(plan.turn_id)
            if state is None:
                return
            state.defect_thread = worker
        worker.start()

    def _record_owner_defect(self, turn_id: str) -> None:
        with self._lock:
            state = self._turns.get(str(turn_id))
            plan = state.plan if state is not None else None
        if plan is None:
            return
        try:
            record = _owner_law.record_owner_complaint(plan.text)
        except Exception as exc:
            # Sem o registro não há o que fingir: o log diz o erro e o
            # turno segue — a lei não pode derrubar a conversa.
            self._event("owner_defect_error", str(turn_id),
                        error=f"{type(exc).__name__}: {exc}")
            plan.defect_wait.set()
            return
        if record:
            plan.defect = record
            self._event("owner_defect_opened", str(turn_id),
                        defect_id=record.get("id", ""),
                        intent=record.get("intent", ""), audit="blocked")
        plan.defect_wait.set()

    def owner_defect(self, turn_id: str, *,
                     timeout_ms: int = 1500) -> dict[str, Any]:
        """Registro do defeito aberto neste turno (espera o fio, com teto)."""
        with self._lock:
            state = self._turns.get(str(turn_id))
            plan = state.plan if state is not None else None
        if plan is None or not plan.decision.owner_complaint:
            return {}
        plan.defect_wait.wait(timeout=max(0.0, timeout_ms / 1000.0))
        return dict(plan.defect or {})

    def owner_law_context(self, turn_id: str) -> str:
        """A linha de lei deste turno, para o chamador do cérebro injetar."""
        with self._lock:
            state = self._turns.get(str(turn_id))
            plan = state.plan if state is not None else None
        return str(plan.owner_law or "") if plan is not None else ""

    # ── memória (UC-1) ──────────────────────────────────────────────────────
    def _start_memory_worker(self, plan: TurnPlan) -> None:
        if not self.config.memory_enabled:
            plan.memory_wait.set()
            return
        worker = threading.Thread(
            target=self._load_memory, args=(plan.turn_id, str(plan.text)),
            name="flows-memory", daemon=True)
        with self._lock:
            state = self._turns.get(plan.turn_id)
            if state is None:
                plan.memory_wait.set()
                return
            state.memory_thread = worker
        worker.start()

    def _lookup_time(self) -> str:
        return dt.datetime.now().strftime("%H:%M")

    def _load_memory(self, turn_id: str, text: str) -> None:
        """Executa os lookups do turno e carrega o contexto de memória.

        Melhor esforço por desenho: memória lenta ou ausente não segura o
        turno e não finge conteúdo. O que foi consultado vai para
        ``plan.lookups_run`` e para o evento ``memory_loaded`` — é a prova
        material de que o cérebro consultou o índice (UC-1).
        """
        with self._lock:
            state = self._turns.get(str(turn_id))
            plan = state.plan if state is not None else None
        if plan is None:
            return
        run: list[str] = []
        topics: list[str] = []
        hits = 0
        blocks: list[str] = []
        failed = ""
        skipped = ""
        started = self.clock()
        try:
            for lk in plan.decision.lookups:
                kind = str(lk.kind or "")
                if kind == "time":
                    run.append("time")
                    blocks.append(f"[hora agora] {self._lookup_time()}")
                elif kind == "system":
                    run.append("system")
                elif kind == "memory":
                    run.append("memory")
                    got = self._memory_block(
                        str(lk.args.get("query") or text),
                        hours=self.config.memory_window_hours)
                    if got:
                        blocks.append(got[0])
                        topics.extend(got[1])
                        hits += got[2]
            if "memory" not in run and self.memory is not None:
                # Consulta automática: mesmo sem a rota pedir, o fluxo manda
                # carregar as últimas horas e os tópicos abertos antes de
                # responder. É o passo 1 do RESPOSTA.
                run.insert(0, "memory(auto)")
                got = self._memory_block(
                    text, hours=self.config.memory_window_hours)
                if got:
                    blocks.append(got[0])
                    topics.extend(got[1])
                    hits += got[2]
            elif "memory" not in run:
                # Sem provedor injetado não há o que fingir: registra-se o
                # motivo, e o log deixa claro que a consulta não aconteceu.
                skipped = "sem provedor de memoria"
        except Exception as exc:
            failed = f"{type(exc).__name__}: {exc}"
        plan.memory_context = "\n".join(blocks)[: self.config.memory_max_chars]
        plan.memory_topics = topics
        plan.memory_hits = hits
        plan.lookups_run = run
        plan.memory_ms = round((self.clock() - started) * 1000.0, 1)
        plan.memory_failed = failed
        plan.memory_wait.set()
        self._event("memory_loaded", str(turn_id), lookups=list(run),
                    topics=list(topics), hits=hits,
                    ms=plan.memory_ms, chars=len(plan.memory_context),
                    **({"skipped": skipped} if skipped else {}),
                    **({"error": failed} if failed else {}))

    def _memory_block(self, query: str, *,
                      hours: float) -> tuple[str, list[str], int] | None:
        if self.memory is None:
            return None
        payload = self.memory(str(query or ""), hours=hours,
                              limit=self.config.memory_limit) or {}
        context = str(payload.get("context") or "").strip()
        raw_topics = payload.get("topics") or []
        topics = [str(t).strip() for t in raw_topics if str(t).strip()][:8]
        try:
            hits = int(payload.get("hits") or 0)
        except (TypeError, ValueError):
            hits = 0
        if not context and not topics:
            return None
        return (context, topics, hits)

    def memory_context(self, turn_id: str, *,
                       timeout_ms: int | None = None) -> str:
        """Bloco de memória para o prompt do cérebro; espera no máximo o teto.

        O portão da boca (<1 s) não passa por aqui — este método é chamado
        pelo caminho do raciocínio, que já espera o modelo. Estourado o teto,
        devolve o que estiver pronto (vazio, se nada) e o turno segue.
        """
        if not self.config.memory_enabled:
            return ""
        budget = (self.config.memory_timeout_ms if timeout_ms is None
                  else max(0, int(timeout_ms)))
        with self._lock:
            state = self._turns.get(str(turn_id))
            plan = state.plan if state is not None else None
        if plan is None:
            return ""
        if budget and not plan.memory_wait.wait(timeout=budget / 1000.0):
            self._event("memory_timeout", str(turn_id),
                        ms=budget, lookups=list(plan.lookups_run))
            return ""
        return str(plan.memory_context or "")

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
                # Partials arrive many times per second. The route is still
                # recomputed every time, but the Activity Log gets one line
                # when the route changes or twice per second — not a storm.
                if (decision.route != state.prewarm_last_route
                        or now - state.prewarm_logged_at >= 0.5):
                    state.prewarm_last_route = decision.route
                    state.prewarm_logged_at = now
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

    def local_answer(self, plan: TurnPlan,
                     cancel_event: threading.Event | None = None) -> MouthResult:
        """Conversa curta respondida pela boca, sem cérebro (rota do begin).

        O guard da fase ``brief`` é o portão final: se a frase afirmar
        resultado de ação, vier com número ou vazar ferramenta, ela morre
        aqui e o chamador devolve o turno ao cérebro. Lei (a) não se afrouxa
        nesta rota; ela é a mesma boca com mais uma frase para dizer.
        """
        if not plan.local_chat:
            return MouthResult(sentence="", fixed=True,
                               raw="[rota nao fechada para a boca]")
        result = self.mouth.answer(plan.text, plan.decision, cancel_event,
                                   context=plan.context)
        self._event("local_chat", plan.turn_id, sentence=result.sentence,
                    reason=plan.local_chat, ms=round(result.ms, 1),
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
            results = [self.executor.run(f"k{i}", a.tool, a.args,
                                      requires_confirm=a.requires_confirm,
                                      cancel_event=event)
                    for i, a in enumerate(actions, 1)]
            self._check_unverified_turn(plan, results)
            return results
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
        # FASE3-TASK4: verifica se algum resultado está unverified e marca o turno.
        self._check_unverified_turn(plan, results)
        return results

    def _check_unverified_turn(self, plan: TurnPlan,
                                results: list[ExecutorResult]) -> None:
        """FASE3-TASK4: marca o turno como UNVERIFIED se algum resultado não tem prova material."""
        has_unverified = any(r.unverified for r in results)
        if has_unverified:
            self._event("TURN_UNVERIFIED", plan.turn_id,
                        task_ids=[r.task_id for r in results if r.unverified],
                        reason="ação sem prova material (recibo HMAC + verificação)")
            # Loga no activity log.
            from core.flows.executor import _log_unverified
            for r in results:
                if r.unverified:
                    _log_unverified(r.task_id, plan.turn_id,
                                    "turno marcado UNVERIFIED pelo orquestrador")

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
        m = {
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
        # FASE4-TASK5: inclui estado de atenção quando o monitor existe.
        if self.attention is not None:
            try:
                m["attention"] = self.attention.attention_event()
            except Exception:
                m["attention"] = {"error": "monitor indisponível"}
        return m

    # ── atenção (FASE4-TASK5) ─────────────────────────────────────────────
    def is_user_attentive(self) -> bool:
        """True quando o usuário está olhando para a tela.

        Se nenhum monitor de atenção foi injetado, retorna True (presença
        assumida — comportamento legado).
        """
        if self.attention is None:
            return True
        try:
            return self.attention.is_attentive
        except Exception:
            return True

    def should_deliver_notification(self) -> bool:
        """Se uma notificação deve ser entregue agora.

        Combina a atenção do usuário com a decisão do monitor. Sem monitor,
        sempre entrega (legado).
        """
        if self.attention is None:
            return True
        try:
            return self.attention.should_notify()
        except Exception:
            return True

    def log_attention_event(self) -> None:
        """Registra o estado de atenção no log de eventos do orquestrador."""
        if self.attention is None:
            return
        try:
            event = self.attention.attention_event()
            self._event("attention", **event)
        except Exception:
            pass

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
