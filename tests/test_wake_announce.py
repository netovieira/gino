"""Acordar para anunciar uma conclusão — e aceitar uma chamada curta dormindo.

Covers the behavior added for the owner's report: a heavy execution finishing
while the assistant sleeps must wake it and speak the notice exactly once, and
a bare "Gino" must survive the local transcriber's anti-hallucination gate
while the assistant is asleep.
"""
from __future__ import annotations

import unittest
from unittest import mock

import numpy as np

import main
from core import local_stt, wake_announce


class WakeAnnounceTest(unittest.TestCase):
    def setUp(self):
        wake_announce.reset()
        self.addCleanup(wake_announce.reset)

    def _announce(self, ident, text, awake, events, now=None):
        return wake_announce.announce(
            ident, text, now=now,
            is_awake=lambda: awake,
            wake=lambda reason: events.append(("wake", reason)),
            say=lambda line: events.append(("say", line)),
        )

    def test_completion_while_asleep_wakes_and_speaks_once(self):
        events = []
        announced = self._announce("job:abc", "Terminei em segundo plano: build.",
                                   awake=False, events=events)
        self.assertTrue(announced)
        self.assertEqual(events, [
            ("wake", wake_announce.WAKE_REASON),
            ("say", "Terminei em segundo plano: build."),
        ])

    def test_awake_completion_does_not_call_wake(self):
        events = []
        self.assertTrue(self._announce("job:abc", "Pronto.", awake=True,
                                       events=events))
        self.assertEqual(events, [("say", "Pronto.")])

    def test_two_completions_are_two_announcements(self):
        events = []
        self.assertTrue(self._announce("job:1", "Primeira.", awake=False,
                                       events=events))
        self.assertTrue(self._announce("job:2", "Segunda.", awake=False,
                                       events=events))
        self.assertEqual([e for e in events if e[0] == "say"],
                         [("say", "Primeira."), ("say", "Segunda.")])

    def test_same_completion_is_never_announced_twice(self):
        events = []
        self.assertTrue(self._announce("job:1", "Terminei.", awake=False,
                                       events=events))
        self.assertFalse(self._announce("job:1", "Terminei.", awake=False,
                                        events=events))
        self.assertEqual(events, [
            ("wake", wake_announce.WAKE_REASON),
            ("say", "Terminei."),
        ])

    def test_notice_without_id_repeats_only_outside_the_window(self):
        events = []
        key = wake_announce.text_key("Terminei a tarefa.")
        self.assertTrue(self._announce(key, "Terminei a tarefa.", awake=True,
                                       events=events, now=1000.0))
        self.assertFalse(self._announce(key, "Terminei a tarefa.", awake=True,
                                        events=events, now=1001.0))
        self.assertTrue(self._announce(
            key, "Terminei a tarefa.", awake=True, events=events,
            now=1000.0 + wake_announce.REPEAT_WINDOW_S + 1))
        self.assertEqual([e[1] for e in events if e[0] == "say"],
                         ["Terminei a tarefa.", "Terminei a tarefa."])

    def test_empty_text_never_claims_the_id(self):
        self.assertFalse(wake_announce.announce(
            "job:1", "   ", is_awake=lambda: False,
            wake=lambda reason: None, say=lambda line: None))
        self.assertTrue(wake_announce.claim("job:1"))


class _Seg:
    def __init__(self, text="Gino.", no_speech_prob=0.85,
                 avg_logprob=-0.3, compression_ratio=1.0):
        self.text = text
        self.no_speech_prob = no_speech_prob
        self.avg_logprob = avg_logprob
        self.compression_ratio = compression_ratio


class _Model:
    def __init__(self, segments):
        self.segments = segments
        self.calls = []

    def transcribe(self, audio, vad_filter=False, vad_parameters=None, **kw):
        self.calls.append(kw)
        return list(self.segments), None


def _whisper(segments):
    obj = local_stt._Whisper.__new__(local_stt._Whisper)
    obj._language = "pt"
    obj._prompt = "Gino"
    model = _Model(segments)
    obj._ensure = lambda: model
    return obj, model


class ShortWakeThresholdTest(unittest.TestCase):
    def test_asleep_short_call_survives_the_no_speech_gate(self):
        obj, model = _whisper([_Seg("Gino.")])
        audio = np.zeros(1600, dtype=np.float32)

        self.assertEqual(obj._decode(audio, 5, "Gino", wake=False), "")
        self.assertEqual(obj._decode(audio, 5, "Gino", wake=True), "Gino.")
        self.assertEqual(model.calls[0]["no_speech_threshold"],
                         local_stt._NO_SPEECH_THRESHOLD)
        self.assertEqual(model.calls[1]["no_speech_threshold"],
                         local_stt.WAKE_NO_SPEECH_THRESHOLD)

    def test_window_stays_at_the_single_documented_constant(self):
        self.assertGreater(local_stt.WAKE_NO_SPEECH_THRESHOLD,
                           local_stt._NO_SPEECH_THRESHOLD)
        self.assertLessEqual(local_stt.WAKE_NO_SPEECH_THRESHOLD, 1.0)


class _FakeWhisper:
    instances = []

    def __init__(self, *args, **kwargs):
        self.wakes = []
        _FakeWhisper.instances.append(self)

    def warm(self):
        pass

    def transcribe_pcm(self, pcm, prompt="", wake=False):
        self.wakes.append(wake)
        return "Gino."


class ListenerThresholdWiringTest(unittest.TestCase):
    def test_listener_passes_its_sleep_state_as_the_wake_flag(self):
        with mock.patch.object(local_stt, "_Whisper", _FakeWhisper):
            listener = local_stt.LocalWhisperListener(
                on_text=lambda *args: None)
        self.addCleanup(listener.stop)
        fake = _FakeWhisper.instances[-1]

        self.assertEqual(listener.transcribe_pcm(b"\x00\x00" * 800), "Gino.")
        listener.sleeping = True
        self.assertEqual(listener.transcribe_pcm(b"\x00\x00" * 800), "Gino.")
        self.assertEqual(fake.wakes, [False, True])


class _UI:
    muted = False

    def __init__(self):
        self.logged = []

    def write_log(self, line):
        self.logged.append(line)


def _jarvis(awake):
    """Minimal JarvisLive surface for the completion announcement paths."""
    obj = object.__new__(main.JarvisLive)
    obj._awake = awake
    obj._qoder_ignore = False
    obj._chat_memory = None
    obj.ui = _UI()
    obj.events = []
    obj.wake = lambda reason="": obj.events.append(("wake", reason))
    obj._say_now = lambda text, **kwargs: obj.events.append(("say", text))
    obj._index_event = lambda *args, **kwargs: None
    return obj


class CompletionPathsTest(unittest.TestCase):
    def setUp(self):
        wake_announce.reset()
        self.addCleanup(wake_announce.reset)

    def test_job_completion_while_asleep_wakes_and_speaks_once(self):
        obj = _jarvis(awake=False)
        ev = {"kind": "done", "id": "j1", "task": "build", "text": "ok"}

        main.JarvisLive._deliver_qoder_event_direct(obj, ev)
        self.assertEqual(obj.events, [
            ("wake", wake_announce.WAKE_REASON),
            ("say", "Terminei em segundo plano: build."),
        ])

        # O mesmo id chegando de novo (retry do sidecar) não repete a fala.
        main.JarvisLive._deliver_qoder_event_direct(obj, ev)
        self.assertEqual(len([e for e in obj.events if e[0] == "say"]), 1)

    def test_two_job_completions_are_two_announcements(self):
        obj = _jarvis(awake=True)

        main.JarvisLive._deliver_qoder_event_direct(
            obj, {"kind": "done", "id": "j1", "task": "build"})
        main.JarvisLive._deliver_qoder_event_direct(
            obj, {"kind": "done", "id": "j2", "task": "testes"})

        self.assertEqual([e[1] for e in obj.events if e[0] == "say"],
                         ["Terminei em segundo plano: build.",
                          "Terminei em segundo plano: testes."])

    def test_subagent_completion_while_asleep_wakes_and_speaks(self):
        obj = _jarvis(awake=False)
        mirror = mock.Mock()

        with mock.patch.object(main, "_task_mirror", mirror), \
                mock.patch.object(main, "_metrics", mock.Mock()):
            main.JarvisLive._qoder_on_task(
                obj, {"phase": "done", "task_id": "t9",
                      "description": "rodar os testes",
                      "status": "completed", "summary": "ok"})

        self.assertEqual(obj.events, [
            ("wake", wake_announce.WAKE_REASON),
            ("say", "Terminei em segundo plano: rodar os testes."),
        ])

    def test_notice_without_turn_wakes_when_asleep(self):
        obj = _jarvis(awake=False)

        main.JarvisLive._qoder_notice(obj, "A tarefa terminou. O recibo saiu.")

        self.assertEqual(obj.events, [
            ("wake", wake_announce.WAKE_REASON),
            ("say", "A tarefa terminou."),
            ("say", "O recibo saiu."),
        ])
        main.JarvisLive._qoder_notice(obj, "A tarefa terminou. O recibo saiu.")
        self.assertEqual(len([e for e in obj.events if e[0] == "say"]), 2)


if __name__ == "__main__":
    unittest.main()
