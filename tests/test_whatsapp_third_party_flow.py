"""FLUXO ponta a ponta do chat individual de terceiro (leis 1, 3, 5 do SKILL).

Incidente 05/11/2026 22:49-22:59: o dono escreveu no chat individual de um
terceiro nomeando o assistente (eco ``from_me`` com o nome) e uma nota de voz
chegou no mesmo chat — nenhuma resposta saiu, em nenhum formato. Causa: o
ramo ``not direct and owner_echo`` de ``main._whatsapp_autoreply`` engolia
TODO eco de terceiro, com ou sem nome (contradizendo a regra 5 do SKILL), e
nota de voz recebida nem chegava candidata ao portão do nome (a checagem era
só no texto bruto, que em áudio é sempre vazio).

Este arquivo exercita o fluxo INTEIRO com a conta e o inbox SIMULADOS reais
do repo (o cliente neonize de mentirinha de ``tests/test_whatsapp_delivery``)
e nenhuma das partes toca rede, áudio de verdade ou segredo:

  (a) texto individual de terceiro CONTENDO o nome  -> UMA resposta naquele
      chat, assinada no ponto único (``core.whatsapp.send``), sem aviso
      falado na sala;
  (b) nota de voz individual de terceiro            -> transcreve, sintetiza
      e envia como ÁUDIO; quando a síntese falha, a MESMA resposta sai em
      TEXTO exatamente uma vez (nunca os dois); nota SEM nome no transcript
      -> silêncio total;
  (c) eco ``from_me`` do dono no chat do terceiro COM o nome -> é ordem e é
      respondida no mesmo chat; sem o nome -> continua eco, zero envio;
  (d) grupo com o nome                              -> zero resposta (a
      recusa de grupo não afrouxa em lugar nenhum).

Os números aqui são constantes de teste; nenhum nome, número ou contato real
aparece em log, inbox ou asserção.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import main
from core import actiongate, whatsapp, whatsapp_watch

ACCOUNT = "principal"
OWNER_CHAT = "5511999990000"          # chat direto do dono (constante de teste)
THIRD_CHAT = "5521888887777"          # chat individual do terceiro (teste)
GROUP_CHAT = "120363000000000000@g.us"
NAME = "Gino"
ANSWER = "Pronto, já cuidei disso por aqui."

_DELIVERY = whatsapp._RECEIPT_DELIVERED


class _FakeClient:
    """Cliente neonize simulado: registra cada envio e devolve IDs únicos."""

    def __init__(self):
        self.calls: list[tuple] = []
        self._n = 0

    def _resp(self):
        self._n += 1
        resp = SimpleNamespace(ID=f"FLOW-{self._n}")
        threading.Timer(0.01, whatsapp._on_receipt,
                        args=(SimpleNamespace(MessageIDs=[resp.ID],
                                              Type=_DELIVERY), None)).start()
        return resp

    def send_message(self, target, text):
        self.calls.append(("text", target, text))
        return self._resp()

    def send_audio(self, target, file, ptt=False):
        self.calls.append(("audio", target, file, ptt))
        return self._resp()


class _UI:
    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, text):
        self.logs.append(text)


def _item(text="", chat=THIRD_CHAT, name="Contato", from_me=False,
          group=False, media=None):
    return {"ts": int(time.time()), "chat": chat, "name": name,
            "from_me": from_me, "group": group, "text": text,
            "id": f"in-{time.time_ns()}", "media": media}


def _voice_media(seconds=5):
    return {"kind": "mensagem de voz", "mime": "audio/ogg", "name": "",
            "seconds": seconds, "ext": ".ogg", "size": 10000}


class FlowHarness:
    """Watch REAL + autoreply REAL + entrega REAL até o cliente simulado.

    Só o cérebro (sessão Qoder) é representado: ``_qoder_turn`` devolve a
    resposta combinada pelo mesmo caminho de entrega do processo vivo
    (``_deliver_whatsapp_answer`` → ``_send_whatsapp_answer`` →
    ``core.whatsapp.send``/``send_media`` — a assinatura nasce no ponto
    único, ``core.whatsapp.signed_text``).
    """

    def __init__(self, transcript="", synth_fail=False):
        self.client = _FakeClient()
        self.transcript = transcript
        self.synth_fail = synth_fail
        self.turns: list[dict] = []
        self.inbox: list[dict] = []
        self.logs: list[str] = []
        self._tmp = tempfile.TemporaryDirectory()
        self.obj = self._build()

    def _build(self):
        obj = object.__new__(main.JarvisLive)
        obj._asst_name = NAME
        obj.ui = _UI()
        obj._chat_memory = SimpleNamespace(
            append_turn=lambda *a, **k: None)
        obj._session_log = []
        obj._last_user_speech = 0.0
        obj._wa_stt_lock = threading.Lock()
        obj._next_turn_ref = lambda chan=1: None
        obj._index_event = lambda *a, **k: None
        obj._begin_user_epoch = lambda: {}
        obj._memory_record_user = lambda text: None
        obj._inject_user_turn = mock.Mock()
        obj._owner_text_notify = mock.Mock()
        obj._owner_text_gate = lambda *a, **k: False
        obj._voice_bootstrap_gate = lambda *a, **k: False
        obj._device_enroll_note = lambda *a, **k: False
        obj._restart_confirmation_pcm = lambda *a, **k: None
        obj._whatsapp_owner_voice = lambda *a, **k: (None, "")
        obj._transcribe_whatsapp_audio = lambda path: self.transcript
        obj._log_line = lambda text, flags=None: text
        obj._speech_route = lambda text, **k: {"muted": False,
                                               "subject": ""}

        def _synthesize(text):
            if self.synth_fail:
                raise RuntimeError("fila de áudio fora do ar")
            path = os.path.join(self._tmp.name, f"resposta-{len(self.client.calls)}.ogg")
            Path(path).write_bytes(b"OggS" + b"\x00" * 64)
            return path

        obj._synthesize_whatsapp_audio = _synthesize

        def _qoder_turn(instruction, pcm=None, epoch=None, **meta):
            self.turns.append({"instruction": instruction, "meta": meta})
            obj._deliver_whatsapp_answer(meta, ANSWER)

        obj._qoder_turn = _qoder_turn
        obj._qoder_hold = lambda *a, **k: None
        obj._qoder_live_ready = lambda: True
        return obj

    def __enter__(self):
        acct = whatsapp._Account(ACCOUNT, "uuid-flow-test", default=True)
        acct.connected = True
        acct.client = self.client
        patches = [
            mock.patch.object(whatsapp, "_accounts", {acct.uuid: acct}),
            mock.patch.object(whatsapp, "_ensure_accounts", lambda: None),
            mock.patch.object(whatsapp, "available", lambda: True),
            mock.patch.object(whatsapp, "resolve",
                              lambda *a, **k: (THIRD_CHAT, [])),
            mock.patch.object(whatsapp, "_display", lambda *a, **k: "Contato"),
            mock.patch.object(whatsapp, "DELIVERY_WAIT_S", 0.3),
            mock.patch.object(whatsapp, "_append_inbox",
                              lambda item, a: self.inbox.append(dict(item))),
            mock.patch.object(whatsapp, "default_account_name",
                              lambda: ACCOUNT),
            mock.patch.object(whatsapp, "account_number",
                              lambda account=None: ""),
            mock.patch.object(whatsapp, "list_accounts",
                              lambda: [{"name": ACCOUNT, "number": ""}]),
            mock.patch.object(whatsapp, "_send_any",
                              lambda client, target, file, caption:
                              ("áudio", client.send_audio(target, file, ptt=True))),
            mock.patch("memory.config_manager.get_plugin_setting",
                       lambda plugin, key, default="":
                       OWNER_CHAT if key == "owner_chat" else default),
        ]
        for p in patches:
            p.start()
        actiongate.close()
        whatsapp_watch.reset()
        whatsapp_watch.set_personas_source(lambda: [])
        # o observador do processo vivo: watch REAL apontando para o
        # autoreply REAL, com o nome e o chat direto do dono simulados
        whatsapp_watch.start(self.obj._whatsapp_autoreply,
                             logger=self.logs.append,
                             assistant_name=NAME,
                             direct_account=ACCOUNT,
                             direct_chat=OWNER_CHAT)
        self._patches = patches
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        whatsapp_watch.set_personas_source(None)
        whatsapp_watch.stop()
        whatsapp_watch.reset()
        actiongate.close()
        self._tmp.cleanup()

    # os chamados do fluxo chegam via watch real, no processador síncrono
    # que o worker da conta usa (whatsapp_watch.handle):
    def feed(self, item):
        return whatsapp_watch.handle(ACCOUNT, item)

    def sent_text(self):
        return [c for c in self.client.calls if c[0] == "text"]

    def sent_audio(self):
        return [c for c in self.client.calls if c[0] == "audio"]


# ── (a) texto individual de terceiro com o nome: UMA resposta assinada ──────
def test_third_party_text_naming_the_assistant_gets_one_signed_reply():
    with FlowHarness() as h:
        assert h.feed(_item(f"{NAME}, consegue me ajudar com uma conta?"))
        assert len(h.sent_text()) == 1
        assert h.sent_audio() == []
        kind, target, text = h.sent_text()[0]
        assert target == THIRD_CHAT
        # assinatura nasce no PONTO ÚNICO core.whatsapp.send (signed_text)
        assert text.startswith(f"*{NAME}:*\n")
        assert ANSWER in text
        # uma resposta só, e nada falado na sala
        assert len(h.client.calls) == 1
        h.obj._inject_user_turn.assert_not_called()
        h.obj._owner_text_notify.assert_not_called()
        assert h.turns[0]["meta"]["speak"] is False
        assert h.turns[0]["meta"]["reply_channel"] == "whatsapp"


# ── (b) nota de voz de terceiro: áudio sintetizado; falha → texto 1× ────────
def test_third_party_voice_note_is_answered_with_synthesized_audio():
    with FlowHarness(transcript=f"{NAME}, me conta uma piada curta") as h:
        assert h.feed(_item("[mensagem de voz de 5s]", media=_voice_media()))
        assert len(h.sent_audio()) == 1
        assert h.sent_text() == []          # espelho: áudio NÃO ganha texto
        assert h.sent_audio()[0][1] == THIRD_CHAT


def test_when_synthesis_fails_the_same_answer_goes_out_as_text_once():
    with FlowHarness(transcript=f"{NAME}, me conta uma piada curta",
                     synth_fail=True) as h:
        assert h.feed(_item("[mensagem de voz de 5s]", media=_voice_media()))
        assert h.sent_audio() == []
        texts = h.sent_text()
        assert len(texts) == 1              # a MESMA resposta, uma única vez
        assert texts[0][2].startswith(f"*{NAME}:*\n")
        assert ANSWER in texts[0][2]


def test_third_party_voice_note_without_the_name_stays_silent():
    with FlowHarness(transcript="oi, tudo bem por aí?") as h:
        assert not h.feed(_item("[mensagem de voz de 5s]",
                                media=_voice_media()))
        assert h.client.calls == []
        assert h.turns == []


# ── (c) eco do dono no chat do terceiro ──────────────────────────────────────
def test_owner_echo_naming_the_assistant_is_an_answered_order():
    with FlowHarness() as h:
        item = _item(f"{NAME}? Mandei áudio aqui pra você, responde…",
                     from_me=True)
        assert h.feed(item)
        assert len(h.sent_text()) == 1
        assert h.sent_text()[0][1] == THIRD_CHAT     # NO MESMO chat
        assert h.sent_text()[0][2].startswith(f"*{NAME}:*\n")
        # as palavras do dono são ORDEM, não "conteúdo de terceiro"
        assert "ORDEM DO DONO NO CHAT DE UM TERCEIRO" in \
            h.turns[0]["instruction"]
        h.obj._inject_user_turn.assert_not_called()  # sem aviso na sala


def test_owner_voice_echo_naming_the_assistant_is_answered_in_audio():
    with FlowHarness(transcript=f"{NAME}, responde aquela dúvida dela") as h:
        item = _item("[mensagem de voz de 112s]", from_me=True,
                     media=_voice_media(seconds=112))
        assert h.feed(item)
        assert len(h.sent_audio()) == 1
        assert h.sent_audio()[0][1] == THIRD_CHAT
        assert h.sent_text() == []


def test_owner_echo_without_the_name_stays_silence():
    with FlowHarness() as h:
        assert not h.feed(_item("boa noite, já chega por hoje",
                                from_me=True))
        assert h.client.calls == []
        assert h.turns == []


def test_own_reply_echo_never_reopens_the_conversation():
    """Assinatura "Gino:" e ID do envio continuam barreira contra laço."""
    with FlowHarness() as h:
        assert not h.feed(_item(f"*{NAME}:*\n{ANSWER}", from_me=True))
        assert h.client.calls == []


# ── (d) grupo: zero, nem com o nome ──────────────────────────────────────────
def test_group_with_the_name_gets_zero_nothing():
    with FlowHarness() as h:
        assert not h.feed(_item(f"{NAME}, olha essa novidade",
                                chat=GROUP_CHAT, group=True))
        assert h.client.calls == []
        assert h.turns == []
        # mesmo chamando o handler direto (defesa em profundidade): nada
        assert not h.obj._whatsapp_autoreply(
            ACCOUNT, _item(f"{NAME}, olha essa novidade",
                           chat=GROUP_CHAT, group=True))
        assert h.client.calls == []
