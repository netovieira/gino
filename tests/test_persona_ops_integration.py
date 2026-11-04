"""Ligação mínima no main.py: boca do Gino calada no relay e no interno."""
from __future__ import annotations

import threading
from unittest import mock

import main
from core.persona_ops.wiring import build

PERSONA = {
    "id": "aline",
    "name": "Aline",
    "gender": "f",
    "voice_id": "voice-aline",
}


class _UI:
    def __init__(self):
        self.logs = []

    def write_log(self, text):
        self.logs.append(text)


def test_forwarded_owner_turn_does_not_speak_on_gino_done():
    obj = object.__new__(main.JarvisLive)
    turn_id = "t-relay"
    obj._qoder_ignore = False
    obj._qoder_lock = threading.RLock()
    obj._qoder_turns = {turn_id: "Aline, revisa o card"}
    obj._qoder_meta = {
        turn_id: {
            "speak": False,
            "source": "owner",
            "reply_channel": "voice",
            "persona_relay": True,
            "persona_slug": "aline",
        }
    }
    obj._qoder_text_buf = {turn_id: "Vou pedir a ela."}
    obj._qoder_spoken = {turn_id: []}
    obj._qoder_say = mock.Mock()
    obj._qoder_finish = lambda _turn_id: None
    obj._qoder_say_events = {}
    obj._memory_librarian = None
    obj._voice_training_hint = lambda: None

    main.JarvisLive._qoder_on_done(obj, turn_id, "Aline vai revisar o card.")

    obj._qoder_say.assert_not_called()


def test_persona_say_is_silent_for_gino_and_speaks_for_owner(tmp_path):
    obj = object.__new__(main.JarvisLive)
    obj._qoder_ignore = False
    obj._say_now = mock.Mock()
    obj.ui = _UI()
    ops = build(
        tmp_path, audit_path=tmp_path / "audit.jsonl",
        speak=lambda ticket: main.JarvisLive._persona_ops_speak(obj, ticket))
    obj._persona_ops_rt = ops
    obj._persona_ops_lock = threading.Lock()

    ops.note_ask("aline", "t-interno", "gino")
    with mock.patch("main._personas.get", return_value=PERSONA):
        main.JarvisLive._persona_say(obj, "aline", "Resumo para o Gino.")
    obj._say_now.assert_not_called()

    ops.finish_ask("aline")
    ops.note_ask("aline", "t-dono", "owner")
    with mock.patch("main._personas.get", return_value=PERSONA):
        main.JarvisLive._persona_say(obj, "aline", "Revisado, criador.")
    obj._say_now.assert_called_once()
    assert obj._say_now.call_args.args[0] == "Revisado, criador."
    assert obj._say_now.call_args.kwargs["voice_id"] == "voice-aline"
