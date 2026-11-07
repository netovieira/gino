"""UV-1 — métrica "nunca ignorar": perguntas do dono sem resposta.

O barge-in foi provado (11+ "interrompido por voz" no activity.log), mas
"zero perguntas sem resposta" não era mensurável. Este arquivo trava o
contrato do contador ``questions_without_answer`` do ``Router``:

  (a) cada pergunta roteada do dono incrementa, no máximo uma vez por turno;
  (b) cada resposta entregue (cérebro, boca de conversa curta, conteúdo do
      parcial) decrementa — e resposta de turno nunca contado é no-op;
  (c) cancelamento/preempt não deixa dívida (contrato "nada morre em
      silêncio": a pergunta preemptada vira contexto do turno seguinte);
  (d) a cada janela (6 h no vivo) o valor vai ao activity.log na linha
      ``[Fluxos] metrica perguntas_sem_resposta valor=N``.

Determinístico: sem Ollama, sem rede, sem thread de supervisor; caller da
boca, relógio e logs injetados.
"""
from __future__ import annotations

import time

from core.flows.config import FlowsConfig
from core.flows.live import QWA_LOG_INTERVAL_S, FlowsRuntime
from core.flows.mouth import Mouth
from core.flows.orchestrator import Orchestrator
from core.flows.router import Router


def _cfg(**kw):
    base = dict(enabled=True, mouth_enabled=True, local_chat_enabled=True,
                partial_prewarm=False, partial_speech=False,
                executor_enabled=False, log_events=False)
    base.update(kw)
    return FlowsConfig(**base)


def _orch(**kw):
    cfg = _cfg(mouth_enabled=False, local_chat_enabled=False, **kw)
    return Orchestrator(cfg)


def _ok_caller(messages, max_tokens, **kw):
    return {"text": "Bem, criador; máquina alegre e sem incêndio hoje.",
            "ttft_ms": 20.0, "first_sentence_ms": 40.0, "end_ms": 60.0,
            "error": None}


def _wait(pred, timeout=2.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not pred():
        time.sleep(0.01)
    return pred()


class _Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        self.t += 0.05
        return self.t


# ── (a)(b) o contador do roteador ───────────────────────────────────────────

def test_pergunta_incrementa_resposta_decrementa():
    r = Router()
    assert r.questions_without_answer == 0
    r.note_question("t1")
    r.note_question("t2")
    assert r.questions_without_answer == 2
    r.note_answer("t1")
    assert r.questions_without_answer == 1
    r.note_answer("t2")
    assert r.questions_without_answer == 0


def test_mesma_pergunta_nao_duplica_e_rebaixa_nunca_passa_de_zero():
    r = Router()
    r.note_question("t1")
    r.note_question("t1")  # re-begin do mesmo turno: não duplica
    assert r.questions_without_answer == 1
    r.note_answer("t1")
    r.note_answer("t1")  # resposta repetida: no-op, contador não fica negativo
    r.note_answer("turno-que-nunca-foi-contado")
    assert r.questions_without_answer == 0


def test_turno_vazio_nao_abre_nem_fecha_divida():
    r = Router()
    r.note_question("")
    r.note_answer("")
    assert r.questions_without_answer == 0


# ── (a)(c) ligações do orquestrador ─────────────────────────────────────────

def test_begin_do_dono_abre_divida_e_mark_answered_baixa():
    o = _orch()
    o.begin("t1", "que horas são?")
    assert o.router.questions_without_answer == 1
    o.begin("t1", "que horas são?")  # mesmo turno não conta duas vezes
    assert o.router.questions_without_answer == 1
    o.mark_answered("t1")
    o.mark_answered("t1")  # idempotente
    assert o.router.questions_without_answer == 0
    assert o.metrics()["router"]["questions_without_answer"] == 0


def test_duas_perguntas_uma_resposta_deixa_uma_dívida():
    o = _orch()
    o.begin("t1", "abre o spotify")
    o.begin("t2", "que horas são?")
    o.mark_answered("t2")
    assert o.router.questions_without_answer == 1
    assert o.metrics()["router"]["questions_without_answer"] == 1


def test_cancelamento_e_preempt_nao_deixam_divida():
    o = _orch()
    o.begin("t1", "abre o spotify")
    o.cancel("t1", "preempt")  # dono falou por cima: carryover responde junto
    assert o.router.questions_without_answer == 0


def test_fala_de_cancelamento_e_evento_nao_contam_pergunta():
    o = _orch()
    o.begin("t1", "deixa quieto")  # decisão cancel: retira pedido, não pergunta
    assert o.router.questions_without_answer == 0
    o.begin("t2", "oi", trust="untrusted")  # lei (c): bloqueado não é pergunta
    assert o.router.questions_without_answer == 0


# ── (b) resposta da boca baixa a dívida pela ponte viva ─────────────────────

def test_conversa_curta_da_boca_baixa_a_divida():
    cfg = _cfg()
    spoken: list[str] = []
    logs: list[str] = []

    def caller(messages, max_tokens, **kw):
        time.sleep(0.15)  # a boca é mais lenta que o assert: dá para ver a
        return _ok_caller(messages, max_tokens, **kw)  # dívida aberta

    rt = FlowsRuntime(cfg, speak=spoken.append, log=logs.append,
                      start_expiry_thread=False, interim_ack_ms=0,
                      latency_log=lambda _line: None)
    rt.orchestrator.mouth = Mouth(cfg, caller=caller)
    rt.start_turn("t1", "como você está?")
    assert rt.orchestrator.router.questions_without_answer == 1
    assert _wait(lambda: bool(spoken)), "a boca não respondeu a conversa curta"
    assert rt.orchestrator.router.questions_without_answer == 0
    # Resposta do cérebro atrás da boca não volta a baixar duas vezes.
    rt.mark_answered("t1")
    assert rt.orchestrator.router.questions_without_answer == 0


def test_brief_de_ack_nao_baixa_a_divida():
    # Rota de ferramenta: o brief é ack (lei a); a dívida só fecha com a
    # resposta do cérebro.
    cfg = _cfg(local_chat_enabled=False)
    spoken: list[str] = []
    rt = FlowsRuntime(cfg, speak=spoken.append, log=lambda _m: None,
                      start_expiry_thread=False, interim_ack_ms=0,
                      latency_log=lambda _line: None)
    rt.orchestrator.mouth = Mouth(cfg, caller=_ok_caller)
    rt.start_turn("t1", "abre o spotify")
    assert _wait(lambda: bool(spoken)), "o brief não saiu"
    assert rt.orchestrator.router.questions_without_answer == 1
    rt.mark_answered("t1")
    assert rt.orchestrator.router.questions_without_answer == 0


# ── (d) o valor vai ao log a cada janela (6 h) ──────────────────────────────

def test_metrica_e_logada_a_cada_janela_de_seis_horas():
    clock = _Clock()
    logs: list[str] = []
    rt = FlowsRuntime(_cfg(mouth_enabled=False, local_chat_enabled=False),
                      speak=None, log=logs.append, clock=clock,
                      start_expiry_thread=False, latency_log=lambda _l: None)
    assert rt._qwa_interval_s == QWA_LOG_INTERVAL_S == 6 * 3600.0
    rt.orchestrator.begin("t1", "que horas são?")

    base = clock.t
    assert rt.qa_metric_tick(now=base + QWA_LOG_INTERVAL_S - 1) is False
    assert logs == []
    assert rt.qa_metric_tick(now=base + QWA_LOG_INTERVAL_S) is True
    assert any("[Fluxos] metrica perguntas_sem_resposta valor=1" in line
               for line in logs), logs
    # Janela seguinte só depois de mais 6 h; entretanto, nada de linha nova.
    assert rt.qa_metric_tick(now=base + 2 * QWA_LOG_INTERVAL_S - 1) is False
    assert rt.qa_metric_tick(now=base + 2 * QWA_LOG_INTERVAL_S) is True
    assert sum("perguntas_sem_resposta" in line for line in logs) == 2

    # Valor zero quando tudo foi respondido — a métrica de sucesso do UV-1.
    rt.mark_answered("t1")
    rt.qa_metric_tick(now=base + 3 * QWA_LOG_INTERVAL_S)
    assert logs[-1].startswith("[Fluxos] metrica perguntas_sem_resposta "
                               "valor=0"), logs[-1]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
