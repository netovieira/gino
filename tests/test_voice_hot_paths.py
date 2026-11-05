"""Latência da voz sem sumiço (05/11/2026): carryover quente, boca primeiro,
cauda de VAD adaptativa e janela de reenvio curta para turno de conversa.

Os três ladrões medidos no laudo (vão mediano 7 s, p95 ~95-101 s no PC):

  (a) preempt em turno sem resposta → a pergunta vira CONTEXTO do turno novo;
      nada entra na fila de prioridade zero;
  (b) preempt com execução pesada em andamento (turno de tarefa da fila) →
      a pergunta continua na fila E o adiamento é falado em uma frase;
  (c) conversa simples → a boca dá a RESPOSTA SUPERFICIAL imediata (uma única
      fala pela runtime) e o cérebro É CHAMADO junto para trazer o conteúdo —
      a expectativa antiga de que "o cérebro nem é chamado" era o desvio do
      passo 3 e morreu em 05/11/2026; a fala única segue por qoder_answered_at
      e pela substituição do [Say] (sem repetição de emenda); o turno marca
      `chat` no sidecar;
  (d) verbo de ação/número/confirmação mantém a cauda de 3 s no endpointing;
      frase comum de conversa fecha em ~1,2 s;
  (e) a boca jamais afirma resultado de ação — lei inegociável, cobrada em
      tests/test_flows_local_chat.py (não enfraquecer).

Tudo determinístico, sem rede: fila em tmp_path, transcritor e sidecar
stubbeados.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from unittest import mock

import pytest

import main
from core import groq_stt
from core import task_queue
from core.flows.config import FlowsConfig
from core.flows.live import FlowsRuntime
from core.flows.mouth import Mouth
from core.flows.router import Router, short_chat_reason
from core.qoder_live import QoderLive


@pytest.fixture
def queue(tmp_path, monkeypatch):
    path = tmp_path / "memory" / "tasks.json"
    monkeypatch.setattr(task_queue, "TASKS_PATH", path)
    task_queue.reset()
    yield task_queue
    task_queue.reset()


class _UI:
    def __init__(self):
        self.logs = []

    def write_log(self, line):
        self.logs.append(str(line))


def _jarvis():
    """Superfície mínima de JarvisLive para o carryover quente."""
    obj = object.__new__(main.JarvisLive)
    obj._turn_owner_text = {}
    obj._speech_audit_lock = threading.RLock()
    obj._speech_audit = {}
    obj._carryover_questions = {}
    obj._carryover_hot = {}
    obj._owner_speech_wall = 0.0
    obj._is_speaking = False
    obj._qoder_lock = threading.RLock()
    obj._qoder_current_id = None
    obj._qoder_meta = {}
    obj.ui = _UI()
    obj.spoken = []
    obj.finished = []
    obj._say_now = lambda text, **kw: obj.spoken.append(str(text))
    obj._qoder_finish = lambda turn_id, flush=True: obj.finished.append(turn_id)
    obj._flows_cancel = lambda turn_id, reason="": None
    obj._answers_on_whatsapp = lambda meta: False
    return obj


# ── (a) responde no lugar: nada na fila, pergunta vira contexto ─────────────

def test_preempt_sem_resposta_guarda_contexto_e_nao_enfileira(queue):
    obj = _jarvis()
    assert obj._carryover_hot_store(
        5, "qual a senha do wifi?", "turno substituído antes de responder") \
        is True
    assert queue.list_tasks("open") == [], "carryover quente não pode enfileirar"
    line = obj._consume_carryover_hot("como está o tempo amanhã?")
    assert line.startswith("[PERGUNTA SEM RESPOSTA]")
    assert "qual a senha do wifi?" in line
    assert "uma única" in line            # a resposta cobre as duas perguntas
    assert queue.list_tasks("open") == []
    # consumida uma vez: não sai duas (senão a pergunta assombra o log)
    assert obj._consume_carryover_hot("oi") == ""


def test_preempt_via_qoder_preempt_store_hot_sem_fila(queue):
    obj = _jarvis()
    obj._qoder_meta = {"old1": {"turn_ref": 7, "source": "owner"}}
    obj._turn_owner_text = {7: "o que é karpathy? "}
    main.JarvisLive._qoder_preempt(obj, "old1")
    assert queue.list_tasks("open") == []
    assert obj._carryover_hot[7]["question"] == "o que é karpathy?"
    line = obj._consume_carryover_hot("explica transformer")
    assert "o que é karpathy?" in line


def test_pergunta_repetida_nao_vira_linha_de_contexto(queue):
    obj = _jarvis()
    assert obj._carryover_hot_store(3, "qual a senha do wifi?") is True
    # O dono repetiu a pergunta: o turno novo JÁ é a resposta dela.
    assert obj._consume_carryover_hot("qual a senha do wifi??") == ""
    assert obj._carryover_hot == {}


def test_hot_estourado_desce_para_a_fila_com_adisamento_falado(queue):
    obj = _jarvis()
    for i, q in enumerate(["pergunta um", "pergunta dois", "pergunta três",
                           "pergunta quatro"], start=1):
        assert obj._carryover_hot_store(i, q) is True
    assert len(obj._carryover_hot) == main._CARRYOVER_HOT_MAX
    assert len(queue.list_tasks("open")) == 1     # o mais velho foi despejado
    assert any("pergunta um" in said for said in obj.spoken)


def test_hot_expirado_vai_para_a_fila_e_fala_o_adisamento(queue, monkeypatch):
    obj = _jarvis()
    monkeypatch.setattr(main, "_CARRYOVER_HOT_TTL_S", 0.0)
    assert obj._carryover_hot_store(9, "me conta uma piada") is True
    assert obj._flush_stale_carryover_hot() == 1
    assert obj._carryover_hot == {}
    open_rows = queue.list_tasks("open")
    assert len(open_rows) == 1
    assert open_rows[0]["payload"]["origin"] == main._CARRYOVER_ORIGIN
    assert any("me conta uma piada" in said for said in obj.spoken)


# ── (b) execução pesada em andamento: fila preservada + adiamento falado ────

def test_heavy_preempt_continua_na_fila_e_fala_o_adisamento(queue):
    obj = _jarvis()
    assert obj._carryover_hot_store(
        4, "gera o relatório dos discos", "turno substituído",
        heavy=True) is True
    open_rows = queue.list_tasks("open")
    assert len(open_rows) == 1
    assert open_rows[0]["payload"]["question"] == "gera o relatório dos discos"
    assert obj._carryover_hot == {}, "heavy não entra no hot: já tem fila"
    defer = " ".join(obj.spoken)
    assert "relatório dos discos" in defer
    assert "fila" in defer or "esvaziar" in defer


def test_preempt_de_turno_de_tarefa_recupera_pergunta_do_payload(queue):
    now = (datetime.now().astimezone()
           - timedelta(seconds=2)).isoformat()
    row = queue.add("Responder à pergunta do dono que ficou sem resposta",
                    payload={"origin": main._CARRYOVER_ORIGIN,
                             "question": "como fica a matrícula por dispositivo?",
                             "session": main._PROCESS_STARTED_WALL,
                             "asked_at": now})
    obj = _jarvis()
    assert obj._task_question_for(row["id"]) == \
        "como fica a matrícula por dispositivo?"
    obj._qoder_meta = {"old1": {"turn_ref": 8, "source": "task",
                                "task_id": row["id"]}}
    obj._turn_owner_text = {}
    with mock.patch.object(main, "_task_mirror", mock.Mock()):
        main.JarvisLive._qoder_preempt(obj, "old1")
    # A pergunta do payload continua devida (fila) e o adiamento é falado.
    questions = [r["payload"].get("question") for r in queue.list_tasks("open")]
    assert "como fica a matrícula por dispositivo?" in questions
    assert any("matrícula" in said for said in obj.spoken)


# ── (c) conversa simples: boca responde antes e o remoto nem é chamado ──────

def _runtime(spoken):
    cfg = FlowsConfig(enabled=True, mouth_enabled=True,
                      local_chat_enabled=True, partial_prewarm=False,
                      partial_speech=False, executor_enabled=False,
                      log_events=False)
    rt = FlowsRuntime(cfg, speak=spoken.append, log=lambda _m: None,
                      clock=time.monotonic, start_expiry_thread=False)
    def caller(messages, max_tokens, **kw):
        return {"text": "Oi, criador; tudo quieto por aqui.", "ttft_ms": 5.0,
                "first_sentence_ms": 8.0, "end_ms": 10.0, "error": None}
    rt.orchestrator.mouth = Mouth(cfg, caller=caller, history=lambda: [])
    return rt


def _wait(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not pred():
        time.sleep(0.01)
    return pred()


def test_boca_atende_antes_e_a_emenda_do_cerebro_nao_acontece():
    # Na RUNTIME da conversa curta a boca fala uma única vez: o brief não
    # compete e a frase não se repete. O cérebro agora É chamado pelo
    # processo vivo logo atrás (passo 3; ver
    # tests/test_flows_fala_paralela.py) — a emenda que não acontece é a
    # repetição: se o cérebro responde antes, a boca cala (qoder_answered_at);
    # se a boca fala antes, o [Say] substitui, nunca duas falas do mesmo tom.
    spoken = []
    rt = _runtime(spoken)
    plan = rt.start_turn("t1", "oi gino, tudo bem?", armed=True)
    assert plan.local_chat == "saudacao"
    assert _wait(lambda: len(spoken) == 1)
    info = rt.await_local_chat("t1")
    assert info and info["sentence"] == "Oi, criador; tudo quieto por aqui."
    assert len(spoken) == 1               # sem dupla fala, sem repetição
    # Marca o turno como de conversa para o sidecar (reenvio curto, item 4).
    assert rt.is_chat_turn("t1") is True
    rt.finish("t1")


def test_turno_de_acao_nao_e_marcado_como_conversa():
    spoken = []
    rt = _runtime(spoken)
    plan = rt.start_turn("t1", "abre o spotify")
    assert plan.local_chat == ""
    assert rt.is_chat_turn("t1") is False
    assert rt.await_local_chat("t1") is None
    rt.finish("t1")
    assert rt.is_chat_turn("inexistente") is False


def test_ask_do_client_manda_flag_chat_para_o_sidecar():
    live = object.__new__(QoderLive)
    sent = []
    live._ensure_ready = lambda: None
    live._send = lambda payload, raise_errors=False: sent.append(payload)
    live.ask("oi, tudo bem?", id="c1", chat=True)
    live.ask("abre o terminal", id="c2")
    assert sent[0].get("chat") is True
    assert "chat" not in sent[1], "sem flag o sidecar mantém a janela longa"


# ── (d) cauda de VAD adaptativa ──────────────────────────────────────────────

def test_verbo_de_acao_e_numero_mantem_cauda_de_tres_segundos():
    assert groq_stt.continue_window_for(
        "desliga a luz do quarto") == groq_stt._CONTINUE_WINDOW_S
    assert groq_stt.continue_window_for(
        "me liga às 14 horas") == groq_stt._CONTINUE_WINDOW_S
    assert groq_stt.continue_window_for(
        "confirma o reinício") == groq_stt._CONTINUE_WINDOW_S
    assert groq_stt.continue_window_for(
        "qual a senha do cadastro") == groq_stt._CONTINUE_WINDOW_S


def test_frase_comum_de_conversa_encurta_a_cauda():
    curto = groq_stt.continue_window_for("bom dia, sumido")
    assert curto == pytest.approx(groq_stt._CONTINUE_WINDOW_CHAT_S)
    assert curto < groq_stt._CONTINUE_WINDOW_S - 1.0   # ~1,8 s ganhos


def test_sem_transcricao_local_a_cauda_decide_pelo_score():
    # score já calculado ("turno quase acabado") → cauda curta
    assert groq_stt.continue_window_for("", 0.8) == \
        groq_stt._CONTINUE_WINDOW_CHAT_S
    # pausa clara de pensamento (score baixo) ou sem score → 3 s
    assert groq_stt.continue_window_for("", 0.1) == groq_stt._CONTINUE_WINDOW_S
    assert groq_stt.continue_window_for("") == groq_stt._CONTINUE_WINDOW_S


def _listener(monkeypatch, got, text):
    monkeypatch.setattr(groq_stt._duck, "up", lambda: None)
    monkeypatch.setattr(groq_stt._duck, "down", lambda: None)
    listener = groq_stt.GroqListener(
        "", "test", "pt",
        on_text=lambda t, r: got.append(("text", t, r)),
        on_utterance=lambda t, pcm: got.append(("utterance", t, len(pcm))),
        on_status=lambda _m: None,
        sample_rate=16000,
        transcribe_fn=lambda pcm: text,
    )
    # A pausa é sempre "incompleta" aqui; só a cauda é adaptativa.
    listener._endpoint_complete = lambda _pcm: False
    return listener


def test_worker_encurta_a_cauda_em_frase_de_conversa(monkeypatch):
    monkeypatch.setattr(groq_stt, "_CONTINUE_WINDOW_CHAT_S", 0.2)
    monkeypatch.setattr(groq_stt, "_CONTINUE_WINDOW_S", 5.0)
    got = []
    listener = _listener(monkeypatch, got, "bom dia, sumido")
    try:
        listener._q.put(b"\x01\x00" * int(0.8 * 16000))
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline and not got:
            time.sleep(0.02)
    finally:
        listener.stop()
    assert got, "a frase de conversa deveria fechar na cauda curta"
    assert got[0][0] == "utterance"


def test_worker_nao_encurta_a_cauda_com_verbo_de_acao(monkeypatch):
    monkeypatch.setattr(groq_stt, "_CONTINUE_WINDOW_CHAT_S", 0.2)
    monkeypatch.setattr(groq_stt, "_CONTINUE_WINDOW_S", 1.0)
    got = []
    listener = _listener(monkeypatch, got, "desliga a luz do quarto")
    try:
        listener._q.put(b"\x01\x00" * int(0.8 * 16000))
        time.sleep(0.6)                   # janela da cauda curta já passou
        early = list(got)
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline and not got:
            time.sleep(0.02)
    finally:
        listener.stop()
    assert early == [], "verbo de ação não pode ser cortado pela cauda curta"
    assert got, "a cauda longa deve devolver o trecho quando esgota"
    assert got[0][0] == "utterance"


# ── (e) política da boca e lei (a) não mudaram ───────────────────────────────

def test_roteador_continua_conservador():
    router = Router()
    assert short_chat_reason("como você está?", router.parse(
        "como você está?")) == "bate-papo"
    assert short_chat_reason("abre o spotify", router.parse(
        "abre o spotify")) == ""
