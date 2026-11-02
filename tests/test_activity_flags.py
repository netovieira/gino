"""Flags de áudio no Activity Log: formato, retrocompatibilidade e origem."""
from __future__ import annotations

import json
import re
import threading
import time
import unittest
from unittest import mock

import main
from core import activity


STAMP_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+([^:]{1,40}):\s?(.*)$")


class MetaFormatTest(unittest.TestCase):
    def test_suffix_is_parseable_and_strip_restores_the_old_line(self):
        line = "[2026-11-01 17:10:02] Gino: Bom dia, chefe."
        meta = activity.make_meta(turn=12, chan="voz", requested=True,
                                  generated=True, shown="Bom dia, chefe.")
        flagged = activity.with_meta(line, meta)
        self.assertTrue(flagged.startswith(line))
        data = activity.parse(flagged)
        self.assertEqual(data["turn"], 12)
        self.assertEqual(data["chan"], "voz")
        self.assertIs(data["audio_requested"], True)
        self.assertIs(data["audio_generated"], True)
        self.assertNotIn("tts", data)          # spoken == shown
        self.assertEqual(activity.strip(flagged), line)

    def test_legacy_line_has_no_flags_and_stays_untouched(self):
        line = "[2026-11-01 16:51:57] SYS: Microphone muted."
        self.assertIsNone(activity.parse(line))
        self.assertEqual(activity.strip(line), line)

    def test_old_reader_still_sees_the_same_source_and_text_prefix(self):
        flagged = activity.with_meta(
            "[2026-11-01 17:10:02] Gino: Bom dia, chefe.",
            activity.make_meta(turn=3, chan="voz", requested=0, generated=0))
        m = STAMP_RE.match(flagged)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2).strip(), "Gino")
        self.assertTrue(m.group(3).startswith("Bom dia, chefe."))

    def test_tts_is_recorded_only_when_the_spoken_text_diverges(self):
        meta = activity.make_meta(turn=1, chan="voz", requested=True,
                                  generated=True,
                                  shown="Custa R$ 10.",
                                  spoken="Custa dez reais.")
        self.assertEqual(meta["tts"], "Custa dez reais.")
        meta = activity.make_meta(turn=1, chan="voz", requested=True,
                                  generated=True,
                                  shown="Bom dia.", spoken="Bom dia.")
        self.assertNotIn("tts", meta)

    def test_lang_comes_from_the_existing_stopword_detector(self):
        meta = activity.make_meta(turn=1, chan="voz", requested=True,
                                  generated=True,
                                  shown="The file is not available to you.")
        self.assertEqual(meta["lang"], "en")
        meta = activity.make_meta(turn=1, chan="voz", requested=True,
                                  generated=True,
                                  shown="Não consegui abrir o arquivo para você.")
        self.assertEqual(meta["lang"], "pt")
        meta = activity.make_meta(turn=1, chan="voz", requested=True,
                                  generated=True, shown="Pronto.")
        self.assertNotIn("lang", meta)

    def test_latency_ms_is_an_integer_field_next_to_the_audio_flags(self):
        meta = activity.make_meta(turn=1, chan="voz", requested=True,
                                  generated=True, shown="Pronto.",
                                  latency_ms=2345.7)
        self.assertEqual(meta["latency_ms"], 2346)
        line = activity.with_meta(
            "[2026-11-01 17:10:02] Gino: Pronto.", meta)
        self.assertEqual(activity.parse(line)["latency_ms"], 2346)
        self.assertEqual(activity.strip(line),
                         "[2026-11-01 17:10:02] Gino: Pronto.")

    def test_invalid_latency_is_dropped_instead_of_logged(self):
        meta = activity.make_meta(turn=1, chan="voz", requested=True,
                                  generated=True, shown="oi",
                                  latency_ms=-5)
        self.assertNotIn("latency_ms", meta)
        meta = activity.make_meta(turn=1, chan="voz", requested=True,
                                  generated=True, shown="oi",
                                  latency_ms=10 * 60 * 1000 + 1)
        self.assertNotIn("latency_ms", meta)
        meta = activity.make_meta(turn=1, chan="voz", requested=True,
                                  generated=True, shown="oi",
                                  latency_ms="não é número")
        self.assertNotIn("latency_ms", meta)

    def test_unknown_channel_is_dropped_instead_of_invented(self):
        meta = activity.make_meta(turn=1, chan="telepatia", requested=True,
                                  generated=True, shown="oi")
        self.assertNotIn("chan", meta)


class _UI:
    def __init__(self):
        self.logs = []

    def write_log(self, text):
        self.logs.append(text)

    def holo_transcript(self, _text):
        pass


class _Queue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


class _Loop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class SayBlockingFlagsTest(unittest.TestCase):
    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._speech_epoch = 0
        obj._asst_name = "Gino"
        obj._session_log = []
        obj._turn_done_event = None
        obj._turn_ref = 7
        obj._turn_input_chan = "voz"
        obj._speech_audit = {}
        obj._speech_audit_lock = threading.RLock()
        obj.audio_in_queue = _Queue()
        obj._loop = _Loop()
        obj.ui = _UI()
        obj._wait_for_user_quiet = lambda: True
        return obj

    def test_generated_audio_line_carries_turn_flags_and_tts(self):
        obj = self._obj()
        with mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.numwords.speakable",
                           side_effect=lambda text, style="words": text + " falado"), \
                mock.patch("core.brain.synth_pcm24k",
                           return_value=b"\x00" * 2400):
            obj._say_blocking("dá R$ 10", label="Gino")
        data = activity.parse(obj.ui.logs[-1])
        self.assertEqual(data["turn"], 7)
        self.assertEqual(data["chan"], "voz")
        self.assertIs(data["audio_requested"], True)
        self.assertIs(data["audio_generated"], True)
        self.assertEqual(data["tts"], "dá R$ 10 falado")
        self.assertTrue(obj.audio_in_queue.items)

    def test_first_generated_audio_carries_measured_latency(self):
        obj = self._obj()
        obj._turn_t0 = time.monotonic() - 1.234
        with mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.brain.synth_pcm24k",
                           return_value=b"\x00" * 2400):
            obj._say_blocking("bom dia", label="Gino")
        data = activity.parse(obj.ui.logs[-1])
        self.assertIn("latency_ms", data)
        self.assertGreaterEqual(data["latency_ms"], 1000)
        self.assertLess(data["latency_ms"], 5000)

    def test_tts_failure_is_logged_as_requested_but_not_generated(self):
        obj = self._obj()
        with mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.brain.synth_pcm24k",
                           side_effect=RuntimeError("sem voz")):
            obj._say_blocking("bom dia", label="Gino")
        data = activity.parse(obj.ui.logs[-1])
        self.assertIs(data["audio_requested"], True)
        self.assertIs(data["audio_generated"], False)
        self.assertEqual(obj.audio_in_queue.items, [])


class TurnAnswerLineTest(unittest.TestCase):
    def _obj(self):
        obj = SayBlockingFlagsTest._obj(self)
        obj.ui = _UI()
        return obj

    def test_line_waits_for_the_last_sentence_and_reports_the_fact(self):
        obj = self._obj()
        obj._turn_ref = 9
        event = threading.Event()

        def _worker():
            # A síntese só termina depois que a linha foi pedida.
            obj._note_speech("bom dia", "bom dia", generated=True,
                             turn=9, origin="qoder")
            event.set()

        obj._write_answer_line("Gino: bom dia", chan="voz", requested=True,
                               shown="bom dia", turn=9,
                               only_origin="qoder", event=event)
        self.assertEqual(obj.ui.logs, [])      # ainda não confirmado
        threading.Thread(target=_worker).start()
        event.wait(2.0)
        deadline = threading.Event()
        deadline.wait(0.5)
        data = activity.parse(obj.ui.logs[-1])
        self.assertIs(data["audio_generated"], True)
        self.assertEqual(data["turn"], 9)

    def test_spoke_without_request_is_visible_as_that_pair(self):
        line = activity.with_meta(
            "WHATSAPP Gino: resposta",
            activity.make_meta(turn=4, chan="whatsapp", requested=False,
                               generated=True, shown="resposta"))
        data = activity.parse(line)
        self.assertFalse(data["audio_requested"])
        self.assertTrue(data["audio_generated"])


if __name__ == "__main__":
    unittest.main()
