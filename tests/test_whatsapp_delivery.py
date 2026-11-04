"""Confirmação de entrega do WhatsApp: ID do envio, recibo e frases faladas."""
from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import whatsapp  # noqa: E402
from plugins import whatsapp_control  # noqa: E402

DELIVERED = whatsapp._RECEIPT_DELIVERED
READ = whatsapp._RECEIPT_READ


class _FakeClient:
    """Cliente neonize de mentira: devolve um SendResponse e, se pedido,
    dispara o recibo depois de alguns milissegundos."""

    def __init__(self, resp, receipt_type=None):
        self.resp = resp
        self.receipt_type = receipt_type
        self.calls = []

    def _reply(self, resp):
        if self.receipt_type is not None:
            timer = threading.Timer(
                0.03,
                whatsapp._on_receipt,
                args=(SimpleNamespace(MessageIDs=[resp.ID],
                                      Type=self.receipt_type), None),
            )
            timer.daemon = True
            timer.start()
        return resp

    def send_message(self, target, text):
        self.calls.append(("text", target, text))
        return self._reply(self.resp)

    def send_audio(self, target, file, ptt=False):
        self.calls.append(("audio", target, file, ptt))
        return self._reply(self.resp)


@pytest.fixture
def env(monkeypatch):
    acct = whatsapp._Account("teste", "uuid-teste", default=True)
    acct.connected = True
    monkeypatch.setattr(whatsapp, "_accounts", {"uuid-teste": acct})
    monkeypatch.setattr(whatsapp, "_ensure_accounts", lambda: None)
    monkeypatch.setattr(whatsapp, "available", lambda: True)
    monkeypatch.setattr(whatsapp, "resolve",
                        lambda *a, **k: ("5511999999999", []))
    monkeypatch.setattr(whatsapp, "_display", lambda *a, **k: "Fulano")
    monkeypatch.setattr(whatsapp, "DELIVERY_WAIT_S", 0.25)
    posted = []
    monkeypatch.setattr(whatsapp, "_append_inbox",
                        lambda item, a: posted.append(item))
    whatsapp._deliveries.clear()
    whatsapp._early_receipts.clear()
    yield SimpleNamespace(acct=acct, posted=posted, monkeypatch=monkeypatch,
                          client=None)
    whatsapp._deliveries.clear()
    whatsapp._early_receipts.clear()


def _use(env, client):
    env.acct.client = client
    env.client = client
    return client


# ── wait_delivery ────────────────────────────────────────────────────────────

def test_wait_delivery_unknown_id_returns_immediately():
    started = time.monotonic()
    out = whatsapp.wait_delivery("nao-existe", timeout=5)
    assert out["state"] == "unknown"
    assert out["delivered"] is False
    assert time.monotonic() - started < 1


def test_wait_delivery_wakes_on_delivered_receipt(env):
    msg_id = whatsapp._remember_send(SimpleNamespace(ID="MSG1"), env.acct)
    assert msg_id == "MSG1"
    out = {}

    def waiter():
        out.update(whatsapp.wait_delivery("MSG1", timeout=2))

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.05)
    whatsapp._on_receipt(SimpleNamespace(MessageIDs=["MSG1"], Type=DELIVERED))
    thread.join(timeout=2)
    assert out["state"] == "delivered"
    assert out["delivered"] is True
    assert out["read"] is False


def test_read_receipt_also_marks_delivered(env):
    whatsapp._remember_send(SimpleNamespace(ID="MSG2"), env.acct)
    whatsapp._on_receipt(SimpleNamespace(MessageIDs=["MSG2"], Type=READ))
    out = whatsapp.wait_delivery("MSG2", timeout=0.1)
    assert out["state"] == "read"
    assert out["delivered"] is True
    assert out["read"] is True


def test_wait_delivery_times_out_as_pending(env):
    whatsapp._remember_send(SimpleNamespace(ID="MSG3"), env.acct)
    out = whatsapp.wait_delivery("MSG3", timeout=0.15)
    assert out["state"] == "pending"
    assert out["delivered"] is False
    assert out["waited"] >= 0.1


def test_receipt_for_another_message_is_ignored(env):
    whatsapp._remember_send(SimpleNamespace(ID="MSG4"), env.acct)
    whatsapp._on_receipt(SimpleNamespace(MessageIDs=["OUTRA"], Type=DELIVERED))
    assert whatsapp.wait_delivery("MSG4", timeout=0).get("delivered") is False


def test_receipt_before_registration_is_not_lost(env):
    # A corrida real: o destinatário confirma no mesmo instante em que o
    # send_* ainda está voltando com o ID. O recibo chegava antes de existir
    # registro e era descartado — o áudio entregue ficava "aguardando".
    whatsapp._on_receipt(SimpleNamespace(MessageIDs=["MSGEARLY"], Type=DELIVERED))
    whatsapp._remember_send(SimpleNamespace(ID="MSGEARLY"), env.acct)
    out = whatsapp.wait_delivery("MSGEARLY", timeout=0.1)
    assert out["state"] == "delivered"
    assert out["delivered"] is True
    assert out["read"] is False


def test_read_receipt_before_registration_is_not_lost(env):
    whatsapp._on_receipt(SimpleNamespace(MessageIDs=["MSGEARLY2"], Type=READ))
    whatsapp._remember_send(SimpleNamespace(ID="MSGEARLY2"), env.acct)
    out = whatsapp.wait_delivery("MSGEARLY2", timeout=0.1)
    assert out["state"] == "read"
    assert out["delivered"] is True
    assert out["read"] is True


# ── os três estados falados no envio de texto ────────────────────────────────

def test_send_reports_delivered_with_receipt(env):
    _use(env, _FakeClient(SimpleNamespace(ID="TXT1"), receipt_type=DELIVERED))
    ok, msg = whatsapp.send("Fulano", "oi", account="teste")
    assert ok is True
    assert "confirmou a entrega" in msg
    assert env.posted[-1]["id"] == "TXT1"


def test_send_reports_awaiting_without_receipt(env):
    _use(env, _FakeClient(SimpleNamespace(ID="TXT2")))
    ok, msg = whatsapp.send("Fulano", "oi", account="teste")
    assert ok is True
    assert "aguardando confirmação de entrega" in msg
    assert "confirmou" not in msg


def test_send_without_id_never_claims_it_was_sent(env):
    _use(env, _FakeClient(SimpleNamespace()))
    ok, msg = whatsapp.send("Fulano", "oi", account="teste")
    # Sem ID não há envio: devolver ok=True aqui era o "sucesso fantasma".
    assert ok is False
    assert "não foi confirmado" in msg
    assert "mandei" not in msg
    assert env.posted[-1]["id"] == ""


def test_send_that_raises_is_failure(env):
    class _Boom:
        def send_message(self, target, text):
            raise RuntimeError("sem rede")

    _use(env, _Boom())
    ok, msg = whatsapp.send("Fulano", "oi", account="teste")
    assert ok is False
    assert "recusou o envio" in msg


# ── texto longo: partes numeradas, sem corte e sem "..." ────────────────────

def test_short_text_is_one_part_without_ellipsis():
    assert whatsapp.split_text("tudo certo") == ["tudo certo"]


def test_long_text_splits_on_sentence_boundaries_and_rebuilds():
    text = " ".join(f"Frase número {i}, com vírgula e ponto." for i in range(1, 80))

    parts = whatsapp.split_text(text, limit=200)

    assert len(parts) > 1
    assert all(len(part) <= 200 for part in parts)
    assert " ".join(parts) == text          # nada se perde na divisão
    for part in parts:
        assert part[-1] in ".!?…"           # quebra em fim de frase real
        assert not part.endswith("...")


def test_split_never_breaks_a_url():
    url = "https://example.com/" + "x" * 300
    text = f"Veja o link {url} para os detalhes. Outra frase no fim."

    parts = whatsapp.split_text(text, limit=100)

    assert len(parts) > 1
    assert any(url in part for part in parts)


def test_long_message_is_sent_in_numbered_parts_and_rebuilds(env, speaker):
    client = _use(env, _FakeClient(SimpleNamespace(ID="LONG1")))
    text = " ".join(f"Frase {i} do relatório completo." for i in range(1, 250))

    ok, msg = whatsapp.send("Fulano", text, account="teste")

    assert ok is True
    assert "em" in msg and "partes" in msg
    sent = [call[2] for call in client.calls]
    assert len(sent) > 1
    assert sent[0].startswith("*Gino:*\n(1/")
    for i, part in enumerate(sent, 1):
        assert f"({i}/{len(sent)})" in part
        assert not part.endswith("...")
        assert not part.endswith("…")
    rebuilt = []
    for part in sent:
        part = part.removeprefix("*Gino:*\n")
        part = re.sub(r"^\(\d+/\d+\)\s*", "", part)
        rebuilt.append(part)
    assert " ".join(rebuilt) == text


def test_group_jid_is_sent_to_the_group_itself(monkeypatch):
    """A resposta a um grupo usa o JID do grupo, não um telefone inventado."""
    pytest.importorskip("neonize")
    acct = whatsapp._Account("teste", "uuid-teste", default=True)
    monkeypatch.setattr(whatsapp, "_accounts", {"uuid-teste": acct})
    monkeypatch.setattr(whatsapp, "_ensure_accounts", lambda: None)

    target, choices = whatsapp.resolve("120363000000000000@g.us")

    assert choices == []
    assert target is not None
    assert str(getattr(target, "Server", "")) == "g.us"
    assert str(getattr(target, "User", "")) == "120363000000000000"


# ── mídia ────────────────────────────────────────────────────────────────────

def test_send_media_reports_delivered(env, monkeypatch, tmp_path):
    audio = tmp_path / "voz.ogg"
    audio.write_bytes(b"x")
    monkeypatch.setattr(whatsapp, "_resolve_file", lambda *a, **k: audio)
    _use(env, _FakeClient(SimpleNamespace(ID="AUD1"), receipt_type=DELIVERED))
    ok, msg = whatsapp.send_media("Fulano", str(audio), account="teste")
    assert ok is True
    assert msg.startswith("mandei o áudio")
    assert "confirmou a entrega" in msg
    assert env.posted[-1]["id"] == "AUD1"


def test_send_media_reports_awaiting_without_receipt(env, monkeypatch, tmp_path):
    audio = tmp_path / "voz.ogg"
    audio.write_bytes(b"x")
    monkeypatch.setattr(whatsapp, "_resolve_file", lambda *a, **k: audio)
    _use(env, _FakeClient(SimpleNamespace(ID="AUD2")))
    ok, msg = whatsapp.send_media("Fulano", str(audio), account="teste")
    assert ok is True
    assert "mandei o áudio" in msg
    assert "aguardando confirmação de entrega" in msg


def test_ptt_audio_gets_the_canonical_mimetype(env, monkeypatch, tmp_path):
    """O OGG como PTT precisa de `audio/ogg; codecs=opus`, não `audio/ogg`.

    O neonize monta o AudioMessage com o mimetype que o libmagic devolve
    (`audio/ogg`). O celular do dono não entrega o áudio nesse formato; o
    dispositivo pareado baixa, o que já fez uma autópsia declarar "entregue".
    """
    audio = tmp_path / "voz.ogg"
    audio.write_bytes(b"x")
    monkeypatch.setattr(whatsapp, "_resolve_file", lambda *a, **k: audio)

    class _AudioClient:
        def __init__(self):
            self.msg = None

        def build_audio_message(self, path, ptt=False):
            return SimpleNamespace(audioMessage=SimpleNamespace(
                mimetype="audio/ogg", PTT=ptt, waveform=b""))

        def send_message(self, target, msg):
            self.msg = msg
            return SimpleNamespace(ID="AUD-PTT")

        def send_audio(self, *a, **k):
            raise AssertionError("devia enviar o AudioMessage corrigido")

    client = _use(env, _AudioClient())
    ok, _msg = whatsapp.send_media("Fulano", str(audio), account="teste")
    assert ok is True
    assert client.msg.audioMessage.mimetype == "audio/ogg; codecs=opus"
    assert client.msg.audioMessage.PTT is True
    assert env.posted[-1]["id"] == "AUD-PTT"


def test_ptt_falls_back_to_neonize_when_build_is_unavailable(env, monkeypatch,
                                                            tmp_path):
    audio = tmp_path / "voz.ogg"
    audio.write_bytes(b"x")
    monkeypatch.setattr(whatsapp, "_resolve_file", lambda *a, **k: audio)
    client = _use(env, _FakeClient(SimpleNamespace(ID="AUD4")))
    whatsapp.send_media("Fulano", str(audio), account="teste")
    assert client.calls == [("audio", "5511999999999", str(audio), True)]


def test_audio_waveform_is_64_levels(tmp_path):
    import math
    import shutil
    import struct
    import wave

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg indisponível")
    wav = tmp_path / "tom.wav"
    rate = 16000
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(rate // 4)))
    waveform = whatsapp._audio_waveform(wav)
    assert len(waveform) == 64
    assert max(waveform) > 0
    assert all(0 <= b <= 100 for b in waveform)


# ── o plugin propaga o resultado real ───────────────────────────────────────

@pytest.fixture
def plugin(monkeypatch):
    monkeypatch.setattr(whatsapp_control, "_resolve_recipient",
                        lambda to: (str(to), ""))
    monkeypatch.setattr(whatsapp_control, "_ambiguous", lambda account: None)


@pytest.mark.parametrize("phrase", [
    "enviado e entregue para Fulano",
    "enviei a mensagem para Fulano, aguardando confirmação de entrega",
])
def test_plugin_send_propagates_success_states(plugin, monkeypatch, phrase):
    monkeypatch.setattr(whatsapp, "send", lambda *a, **k: (True, phrase))
    spoken, body = whatsapp_control._do_send("Fulano", "oi")
    assert spoken == phrase
    assert body == phrase


def test_plugin_send_marks_failure(plugin, monkeypatch):
    reason = "o WhatsApp recusou o envio para Fulano: sem rede"
    monkeypatch.setattr(whatsapp, "send", lambda *a, **k: (False, reason))
    spoken, body = whatsapp_control._do_send("Fulano", "oi")
    assert spoken == f"falhou: {reason}"
    assert body == reason


@pytest.mark.parametrize("phrase", [
    "mandei o áudio e o WhatsApp confirmou a entrega para Fulano",
    "mandei o áudio para Fulano, aguardando confirmação de entrega",
])
def test_plugin_send_file_propagates_success_states(plugin, monkeypatch, phrase):
    monkeypatch.setattr(whatsapp, "send_media", lambda *a, **k: (True, phrase))
    spoken, body = whatsapp_control._do_send_file("Fulano", "/tmp/voz.ogg", "")
    assert spoken == phrase
    assert body == phrase


def test_plugin_send_file_marks_failure(plugin, monkeypatch):
    reason = "o WhatsApp recusou o envio para Fulano: formato"
    monkeypatch.setattr(whatsapp, "send_media", lambda *a, **k: (False, reason))
    spoken, body = whatsapp_control._do_send_file("Fulano", "/tmp/x.mkv", "")
    assert spoken == f"falhou: {reason}"
    assert body == reason


# ── assinatura do TEXTO: um ponto só, e quem fala decide ────────────────────
#
# Regra do dono: texto leva o nome de quem fala; áudio nunca. O `speaker=` do
# `whatsapp.send` é o único ponto de decisão; `signed=False` é o texto ditado
# pelo dono para outra pessoa (a mensagem é dele, não do assistente).

@pytest.fixture
def speaker(monkeypatch):
    monkeypatch.setattr(whatsapp, "current_speaker", lambda: "Gino")


def test_assistant_text_is_signed_with_the_current_speaker(env, speaker):
    client = _use(env, _FakeClient(SimpleNamespace(ID="SIG1")))
    ok, _msg = whatsapp.send("Fulano", "tudo certo", account="teste")
    assert ok is True
    assert client.calls[-1] == ("text", "5511999999999",
                                "*Gino:*\ntudo certo")


def test_persona_text_is_signed_with_the_persona_name(env):
    client = _use(env, _FakeClient(SimpleNamespace(ID="SIG2")))
    whatsapp.send("Fulano", "revisado", account="teste", speaker="Aline")
    assert client.calls[-1][2] == "*Aline:*\nrevisado"


def test_dictated_owner_text_has_no_signature(env, speaker):
    client = _use(env, _FakeClient(SimpleNamespace(ID="SIG3")))
    whatsapp.send("Fulano", "eu te amo", account="teste", signed=False)
    assert client.calls[-1][2] == "eu te amo"


def test_an_existing_signature_is_not_duplicated(env, speaker):
    client = _use(env, _FakeClient(SimpleNamespace(ID="SIG4")))
    whatsapp.send("Fulano", "*Gino:*\ntudo certo", account="teste")
    assert client.calls[-1][2] == "*Gino:*\ntudo certo"


def test_plugin_send_signs_the_assistant_text(env, plugin, speaker):
    client = _use(env, _FakeClient(SimpleNamespace(ID="SIG5")))
    spoken, body = whatsapp_control._do_send("Fulano", "tudo certo")
    assert spoken == body
    assert client.calls[-1][2] == "*Gino:*\ntudo certo"


def test_plugin_dictated_message_keeps_the_owner_voice(env, plugin, speaker):
    client = _use(env, _FakeClient(SimpleNamespace(ID="SIG6")))
    whatsapp_control._do_send("Fulano", "eu te amo", dictated=True)
    assert client.calls[-1][2] == "eu te amo"


def test_run_maps_the_dictated_parameter_to_the_unsigned_send(env, plugin,
                                                              speaker):
    client = _use(env, _FakeClient(SimpleNamespace(ID="SIG7")))
    whatsapp_control.run({"action": "send", "to": "Fulano",
                          "text": "eu te amo", "dictated": True})
    assert client.calls[-1][2] == "eu te amo"
    whatsapp_control.run({"action": "send", "to": "Fulano",
                          "text": "tudo certo"})
    assert client.calls[-1][2] == "*Gino:*\ntudo certo"
