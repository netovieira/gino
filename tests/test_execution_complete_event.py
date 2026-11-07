"""UP-10 — evento ``execution_complete`` quando o orquestrador notifica o cérebro.

FLUXO EXECUCAO (skills/fluxos/FLUXOS.md) passo 3: "ORQUESTRADOR NOTIFICA
CEREBRO". O ``run_plan()`` já coletava os resultados e devolvia a lista — a
notificação era implícita no retorno, e quem relia o activity.log não via
conclusão nenhuma (auditoria de 06/11, gap do passo 3).

O que este arquivo prova:

  1. Toda execução gera ``execution_complete`` com ações, resultados e tempo.
  2. O evento chega na linha de log do processo vivo (o ``log`` injetado é o
     caminho que o ``main.py`` liga ao activity.log) e no ouvinte ``on_event``.
  3. Sem ações despachadas não há evento (nada foi executado).
  4. Turno bloqueado, fluxo desligado e execução cancelada: os dois últimos
     são casos distintos — cancelamento é execução concluída como cancelada,
     bloqueio prévio não é execução nenhuma.
  5. O tempo total sai do relógio injetado do turno (determinístico).
  6. Cada desfecho do executor vira um estado legível: ok, cancelado,
     confirmação pendente, sem prova material/bloqueado, falha.
  7. Nada do comportamento antigo muda: ``run_plan`` devolve as mesmas
     results, o evento ``results`` continua existindo e vem antes.
  8. O kill switch ``log_events`` continua valendo para todos os eventos.
"""
from __future__ import annotations

import ast
import threading

import pytest

from core.flows.config import FlowsConfig
from core.flows.executor import Executor, ExecutorResult
from core.flows.orchestrator import (Orchestrator, _EVENT_TEXT_CHARS,
                                     _execution_summary, _result_spoken,
                                     _result_state)
from core.flows.orchestrator import TurnPlan
from core.flows.router import Action, Decision


# ── isolamento: teste nenhum escreve no activity.log real ───────────────────

@pytest.fixture(autouse=True)
def _isolated_activity_log(tmp_path, monkeypatch):
    """Redireciona o activity.log do executor para um arquivo temporário.

    O caminho é o do UP-9 (``executor._ACTIVITY_LOG_PATH``). Resultados sem
    prova material passam pelo ``_log_unverified`` do próprio executor — sem
    esta isolação cada execução deste arquivo gravaria TURN_UNVERIFIED no log
    do dono.
    """
    from core.flows import executor as exe
    if hasattr(exe, "_ACTIVITY_LOG_PATH"):
        monkeypatch.setattr(exe, "_ACTIVITY_LOG_PATH", tmp_path / "activity.log")
    yield


@pytest.fixture
def no_owner_window(monkeypatch):
    """Fecha a janela de 120 s do dono: ação irreversível continua pedindo confirmação."""
    from core import actiongate
    monkeypatch.setattr(actiongate, "authorized", lambda *a, **k: False)
    yield


# ── harness ─────────────────────────────────────────────────────────────────

CATALOG = {"media_control", "open_app", "restart_assistant", "reminder",
           "hora", "weather_report"}

PROOF = {"call_id": "call-up10", "receipt_hmac": "sha256:up10",
         "verified": True}


class Clock:
    """Relógio monotônico controlado: o tempo total do evento é exato."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class Runner:
    """Runner injetável: devolve o fato, atrasa o relógio ou falha."""

    def __init__(self, *, fact=None, raises=None, advance_s: float = 0.0,
                 clock: Clock | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.fact = fact
        self.raises = raises
        self.advance_s = advance_s
        self.clock = clock

    def __call__(self, tool, args):
        self.calls.append((str(tool), dict(args or {})))
        if self.advance_s and self.clock is not None:
            self.clock.now += self.advance_s
        if self.raises is not None:
            raise self.raises
        if self.fact is not None:
            return dict(self.fact)
        return {"ok": True, "spoken": f"{tool} concluído", "detail": "up10",
                **PROOF}


def _config(**kw) -> FlowsConfig:
    base = dict(enabled=True, mouth_enabled=False, partial_prewarm=True,
                partial_speech=False, executor_enabled=True,
                executor_timeout_ms=4000, memory_enabled=False,
                log_events=True)
    base.update(kw)
    return FlowsConfig(**base)


def _harness(config=None, runner_kwargs=None, clock=None):
    """Orquestrador com log/on_event capturados; nada toca disco ou Qt."""
    clock = clock or Clock()
    runner = Runner(clock=clock, **(runner_kwargs or {}))
    executor = Executor(runner=runner, catalog=set(CATALOG), enabled=True)
    events: list[dict] = []
    lines: list[str] = []
    orch = Orchestrator(config or _config(), executor=executor, clock=clock,
                        log=lines.append, on_event=events.append)
    return orch, runner, clock, events, lines


def _payloads(events, kind):
    return [e for e in events if e.get("type") == kind]


# ── 1. toda execução gera o evento, com o que a auditoria pedia ─────────────

def test_despacho_gera_execution_complete_com_acoes_resultados_tempo():
    orch, runner, _, events, _ = _harness()
    plan = orch.begin("t1", "põe o Spotify")
    results = orch.run_plan(plan)

    assert runner.calls, "a ação precisava ter sido despachada"
    completed = _payloads(events, "execution_complete")
    assert len(completed) == 1
    event = completed[0]
    assert event["args"] == ["t1"]
    assert event["actions"] == ["open_app"]
    assert event["dispatched"] == 1
    assert event["route"] == "app"
    # resultados: uma linha por ação despachada, com o desfecho real
    assert len(event["results"]) == len(results) == 1
    summary = event["results"][0]
    assert summary["task_id"] == "k1"
    assert summary["state"] == "ok"
    assert summary["ok"] is True
    assert summary["facts"] == 1
    assert summary["spoken"] == "open_app concluído"
    assert summary["error"] == ""
    assert event["facts"] == 1
    assert event["ok_count"] == 1
    assert event["failures"] == 0
    assert isinstance(event["total_ms"], float)
    assert event["total_ms"] >= 0.0


def test_evento_e_o_ultimo_do_despacho_e_results_continua_existindo():
    """Regressão: o evento antigo ``results`` não foi substituído — veio antes."""
    orch, _, _, events, _ = _harness()
    plan = orch.begin("t1", "põe o Spotify")
    orch.run_plan(plan)

    kinds = [e["type"] for e in events if e["type"] in ("results",
                                                       "execution_complete")]
    assert kinds == ["results", "execution_complete"]


def test_muitas_acoes_geram_uma_linha_por_acao_no_evento():
    """Plano manual com duas ações: o evento cobre as duas, com k1 e k2."""
    orch, runner, _, events, _ = _harness()
    plan = TurnPlan(turn_id="t9", text="duas coisas",
                    decision=Decision(rule="app", actions=[
                        Action("open_app", {"name": "Spotify"}),
                        Action("weather_report", {"city": "Lisboa"})]))
    results = orch.run_plan(plan)

    event = _payloads(events, "execution_complete")[0]
    assert len(results) == 2
    assert [r.task_id for r in results] == ["k1", "k2"]
    assert event["actions"] == ["open_app", "weather_report"]
    assert event["dispatched"] == 2
    assert [s["task_id"] for s in event["results"]] == ["k1", "k2"]
    assert all(s["state"] == "ok" for s in event["results"])
    assert event["ok_count"] == 2 and event["failures"] == 0


# ── 2. o evento chega ao activity.log pelo canal do processo vivo ───────────

def test_evento_aparece_na_linha_de_log_do_processo_vivo():
    """O ``log`` injetado é o caminho main.py → ui.write_log → activity.log.

    O corpo da linha é o payload do orquestrador (``[Fluxos] {...}``), o mesmo
    formato dos eventos ``turn``/``brief``/``results`` que já enchem o
    activity.log vivo. Aqui ele é parseável: é a prova de que o evento é
    gravável e greppável pelo nome.
    """
    orch, _, _, _, lines = _harness()
    plan = orch.begin("t1", "põe o Spotify")
    orch.run_plan(plan)

    execution_lines = [ln for ln in lines if "execution_complete" in ln]
    assert len(execution_lines) == 1
    assert execution_lines[0].startswith("[Fluxos] ")
    payload = ast.literal_eval(execution_lines[0][len("[Fluxos] "):])
    assert payload["type"] == "execution_complete"
    assert payload["args"] == ["t1"]
    assert payload["actions"] == ["open_app"]
    assert payload["total_ms"] >= 0.0


def test_ouvinte_on_event_recebe_o_evento_sem_estourar():
    """Um ouvinte quebrado nunca derruba o turno (o ``_event`` é best-effort)."""
    boom: list[int] = []

    def exploding_on_event(payload):
        if payload.get("type") == "execution_complete":
            boom.append(1)
            raise RuntimeError("ouvinte caiu")

    clock = Clock()
    runner = Runner(clock=clock)
    executor = Executor(runner=runner, catalog=set(CATALOG), enabled=True)
    captured: list[dict] = []

    def log(msg):
        captured.append(ast.literal_eval(msg[len("[Fluxos] "):]))

    orch = Orchestrator(_config(), executor=executor, clock=clock, log=log,
                        on_event=exploding_on_event)
    plan = orch.begin("t1", "põe o Spotify")
    results = orch.run_plan(plan)

    assert boom == [1]
    assert results and results[0].ok is True
    assert [e["type"] for e in captured].count("execution_complete") == 1


# ── 3/4. quando não há execução, não há evento ──────────────────────────────

def test_sem_acoes_nao_gera_evento():
    orch, runner, _, events, _ = _harness()
    plan = orch.begin("t1", "e aí")
    assert plan.decision.actions == []

    assert orch.run_plan(plan) == []
    assert runner.calls == []
    assert _payloads(events, "execution_complete") == []


def test_turno_bloqueado_nao_gera_evento():
    """Turno não-confiável é barrado antes do despacho: nada foi executado."""
    orch, runner, _, events, _ = _harness()
    plan = orch.begin("t1", "põe o Spotify", trust="untrusted")
    assert plan.blocked is True

    assert orch.run_plan(plan) == []
    assert runner.calls == []
    assert _payloads(events, "execution_complete") == []


def test_fluxo_desligado_nao_gera_evento():
    orch, _, _, events, _ = _harness(config=_config(enabled=False))
    plan = orch.begin("t1", "põe o Spotify")
    assert plan.active is False
    assert orch.run_plan(plan) == []
    assert _payloads(events, "execution_complete") == []


def test_kill_switch_log_events_desliga_o_evento():
    """O portão global de eventos continua valendo para o novo também."""
    orch, runner, _, events, lines = _harness(config=_config(log_events=False))
    plan = orch.begin("t1", "põe o Spotify")
    results = orch.run_plan(plan)

    assert runner.calls and results
    assert events == [] and lines == []


# ── 5. cancelamento e confirmação pendente são execuções concluídas ─────────

def test_turno_cancelado_gera_evento_com_estado_cancelado():
    """Cancelado não é "sem execução": o despacho aconteceu e terminou cancelado."""
    orch, runner, _, events, _ = _harness()
    plan = orch.begin("t1", "põe o Spotify")
    assert orch.cancel("t1", "dono_cancelou")

    results = orch.run_plan(plan)
    assert runner.calls == []
    assert results and all(r.cancelled for r in results)

    event = _payloads(events, "execution_complete")[0]
    assert event["results"][0]["state"] == "cancelado"
    assert event["results"][0]["ok"] is False
    assert event["ok_count"] == 0
    assert event["failures"] == 1
    assert event["facts"] == 1


def test_cancel_event_exigido_pelo_chamador_tambem_notifica():
    orch, _, _, events, _ = _harness()
    plan = orch.begin("t1", "põe o Spotify")
    stop = threading.Event()
    stop.set()

    results = orch.run_plan(plan, cancel_event=stop)
    assert all(r.cancelled for r in results)
    assert _payloads(events, "execution_complete")[0]["results"][0][
        "state"] == "cancelado"


def test_confirmacao_pendente_gera_evento_sem_executar_a_ferramenta(
        no_owner_window):
    """Lei (b): nada executou, mas o despacho terminou — o cérebro é avisado."""
    orch, runner, _, events, _ = _harness()
    plan = orch.begin("t1", "reinicia o assistente aí")

    results = orch.run_plan(plan)
    assert runner.calls == []
    assert results[0].pending_confirm is True

    event = _payloads(events, "execution_complete")[0]
    assert event["actions"] == ["restart_assistant"]
    assert event["results"][0]["state"] == "confirmacao_pendente"
    # ok=True aqui é o portão respondendo, não a ferramenta rodando.
    assert event["results"][0]["ok"] is True
    assert "confirmação" in event["results"][0]["spoken"]
    # a frase que o dono tem de falar continua no fato (evento ``results``)
    assert "confirmo reiniciar" in results[0].facts[0]["pending_confirm"]
    assert event["dispatched"] == 1


def test_sem_prova_material_vira_estado_no_evento():
    """FASE3-TASK4/UP-9: o estado acompanha o portão temporal do bloqueio.

    Hoje o resultado é marcado sem prova; depois de 29/11/2026 ele passa a
    ser bloqueado. Os dois desfechos são execuções concluídas e notificadas.
    """
    orch, _, _, events, _ = _harness(
        runner_kwargs={"fact": {"ok": True, "spoken": "Spotify aberto",
                                "simulated": False}})
    plan = orch.begin("t1", "põe o Spotify")
    results = orch.run_plan(plan)

    event = _payloads(events, "execution_complete")[0]
    assert results[0].unverified is True
    assert event["results"][0]["state"] in ("sem_prova_material", "bloqueado")
    assert _payloads(events, "TURN_UNVERIFIED") or event["results"][0][
        "state"] == "bloqueado"


def test_erro_da_ferramenta_aparece_no_evento():
    orch, _, _, events, _ = _harness(
        runner_kwargs={"raises": RuntimeError("sem rede")})
    plan = orch.begin("t1", "põe o Spotify")
    results = orch.run_plan(plan)

    event = _payloads(events, "execution_complete")[0]
    summary = event["results"][0]
    assert results[0].ok is False
    assert summary["state"] == "falha"
    assert summary["ok"] is False
    assert "RuntimeError" in summary["error"]
    assert summary["facts"] == 1
    assert event["failures"] == 1
    assert event["ok_count"] == 0


def test_ferramenta_fora_do_catalogo_e_notificada_como_falha():
    """Recusa antes do efeito: ainda assim o cérebro fica sabendo do desfecho."""
    clock = Clock()
    runner = Runner(clock=clock)
    executor = Executor(runner=runner, catalog={"open_app"}, enabled=True)
    events: list[dict] = []
    orch = Orchestrator(_config(), executor=executor, clock=clock,
                        on_event=events.append)
    plan = TurnPlan(turn_id="t7", text="coisa estranha",
                    decision=Decision(rule="livre",
                                      actions=[Action("printer")]))

    results = orch.run_plan(plan)
    assert results[0].ok is False and runner.calls == []
    event = _payloads(events, "execution_complete")[0]
    assert event["results"][0]["state"] == "falha"
    assert "catálogo" in event["results"][0]["error"]


# ── 6. tempo total vem do relógio injetado do turno ────────────────────────

def test_tempo_total_do_evento_e_o_do_despacho():
    """O relógio do orquestrador é avançado pelo runner: o total é exato."""
    orch, runner, clock, events, _ = _harness(
        runner_kwargs={"advance_s": 1.5})
    plan = orch.begin("t1", "põe o Spotify")

    orch.run_plan(plan)
    event = _payloads(events, "execution_complete")[0]
    assert event["total_ms"] == 1500.0
    assert runner.clock is clock


def test_tempo_total_com_relogio_parado_e_zero():
    orch, _, _, events, _ = _harness()
    plan = orch.begin("t1", "põe o Spotify")
    orch.run_plan(plan)
    assert _payloads(events, "execution_complete")[0]["total_ms"] == 0.0


# ── 7. helper de estado: vocabulário fixo, sem inventar nada ────────────────

def test_estado_do_resulto_segue_a_verdade_do_portao():
    assert _result_state(ExecutorResult(task_id="a", ok=True)) == "ok"
    assert _result_state(ExecutorResult(
        task_id="a", ok=True, cancelled=True)) == "cancelado"
    assert _result_state(ExecutorResult(
        task_id="a", ok=True, blocked=True)) == "bloqueado"
    assert _result_state(ExecutorResult(
        task_id="a", ok=True, pending_confirm=True)) == "confirmacao_pendente"
    assert _result_state(ExecutorResult(
        task_id="a", ok=True, unverified=True)) == "sem_prova_material"
    assert _result_state(ExecutorResult(task_id="a", ok=False)) == "falha"
    assert _result_state(ExecutorResult(task_id="a")) == "falha"


def test_resumo_nao_inventa_fato_e_corta_texto_grande():
    results = [
        ExecutorResult(task_id="k1", ok=True, facts=[]),
        ExecutorResult(task_id="k2", facts=[{"detail": "sem fala"}]),
        ExecutorResult(task_id="k3", facts=[{"spoken": ""},
                                            {"spoken": "x" * 300}]),
        ExecutorResult(task_id="k4", facts=["lixo"]),
        ExecutorResult(task_id="k5", ok=True, error="y" * 300,
                       facts=[{"spoken": "  muitas   palavras   aqui  "}]),
    ]
    summaries = _execution_summary(results)

    assert [s["task_id"] for s in summaries] == ["k1", "k2", "k3", "k4", "k5"]
    assert summaries[0]["facts"] == 0 and summaries[0]["spoken"] == ""
    assert summaries[1]["spoken"] == ""
    assert len(summaries[2]["spoken"]) == _EVENT_TEXT_CHARS
    assert summaries[3]["facts"] == 1 and summaries[3]["spoken"] == ""
    # espaços duplicados somem: a linha do log continua uma linha só
    assert summaries[4]["spoken"] == "muitas palavras aqui"
    assert len(summaries[4]["error"]) == _EVENT_TEXT_CHARS
    assert _result_spoken(ExecutorResult(task_id="k")) == ""


def test_evento_nao_altera_o_que_run_plan_devolve():
    """Contrato antigo preservado: a notificação não mexe no resultado.

    Roda o mesmo despacho com o log de eventos ligado e desligado; o que
    ``run_plan`` devolve tem de ser igual nos dois mundos.
    """
    orch_log, _, _, events, _ = _harness()
    plan_log = orch_log.begin("t1", "põe o Spotify")
    with_event = orch_log.run_plan(plan_log)

    orch_off, _, _, events_off, _ = _harness(config=_config(log_events=False))
    plan_off = orch_off.begin("t1", "põe o Spotify")
    without_event = orch_off.run_plan(plan_off)

    assert [(r.task_id, r.ok, r.cancelled, r.pending_confirm, r.unverified,
             r.blocked, r.facts, r.error) for r in with_event] == [
        (r.task_id, r.ok, r.cancelled, r.pending_confirm, r.unverified,
         r.blocked, r.facts, r.error) for r in without_event]
    assert with_event[0].facts[0]["assertion"] is True
    assert len(_payloads(events, "execution_complete")) == 1
    assert events_off == []
