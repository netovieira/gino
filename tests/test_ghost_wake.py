"""O "Gino" fantasma: um nome isolado não acorda sem prova de voz.

Provas reais de 02/11/2026: clipes de 2,6–3,0 s de ruído/TV, score 0,25–0,37,
voltaram do STT como "Gino." e até "Gino. Gino. Gino.", e o assistente acordou
(com o TTS travado, portanto não era auto-eco). O caminho do despertar não
tinha nenhuma checagem de voz.

Estes testes fixam:
  * "Gino." com o voiceprint do dono ACORDA;
  * "Gino." sem prova e com score baixo NÃO acorda, e loga o descarte;
  * "Gino. Gino. Gino." num único decode NÃO acorda, mesmo com score alto;
  * "Gino, fecha isso" com prova acorda normalmente;
  * fala com conteúdo tirada da faixa cinza não é silenciada pela trava;
  * energia sozinha (um estrondo da chuva em nível pleno) não é chamado: a
    prova por nível exige também fala sustentada (13:33:12 de 02/11);
  * um decode descartado não vira "YOU:" no Activity Log;
  * o prompt do Whisper cita o nome uma vez só, e o filtro de eco roda dormindo.
"""
from __future__ import annotations

import re
import unittest
from types import SimpleNamespace
from unittest import mock

import main
from core import groq_stt


class FakeUI:
    muted = True

    def __init__(self):
        self.logs: list[str] = []
        self.state = ""

    def write_log(self, text):
        self.logs.append(text)

    def set_state(self, state):
        self.state = state


class FakeSpeaker:
    """Stands in for core.speaker_id.SpeakerId without any ONNX model."""

    owner_name = "anthero"

    def __init__(self, score=0.72, known=True, authorized=True,
                 name="Anthero"):
        self.score = score
        self.known = known
        self.authorized = authorized
        self.name = name

    def embedding(self, pcm):
        return [1.0, 0.0, 0.0]

    def identify(self, embedding, include_auto=False, **kwargs):
        return {
            "name": self.name if self.known else None,
            "score": self.score,
            "sample_score": self.score,
            "effective_score": self.score,
            "known": self.known,
            "authorized": self.authorized and self.known,
            "verdict": "known" if self.known else "unknown",
        }

    def list(self):
        return [{"name": self.name, "owner": True}]


def _asleep(speaker=None):
    """Minimal JarvisLive while asleep, enough to run the wake decision."""
    obj = object.__new__(main.JarvisLive)
    obj._loop = object()
    obj.session = object()
    obj._groq = None
    obj._current_pcm = None
    obj._restart = None
    obj._power = None
    obj._voice_id_enabled = True
    obj._speaker = speaker if speaker is not None else FakeSpeaker()
    obj._voice_train = None
    obj._series_gate = None
    obj._cinema_mode_enabled = False
    obj._qoder_live_enabled = True
    obj._qoder_live_ready = lambda: True
    obj._qoder_turn = lambda *a, **k: None
    obj._qoder_pending = None
    obj._awake = False
    obj._wake_enabled = True
    obj._asst_name = "Gino"
    obj._is_speaking = False
    obj._reply_active = False
    obj._last_user_speech = 1.0
    obj._asleep_owner_speech = []
    obj._session_log = []
    obj._turn_ref = 0
    obj._voice_indulgence_gray_floor = main.DEFAULT_VOICE_INDULGENCE_GRAY_FLOOR
    obj._begin_user_epoch = lambda: {}
    obj._memory_record_user = lambda text: None
    obj._index_event = lambda *a, **k: None
    obj._qoder_hold = lambda *a, **k: None
    obj.ui = FakeUI()
    obj.said: list[str] = []
    obj._say_now = lambda text, log=True: obj.said.append(text)
    obj.woke: list[str] = []
    obj._qoder_calls: list[tuple] = []
    obj._qoder_turn = lambda text, pcm=None, **k: obj._qoder_calls.append(
        (text, pcm))
    obj.wake = lambda reason="": (obj.woke.append(reason),
                                  setattr(obj, "_awake", True))
    return obj


def _clip(pre_rms=0.008, gain=7.0, speech_s=0.0):
    """240 ms of int16 'audio'; only the recorded features matter here."""
    return groq_stt.HeardClip(b"\x01\x00" * 1920,
                              boost_gain=gain, pre_rms=pre_rms,
                              speech_s=speech_s)


class FakeEchoGuard:
    """The only surface the wake guard needs from EchoTextGuard."""

    def __init__(self, active):
        self.active = bool(active)

    def matches(self, _text):
        return False

    def spoke_during(self, _inicio, _fim):
        return self.active


class BareAddressWakeTest(unittest.TestCase):
    def test_bare_name_with_owner_voiceprint_wakes(self):
        obj = _asleep(FakeSpeaker(score=0.72))
        main.JarvisLive._on_groq_text(obj, "Gino.", _clip(pre_rms=0.05,
                                                          gain=1.0))
        self.assertTrue(obj._awake)
        self.assertEqual(obj.woke, ["você chamou Gino"])

    def test_bare_name_without_proof_does_not_wake_and_logs(self):
        obj = _asleep(FakeSpeaker(score=0.12, known=False, name=""))
        main.JarvisLive._on_groq_text(obj, "Gino.", _clip())
        self.assertFalse(obj._awake)
        self.assertEqual(obj.woke, [])
        line = [l for l in obj.ui.logs if "endereço nu" in l]
        self.assertTrue(line, obj.ui.logs)
        self.assertIn("descartado por falta de prova", line[0])
        self.assertIn("score=0.12", line[0])
        # The log names the discarded call, never a transcript beyond it.
        self.assertIn("«Gino»", line[0])

    def test_repeated_name_in_one_decode_never_wakes(self):
        obj = _asleep(FakeSpeaker(score=0.91))
        main.JarvisLive._on_groq_text(
            obj, "Gino. Gino. Gino.", _clip(pre_rms=0.05, gain=1.0))
        self.assertFalse(obj._awake)
        self.assertEqual(obj.woke, [])
        line = [l for l in obj.ui.logs if "repetido" in l]
        self.assertTrue(line, obj.ui.logs)
        self.assertIn("3x", line[0])

    def test_name_plus_content_with_proof_wakes_normally(self):
        obj = _asleep(FakeSpeaker(score=0.80))
        main.JarvisLive._on_groq_text(obj, "Gino, fecha isso", _clip())
        self.assertTrue(obj._awake)
        self.assertEqual(obj.woke, ["você chamou Gino"])

    def test_content_phrase_is_not_silenced_by_the_trap(self):
        # Gray-band score (0.30) that already fails the voiceprint threshold.
        # The line has content, so the bare-address trap must not touch it.
        obj = _asleep(FakeSpeaker(score=0.30, known=False, name=""))
        main.JarvisLive._on_groq_text(obj, "Gino, fecha isso", _clip())
        self.assertTrue(obj._awake)
        self.assertEqual(obj.woke, ["você chamou Gino"])
        self.assertFalse(any("endereço nu" in l for l in obj.ui.logs))

    def test_energy_alone_sustains_a_full_level_clip(self):
        # No voiceprint match, but the original clip was already at full level,
        # was NOT amplified, and held a voice long enough to be a call.
        obj = _asleep(FakeSpeaker(score=0.10, known=False, name=""))
        main.JarvisLive._on_groq_text(obj, "Gino.", _clip(pre_rms=0.06,
                                                          gain=1.0,
                                                          speech_s=0.45))
        self.assertTrue(obj._awake)

    def test_full_level_thump_is_not_a_call(self):
        # 02/11 13:33:12: one rain thump reached 0.054 RMS at gain 1.0 and
        # woke the assistant while the owner sat in silence. Level alone is
        # proof of a loud moment, not of a voice: the call needs sustained
        # speech.
        obj = _asleep(FakeSpeaker(score=0.25, known=False, name=""))
        main.JarvisLive._on_groq_text(obj, "Gino.", _clip(pre_rms=0.054,
                                                          gain=1.0,
                                                          speech_s=0.13))
        self.assertFalse(obj._awake)
        line = [l for l in obj.ui.logs if "endereço nu" in l]
        self.assertTrue(line, obj.ui.logs)
        self.assertIn("fala=0.13s", line[0])

    def test_discarded_ghost_is_not_logged_as_the_owner(self):
        # The YOU line used to be written before the wake gate, so every
        # hallucination appeared in the panel as if the owner had spoken.
        obj = _asleep(FakeSpeaker(score=0.25, known=False, name=""))
        main.JarvisLive._on_groq_text(obj, "Gino.", _clip(pre_rms=0.054,
                                                          gain=1.0,
                                                          speech_s=0.10))
        self.assertFalse(obj._awake)
        self.assertFalse([l for l in obj.ui.logs if l.startswith("YOU:")],
                         obj.ui.logs)
        self.assertFalse(any("You: Gino" in s for s in obj._session_log))

    def test_real_call_is_logged_as_the_owner(self):
        obj = _asleep(FakeSpeaker(score=0.72))
        main.JarvisLive._on_groq_text(obj, "Gino.", _clip(pre_rms=0.09,
                                                          gain=1.0,
                                                          speech_s=0.5))
        self.assertTrue(obj._awake)
        self.assertTrue([l for l in obj.ui.logs if l.startswith("YOU:")],
                        obj.ui.logs)

    def test_owner_gray_floor_is_the_project_floor_not_lowered(self):
        # Single enrolled voice, score 0.40: the documented 0.38 floor accepts.
        obj = _asleep(FakeSpeaker(score=0.40, known=False, name=""))
        main.JarvisLive._on_groq_text(obj, "Gino.", _clip())
        self.assertTrue(obj._awake)

        # 0.37 is the ghost band from 02/11: still not proof.
        obj = _asleep(FakeSpeaker(score=0.37, known=False, name=""))
        main.JarvisLive._on_groq_text(obj, "Gino.", _clip())
        self.assertFalse(obj._awake)
        self.assertEqual(obj.woke, [])


class EchoGuardWakeTest(unittest.TestCase):
    def test_wake_right_after_tts_requires_more_than_the_gray_floor(self):
        # A corrupted PC profile gives the TTS tail 0.41 — inside the 0.38
        # indulgence band. During our own echo window that is not a call, and
        # level/sustained-speech alone cannot substitute for the voiceprint.
        obj = _asleep(FakeSpeaker(score=0.41, known=False, name=""))
        obj._echo_text = FakeEchoGuard(active=True)
        clip = _clip(pre_rms=0.06, gain=1.0, speech_s=0.5)
        main.JarvisLive._on_groq_text(obj, "Gino.", clip)
        self.assertFalse(obj._awake)
        self.assertTrue([l for l in obj.ui.logs if "eco do meu TTS" in l],
                        obj.ui.logs)
        ok, _score, detail = main.JarvisLive._bare_address_evidence(obj, clip)
        self.assertFalse(ok, detail)
        self.assertIn("eco_tts", detail)

    def test_strong_owner_voice_still_wakes_after_tts(self):
        # The echo window is a stricter threshold, never a blanket mute: the
        # owner answering immediately, with his real voiceprint, still wakes.
        obj = _asleep(FakeSpeaker(score=0.72))
        obj._echo_text = FakeEchoGuard(active=True)
        main.JarvisLive._on_groq_text(obj, "Gino, fecha isso", _clip())
        self.assertTrue(obj._awake)
        self.assertEqual(obj.woke, ["você chamou Gino"])


class BareAddressShapeTest(unittest.TestCase):
    def _obj(self):
        return _asleep()

    def test_pure_calls(self):
        obj = self._obj()
        for text in ("Gino.", "Gino, tá aí?", "oi Gino", "Hey Gino!"):
            self.assertTrue(obj._is_pure_bare_address(text), text)

    def test_commands_are_not_pure_calls(self):
        obj = self._obj()
        for text in ("Gino, fecha isso", "Gino, me ajuda", "Gino, que horas são?"):
            self.assertFalse(obj._is_pure_bare_address(text), text)

    def test_misheard_repetition_is_detected_by_sound(self):
        obj = self._obj()
        self.assertEqual(obj._repeated_name_only("Gino. Gino."), 2)
        self.assertEqual(obj._repeated_name_only("Jino, gino, GINO."), 3)
        self.assertEqual(obj._repeated_name_only("Gino, fecha isso"), 0)


class PromptAndEchoTest(unittest.TestCase):
    def test_hint_cites_the_name_once(self):
        obj = object.__new__(main.JarvisLive)
        obj._asst_name = "Gino"
        fake_cfg = SimpleNamespace(
            read_text=lambda encoding=None: '{"user_name": "Anthero"}')
        with mock.patch.object(main, "API_CONFIG_PATH", fake_cfg):
            hint = main.JarvisLive._stt_name_hint(obj, "pt")
        self.assertEqual(len(re.findall(r"\bgino\b", hint, re.I)), 1, hint)
        self.assertNotIn("Hey Gino", hint)

    def test_prompt_echo_filter_runs_while_asleep(self):
        listener = groq_stt.GroqListener(
            "", "test", "pt", on_text=lambda _t: None,
            on_status=lambda _m: None, sample_rate=16000,
            transcribe_fn=lambda _pcm: "",
            prompt="Gino. Anthero. legenda da comunidade amara organizada")
        self.addCleanup(listener.stop)
        listener.sleeping = True
        self.assertTrue(listener._is_prompt_echo(
            "legenda da comunidade amara organizada"))
        # A short honest call is below the 8-character fragment floor and is
        # never mistaken for an echo of the one-name hint.
        self.assertFalse(listener._is_prompt_echo("Gino, tá aí?"))


class BoostMetadataTest(unittest.TestCase):
    def test_boost_records_gain_and_original_level(self):
        import numpy as np
        quiet = np.full(16000, 160, dtype=np.int16).tobytes()  # rms ~0.005
        loud = np.full(16000, 3000, dtype=np.int16).tobytes()  # rms ~0.09
        boosted = groq_stt._boost_quiet(quiet)
        full = groq_stt._boost_quiet(loud)
        self.assertGreater(boosted.boost_gain, 4.0)
        self.assertLess(boosted.pre_rms, main.BARE_ADDRESS_MIN_RMS)
        self.assertEqual(full.boost_gain, 1.0)
        self.assertGreaterEqual(full.pre_rms, main.BARE_ADDRESS_MIN_RMS)

    def test_speech_seconds_tell_a_voice_from_a_thump(self):
        import numpy as np
        sr = 16000
        t = np.arange(sr, dtype=np.float32) / sr
        voice = (0.25 * np.sin(2 * np.pi * 180 * t)).astype(np.float32)
        rain = (0.02 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
        thump = np.zeros(sr, dtype=np.float32)
        thump[:1024] = 0.25            # one 64 ms burst, then silence
        to_pcm = lambda a: (a * 32767).astype(np.int16).tobytes()
        heard_voice = groq_stt._boost_quiet(to_pcm(voice))
        heard_rain = groq_stt._boost_quiet(to_pcm(rain))
        heard_thump = groq_stt._boost_quiet(to_pcm(thump))
        self.assertFalse(heard_rain.speech_s)
        self.assertGreaterEqual(heard_voice.speech_s, 0.9)
        self.assertLess(heard_thump.speech_s,
                        main.BARE_ADDRESS_MIN_SPEECH_S)
        self.assertGreaterEqual(heard_voice.speech_s,
                                main.BARE_ADDRESS_MIN_SPEECH_S)


if __name__ == "__main__":
    unittest.main()
