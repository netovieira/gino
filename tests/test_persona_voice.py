"""Persona voice/handoff integration: per-line Fish voice, handoff, cancellation."""
from __future__ import annotations

import threading
import unittest
from unittest import mock

import main
from core import brain, tool_bridge


class _UI:
    def __init__(self):
        self.logs = []

    def write_log(self, text):
        self.logs.append(text)


class _Queue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


class _Loop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


PERSONA = {
    "id": "fulana",
    "name": "Fulana",
    "gender": "f",
    "voice_id": "voice-fulana",
}


class FishVoiceOverrideTest(unittest.TestCase):
    def test_override_wins_and_does_not_mutate_the_caller(self):
        st = {"fish_voice_id": "global", "tts_engine": "fish"}
        with mock.patch("memory.config_manager.get_plugin_config",
                        return_value={"fish_voice_id": "global",
                                      "fish_model": "s2.1-pro-free"}):
            brain.refresh_fish_voice(st, "voice-fulana", override=True)
        self.assertEqual(st["fish_voice_id"], "voice-fulana")

        original = {"fish_voice_id": "global", "tts_engine": "fish"}
        state = dict(original)
        with mock.patch("memory.config_manager.get_plugin_config",
                        return_value={"fish_voice_id": "global",
                                      "fish_model": "s2.1-pro-free"}), \
                mock.patch("core.brain._fish_pieces", return_value=["oi"]), \
                mock.patch("core.brain.tts_fish_pcm24k",
                           side_effect=lambda _text, s: s["fish_voice_id"].encode()):
            pcm = brain.synth_pcm24k("oi", state, voice_id="voice-fulana")
        self.assertEqual(pcm, b"voice-fulana")
        self.assertEqual(state, original)   # per-line override copies the dict


class SayVoiceTest(unittest.TestCase):
    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._speech_epoch = 0
        obj._asst_name = "Gino"
        obj._session_log = []
        obj._turn_done_event = None
        obj.audio_in_queue = _Queue()
        obj._loop = _Loop()
        obj.ui = _UI()
        obj._wait_for_user_quiet = lambda: True
        return obj

    def test_say_blocking_passes_voice_and_label(self):
        obj = self._obj()
        with mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.brain.synth_pcm24k",
                           return_value=b"\x00" * 2400) as synth:
            obj._say_blocking("oi", voice_id="voice-fulana", label="Fulana")
        synth.assert_called_once()
        self.assertEqual(synth.call_args.kwargs.get("voice_id"), "voice-fulana")
        self.assertTrue(any(line.startswith("Fulana:") for line in obj.ui.logs))


class PersonaHandoffTest(unittest.TestCase):
    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._qoder_lock = threading.RLock()
        obj._qoder_meta = {}
        obj._qoder_tool_count = 0
        obj._qoder_touch = lambda: None
        obj._qoder_ignore = False
        obj._asst_name = "Gino"
        obj.ui = _UI()
        obj.said = []
        obj.scheduled = []
        obj._say_now = lambda text, **kwargs: obj.said.append((text, kwargs))
        obj._schedule_narration = (
            lambda turn_id, name, tool_input: obj.scheduled.append(
                (turn_id, name, tool_input)))
        return obj

    def test_persona_ask_is_planned_without_an_immediate_announcement(self):
        obj = self._obj()
        with mock.patch("core.personas.get", return_value=PERSONA):
            main.JarvisLive._qoder_on_tool(
                obj, "t1", "mcp__jarvis__persona_ask", {"persona": "fulana"})
        self.assertEqual(obj.said, [])
        self.assertEqual(obj.scheduled[0][1], "mcp__jarvis__persona_ask")
        plan = main._narration.NarrationPolicy().plan(
            "mcp__jarvis__persona_ask", {"persona": "fulana"},
            user_text="pede pra ela revisar o texto", persona_name="Fulana")
        self.assertIn("Fulana", plan.text)
        self.assertFalse(main._narration.is_technical(plan.text))


class PersonaSpeechTest(unittest.TestCase):
    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._qoder_lock = threading.RLock()
        obj._qoder_ignore = False
        obj._persona_text_buf = {}
        obj._persona_seen = set()
        obj._qoder_touch = lambda: None
        obj.said = []
        obj._say_now = lambda text, **kwargs: obj.said.append((text, kwargs))
        return obj

    def test_persona_text_is_spoken_with_persona_voice(self):
        obj = self._obj()
        with mock.patch("core.personas.get", return_value=PERSONA):
            main.JarvisLive._qoder_on_persona_text(obj, "fulana", "Olá, mundo.")
        self.assertEqual(obj.said[0][0], "Olá, mundo.")
        self.assertEqual(obj.said[0][1]["voice_id"], "voice-fulana")
        self.assertEqual(obj.said[0][1]["label"], "Fulana")


class PersonaCancelTest(unittest.TestCase):
    def test_interrupt_aborts_all_personas(self):
        class Live:
            def __init__(self):
                self.calls = []

            def interrupt_persona(self, target):
                self.calls.append(target)

        obj = object.__new__(main.JarvisLive)
        obj._speech_epoch = 0
        obj._qoder_live = Live()
        obj._qoder_lock = threading.RLock()
        obj._persona_text_buf = {"fulana": "resto"}
        obj._persona_seen = {"fulana"}
        obj._interrupted = False
        obj._reply_active = True
        obj.audio_in_queue = None
        obj._visemes = type("V", (), {"reset": lambda self: None})()
        obj._play_cursor = 0.0
        obj._turn_done_event = None
        obj.ui = _UI()
        obj.set_speaking = lambda _value: None
        main.JarvisLive.interrupt(obj)
        self.assertEqual(obj._qoder_live.calls, ["all"])
        self.assertEqual(obj._persona_text_buf, {})


class ThinkingCueTest(unittest.TestCase):
    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._qoder_ignore = False
        obj._asst_name = "Gino"
        obj.ui = _UI()
        obj.said = []
        obj._say_now = lambda text, **kwargs: obj.said.append((text, kwargs))
        return obj

    def test_generic_legacy_cue_is_never_spoken(self):
        obj = self._obj()
        main.JarvisLive._qoder_on_thinking(obj, None, "Deixa eu pensar...")
        main.JarvisLive._qoder_on_thinking(obj, None, "Estou pensando numa solução...")
        self.assertEqual(obj.said, [])

    def test_specific_cue_uses_persona_voice(self):
        obj = self._obj()
        with mock.patch("core.personas.get", return_value=PERSONA):
            main.JarvisLive._qoder_on_thinking(
                obj, "fulana", "Enviando a mensagem para Maria no WhatsApp.")
        self.assertEqual(obj.said[0][0],
                         "Enviando a mensagem para Maria no WhatsApp.")
        self.assertEqual(obj.said[0][1]["voice_id"], "voice-fulana")
        self.assertEqual(obj.said[0][1]["label"], "Fulana")


class PersonaToolTierTest(unittest.TestCase):
    def test_persona_reads_are_safe_and_writes_are_high(self):
        obj = object.__new__(tool_bridge.ToolBridge)
        self.assertEqual(obj.risk_tier("personas", {"action": "list"}), "safe")
        self.assertEqual(obj.risk_tier("personas", {"action": "models"}), "safe")
        self.assertEqual(obj.risk_tier("personas", {"action": "create"}), "high")
        self.assertEqual(obj.risk_tier("personas", {"action": "delete"}), "high")


if __name__ == "__main__":
    unittest.main()
