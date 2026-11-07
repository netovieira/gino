"""UC-2: latência — teto de 30 s para o cérebro + boca nunca muda de papel.

Contrato medido (task file PRIORIDADE1-UC2, auditoria gap #2):

  * 10 turnos consecutivos com o turno pertencendo ao cérebro têm
    ``primeira_unidade_ms < 30000`` registrado como métrica e2e;
  * a boca dá o ack intermediário determinístico em <5 s (portão do
    ``interim_ack_ms``) quando nada saiu no ar — o dono não espera 44 s
    em silêncio;
  * a métrica ponta-a-ponta vai ao activity.log (formato canônico
    ``[Fluxos] latencia e2e caminho=... primeira_unidade_ms=...``);
  * o caminho rápido da boca (brief/chat) paga timeout REDUZIDO do Ollama,
    nunca os 25 s do complement.

Determinístico: sem rede, sem Ollama; caller da boca, relógio e
``latency_log`` injetados — os testes nunca tocam o log real.
"""
from __future__ import annotations

import re
import time

import pytest

from core.flows import guard as _guard
from core.flows.config import FlowsConfig
from core.flows.live import FlowsRuntime
from core.flows.mouth import (FAST_ACK_TIMEOUT_S, INTERIM_ACK_MS,
                              INTERIM_SENTENCE, Mouth, stream_ollama)
from core.flows.router import Router

_CEIL = 30_000.0


def _cfg(**kw):
    base = dict(enabled=True, mouth_enabled=True, local_chat_enabled=True,
                partial_prewarm=False, partial_speech=False,
                executor_enabled=False, log_events=False)
    base.update(kw)
    return FlowsConfig(**base)


def _wait(pred, timeout=2.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not pred():
        time.sleep(0.01)
    return pred()


def _result(text="Certo, criador.", sleep_s=0.0, error=None):
    def caller(messages, max_tokens, **kw):
        if sleep_s:
            time.sleep(sleep_s)
        return {"text": text if not error else "", "ttft_ms": 5.0,
                "first_sentence_ms": 8.0, "end_ms": 12.0, "error": error}
    return caller


def _runtime(cfg=None, *, caller=None, interim_ack_ms=80.0, clock=None,
             speak=None, latency=None, logs=None):
    cfg = cfg or _cfg()
    spoken = [] if speak is None else speak
    lat = [] if latency is None else latency
    lg = [] if logs is None else logs
    kw = {}
    if clock is not None:
        kw["clock"] = clock
    rt = FlowsRuntime(cfg, speak=spoken.append, log=lg.append,
                      latency_log=lat.append, interim_ack_ms=interim_ack_ms,
                      start_expiry_thread=False, **kw)
    rt.orchestrator.mouth = Mouth(cfg, caller=caller or _result(),
                                  history=lambda: [])
    return rt, spoken, lat, lg


def _cerebro_lines(lat):
    out = []
    for line in lat:
        m = re.search(r"latencia e2e caminho=cerebro "
                      r"primeira_unidade_ms=(\d+).*status=(\w+)", line)
        if m:
            out.append((float(m.group(1)), m.group(2)))
    return out


# ── critério de aceitação: 10 turnos cerebro, primeira unidade < 30 s ──────

def test_dez_turnos_cerebro_primeira_unidade_abaixo_do_teto():
    # Turnos de AÇÃO (conteúdo é do cérebro) com a boca presa: sem o portão
    # intermediário o dono esperaria o modelo em silêncio; com ele, a
    # primeira unidade sai no teto do watchdog e a métrica e2e é registrada.
    lat = []
    rt, spoken, lat, _logs = _runtime(
        caller=_result(text="", sleep_s=0.30, error="ConnectionError"),
        interim_ack_ms=80.0, latency=lat)
    for i in range(10):
        tid = f"t{i}"
        rt.start_turn(tid, "abre o spotify", armed=True)
        base = len(spoken)
        assert _wait(lambda: len(spoken) >= base + 1), \
            f"turno {tid}: nenhuma unidade de fala saiu"
        assert spoken[-1] == INTERIM_SENTENCE
        # o cérebro devolve o conteúdo logo (simulado): a métrica do caminho
        # cerebro registra a primeira unidade real do turno.
        rt.mark_answered(tid)
        rt.finish(tid)
    linhas = _cerebro_lines(lat)
    assert len(linhas) == 10
    for ms, status in linhas:
        assert ms < _CEIL, f"primeira_unidade_ms={ms} estourou o teto"
        assert status == "OK"
    assert all("teto_ms=30000" in line for line in lat
               if "latencia e2e caminho=" in line)


# ── métrica no vivo: o caso do bug (44 s) é marcado ACIMA_DO_TETO ───────────

def test_metrica_cerebro_lenta_registra_acima_do_teto():
    now = [1000.0]
    rt, spoken, lat, _logs = _runtime(
        interim_ack_ms=0.0, clock=lambda: now[0])
    # speak_allowed=False: nenhum worker fala; a primeira unidade é do cérebro.
    rt.start_turn("t1", "pesquisa o preco do bitcoin", speak_allowed=False)
    now[0] += 44.0  # o 44 s medido no log da auditoria
    rt.mark_answered("t1")
    linhas = _cerebro_lines(lat)
    assert len(linhas) == 1
    ms, status = linhas[0]
    assert ms == pytest.approx(44000.0, rel=0.01)
    assert status == "ACIMA_DO_TETO"
    assert "unidade=cerebro" in lat[0]
    assert "cerebro_primeiro_texto_ms=" in lat[0]


def test_metrica_cerebro_dentro_do_teto_registra_ok():
    now = [1000.0]
    rt, _spoken, lat, _logs = _runtime(
        interim_ack_ms=0.0, clock=lambda: now[0])
    rt.start_turn("t1", "pesquisa o preco do bitcoin", speak_allowed=False)
    now[0] += 12.0
    rt.mark_answered("t1")
    ms, status = _cerebro_lines(lat)[0]
    assert ms == pytest.approx(12000.0, rel=0.01)
    assert status == "OK"


# ── passo 2: o ack intermediário em <5 s, com todas as travas ───────────────

def test_portao_intermediario_padrao_e_cinco_segundos():
    rt, _spoken, _lat, _logs = _runtime(interim_ack_ms=None)
    assert rt._interim_ms == INTERIM_ACK_MS == 5000.0


def test_interim_nao_dispara_quando_a_boca_ja_falou():
    rt, spoken, lat, logs = _runtime(caller=_result(text="Certo, criador."),
                                     interim_ack_ms=120.0)
    rt.start_turn("t1", "abre o spotify", armed=True)
    assert _wait(lambda: len(spoken) == 1)
    assert spoken[0] == "Certo, criador."
    time.sleep(0.4)  # o portão venceu o brief? não: ele já falou antes
    assert len(spoken) == 1
    assert not any("intermediaria" in line for line in logs)
    assert "unidade=brief" in lat[0]
    rt.finish("t1")


def test_interim_calado_quando_o_cerebro_responde_primeiro():
    rt, spoken, _lat, logs = _runtime(interim_ack_ms=150.0)
    # O cérebro responde ANTES de a boca começar: mark_answered sob o lock,
    # o mesmo padrão de corrida da regra A (test_flows_live).
    with rt._lock:
        rt.start_turn("t1", "abre o spotify", armed=True)
        rt.mark_answered("t1")
    time.sleep(0.45)
    assert spoken == []
    assert not any("intermediaria" in line for line in logs)
    rt.finish("t1")


def test_interim_desarmado_voz_dono_decidindo_nao_fala():
    rt, spoken, _lat, _logs = _runtime(interim_ack_ms=60.0)
    rt.start_turn("t1", "abre o spotify", armed=False)
    time.sleep(0.3)
    assert spoken == []
    rt.finish("t1")


def test_interim_cancelado_por_finish_e_por_cancel():
    # Modelo preso (300 ms): sem o cancelamento do watchdog, a frase saindo
    # depois do fim do turno ainda poderia falar; o timer morre com o turno.
    rt, spoken, _lat, _logs = _runtime(
        caller=_result(text="Certo, criador.", sleep_s=0.3),
        interim_ack_ms=80.0)
    rt.start_turn("t1", "abre o spotify", armed=True)
    rt.finish("t1")
    rt.start_turn("t2", "fecha o spotify", armed=True)
    rt.cancel("t2", "barulho novo")
    time.sleep(0.45)
    assert spoken == []


def test_interim_nao_rouba_turno_de_conversa_curta():
    # Conversa pura: o chat worker fala; o portão só cobre silêncio real.
    rt, spoken, _lat, logs = _runtime(caller=_result(
        text="Oi, criador; tudo quieto por aqui."), interim_ack_ms=150.0)
    rt.start_turn("t1", "como você está?", armed=True)
    assert _wait(lambda: len(spoken) == 1)
    time.sleep(0.4)
    assert len(spoken) == 1
    assert not any("intermediaria" in line for line in logs)
    rt.finish("t1")


# ── a lei do dono (a) não se afrouxa: o intermediário é ack, não anúncio ────

def test_frase_intermediaria_passa_pelos_portoes_da_boca():
    assert _guard.announcement(INTERIM_SENTENCE) == ""
    ok, why = _guard.guard(INTERIM_SENTENCE, None, "brief")
    assert ok, why


# ── passo 4: timeout do Ollama reduzido só no caminho rápido ────────────────

def test_timeout_redondo_do_caminho_rapido_brief_e_chat():
    kw_brief, kw_chat, kw_compl = {}, {}, {}
    cfg = _cfg()

    def captura(dest):
        def caller(messages, max_tokens, **kw):
            dest.update(kw)
            return {"text": "Certo, criador.", "ttft_ms": 1.0,
                    "first_sentence_ms": 2.0, "end_ms": 3.0, "error": None}
        return caller

    m = Mouth(cfg, caller=captura(kw_brief))
    m.brief("tocar Queen", Router().parse("tocar Queen"))
    assert kw_brief["timeout_s"] <= FAST_ACK_TIMEOUT_S
    assert kw_brief["deadline_s"] <= FAST_ACK_TIMEOUT_S

    m2 = Mouth(cfg, caller=captura(kw_chat))
    m2.answer("como você está?", Router().parse("como você está?"))
    assert kw_chat["timeout_s"] <= FAST_ACK_TIMEOUT_S
    assert kw_chat["deadline_s"] <= FAST_ACK_TIMEOUT_S

    m3 = Mouth(cfg, caller=captura(kw_compl))
    m3.complement("abrir o Spotify", Router().parse("abrir o Spotify"),
                  [{"spoken": "Spotify aberto", "assertion": True}])
    assert kw_compl["timeout_s"] == cfg.mouth_timeout_s  # 25 s: pós-fato
    assert "deadline_s" not in kw_compl


def test_stream_ollama_aceita_deadline_assinatura():
    import inspect
    params = inspect.signature(stream_ollama).parameters
    assert "deadline_s" in params
    assert params["deadline_s"].default is None


# ── passo 6: métrica ponta-a-ponta com detalhamento por etapa ───────────────

def test_finish_registra_breakdown_ponta_a_ponta():
    rt, _spoken, lat, _logs = _runtime(interim_ack_ms=0.0)
    rt.start_turn("t1", "abre o spotify", armed=True)
    time.sleep(0.05)
    rt.finish("t1")
    breakdown = [line for line in lat if "latencia e2e turno=" in line]
    assert len(breakdown) == 1
    for chave in ("total_ms=", "rota_ms=", "fallback_ms=", "memoria_ms=",
                  "boca_brief_ms="):
        assert chave in breakdown[0], chave


def test_primeira_unidade_da_boca_tambem_vira_linha_e2e():
    rt, spoken, lat, _logs = _runtime(
        caller=_result(text="", sleep_s=0.25, error="ConnectionError"),
        interim_ack_ms=60.0)
    rt.start_turn("t1", "abre o spotify", armed=True)
    assert _wait(lambda: len(spoken) == 1)
    assert any(re.search(r"latencia e2e caminho=boca primeira_unidade_ms="
                         r"(\d+) teto_ms=30000 unidade=interim", line)
               for line in lat)
    rt.finish("t1")
