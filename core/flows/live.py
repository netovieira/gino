"""Ponte entre o pipeline ao vivo e o orquestrador.

O ``main.py`` e o sidecar só chamam estes métodos; nenhuma regra de fluxo mora
aqui. A ponte cuida do que é específico do processo vivo:

* agenda o brief da boca em thread (não segura o ``ask`` do Qoder);
* agenda a checagem de estabilidade do parcial (a hipótese precisa parar de
  mudar antes de virar fala);
* mantém o supervisor expirando chats efêmeros em segundo plano;
* guarda as marcas de tempo por turno para medir 1ª frase/1º áudio;
* consulta o portão de atenção (UP-5) antes de cada fala — só suprime com a
  flag opt-in ligada em ``config/webcam_attention.json``.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import FlowsConfig
from .executor import Executor
from . import mouth as _mouth
from .orchestrator import Orchestrator, TurnPlan
from .router import TRUST_OWNER
from .supervisor import Supervisor

# UV-1 (métrica "nunca ignorar"): janela de gravação do contador
# ``questions_without_answer`` no activity.log — a cada 6 h, uma linha
# ``[Fluxos] metrica perguntas_sem_resposta valor=N``. É a prova que o dono
# pode grepar depois de 24 h de uso normal.
QWA_LOG_INTERVAL_S = 6 * 3600.0


@dataclass
class _LiveTurn:
    plan: TurnPlan
    speak_allowed: bool
    source: str
    started_at: float
    partial_first_at: float = 0.0
    first_phrase_at: float = 0.0
    qoder_answered_at: float = 0.0
    brief_ms: float = 0.0
    release_ms: float = 0.0
    stability_timer: threading.Timer | None = None
    timer_generation: int = 0
    notes: list[str] = field(default_factory=list)
    # C: turno aberto antes da diarização, com a FALA desarmada. A boca pode
    # gerar a frase enquanto o voiceprint decide; ela só vai ao ar no arm.
    speech_armed: bool = True
    brief_started_at: float = 0.0
    brief_spoken_at: float = 0.0
    pending_brief: str = ""
    pending_kind: str = "brief"
    cancelled: bool = False
    cancel_reason: str = ""
    # D: conversa curta na boca — o worker responde em paralelo com a
    # diarização como RESPOSTA SUPERFICIAL imediata (fluxo do dono 05/11/2026,
    # passo 2). O processo vivo NÃO espera mais o veredito: o cérebro é
    # chamado junto (passo 3) e a substituição de fala é dos eventos
    # `qoder_answered_at` / `[Say]`, nunca de um `return` que pulava o cérebro.
    chat_event: threading.Event | None = None
    chat_info: dict[str, Any] | None = None
    # UC-2 (FALA passo 2): watchdog do ack intermediário. Se em
    # ``interim_ack_ms`` nenhuma unidade de fala saiu no turno (boca presa
    # com o modelo frio, brief suprimido), a boca fala o template puro e o
    # dono deixa de esperar em silêncio pelos 30-44 s do cérebro.
    interim_timer: threading.Timer | None = None
    interim_spoken: bool = False


class FlowsRuntime:
    def __init__(self, config: FlowsConfig, *,
                 speak: Callable[[str], Any] | None = None,
                 log: Callable[[str], None] | None = None,
                 on_prewarm: Callable[[TurnPlan], None] | None = None,
                 fallback: Callable[[str, int], dict[str, Any] | None] | None = None,
                 executor_runner: Callable[[str, dict[str, Any]], Any] | None = None,
                 history: Callable[[], list[dict[str, Any]]] | None = None,
                 on_chat_route: Callable[[dict[str, Any]], None] | None = None,
                 assistant_name: str = "Gino",
                 clock: Callable[[], float] = time.monotonic,
                 memory: Callable[..., dict[str, Any]] | None = None,
                 start_expiry_thread: bool = True,
                 interim_ack_ms: float | None = None,
                 latency_log: Callable[[str], None] | None = None,
                 attention: Any | None = None,
                 attention_log: Callable[[str], None] | None = None,
                 qwa_log_interval_s: float | None = None):
        self.config = config
        self.speak = speak
        self.log = log or (lambda _msg: None)
        self.on_prewarm = on_prewarm
        self.on_chat_route = on_chat_route
        self.clock = clock
        self.assistant_name = assistant_name
        # UC-2 (passo 2): portão do ack intermediário. O processo vivo usa o
        # teto de 5 s; testes apertam via ``interim_ack_ms`` (0 desliga).
        self._interim_ms = float(
            _mouth.INTERIM_ACK_MS if interim_ack_ms is None else interim_ack_ms)
        # UC-2 (passo 6): métrica de latência ponta-a-ponta no activity.log.
        # Injetável para os testes nunca escreverem no log real.
        self._latency = latency_log or _mouth._log_activity
        # UP-5: monitor de atenção por webcam. Quem decide se a fala sai é o
        # orquestrador (``attention_gate``); a ponte só empresta o caminho de
        # log. ``attention=None`` (legado) não muda nada no vivo.
        self.attention = attention
        self._attention_log = attention_log or _mouth._log_activity
        self.supervisor = Supervisor(clock=clock)
        # O executor é o dono do portão de ferramenta. O runner é injetado
        # pelo processo vivo (main.py); sem ele o executor continua o stub
        # declarado e nunca finge sucesso. O portão de confirmação (lei b)
        # vale mesmo com o executor desligado: exige_confirmar nunca executa.
        executor = None
        if executor_runner is not None:
            executor = Executor(
                runner=executor_runner, timeout_ms=config.executor_timeout_ms,
                enabled=config.executor_enabled)
        self.orchestrator = Orchestrator(
            config, supervisor=self.supervisor, fallback=fallback,
            executor=executor, clock=clock, log=self.log,
            assistant_name=assistant_name, history=history, memory=memory,
            attention=attention)
        self._turns: dict[str, _LiveTurn] = {}
        self._lock = threading.RLock()
        self._closed = False
        # UV-1: relógio da janela de 6 h do contador de perguntas sem
        # resposta. O primeiro registro sai uma janela depois de criado;
        # testes encurtam via ``qwa_log_interval_s``.
        self._qwa_interval_s = float(
            QWA_LOG_INTERVAL_S if qwa_log_interval_s is None
            else max(0.0, float(qwa_log_interval_s)))
        self._qwa_last = self.clock()
        self._expiry_stop = threading.Event()
        self._expiry_thread: threading.Thread | None = None
        self._warm_thread: threading.Thread | None = None
        if start_expiry_thread and config.enabled:
            self._expiry_thread = threading.Thread(
                target=self._expiry_loop, name="flows-supervisor",
                daemon=True)
            self._expiry_thread.start()

    def warm_async(self) -> None:
        """Carrega o modelo da boca sem segurar a primeira fala do turno.

        O primeiro ``brief`` logo depois de subir o processo pagaria o
        carregamento do 7b (medido em ~8 s). O warm é best-effort: se o Ollama
        não estiver de pé, a boca cai no template e o Qoder segue.
        """
        if not self.config.mouth_enabled:
            return
        if self._warm_thread is not None and self._warm_thread.is_alive():
            return
        def _run() -> None:
            try:
                result = self.orchestrator.mouth.warm()
                self._log(f"[Fluxos] boca aquecida: {result}")
            except Exception as exc:
                self._log(f"[Fluxos] warm da boca falhou: {exc!r}")
        self._warm_thread = threading.Thread(
            target=_run, name="flows-warm", daemon=True)
        self._warm_thread.start()

    # ── turno ───────────────────────────────────────────────────────────────
    def start_turn(self, turn_id: str, text: str, *,
                   speak_allowed: bool = True, source: str = "owner",
                   trust: str = TRUST_OWNER, armed: bool = True) -> TurnPlan | None:
        if not self.config.enabled:
            return None
        try:
            plan = self.orchestrator.begin(
                turn_id, text, trust=trust, speak=speak_allowed, source=source)
        except Exception as exc:
            self._log(f"[Fluxos] begin falhou: {exc!r}")
            return None
        if not plan.active or plan.blocked:
            return plan
        live = _LiveTurn(plan=plan, speak_allowed=bool(speak_allowed),
                         source=str(source), started_at=self.clock(),
                         speech_armed=bool(armed))
        with self._lock:
            self._turns[plan.turn_id] = live
        # Portão de ferramenta: ações marcadas ``requires_confirm`` vão ao
        # executor ANTES de qualquer fala de resultado. O executor devolve
        # ``pending_confirm`` e não executa nada (lei b); a autoridade continua
        # no gate de voz do app. O brief fixo do roteador já avisa o dono.
        if any(a.requires_confirm for a in plan.decision.actions):
            threading.Thread(target=self._gate_worker, args=(plan.turn_id,),
                             name="flows-gate", daemon=True).start()
        if (speak_allowed and plan.local_chat
                and self.config.local_chat_enabled
                and self.config.mouth_enabled):
            # Conversa curta: a boca gera a RESPOSTA SUPERFICIAL já em
            # paralelo com a diarização (passo 2 do fluxo). O cérebro também
            # é chamado logo (passo 3): a frase local não é alternativa ao
            # raciocínio, é o ack de <1 s; o conteúdo chega atrás.
            live.chat_event = threading.Event()
            threading.Thread(target=self._chat_worker, args=(plan.turn_id,),
                             name="flows-chat", daemon=True).start()
        elif speak_allowed and self._should_brief(plan):
            # O brief começa a ser gerado JÁ. Se ``armed`` for falso (voiceprint
            # ainda decidindo), a frase fica pronta e só vai ao ar no arm.
            threading.Thread(target=self._brief_worker, args=(plan.turn_id,),
                             name="flows-brief", daemon=True).start()
        if (speak_allowed and self._interim_ms > 0.0
                and not plan.decision.cancel):
            # UC-2 passo 2: último relógio do silêncio. Dispara em 5 s (padrão
            # do código; config pode apertar via atributo ``interim_ack_ms``).
            # Fala só quando NADA saiu no ar — o brief/chat normais bloqueiam
            # o disparo por ``first_phrase_at``.
            timer = threading.Timer(self._interim_ms / 1000.0,
                                    self._interim_fired,
                                    args=(plan.turn_id,))
            timer.daemon = True
            with self._lock:
                live.interim_timer = timer
            timer.start()
        return plan

    def arm_speech(self, turn_id: str, allowed: bool = True) -> bool:
        """Libera/recusa a fala do turno depois do voiceprint.

        O brief já gerado durante a diarização é falado aqui, na hora — é isso
        que evita o atraso de 1 a 2 s. ``allowed=False`` (voz recusada) descarta
        a frase pendente sem som, com o motivo no log.
        """
        with self._lock:
            live = self._turns.get(str(turn_id))
            if live is None:
                return False
            if not allowed:
                live.speech_armed = False
                pending = live.pending_brief
                live.pending_brief = ""
                if pending:
                    self._log("[Fluxos] brief cancelado: voiceprint recusou "
                              f"a fala do turno ({pending!r})")
                return False
            live.speech_armed = True
            pending = live.pending_brief
            live.pending_brief = ""
            if pending and (live.cancelled
                            or (live.qoder_answered_at
                                and live.qoder_answered_at
                                <= live.brief_started_at)):
                self._log("[Fluxos] brief cancelado: cérebro respondeu antes "
                          f"da fala ({pending!r})")
                return False
        if pending:
            self._speak(live, pending, kind=live.pending_kind)
        return True

    def _gate_worker(self, turn_id: str) -> None:
        """Só ações que exigem confirmação; nenhuma ferramenta executa aqui."""
        with self._lock:
            live = self._turns.get(str(turn_id))
        if live is None:
            return
        for index, action in enumerate(live.plan.decision.actions, 1):
            if not action.requires_confirm:
                continue
            try:
                result = self.orchestrator.executor.run(
                    f"g{index}", action.tool, action.args,
                    requires_confirm=True)
            except Exception as exc:
                self._log(f"[Fluxos] portão de {action.tool} falhou: {exc!r}")
                continue
            phrase = ""
            for fact in result.facts:
                phrase = str(fact.get("pending_confirm") or phrase)
            live.notes.append(f"gate:{action.tool}:{'pendente' if phrase else result.error}")
            self._log(f"[Fluxos] portão de ferramenta {action.tool}: "
                      f"{phrase or result.error or 'sem resultado'}; nada executado")

    def _should_brief(self, plan: TurnPlan) -> bool:
        if plan.decision.cancel:
            return True
        if plan.decision.intent in ("confirm_required", "clarify"):
            return True
        return self.config.mouth_speaks_route(plan.route)

    def _brief_worker(self, turn_id: str) -> None:
        with self._lock:
            live = self._turns.get(str(turn_id))
            if live is None or self._closed:
                return
            if live.cancelled:
                self._log("[Fluxos] brief cancelado: turno cancelado "
                          f"({live.cancel_reason or 'sem motivo'})")
                return
            if live.qoder_answered_at:
                # A: o cérebro falou ANTES de a boca começar — aqui sim o
                # brief morre, com o motivo registrado.
                self._log("[Fluxos] brief cancelado: cérebro respondeu antes "
                          "da boca começar")
                return
            # A: daqui para baixo a boca está em síntese. O primeiro texto do
            # cérebro não descarta mais a frase; ele só vira complemento.
            live.brief_started_at = self.clock()
        result = self.orchestrator.brief(live.plan)
        if not result.sentence:
            return
        with self._lock:
            if (self._closed or self._turns.get(str(turn_id)) is not live
                    or live.cancelled):
                self._log("[Fluxos] brief cancelado: turno encerrado durante "
                          "a síntese da boca")
                return
            if (live.qoder_answered_at
                    and live.qoder_answered_at <= live.brief_started_at):
                self._log("[Fluxos] brief cancelado: cérebro respondeu antes "
                          f"da fala ({result.sentence!r})")
                return
            live.brief_ms = float(result.ms)
            if not live.speech_armed:
                live.pending_brief = result.sentence
                self._log("[Fluxos] brief pronto aguardando voiceprint: "
                          f"{result.sentence!r}")
                return
        self._speak(live, result.sentence, kind="brief")

    def _interim_fired(self, turn_id: str) -> None:
        """UC-2 passo 2: o silêncio passou do portão — a boca acka o cérebro.

        Template determinístico, sem modelo e sem rede: nunca afirma fato
        (lei a) e não anuncia ação (``guard.announcement`` medido 07/11/2026
        devolve vazio para a frase). Só vai ao ar se REALMENTE nada saiu:
        brief/chat/já falado, turno encerrado/cancelado, voiceprint indeciso
        (armado é condição) ou cérebro que respondeu primeiro bloqueiam o
        disparo. O conteúdo continua vindo do cérebro (passo 4): a frase é
        ack, não resposta.
        """
        with self._lock:
            live = self._turns.get(str(turn_id))
            if (live is None or self._closed or live.cancelled
                    or live.interim_spoken or live.first_phrase_at
                    or live.pending_brief or live.qoder_answered_at
                    or not live.speech_armed):
                return
            live.interim_spoken = True
        self._speak(live, _mouth.INTERIM_SENTENCE, kind="interim")
        self._log("[Fluxos] boca intermediaria: cerebro sem primeira unidade "
                  f"apos {self._interim_ms:.0f}ms ({turn_id})")

    def _chat_worker(self, turn_id: str) -> None:
        """D: resposta de conversa curta feita pela boca, sem tocar o cérebro.

        O guard da fase ``brief`` decide a última palavra: se a frase vier
        vazia (modelo fora do ar, guard travado), o turno NUNCA é respondido
        pela boca — o worker recua para o brief normal e o processo vivo
        segue para o cérebro. A boca pode anunciar que ouviu; fato só vira
        fala quando a ferramenta devolve (lei a, intocada aqui).
        """
        info: dict[str, Any] | None = None
        live = None
        try:
            with self._lock:
                live = self._turns.get(str(turn_id))
                if (live is None or self._closed or live.cancelled
                        or live.qoder_answered_at):
                    return
            result = self.orchestrator.local_answer(live.plan)
            if result.sentence:
                should_speak = False
                with self._lock:
                    if (self._closed or self._turns.get(str(turn_id)) is not live
                            or live.cancelled):
                        result = None
                    elif live.qoder_answered_at:
                        # O cérebro respondeu antes de a boca falar: a frase
                        # superficial morre (lei da fala única, sem duplicar).
                        result = None
                        self._log("[Fluxos] conversa curta calada: o cérebro "
                                  "respondeu antes da boca")
                    elif live.speech_armed:
                        should_speak = True
                    else:
                        live.pending_brief = result.sentence
                        live.pending_kind = "chat"
                        self._log("[Fluxos] resposta da boca pronta aguardando "
                                  f"voiceprint: {result.sentence!r}")
                    if result is not None:
                        info = {"sentence": result.sentence,
                                "reason": str(live.plan.local_chat),
                                "chat_ms": round(float(result.ms), 1)}
                if info is not None and should_speak:
                    self._speak(live, result.sentence, kind="chat")
        except Exception as exc:
            self._log(f"[Fluxos] conversa curta falhou: {exc!r}")
            info = None
        finally:
            if info is None and live is not None:
                # A boca não respondeu: o brief normal volta a ser o ack do
                # turno enquanto o cérebro pega a fala.
                self._brief_worker(turn_id)
            with self._lock:
                if live is not None:
                    live.chat_info = info
                    if live.chat_event is not None:
                        live.chat_event.set()

    def await_local_chat(self, turn_id: str,
                         timeout_s: float = 4.0) -> dict[str, Any] | None:
        """Veredito da conversa curta: dict com a frase falada, ou None.

        Desde 05/11/2026 (fluxo do dono, passo 3) o processo vivo NÃO espera
        mais este veredito: o cérebro é chamado independentemente da boca, e
        a frase superficial vai ao ar sozinha quando ficar pronta. O método
        fica para medição e testes; nenhum canto de ``main.py`` pode voltar a
        segurar um turno nele (se a boca for esperada, o cérebro fica igual
        era antes — lei do dono).
        """
        with self._lock:
            live = self._turns.get(str(turn_id))
            if live is None or live.chat_event is None:
                return None
            event = live.chat_event
        if not event.wait(max(0.05, float(timeout_s))):
            with self._lock:
                live.cancelled = True
                live.cancel_reason = "conversa curta atrasou"
            self._log("[Fluxos] conversa curta atrasou; o cérebro assume "
                      f"o turno ({turn_id})")
            return None
        with self._lock:
            info = live.chat_info
            if info is None:
                return None
            return {**info,
                    "route": str(live.plan.route),
                    "local_chat_ms": round(
                        (self.clock() - live.started_at) * 1000.0, 1)}

    def owner_defect(self, turn_id: str) -> dict[str, Any]:
        """UC-5: o defeito aberto por esta fala do dono (vazio se não houve)."""
        try:
            return dict(self.orchestrator.owner_defect(str(turn_id)) or {})
        except Exception:
            return {}

    def owner_law_context(self, turn_id: str) -> str:
        """UC-5: a lei "não auditar o dono" para o prompt deste turno."""
        try:
            return str(self.orchestrator.owner_law_context(str(turn_id)) or "")
        except Exception:
            return ""

    def memory_context(self, turn_id: str, *,
                       timeout_ms: int | None = None) -> str:
        """UC-1: bloco de memória consultada neste turno, para o prompt do cérebro.

        É o ponto de entrada do processo vivo: só o caminho do raciocínio
        espera, com teto de ``memory_timeout_ms``. A boca (passo 2, <1 s) não
        chama este método e por isso não paga a consulta.
        """
        try:
            return self.orchestrator.memory_context(
                str(turn_id), timeout_ms=timeout_ms)
        except Exception as exc:
            self._log(f"[Fluxos] contexto de memoria falhou: {exc!r}")
            return ""

    def is_chat_turn(self, turn_id: str) -> bool:
        """O plano deste turno é conversa pura (nenhuma ação/consulta/ambiguidade)?

        Não é portão de nada: só informa ao processo vivo que o sidecar pode
        usar o reenvio curto (3 s) se o abort do turno anterior comer a fala.
        Ação privilegiada nunca passa por aqui — os gates do app resolvem
        antes do modelo. Sem plano ativo: False (reenvio longo, como hoje).
        """
        with self._lock:
            live = self._turns.get(str(turn_id))
        if live is None:
            return False
        d = live.plan.decision
        return bool(live.plan.active and not live.plan.blocked
                    and not d.actions and not d.lookups
                    and not d.cancel and not d.blocked
                    and not d.needs_fallback)

    def partial(self, turn_id: str, delta: str) -> None:
        if not (self.config.enabled and self.config.partial_prewarm):
            return
        with self._lock:
            live = self._turns.get(str(turn_id))
        if live is None:
            return
        if not live.partial_first_at:
            live.partial_first_at = self.clock()
        decision = self.orchestrator.partial(turn_id, delta)
        if decision is not None and decision.release:
            self._release(live, decision.phrase)
            return
        if self.config.partial_speech and live.speak_allowed and live.speech_armed:
            self._schedule_stability_check(live)

    def _schedule_stability_check(self, live: _LiveTurn) -> None:
        with self._lock:
            live.timer_generation += 1
            generation = live.timer_generation
            old = live.stability_timer
        if old is not None:
            try:
                old.cancel()
            except Exception:
                pass
        delay = max(0.0, self.config.partial_stability_ms / 1000.0)
        timer = threading.Timer(
            delay, self._stability_fired, args=(live.plan.turn_id, generation))
        timer.daemon = True
        with self._lock:
            live.stability_timer = timer
        timer.start()

    def _stability_fired(self, turn_id: str, generation: int) -> None:
        with self._lock:
            live = self._turns.get(str(turn_id))
            if live is None or generation != live.timer_generation:
                return
        decision = self.orchestrator.release_if_stable(turn_id)
        if decision is not None and decision.release:
            self._release(live, decision.phrase)

    def _release(self, live: _LiveTurn, phrase: str) -> None:
        if not phrase or not live.speak_allowed or not live.speech_armed:
            return
        if live.plan.local_chat:
            # D: no turno de conversa curta a boca fala a RESPOSTA; o ack de
            # parcial competiria com ela.
            return
        if live.qoder_answered_at:
            return
        self._speak(live, phrase, kind="partial")
        live.release_ms = (self.clock() - live.started_at) * 1000.0

    def mark_answered(self, turn_id: str) -> None:
        self.orchestrator.mark_answered(turn_id)
        first_unit: float | None = None
        brain_ms: float | None = None
        unit_kind = "cerebro"
        with self._lock:
            live = self._turns.get(str(turn_id))
            if live is not None and not live.qoder_answered_at:
                live.qoder_answered_at = self.clock()
                self._cancel_timer(live)
                brain_ms = (live.qoder_answered_at - live.started_at) * 1000.0
                if live.first_phrase_at:
                    first_unit = ((live.first_phrase_at - live.started_at)
                                  * 1000.0)
                    unit_kind = ("intermediaria" if live.interim_spoken
                                 else "boca")
                else:
                    first_unit = brain_ms
        if live is not None and brain_ms is not None:
            # UC-2 passo 6 (caminho cerebro): primeira unidade medida no
            # vivo mais o texto do cérebro. O teto de 30 s (auditoria gap
            # #2) é avaliado na primeira unidade de fala do turno; estourado,
            # a linha diz ACIMA_DO_TETO em vez de fingir.
            status = ("OK" if first_unit <= _mouth._CEILING_MS
                      else "ACIMA_DO_TETO")
            self._log_latency(
                "cerebro", first_unit,
                extra=(f"unidade={unit_kind} "
                       f"cerebro_primeiro_texto_ms={brain_ms:.0f} "
                       f"status={status}"))

    def finish(self, turn_id: str) -> None:
        with self._lock:
            live = self._turns.pop(str(turn_id), None)
        if live is not None:
            self._cancel_timer(live)
            self._log_turn_breakdown(live)
        self.orchestrator.finish(turn_id)

    def cancel(self, turn_id: str, reason: str = "cancelado") -> bool:
        with self._lock:
            live = self._turns.get(str(turn_id))
            if live is not None:
                live.cancelled = True
                live.cancel_reason = str(reason or "")
                if live.pending_brief:
                    self._log("[Fluxos] brief cancelado: turno cancelado "
                              f"({reason or 'sem motivo'})")
                live.pending_brief = ""
        if live is not None:
            self._cancel_timer(live)
        return self.orchestrator.cancel(turn_id, reason)

    # ── monitores/prazos ────────────────────────────────────────────────────
    def monitor_event(self, source: str, text: str,
                      trust: str = "untrusted") -> dict[str, Any]:
        return self.orchestrator.monitor_event(source, text, trust=trust)

    def attention_allows(self, *, reason: str = "notificacao") -> bool:
        """Portão para quem entrega fora do turno (notificação de monitor).

        É a mesma consulta da fala, exposta para o processo vivo usar antes
        de avisar por WhatsApp/e-mail. Nenhuma chamada aqui é obrigatória
        hoje: com a flag desligada o resultado é sempre True.
        """
        return self._attention_gate(str(reason))

    def expire(self) -> list[tuple[str, str]]:
        killed = self.orchestrator.expire()
        for chat, reason in killed:
            self._log(f"[Fluxos] supervisor matou {chat}: {reason}")
        return killed

    def qa_metric_tick(self, now: float | None = None) -> bool:
        """UV-1: grava o contador de perguntas sem resposta na janela vencida.

        Uma linha por janela (6 h no vivo; encurtável para medir em teste) no
        log dos fluxos — no processo vivo, o ``log`` injetado é o
        activity.log. Devolve True quando gravou. Nunca quebra o turno: só
        lê e escreve log.
        """
        with self._lock:
            instante = self.clock() if now is None else float(now)
            if instante - self._qwa_last < self._qwa_interval_s:
                return False
            self._qwa_last = instante
            valor = int(self.orchestrator.router.questions_without_answer)
        self._log(f"[Fluxos] metrica perguntas_sem_resposta valor={valor} "
                  f"janela_s={int(self._qwa_interval_s)}")
        try:
            # Espelho no painel interno de métricas (core/metrics.py): o
            # valor observado é o mesmo do log, não uma segunda conta.
            from core import metrics as _metrics
            _metrics.observe("questions_without_answer", float(valor))
        except Exception:
            pass
        return True

    def _expiry_loop(self) -> None:
        while not self._expiry_stop.is_set():
            try:
                self.expire()
            except Exception as exc:
                self._log(f"[Fluxos] expire falhou: {exc!r}")
            try:
                # UV-1: a mesma batida de 5 s do supervisor decide se a
                # janela de 6 h do contador venceu; sem janela vencida, o
                # custo é uma subtração de relógio.
                self.qa_metric_tick()
            except Exception as exc:
                self._log(f"[Fluxos] metrica qwa falhou: {exc!r}")
            self._expiry_stop.wait(5.0)

    def close(self) -> None:
        self._closed = True
        self._expiry_stop.set()
        with self._lock:
            live_turns = list(self._turns.values())
            self._turns.clear()
        for live in live_turns:
            self._cancel_timer(live)
        # UP-5: a câmera do monitor é recurso do processo vivo — ao fechar a
        # ponte ela é liberada (sem isso o dispositivo fica preso).
        att = self.attention
        if att is not None:
            try:
                att.stop()
            except Exception:
                pass

    # ── medição ─────────────────────────────────────────────────────────────
    def turn_metrics(self, turn_id: str) -> dict[str, Any]:
        with self._lock:
            live = self._turns.get(str(turn_id))
        if live is None:
            return {}
        now = self.clock()
        return {
            "turn_id": str(turn_id),
            "route": live.plan.route,
            "started_at": live.started_at,
            "first_partial_ms": (round((live.partial_first_at
                                        - live.started_at) * 1000.0, 1)
                                 if live.partial_first_at else None),
            "first_phrase_ms": (round((live.first_phrase_at
                                       - live.started_at) * 1000.0, 1)
                                if live.first_phrase_at else None),
            "qoder_first_text_ms": (round((live.qoder_answered_at
                                           - live.started_at) * 1000.0, 1)
                                    if live.qoder_answered_at else None),
            "brief_model_ms": round(live.brief_ms, 1),
            "partial_release_ms": (round(live.release_ms, 1)
                                   if live.release_ms else None),
            "now_ms": round((now - live.started_at) * 1000.0, 1),
        }

    def metrics(self) -> dict[str, Any]:
        data = self.orchestrator.metrics()
        data["live_turns"] = len(self._turns)
        return data

    # ── plumbing ────────────────────────────────────────────────────────────
    def _attention_gate(self, kind: str) -> bool:
        """Portão de atenção (UP-5) — consulta, grava o evento e responde.

        Retorna False só quando o gate está LIGADO por flag opt-in
        (``config/webcam_attention.json``), a webcam já viu o usuário e ele
        desviou o olhar/sumiu do enquadramento. Qualquer outro caso — sem
        monitor, monitor desligado, falha do monitor — entrega a fala, que é
        o comportamento de sempre. O ``attention_event`` vai ao activity.log
        em todos os casos com monitor, inclusive desligado: é a prova de que
        o portão foi consultado antes de falar.
        """
        if self.attention is None:
            return True
        try:
            deliver = bool(self.orchestrator.attention_gate(reason=kind))
        except Exception as exc:
            self._log(f"[Fluxos] portão de atenção indisponível: {exc!r}")
            return True
        try:
            event = dict(self.attention.attention_event())
        except Exception:
            event = {}
        try:
            # O estado bruto do detector ("attentive" do snapshot) pode ser
            # False com a flag desligada — quem decide a entrega é a resposta
            # do monitor, então é ela que vai na linha, junto do "deliver".
            attentive = bool(self.attention.is_attentive)
        except Exception:
            attentive = bool(event.get("attentive"))
        try:
            self._attention_log(
                f"[Fluxos] attention_event motivo={kind} "
                f"deliver={int(deliver)} "
                f"attentive={int(attentive)} "
                f"face={int(bool(event.get('face_detected')))} "
                f"olhando_tela={int(bool(event.get('looking_at_screen')))} "
                f"enabled={int(bool(event.get('enabled')))} "
                f"frames={int(event.get('total_frames') or 0)} "
                f"rostos={int(event.get('total_faces') or 0)}")
        except Exception:
            pass
        return deliver

    def _speak(self, live: _LiveTurn, text: str, *, kind: str) -> None:
        if self.speak is None or not text:
            return
        if not self._attention_gate(kind):
            # UP-5: o usuário não está olhando para a tela e o gate está
            # ligado (flag opt-in). A frase morre aqui, antes de qualquer
            # marcador de tempo: o turno continua vivo, o cérebro segue
            # trabalhando e a métrica de latência não é tocada.
            live.notes.append(f"atencao:suprimida:{kind}")
            self._log(f"[Fluxos] fala {kind} suprimida: usuário desatento "
                      "(gate de atenção ligado)")
            return
        first_unit = not live.first_phrase_at
        if first_unit:
            live.first_phrase_at = self.clock()
        if kind == "brief":
            # A boca já está em fala: o primeiro texto do cérebro não pode
            # mais descartar esta frase (regra A).
            live.brief_spoken_at = self.clock()
        try:
            self.speak(text)
        except Exception as exc:
            self._log(f"[Fluxos] fala {kind} falhou: {exc!r}")
            return
        if kind in ("chat", "partial"):
            # UV-1: resposta da boca (conversa curta) ou conteúdo solto do
            # parcial é resposta ENTREGUE deste turno — baixa a dívida da
            # pergunta. Brief e intermediária não baixam: são ack, não
            # resposta (a resposta real ainda vem do cérebro).
            self.orchestrator.router.note_answer(live.plan.turn_id)
        if kind == "chat" and self.on_chat_route is not None:
            # Mediação do dono (passo 2): no instante em que a resposta
            # superficial SAI, não quando foi decidida. O processo vivo registra
            # `[Rota] caminho=boca primeira_unidade_ms=...`.
            try:
                self.on_chat_route({
                    "sentence": text,
                    "reason": str(live.plan.local_chat),
                    "first_ms": round(
                        (self.clock() - live.started_at) * 1000.0, 1)})
            except Exception as exc:
                self._log(f"[Fluxos] rota boca (medição) falhou: {exc!r}")
        self._log(f"[Fluxos] boca ({kind}): {text!r}")
        if first_unit:
            # UC-2 passo 6: métrica ponta-a-ponta no activity.log — o tempo
            # do dono ouvir até a PRIMEIRA unidade de fala do turno (brief,
            # chat, parcial ou intermediária). POR ÚLTIMO: o disco nunca
            # pode abrir janela entre a fala e o callback de medição do
            # processo vivo (on_chat_route), que é síncrono por contrato.
            self._log_latency(
                "boca", (live.first_phrase_at - live.started_at) * 1000.0,
                extra=f"unidade={kind}")

    @staticmethod
    def _cancel_timer(live: _LiveTurn) -> None:
        timer = live.stability_timer
        live.stability_timer = None
        live.timer_generation += 1
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        interim = live.interim_timer
        live.interim_timer = None
        if interim is not None:
            try:
                interim.cancel()
            except Exception:
                pass

    def _log_latency(self, caminho: str, first_unit_ms: float, *,
                     extra: str = "") -> None:
        """Linha canônica da métrica ponta-a-ponta (UC-2 passo 6).

        Formato: ``[Fluxos] latencia e2e caminho=<boca|cerebro>
        primeira_unidade_ms=<N> teto_ms=30000 <extra>``. Vai direto ao
        activity.log (``core.flows.mouth._log_activity``), por design
        independente do ``log`` da UI; falha de disco nunca derruba o turno.
        """
        line = (f"[Fluxos] latencia e2e caminho={caminho} "
                f"primeira_unidade_ms={first_unit_ms:.0f} "
                f"teto_ms={_mouth._CEILING_MS}"
                + (f" {extra}" if extra else ""))
        try:
            self._latency(line)
        except Exception:
            pass

    def _log_turn_breakdown(self, live: _LiveTurn) -> None:
        """UC-2 passo 1: onde o tempo foi gasto — grava o detalhamento.

        Roteador, fallback remoto, carga de memória e tempo do modelo da
        boca saem medidos no activity.log a cada turno fechado; é a prova
        material de que o cérebro paraleliza com a boca (passo 5) e de
        quanto cada etapa custou no ponta-a-ponta.
        """
        try:
            total_ms = (self.clock() - live.started_at) * 1000.0
            first = (live.first_phrase_at or live.qoder_answered_at)
            first_part = (f" primeira_unidade_ms="
                          f"{(first - live.started_at) * 1000.0:.0f}"
                          if first else "")
            self._latency(
                f"[Fluxos] latencia e2e turno={live.plan.turn_id} "
                f"total_ms={total_ms:.0f}{first_part} "
                f"rota_ms={live.plan.route_ms:.1f} "
                f"fallback_ms={live.plan.fallback_ms:.1f} "
                f"memoria_ms={live.plan.memory_ms:.1f} "
                f"boca_brief_ms={live.brief_ms:.1f}")
        except Exception:
            pass

    def _log(self, message: str) -> None:
        try:
            self.log(message)
        except Exception:
            pass
