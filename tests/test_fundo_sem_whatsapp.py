"""Decisão do dono (03/11/2026): aviso de fundo NÃO vaza para o WhatsApp.

Ele reclamou dos avisos de conclusão chegando no WhatsApp enquanto falava pelo
PC, e já havia cancelado a cópia por áudio antes. O que este arquivo trava:

  1. conclusão de subagente, com `owner_notify=true` e `owner_chat` cheio,
     fala na sala e não chama `core.whatsapp.send` (o controle abaixo prova
     que o canal responderia se fosse chamado);
  2. turno vindo do WhatsApp continua sendo respondido por
     `_deliver_whatsapp_answer` — o silêncio é só do aviso de fundo;
  3. lembrete da fila continua avisando o dono pelo canal antigo.
"""
from __future__ import annotations

import threading
import time
from unittest import mock

import pytest

import main
from core import task_queue, wake_announce, whatsapp
from memory import config_manager


class _UI:
    def __init__(self):
        self.muted = False
        self.logs: list[str] = []

    def write_log(self, text) -> None:
        self.logs.append(str(text))


def _live(**overrides):
    obj = object.__new__(main.JarvisLive)
    obj._awake = True
    obj._asst_name = "Gino"
    obj._qoder_ignore = False
    obj._chat_memory = None
    obj.ui = _UI()
    obj.said: list[str] = []
    obj._say_now = lambda text, **k: obj.said.append(str(text))
    obj._index_event = lambda *a, **k: None
    obj._speech_route = lambda *a, **k: {"muted": False, "subject": ""}
    for name, value in overrides.items():
        setattr(obj, name, value)
    return obj


@pytest.fixture(autouse=True)
def _fresh_announcements():
    wake_announce.reset()
    yield
    wake_announce.reset()


@pytest.fixture
def owner_channel(monkeypatch):
    """owner_notify ligado e owner_chat preenchido, com o envio interceptado."""
    settings = {"owner_notify": True, "owner_chat": "5517999999999",
                "default_account": "trabalho"}

    def _setting(namespace, key, default=None):
        assert namespace == "whatsapp"
        return settings.get(key, default)

    monkeypatch.setattr(config_manager, "get_plugin_setting", _setting)
    monkeypatch.setattr(whatsapp, "default_account_name", lambda: "trabalho")
    sent: list[tuple[str, str]] = []

    def _send(recipient, text, account=None, speaker=None, signed=True):
        sent.append((str(recipient), str(text)))
        return True, "ok"

    monkeypatch.setattr(whatsapp, "send", _send)
    return sent


def test_subagent_completion_speaks_but_never_sends_whatsapp(owner_channel):
    obj = _live()

    # Controle: com owner_notify/owner_chat preenchidos o canal responde.
    assert main.JarvisLive._whatsapp_notify_owner(obj, "controle") is True
    assert owner_channel == [("5517999999999", "controle")]

    with mock.patch.object(main, "_task_mirror", mock.Mock()), \
            mock.patch.object(main, "_metrics", mock.Mock()):
        main.JarvisLive._qoder_on_task(
            obj, {"phase": "done", "task_id": "t9", "status": "completed",
                  "description": "rodar os testes", "summary": "passou"})

    # A conclusão continua saindo na sala...
    assert obj.said == ["Terminei em segundo plano: rodar os testes."]
    # ...e nada novo foi para o WhatsApp (sem thread pendente).
    time.sleep(0.15)
    assert owner_channel == [("5517999999999", "controle")]


def test_whatsapp_turn_still_answers_through_deliver_whatsapp_answer():
    obj = _live()
    obj._qoder_lock = threading.RLock()
    obj._qoder_turns = {"t1": "pedido do dono"}
    obj._qoder_meta = {"t1": {"source": "whatsapp",
                              "reply_channel": "whatsapp", "speak": False,
                              "wa_account": "trabalho",
                              "wa_chat": "5511999999999"}}
    obj._qoder_text_buf = {"t1": "resposta no zap"}
    obj._qoder_spoken = {"t1": []}
    obj._qoder_finish = mock.Mock()
    obj._session_log: list[str] = []
    obj._memory_librarian = None
    obj._wa_orphan_turns = {}
    sends: list[tuple[str, str, str]] = []

    def _send(account, chat, text, media_type="text", speaker=""):
        sends.append((str(account), str(chat), str(text)))
        return True

    obj._send_whatsapp_answer = _send
    delivered: list[str] = []
    real = main.JarvisLive._deliver_whatsapp_answer.__get__(obj)
    obj._deliver_whatsapp_answer = lambda meta, full: (
        delivered.append(full) or real(meta, full))

    main.JarvisLive._qoder_on_done(obj, "t1", "resposta no zap")

    assert delivered == ["resposta no zap"]
    assert sends == [("trabalho", "5511999999999", "resposta no zap")]
    # E a sala continua calada para esse turno.
    assert obj.said == []


def test_queue_reminder_still_notifies_the_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(task_queue, "TASKS_PATH", tmp_path / "tasks.json")
    task_queue.reset()
    obj = _live()
    obj._qoder_lock = threading.RLock()
    obj._qoder_current_id = None
    obj._qoder_live_ready = lambda: True
    obj._is_speaking = False
    notified: list[str] = []
    done = threading.Event()

    def _notify(text):
        notified.append(str(text))
        done.set()
        return True

    obj._whatsapp_notify_owner = _notify
    try:
        task_queue.add("Tomar o remédio", kind="reminder")
        main.JarvisLive._task_watch_step(obj)
        assert done.wait(2.0), "lembrete precisa continuar avisando o dono"
        # O item foi anunciado e fechado, como antes.
        assert [row["status"] for row in task_queue.list_tasks()] == ["done"]
    finally:
        task_queue.reset()

    assert notified == ["Lembrete: Tomar o remédio"]
    assert obj.said == ["Lembrete: Tomar o remédio"]
