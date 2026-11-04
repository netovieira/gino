"""Prosa de saída: tag do Fish injetada e limpa, e o caminho do dono.

O Fish S2.1 não tem parâmetro de emoção — o tom viaja como marca natural no
texto. Estes testes fixam os três pontos que importam:

* a tag entra em CADA peça que vai ao Fish;
* a tag NUNCA chega ao Edge (que leria "calm" em voz alta) nem sobra no texto;
* só a resposta do dono no turno recebe estilo: portão, persona, narração e
  WhatsApp continuam neutros.
"""
from __future__ import annotations

import array
import math
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import main
from core import brain, emotion


def _tone(hz: float = 230.0, seconds: float = 2.0, amp: float = 0.55,
          sr: int = 16000) -> bytes:
    """PCM s16le de um tom vocalmente plausível (ativação alta)."""
    return array.array(
        "h",
        (int(amp * 32767 * math.sin(2 * math.pi * hz * i / sr))
         for i in range(int(sr * seconds)))).tobytes()


class _UI:
    def __init__(self):
        self.logs = []

    def write_log(self, text):
        self.logs.append(text)

    def set_state(self, state):
        pass


class _Queue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


class _Loop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class EdgeNeverHearsTheTagTest(unittest.TestCase):
    def test_edge_strips_the_tag_and_gets_no_style_argument(self):
        seen = []
        with mock.patch(
                "core.brain.tts_pcm24k",
                side_effect=lambda text, voice: seen.append(text)
                or b"\x00\x00"):
            pcm = brain.synth_pcm24k(
                "[calm] Bom dia.", {"tts_engine": "edge", "tts_voice": "v"},
                style="[calm]")
        self.assertEqual(seen, ["Bom dia."])
        self.assertEqual(pcm, b"\x00\x00")

    def test_edge_strips_a_leaked_tag_even_without_style(self):
        seen = []
        with mock.patch(
                "core.brain.tts_pcm24k",
                side_effect=lambda text, voice: seen.append(text)
                or b"\x00\x00"):
            brain.synth_pcm24k("[excited] Pronto!",
                               {"tts_engine": "edge", "tts_voice": "v"})
        self.assertEqual(seen, ["Pronto!"])

    def test_strip_keeps_brackets_in_the_middle_alone(self):
        self.assertEqual(brain.strip_style_tags("[calm] Oi."), "Oi.")
        self.assertEqual(brain.strip_style_tags("[calm][excited] Oi."), "Oi.")
        self.assertEqual(brain.strip_style_tags("Vale [10] reais."),
                         "Vale [10] reais.")


class FishTagOnEveryPieceTest(unittest.TestCase):
    def test_tag_goes_to_each_piece_and_gap_is_kept(self):
        st = {"tts_engine": "fish"}
        calls = []

        def fake(text, settings, style=None):
            calls.append((text, style))
            return b"\x00\x00"

        with mock.patch("core.brain.refresh_fish_voice", return_value=st), \
                mock.patch("core.brain._fish_pieces",
                           return_value=["peça um", "peça dois"]), \
                mock.patch("core.brain.tts_fish_pcm24k", side_effect=fake):
            pcm = brain.synth_pcm24k("texto longo", st, style="[calm]")

        self.assertEqual(calls, [("peça um", "[calm]"),
                                 ("peça dois", "[calm]")])
        self.assertEqual(len(pcm), 4 + len(brain._FISH_GAP))

    def test_old_two_argument_call_shape_still_works(self):
        st = {"tts_engine": "fish"}
        with mock.patch("core.brain.refresh_fish_voice", return_value=st), \
                mock.patch("core.brain._fish_pieces", return_value=["oi"]), \
                mock.patch("core.brain.tts_fish_pcm24k",
                           side_effect=lambda text, settings: b"\x01\x02"):
            self.assertEqual(brain.synth_pcm24k("oi", st), b"\x01\x02")

    def test_single_piece_gets_the_tag(self):
        st = {"tts_engine": "fish"}
        calls = []
        with mock.patch("core.brain.refresh_fish_voice", return_value=st), \
                mock.patch("core.brain._fish_pieces", return_value=["oi"]), \
                mock.patch("core.brain.tts_fish_pcm24k",
                           side_effect=lambda text, settings, style=None:
                           calls.append(style) or b"\x00\x00"):
            brain.synth_pcm24k("oi", st, style="[excited]")
        self.assertEqual(calls, ["[excited]"])


class FishBodyTest(unittest.TestCase):
    def _post(self, text, style):
        response = mock.Mock(status_code=200, text="", content=b"RIFF")
        response.raise_for_status.return_value = None
        decoded = mock.Mock(samples=[0, 0, 0, 0])
        with mock.patch("core.brain.requests.post",
                        return_value=response) as post, \
                mock.patch("miniaudio.decode", return_value=decoded):
            brain.tts_fish_pcm24k(
                text, {"fish_api_key": "k", "fish_voice_id": "v"}, style=style)
        return post.call_args.kwargs["json"]["text"]

    def test_tag_is_prepended_to_the_request_text(self):
        self.assertEqual(self._post("Oi.", "[calm]"), "[calm] Oi.")

    def test_tag_already_in_the_text_is_not_duplicated(self):
        self.assertEqual(self._post("[calm] Oi.", "[calm]"), "[calm] Oi.")

    def test_without_style_the_text_is_untouched(self):
        self.assertEqual(self._post("Oi.", ""), "Oi.")


def _say_obj(turn_ref: int = 5) -> main.JarvisLive:
    obj = object.__new__(main.JarvisLive)
    obj._speech_epoch = 0
    obj._asst_name = "Gino"
    obj._session_log = []
    obj._turn_done_event = None
    obj._turn_ref = turn_ref
    obj._turn_input_chan = "voz"
    obj._speech_audit = {}
    obj._speech_audit_lock = threading.RLock()
    obj.audio_in_queue = _Queue()
    obj._loop = _Loop()
    obj.ui = _UI()
    obj._wait_for_user_quiet = lambda: True
    obj._qoder_lock = threading.RLock()
    obj._qoder_ignore = False
    obj._qoder_first_sentence = False
    obj._turn_t0 = time.monotonic()
    obj._qoder_turns = {"t1": "oi"}
    obj._qoder_spoken = {}
    obj._qoder_meta = {}
    return obj


def _meta(mood: str = "irritado", *, age: float = 0.0,
          channel: str = "voice", turn_ref: int = 5) -> dict:
    return {"turn_ref": turn_ref, "reply_channel": channel, "emotion": mood,
            "emotion_at": time.monotonic() - age}


class SayStyleChainTest(unittest.TestCase):
    """_qoder_say deixa a dica; _say_blocking decide e repassa ao synth."""

    def _speak(self, obj, **kwargs):
        seen = {}
        with mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.brain.synth_pcm24k",
                           return_value=b"\x00" * 2400) as synth:
            obj._say_blocking("Pronto.", log=False, **kwargs)
        seen["kwargs"] = synth.call_args.kwargs
        return seen

    def test_fresh_owner_emotion_reaches_the_fish_synth(self):
        obj = _say_obj()
        obj._qoder_meta["t1"] = _meta("irritado")
        obj._say_now = lambda text, **kw: None
        main.JarvisLive._qoder_say(obj, "t1", "Pronto.")
        self.assertEqual(
            self._speak(obj, origin="qoder")["kwargs"].get("style"), "[calm]")

    def test_stale_emotion_does_not_color_a_late_answer(self):
        obj = _say_obj()
        obj._qoder_meta["t1"] = _meta("irritado", age=1000.0)
        obj._say_now = lambda text, **kw: None
        main.JarvisLive._qoder_say(obj, "t1", "Pronto.")
        self.assertNotIn("style", self._speak(obj, origin="qoder")["kwargs"])

    def test_neutral_turn_has_no_tag(self):
        obj = _say_obj()
        obj._qoder_meta["t1"] = _meta("neutro")
        obj._say_now = lambda text, **kw: None
        main.JarvisLive._qoder_say(obj, "t1", "Pronto.")
        self.assertNotIn("style", self._speak(obj, origin="qoder")["kwargs"])

    def test_whatsapp_turn_never_sets_a_tag(self):
        obj = _say_obj()
        obj._qoder_meta["t1"] = _meta("irritado", channel="whatsapp")
        self.assertEqual(obj._qoder_emotion_style("t1"), "")

    def test_gate_and_persona_lines_ignore_the_hint(self):
        obj = _say_obj()
        obj._qoder_meta["t1"] = _meta("irritado")
        obj._say_now = lambda text, **kw: None
        main.JarvisLive._qoder_say(obj, "t1", "Pronto.")
        for origin in ("narração", "persona", "aviso", "portão", ""):
            with self.subTest(origin=origin):
                self.assertNotIn(
                    "style", self._speak(obj, origin=origin)["kwargs"])

    def test_hint_from_another_turn_is_refused(self):
        obj = _say_obj(turn_ref=5)
        obj._qoder_meta["t1"] = _meta("irritado", turn_ref=4)
        obj._say_now = lambda text, **kw: None
        main.JarvisLive._qoder_say(obj, "t1", "Pronto.")
        self.assertNotIn("style", self._speak(obj, origin="qoder")["kwargs"])

    def test_missing_attributes_do_not_break_a_bare_object(self):
        obj = _say_obj()
        self.assertEqual(obj._resolve_say_style("qoder", 5), "")
        self.assertNotIn("style", self._speak(obj, origin="qoder")["kwargs"])


class WhatsAppStaysNeutralTest(unittest.TestCase):
    def test_whatsapp_tts_is_called_without_style(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        obj = object.__new__(main.JarvisLive)
        obj._asst_name = "Gino"
        obj._wa_tts_text = ""
        obj._qoder_meta = {"t1": _meta("irritado")}
        with mock.patch.object(main, "BASE_DIR", Path(tmp.name)), \
                mock.patch("core.personas.list_personas", return_value=[]), \
                mock.patch("core.whatsapp.ffmpeg_ready"), \
                mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                mock.patch("core.brain.settings",
                           return_value={"tts_engine": "fish"}), \
                mock.patch("core.numwords.speakable",
                           side_effect=lambda text, style=None: text), \
                mock.patch("core.brain.synth_pcm24k",
                           return_value=b"\x00" * 480) as synth, \
                mock.patch("subprocess.run",
                           return_value=SimpleNamespace(returncode=1)):
            out = main.JarvisLive._synthesize_whatsapp_audio(obj, "Oi.")
        self.assertEqual(out, "")
        synth.assert_called_once()
        self.assertEqual(synth.call_args.kwargs, {})


class TurnContextTest(unittest.TestCase):
    """Fase 1: a emoção do dono entra no contexto do ask como TOM."""

    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._last_user_speech = time.monotonic()
        obj._qoder_retry_used = False
        obj._device_enroll_cancel_gate = lambda *a, **k: False
        obj._restart = None
        obj._power = None
        obj._restart_answer_expected = lambda text: False
        obj._mail_send_answer_expected = lambda text: False
        obj._owner_direct_action_gate = lambda *a, **k: False
        obj._cinema_mode_gate = lambda *a, **k: False
        obj._silence_mode_gate = lambda *a, **k: False
        obj._series_mode_gate = lambda *a, **k: False
        obj._mirror_show_gate = lambda *a, **k: False
        obj._try_action_consent = lambda *a, **k: False
        obj._qoder_lock = threading.RLock()
        obj._qoder_current_id = None
        obj._qoder_ignore = False
        obj._voice_id_enabled = False
        obj._speaker = None
        obj.ui = _UI()
        obj._cancel_narration = lambda: None
        obj._narration_spoken = False
        obj._qoder_first_sentence = False
        obj._turn_t0 = time.monotonic()
        obj._qoder_turn_started = 0.0
        obj._qoder_tool_count = 0
        obj._reply_active = False
        obj._brain_busy = False
        obj._turn_done_event = None
        obj._loop = None
        obj._next_turn_ref = lambda chan="": 1
        obj._qoder_text_buf = {}
        obj._qoder_spoken = {}
        obj._qoder_turns = {}
        obj._qoder_meta = {}
        obj._qoder_last_activity = 0.0
        obj._turn_ref = 5
        obj._turn_input_chan = "voz"
        obj._recent_spoken_text = ""
        obj.calls = []

        class _Live:
            def ask(self, payload, **kw):
                obj.calls.append((payload, kw))

        obj._qoder_live = _Live()
        return obj

    def test_owner_voice_gets_the_tone_line_and_the_meta(self):
        obj = self._obj()
        pcm = (b"\x01\x00" * 32000)
        main.JarvisLive._qoder_turn(
            obj, "que raiva, nada funciona", pcm,
            epoch={"prompt": "[NOVO ASSUNTO APÓS PAUSA]"})
        self.assertEqual(len(obj.calls), 1)
        context = obj.calls[0][1]["context"]
        self.assertTrue(context.startswith("[NOVO ASSUNTO APÓS PAUSA]"))
        self.assertIn("[TOM DO DONO AGORA]", context)
        self.assertIn("não mude nenhum fato", context)
        meta = next(iter(obj._qoder_meta.values()))
        self.assertEqual(meta["emotion"], "irritado")
        self.assertGreater(meta["emotion_at"], 0.0)

    def test_typed_text_without_pcm_is_not_measured(self):
        obj = self._obj()
        main.JarvisLive._qoder_turn(obj, "que raiva, nada funciona")
        self.assertIsNone(obj.calls[0][1]["context"])
        meta = next(iter(obj._qoder_meta.values()))
        self.assertEqual(meta["emotion"], "")
        self.assertEqual(meta["emotion_at"], 0.0)

    def test_empty_or_noisy_transcript_never_runs_the_detector(self):
        for text in ("", "   ", "...", "?!", "123 456"):
            with self.subTest(text=text):
                obj = self._obj()
                with mock.patch("main._emotion.detect") as detect:
                    main.JarvisLive._qoder_turn(
                        obj, text, b"\x01\x00" * 32000)
                detect.assert_not_called()
                self.assertIsNone(obj.calls[0][1]["context"])
                meta = next(iter(obj._qoder_meta.values()))
                self.assertEqual(meta["emotion"], "")
                self.assertEqual(meta["emotion_at"], 0.0)

    def test_echo_of_our_own_reply_never_runs_the_detector(self):
        obj = self._obj()
        obj._recent_spoken_text = "bom dia, tudo certo por aqui"
        with mock.patch("main._emotion.detect") as detect:
            main.JarvisLive._qoder_turn(
                obj, "bom dia, tudo certo por aqui",
                b"\x01\x00" * 32000)
        detect.assert_not_called()
        self.assertIsNone(obj.calls[0][1]["context"])
        meta = next(iter(obj._qoder_meta.values()))
        self.assertEqual(meta["emotion"], "")

    def test_aggressive_pcm_with_calm_words_gets_no_tone_line(self):
        obj = self._obj()
        main.JarvisLive._qoder_turn(obj, "bom dia, tudo bem?", _tone())
        self.assertIsNone(obj.calls[0][1]["context"])
        meta = next(iter(obj._qoder_meta.values()))
        self.assertEqual(meta["emotion"], "neutro")
        self.assertEqual(emotion.prompt_line(meta["emotion"]), "")

    def test_whatsapp_channel_is_out_of_scope(self):
        obj = self._obj()
        main.JarvisLive._qoder_turn(
            obj, "que raiva, nada funciona", b"\x01\x00" * 32000,
            source="owner", reply_channel="whatsapp")
        self.assertIsNone(obj.calls[0][1]["context"])
        meta = next(iter(obj._qoder_meta.values()))
        self.assertEqual(meta["emotion"], "")

    def test_detection_failure_never_blocks_the_turn(self):
        obj = self._obj()
        with mock.patch("main._emotion.detect", side_effect=RuntimeError("x")):
            main.JarvisLive._qoder_turn(
                obj, "bom dia", b"\x01\x00" * 32000)
        self.assertEqual(len(obj.calls), 1)
        self.assertIsNone(obj.calls[0][1]["context"])
        meta = next(iter(obj._qoder_meta.values()))
        self.assertEqual(meta["emotion"], "")


if __name__ == "__main__":
    unittest.main()
