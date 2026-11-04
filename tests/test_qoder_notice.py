"""A resposta a uma notificação de segundo plano tem que virar fala do dono."""
from __future__ import annotations

import threading
import unittest

import main


class _Fake:
    """Minimal JarvisLive surface used by _qoder_on_text/_qoder_notice."""

    def __init__(self):
        obj = object.__new__(main.JarvisLive)
        obj._qoder_lock = threading.RLock()
        obj._qoder_turns = {}
        obj._qoder_current_id = None
        obj._qoder_meta = {}
        obj._qoder_ignore = False
        obj._awake = True
        obj._chat_memory = None
        obj.said = []
        obj.notes = []
        obj._qoder_touch = lambda: None
        obj._cancel_narration = lambda *_a, **_k: None
        obj._qoder_say = lambda *a, **k: obj.said.append(("SAY", a, k))
        obj._say_now = lambda text, **k: obj.said.append((text, k))
        obj._index_event = lambda *a, **k: obj.notes.append((a, k))
        # O canal de texto é coberto em test_completion_whatsapp; aqui não se
        # toca no WhatsApp real (sem config/rede/thread).
        obj._completion_notify_owner = lambda *a, **k: False
        self.obj = obj


class QoderNoticeTest(unittest.TestCase):
    def setUp(self):
        from core import wake_announce
        wake_announce.reset()
        self.addCleanup(wake_announce.reset)

    def test_notification_answer_without_turn_is_spoken(self):
        obj = _Fake().obj

        main.JarvisLive._qoder_on_text(
            obj, None, "A tarefa terminou. O recibo foi confirmado.", False)

        spoken = [item[0] for item in obj.said]
        self.assertEqual(spoken, ["A tarefa terminou.", "O recibo foi confirmado."])
        self.assertFalse(any(item[0] == "SAY" for item in obj.said),
                         "não há turno registrado para falar pelo pipeline do turno")

    def test_microphone_mute_does_not_silence_the_announcement(self):
        obj = _Fake().obj
        obj.ui = type("UI", (), {"muted": True})()

        main.JarvisLive._qoder_on_text(obj, None, "Terminei a tarefa.", False)

        self.assertEqual([item[0] for item in obj.said], ["Terminei a tarefa."])

    def test_barge_in_silences_the_notification(self):
        obj = _Fake().obj
        obj._qoder_ignore = True

        main.JarvisLive._qoder_on_text(obj, None, "Terminei a tarefa.", False)

        self.assertEqual(obj.said, [])

    def test_unknown_turn_id_is_not_mistaken_for_a_notification(self):
        obj = _Fake().obj

        main.JarvisLive._qoder_on_text(obj, "id-antigo", "Texto atrasado.", False)

        self.assertEqual(obj.said, [])


if __name__ == "__main__":
    unittest.main()
