"""Confirmação de entrega do WhatsApp: ID do envio, recibo e frases faladas."""
from __future__ import annotations

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
    yield SimpleNamespace(acct=acct, posted=posted, monkeypatch=monkeypatch,
                          client=None)
    whatsapp._deliveries.clear()


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
    assert ok is True
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
