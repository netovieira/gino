"""Fluxo FALA do dono (05/11/2026, passos 2/3): boca superficial + cérebro junto.

Expectativas travadas aqui, na letra do plano:

  2. "BOCA OUVE RESPONDE SUPERFICIALMENTE (COM O CONTEXTO DOS ULTIMOS 15
     MINUTOS) (MENOS DE 1S DE ESPERA)"
  3. "CEREBRO OUVE (NO MESMO INSTANTE QUE A BOCA) ..."

Os três crimes que a auditoria provou e este arquivo não deixa voltar:

  (a) em conversa curta o cérebro era PULADO (`return` do veredito da boca);
  (b) o cérebro esperava o veredito da boca (`await` de 4 s) — se a boca
      atrasasse, o turno ficava preso atrás dela ("SE A BOCA FOR ESPERAR O
      CEREBRO FICA IGUAL ERA ANTES", lei do dono);
  (c) o contexto da boca era de DUAS horas com teto 6, não 15 minutos.

Mais a rota de conversa curta, que estava morta: os padrões ampliados com
evidência do activity log de 05/11/2026 (43 turnos ``type=turn``) casam
conversa pura e NÃO casam ação/consulta/número/confirmação (d). E a prova de
que boca e cérebro no mesmo turno nunca falam duas vezes a mesma coisa (e).

Determínístico: sem rede, sem Ollama; caller da boca e sidecar injetados.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

import main
from core.flows.config import FlowsConfig
from core.flows.live import FlowsRuntime
from core.flows.mouth import Mouth
from core.flows.router import Router, short_chat_reason

PCM = b"\x01\x00" * 4000


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


class FakeUI:
    def __init__(self):
        self.logs = []

    def write_log(self, text):
        self.logs.append(str(text))

    def set_state(self, _state):
        pass


class FakeSpeaker:
    owner_name = "Anthero"

    def list(self):
        return [{"name": "Anthero", "owner": True, "authorized": True}]

    def analyze_utterance(self, _pcm, transcribe=None, full_text=""):
        return {
            "labeled_text": full_text,
            "primary": {"known": True, "authorized": True,
                        "verdict": "known", "effective_score": 0.9},
            "primary_name": "Anthero", "segments": [],
        }


class FakeLive:
    def __init__(self):
        self.asks = []

    def ask(self, payload, **kw):
        self.asks.append((payload, kw, time.monotonic()))


class FakeMemory:
    def __init__(self, turns=12):
        self.hours = None
        self.turns = [{"role": "user" if i % 2 else "assistant",
                       "text": f"fala numero {i}"} for i in range(turns)]

    def recent_turns(self, hours=6.0, now=None):
        self.hours = hours
        return list(self.turns)

    def append_turn(self, role, text):
        self.turns.append({"role": role, "text": text})


def _jarvis_with_flows(mouth_caller, sleep_s=0.0):
    """JarvisLive mínimo com runtime de fluxos REAL (padrão do repo)."""
    obj = object.__new__(main.JarvisLive)
    obj._speaker = FakeSpeaker()
    obj._voice_id_enabled = True
    obj._power = None
    obj._asst_name = "Gino"
    obj._qoder_lock = threading.RLock()
    obj._qoder_current_id = None
    obj._qoder_turns = {}
    obj._qoder_text_buf = {}
    obj._qoder_spoken = {}
    obj._qoder_meta = {}
    obj._qoder_live = FakeLive()
    obj._qoder_ignore = False
    obj._qoder_retry_used = False
    obj._qoder_first_sentence = False
    obj._qoder_tool_count = 0
    obj._turn_ref = 3
    obj._turn_input_chan = "voz"
    obj._turn_done_event = None
    obj._loop = None
    obj._reply_active = False
    obj._brain_busy = False
    obj._awake = True
    obj._chat_memory = FakeMemory()
    obj.ui = FakeUI()
    obj.indexed = []
    obj.spoken = []
    obj.events_lock = threading.Lock()

    def record_say(text, **kw):
        with obj.events_lock:
            obj.spoken.append((str(text), time.monotonic()))

    obj._say_now = record_say
    obj._index_event = lambda role, text, **kw: obj.indexed.append(text)
    obj._device_enroll_cancel_gate = lambda *a, **k: False
    obj._restart_answer_expected = lambda *a, **k: False
    obj._mail_send_answer_expected = lambda *a, **k: False
    obj._owner_direct_action_gate = lambda *a, **k: False
    obj._cinema_mode_gate = lambda *a, **k: False
    obj._silence_mode_gate = lambda *a, **k: False
    obj._series_mode_gate = lambda *a, **k: False
    obj._mirror_show_gate = lambda *a, **k: False
    obj._try_action_consent = lambda *a, **k: False
    obj._emotion_hears_owner = lambda _text: False
    obj._series_decision_for = lambda _pcm: None
    obj._cinema_observe = lambda *a, **k: None
    obj._auto_enroll_voice = lambda *a, **k: None
    obj._cancel_narration = lambda *a, **k: None
    obj._consume_carryover_hot = lambda _text: ""
    obj._qoder_hold = lambda *a, **k: None

    cfg = _cfg()

    def caller(messages, max_tokens, **kw):
        if sleep_s:
            time.sleep(sleep_s)
        return _caller_result(messages)

    def _caller_result(messages):
        last = messages[-1]["content"]
        if "Sua resposta:" in last:
            return {"text": "Oi, criador; tudo quieto por aqui.",
                    "ttft_ms": 5.0, "first_sentence_ms": 8.0,
                    "end_ms": 10.0, "error": None}
        return {"text": "", "ttft_ms": 0.0, "first_sentence_ms": 0.0,
                "end_ms": 0.0, "error": None}

    rt = FlowsRuntime(cfg, speak=obj._flows_speak, log=obj.ui.write_log,
                      on_chat_route=obj._flows_on_chat_route,
                      history=obj._flows_history,
                      start_expiry_thread=False)
    rt.orchestrator.mouth = Mouth(cfg, caller=caller,
                                  history=obj._flows_history)
    obj._flows_rt = rt
    obj._answers_on_whatsapp = lambda _meta: False
    return obj


# ── (a) a boca não é alternativa ao cérebro: sem `return`, cérebro é chamado ─

def test_conversa_curta_chama_o_cerebro_mesmo_com_a_boca_respondendo():
    obj = _jarvis_with_flows(None)
    main.JarvisLive._qoder_turn(obj, "como você está?", PCM)
    assert obj._qoder_live.asks, "cérebro foi pulado pela rota da boca"
    assert _wait(lambda: obj.spoken), "a boca não deu a resposta superficial"
    assert obj.spoken[0][0] == "Oi, criador; tudo quieto por aqui."
    # O cérebro continua e o turno segue o pipeline normal (meta registrado).
    assert len(obj._qoder_meta) == 1
    # A fala superficial é medida no instante em que sai ([Rota] caminho=boca
    # via callback) e o log do dono registra a rota.
    assert any("boca atendeu conversa curta" in line for line in obj.ui.logs)
    assert obj.indexed == ["Oi, criador; tudo quieto por aqui."]


def test_turno_de_acao_nao_ganha_rota_de_boca_superficial():
    obj = _jarvis_with_flows(None)
    main.JarvisLive._qoder_turn(obj, "abre o spotify", PCM)
    assert obj._qoder_live.asks
    time.sleep(0.2)
    # Ação: nada de resposta superficial de conversa; a rota-boca de conversa
    # curta não dispara (conteúdo e execução são do cérebro).
    assert not any("boca atendeu conversa curta" in line for line in obj.ui.logs)


# ── (b) boca atrasada/nula NÃO atrasa o cérebro ─────────────────────────────

def test_cerebro_nao_espera_a_boca_atrasada():
    # A boca leva 600 ms; o turno não pode ficar preso atrás dela.
    obj = _jarvis_with_flows(None, sleep_s=0.6)
    started = time.monotonic()
    main.JarvisLive._qoder_turn(obj, "como você está?", PCM)
    asked_at = obj._qoder_live.asks[0][2]
    assert asked_at - started < 0.3, "o cérebro esperou a boca"
    assert obj.spoken == [], "o turno deveria ter seguido antes da boca"
    assert _wait(lambda: len(obj.spoken) == 1)


def test_boca_nula_devolve_o_turno_inteiro_ao_cerebro():
    # Boca sem frase (modelo fora do ar): cérebro chamado igual. A rota de
    # conversa curta NÃO existe (nenhum [Rota] caminho=boca): o veredito
    # vazio não segura nem pula o cérebro; no máximo volta o ack determinístico
    # do brief normal, como hoje.
    obj = _jarvis_with_flows(None)

    def morta(messages, max_tokens, **kw):
        return {"text": "", "ttft_ms": 0.0, "first_sentence_ms": 0.0,
                "end_ms": 1.0, "error": "ConnectionError"}

    obj._flows_rt.orchestrator.mouth = Mouth(_cfg(), caller=morta,
                                             history=lambda: [])
    main.JarvisLive._qoder_turn(obj, "oi gino, tudo bem?", PCM)
    assert obj._qoder_live.asks
    time.sleep(0.2)
    assert not any("boca atendeu conversa curta" in line for line in obj.ui.logs)
    assert obj.indexed == []  # nada foi indexado como resposta da boca


# ── (c) contexto da boca: 15 minutos vindos do config, default no código ────

def test_janela_de_contexto_da_boca_e_quinze_minutos_por_padrao():
    cfg = FlowsConfig()
    assert cfg.mouth_context_minutes == 15.0
    assert cfg.mouth_context_turns == 8


def test_janela_de_contexto_le_de_config_flows_json(tmp_path):
    path = tmp_path / "flows.json"
    path.write_text(json.dumps({"mouth": {"context_minutes": 7,
                                          "context_turns": 5}}),
                    encoding="utf-8")
    cfg = FlowsConfig.load(path)
    assert cfg.mouth_context_minutes == 7.0
    assert cfg.mouth_context_turns == 5
    # Arquivo sem a chave nova mantém o default de 15 (não regride 2 h).
    vazio = tmp_path / "vazio.json"
    vazio.write_text("{}", encoding="utf-8")
    assert FlowsConfig.load(vazio).mouth_context_minutes == 15.0


def test_arquivo_oficial_do_repo_tem_quinze_minutos():
    cfg = FlowsConfig.load()
    assert cfg.mouth_context_minutes == 15.0
    assert cfg.mouth_context_turns == 8


def test_flows_history_usa_a_janela_do_config_com_teto_de_falas():
    obj = _jarvis_with_flows(None)
    turns = main.JarvisLive._flows_history(obj)
    assert obj._chat_memory.hours == pytest.approx(15.0 / 60.0)
    assert len(turns) == 8  # teto de falas; memória do teste tem 12


def test_mouth_aplica_o_teto_de_falas_do_config():
    cfg = _cfg(mouth_context_turns=3)
    hist = [{"role": "user", "text": f"fala {i}"} for i in range(10)]
    m = Mouth(cfg, caller=lambda *a, **k: {}, history=lambda: hist)
    assert len(m._recent_lines().splitlines()) == 3


# ── (d) rota viva: padrões com evidência do log de 05/11/2026 ───────────────

CONVERSA_PUR_A_HOJE = [
    "Gino.",                                   # 3 turnos só de vocativo
    "Gino, está aí?",
    "Como assim, boca nova e corpo velho? Você não arrumou tudo que precisava arrumar?",
    "A regra não é essa GINO!! EU ESPECIFIQUEI CARA E VOCE TA QUEBRANDO ELAS!",
    "vai se foder, Gino",
    "seu mentiroso",
]
NAO_E_CONVERSA_PUR_A = [
    "abre o spotify, porra",                   # ação + xingamento
    "confirma o envio, caralho",               # confirmação
    "que horas são, merda",                    # consulta
    "quanto é dois mais dois, burro",          # cálculo
    "reinicia agora, seu mentiroso",           # confirmação pesada (lei b)
    "pesquisa o preço, idiota",                # busca
    "18:11 mentiroso",                         # número
    "abre o terminal, mentiroso",              # ferramenta
]


def test_rota_de_conversa_curta_casa_conversa_pura_do_log_de_hoje():
    router = Router()
    for text in CONVERSA_PUR_A_HOJE:
        reason = short_chat_reason(text, router.parse(text), name="Gino")
        assert reason, f"conversa pura não casou: {text!r}"


def test_rota_ampliada_nao_casa_acao_consulta_numero_confirmacao():
    router = Router()
    for text in NAO_E_CONVERSA_PUR_A:
        reason = short_chat_reason(text, router.parse(text), name="Gino")
        assert reason == "", f"virou conversa pura indevidamente: {text!r}"


def test_vocativo_puro_nao_casa_sem_nome_do_assistente():
    # "gino." com nome do assistente diferente do cadastrado não fecha rota.
    assert short_chat_reason("Gino.", Router().parse("Gino."),
                             name="Jarvis") == ""


# ── (e) boca + cérebro no mesmo turno: nunca duas falas do mesmo conteúdo ───

def test_cerebro_respondendo_antes_cal_a_a_boca_de_conversa():
    # Boca lenta; o cérebro responde enquanto ela sintetiza. A frase local
    # morre no portão de fala única (qoder_answered_at) — nada é falado duas
    # vezes; o conteúdo chega uma única vez, pelo cérebro.
    spoken = []
    logs = []
    cfg = _cfg()
    rt = FlowsRuntime(cfg, speak=spoken.append, log=logs.append,
                      start_expiry_thread=False)

    def lenta(messages, max_tokens, **kw):
        time.sleep(0.3)
        return {"text": "Oi, criador; tudo quieto por aqui.", "ttft_ms": 10.0,
                "first_sentence_ms": 15.0, "end_ms": 300.0, "error": None}

    rt.orchestrator.mouth = Mouth(cfg, caller=lenta, history=lambda: [])
    plan = rt.start_turn("t1", "como você está?", armed=True)
    assert plan.local_chat == "bate-papo"
    time.sleep(0.05)                 # worker entrou e está em síntese
    rt.mark_answered("t1")           # o cérebro respondeu no meio
    time.sleep(0.5)
    assert spoken == [], "boca falou depois que o cérebro já tinha respondido"
    assert any("cérebro respondeu antes" in line for line in logs)
    rt.finish("t1")


def test_boca_que_fala_primeira_emite_a_medicao_de_rota_uma_unica_vez():
    spoken, routes = [], []
    cfg = _cfg()
    rt = FlowsRuntime(cfg, speak=spoken.append, log=lambda _m: None,
                      on_chat_route=routes.append, start_expiry_thread=False)
    rt.orchestrator.mouth = Mouth(
        cfg, caller=lambda messages, max_tokens, **kw: {
            "text": "Opa, criador.", "ttft_ms": 2.0, "first_sentence_ms": 3.0,
            "end_ms": 5.0, "error": None}, history=lambda: [])
    rt.start_turn("t1", "Gino.", armed=True)
    assert _wait(lambda: len(spoken) == 1)
    assert spoken == ["Opa, criador."]
    assert len(routes) == 1 and routes[0]["reason"] == "chamou"
    assert routes[0]["first_ms"] >= 0.0
    rt.finish("t1")
