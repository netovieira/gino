"""Frente local: conversa curta na boca, raciocínio no cérebro.

Decisão do dono (03/11/2026, medida 05/11/2026): o cérebro remoto leva ~16 s
para a primeira frase; bate-papo puro sai da boca local em ~2 s. Os testes
aqui travam a política conservadora do roteador e a lei inegociável: a boca
NUNCA afirma resultado de ação — se o guard derruba a frase, o turno volta
ao cérebro como hoje. Nada aqui toca Ollama nem rede: o caller é injetado.
"""
from __future__ import annotations

import threading
import time

from core.flows.config import FlowsConfig
from core.flows.live import FlowsRuntime
from core.flows.mouth import Mouth
from core.flows.router import Router, short_chat_reason


def _config(**kw):
    base = dict(enabled=True, mouth_enabled=True, local_chat_enabled=True,
                partial_prewarm=False, partial_speech=False,
                executor_enabled=False, log_events=False)
    base.update(kw)
    return FlowsConfig(**base)


def _ok_caller(messages, max_tokens, **kw):
    return {"text": "Bem, criador; máquina alegre e sem incêndio hoje.",
            "ttft_ms": 20.0, "first_sentence_ms": 40.0, "end_ms": 60.0,
            "error": None}


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        self.t += 0.05
        return self.t


# ── política do roteador ────────────────────────────────────────────────────

CHAT_CURTA = [
    "oi", "bom dia", "boa noite", "tudo bem?", "e aí, tudo bem com você?",
    "como você está?", "você está bem?", "quem é você?",
    "o que você acha de você", "repete o que eu disse",
    "o que você disse agora?", "obrigado", "valeu", "tchau, até amanhã",
    "como foi seu dia?",
]
CEREBRO = [
    "abre o spotify",                    # ferramenta
    "toca queen",                         # ferramenta
    "que horas são?",                     # consulta
    "me lembra de pagar o aluguel",       # ferramenta
    "reinicia o assistente",              # confirmação (lei b)
    "o que você acha do filme?",          # opinião com conteúdo externo
    "quanto é dois mais dois?",           # cálculo
    "como está o clima hoje?",            # fato do mundo
    "o que eu pedi ontem?",               # memória antiga
    "aquele negócio de novo",             # ambíguo
    "pesquisa o preço do notebook",       # ferramenta/busca
    "",                                   # vazio
]


def test_conversa_curta_fecha_para_a_boca():
    router = Router()
    for text in CHAT_CURTA:
        decision = router.parse(text)
        assert short_chat_reason(text, decision), (text, decision.route)


def test_pedido_de_ferramenta_consulta_e_duvida_vao_ao_cerebro():
    router = Router()
    for text in CEREBRO:
        decision = router.parse(text)
        assert short_chat_reason(text, decision) == "", (text, decision.route)


def test_rota_de_boca_exige_decisao_de_regra_sem_acao():
    # Sem decisão (nem fallback, nem ferramenta) o padrão é o cérebro.
    assert short_chat_reason("oi", None) == "saudacao"
    assert short_chat_reason("abre o spotify", None) == ""


# ── boca: lei (a) inegociável ───────────────────────────────────────────────

def test_boca_responde_conversa_curta():
    mouth = Mouth(_config(), caller=_ok_caller)
    decision = Router().parse("como você está?")
    result = mouth.answer("como você está?", decision)
    assert result.sentence == "Bem, criador; máquina alegre e sem incêndio hoje."
    assert result.fixed is False
    # nenhuma ferramenta oferecida no prompt da conversa
    assert "qoder" not in str(result.raw).lower()


def test_boca_jamais_afirma_resultado_de_acao():
    def caller(*_a, **_kw):
        return {"text": "Spotify aberto, criador.", "ttft_ms": 5.0,
                "first_sentence_ms": 5.0, "end_ms": 5.0, "error": None}

    mouth = Mouth(_config(), caller=caller)
    decision = Router().parse("oi")
    result = mouth.answer("oi", decision)
    assert result.sentence == ""           # lei (a): nada vira fala sem ferramenta
    assert result.guard_tripped
    assert mouth.guard_trips == 1


def test_boca_recusa_numero_e_gerundio_de_andamento():
    for inventada in ["Estou há 2 horas online.", "Conferindo o pedido, criador."]:
        def caller(*_a, _inv=inventada, **_kw):
            return {"text": _inv, "ttft_ms": 1.0, "first_sentence_ms": 1.0,
                    "end_ms": 1.0, "error": None}

        mouth = Mouth(_config(), caller=caller)
        assert mouth.answer("oi", Router().parse("oi")).sentence == ""


def test_boca_fora_do_ar_devolve_frase_vazia_sem_template():
    def caller(*_a, **_kw):
        return {"text": "", "ttft_ms": 0.0, "first_sentence_ms": 0.0,
                "end_ms": 1.0, "error": "ConnectionError"}

    mouth = Mouth(_config(), caller=caller)
    result = mouth.answer("oi", Router().parse("oi"))
    assert result.sentence == ""
    assert result.fixed is True           # sem fallback inventado: vazio


def test_config_desligada_rota_nao_fecha():
    mouth = Mouth(_config(local_chat_enabled=False), caller=_ok_caller)
    result = mouth.answer("oi", Router().parse("oi"))
    assert result.sentence == ""
    assert mouth.calls == 0               # nem tocou o modelo


# ── runtime: brief suprimido, resposta falada, medição por turno ────────────

def _runtime(spoken, logs=None, **cfgkw):
    clock = _Clock()
    cfg = _config(**cfgkw)
    rt = FlowsRuntime(cfg, speak=spoken.append, log=(logs or []).append,
                      clock=clock, start_expiry_thread=False)
    rt.orchestrator.mouth = Mouth(cfg, caller=_ok_caller,
                                  history=lambda: [])
    return rt


def _wait(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not pred():
        time.sleep(0.01)
    return pred()


def test_turno_de_boca_nao_fala_brief_e_responde_com_a_frase_real():
    spoken = []
    rt = _runtime(spoken)
    plan = rt.start_turn("t1", "como você está?", armed=True)
    assert plan.local_chat == "bate-papo"
    assert _wait(lambda: len(spoken) == 1)
    assert spoken == ["Bem, criador; máquina alegre e sem incêndio hoje."]
    info = rt.await_local_chat("t1")
    assert info and info["reason"] == "bate-papo"
    assert info["local_chat_ms"] > 0
    assert len(spoken) == 1               # sem dupla fala
    rt.finish("t1")


def test_turno_falado_so_depois_do_voiceprint():
    spoken = []
    rt = _runtime(spoken)
    plan = rt.start_turn("t1", "oi", armed=False)
    assert plan.local_chat == "saudacao"
    _wait(lambda: rt._turns["t1"].chat_event is not None
          and rt._turns["t1"].chat_event.is_set())
    assert spoken == []                    # ainda não foi ao ar
    rt.arm_speech("t1", True)
    assert spoken == ["Bem, criador; máquina alegre e sem incêndio hoje."]
    rt.finish("t1")


def test_rota_de_cerebro_nao_gera_boca_de_conversa():
    spoken = []
    rt = _runtime(spoken)
    plan = rt.start_turn("t1", "abre o spotify")
    assert plan.local_chat == ""
    assert rt.await_local_chat("t1") is None
    rt.finish("t1")


def test_guard_que_devolve_falha_devolve_o_turno_ao_cerebro():
    spoken = []
    logs = []
    cfg = _config()
    rt = FlowsRuntime(cfg, speak=spoken.append, log=logs.append,
                      clock=_Clock(), start_expiry_thread=False,
                      )

    def afirmar(messages, max_tokens, **kw):
        # conteúdo de conversa (brief) e afirmação (answer): o guard da
        # resposta cai e o ack determinístico do brief assume
        if "Sua resposta:" in messages[-1]["content"]:
            return {"text": "Tudo pronto, criador.", "ttft_ms": 1.0,
                    "first_sentence_ms": 1.0, "end_ms": 1.0, "error": None}
        return {"text": "", "ttft_ms": 0.0, "first_sentence_ms": 0.0,
                "end_ms": 0.0, "error": None}

    rt.orchestrator.mouth = Mouth(cfg, caller=afirmar, history=lambda: [])
    plan = rt.start_turn("t1", "oi", armed=True)
    assert plan.local_chat == "saudacao"
    assert _wait(lambda: rt._turns["t1"].chat_event.is_set())
    assert rt.await_local_chat("t1") is None
    assert spoken == ["Tudo em ordem por aqui, criador."]
    rt.finish("t1")
