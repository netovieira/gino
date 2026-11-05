"""Modo série: progressive voice gate, DSP playback veto and spoken toggle.

No ONNX model is loaded here: the fake speaker decides identity from the
window length. The synthetic "live" and "TV" signals exercise the DSP vote
deterministically. The listener test proves a rejected segment never reaches
the transcriber (no paid call) and never reaches on_utterance (no brain).
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import main
from core import groq_stt
from core import playback_detect
from core import series_gate

SR = 16000
_rng = np.random.default_rng(7)


def _pcm(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def live_voice(seconds: float = 2.5, seed: int = 7) -> bytes:
    """A voice-like harmonic signal with jitter, syllables and a little air."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    f0 = 118 + 6 * np.sin(2 * np.pi * 3.1 * t) + 2 * np.sin(2 * np.pi * 7.7 * t)
    phase = 2 * np.pi * np.cumsum(f0) / SR
    x = np.zeros_like(t)
    for k in range(1, 20):
        if 118 * k > 7000:
            break
        x += (1.0 / k ** 1.1) * np.sin(k * phase + rng.uniform(0, 6.28))
    env = np.clip(0.5 + 0.5 * np.sin(2 * np.pi * 2.7 * t), 0.05, 1.0)
    gate = (np.sin(2 * np.pi * 0.7 * t) > -0.2).astype(float)
    x = x * env * gate
    x += 0.02 * rng.normal(size=t.size) + 0.004 * rng.normal(size=t.size)
    return _pcm(x / (np.max(np.abs(x)) + 1e-9))


def tv_speech(seconds: float = 3.0, seed: int = 7) -> bytes:
    """A produced mix: bass bed, band-limited speech, compression, no air."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    x = (0.55 * np.sin(2 * np.pi * 55 * t)
         + 0.25 * np.sin(2 * np.pi * 110 * t)
         + 0.12 * np.sin(2 * np.pi * 165 * t))
    noise = rng.normal(size=t.size)
    spec = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(t.size, 1 / SR)
    spec[(freqs < 300) | (freqs > 3400)] = 0
    noise = np.fft.irfft(spec, n=t.size)
    noise = noise / (np.max(np.abs(noise)) + 1e-9)
    x = x + 0.5 * noise * (0.55 + 0.45 * np.sin(2 * np.pi * 2.2 * t))
    x = np.tanh(1.9 * x)
    return _pcm(x / (np.max(np.abs(x)) + 1e-9))


class FakeSpeaker:
    """Identity by window length; no model, no audio retained."""

    owner_name = "Anthero"
    threshold = 0.45

    def __init__(self, name: str = "Anthero", authorized: bool = True,
                 known_after: float = 1.0, score: float = 0.91) -> None:
        self.name = name
        self.authorized = authorized
        self.known_after = known_after
        self.score = score

    def embedding(self, pcm) -> list[float]:
        samples = pcm.size if hasattr(pcm, "size") else len(pcm) // 2
        return [samples / SR, 1.0]

    def identify(self, embedding, include_auto=False,
                 voiced_seconds=None, device=None) -> dict:
        seconds = float(embedding[0])
        if seconds + 1e-9 >= self.known_after:
            return {"name": self.name, "score": self.score,
                    "sample_score": self.score, "known": True,
                    "authorized": self.authorized}
        return {"name": None, "score": 0.1, "sample_score": 0.1,
                "known": False, "authorized": False}

    def list(self) -> list[dict]:
        return [{"name": "Anthero", "owner": True, "authorized": True}]


class GateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "series_mode.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def gate(self, speaker=None, enabled=False, **kw) -> series_gate.SeriesGate:
        logs: list[str] = []
        gate = series_gate.SeriesGate(
            speaker=speaker or FakeSpeaker(),
            state_path=self.state,
            logger=logs.append,
            **kw)
        gate.set_enabled(enabled)
        self.__dict__["_logs"] = logs
        return gate


class ProgressiveGateTest(GateTestCase):
    def test_short_owner_sample_passes_immediately(self):
        gate = self.gate(FakeSpeaker(known_after=0.8), enabled=True)
        ute = live_voice(1.5)
        decision = gate.evaluate(ute)
        self.assertEqual(decision["verdict"], "accept")
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["speaker"], "Anthero")
        self.assertEqual(len(decision["attempts"]), 1,
                         "a confident first sample must not wait the ceiling")
        self.assertLessEqual(decision["attempts"][0]["seconds"], 1.1)
        self.assertTrue(gate.precheck(ute))

    def test_indecisive_sample_extends_before_accepting(self):
        gate = self.gate(FakeSpeaker(known_after=1.5), enabled=True)
        decision = gate.evaluate(live_voice(2.6))
        self.assertEqual(decision["verdict"], "accept")
        self.assertEqual([a["seconds"] for a in decision["attempts"][:2]],
                         [1.0, 2.0])
        self.assertEqual(len(decision["attempts"]), 2)

    def test_default_ceiling_discards_in_silence(self):
        gate = self.gate(FakeSpeaker(known_after=99), enabled=True)
        pcm = live_voice(6.0)
        decision = gate.evaluate(pcm)
        self.assertEqual(decision["verdict"], "unknown")
        self.assertFalse(decision["allowed"])
        self.assertEqual([a["seconds"] for a in decision["attempts"]],
                         [1.0, 2.0, 3.0])
        self.assertFalse(gate.precheck(pcm))
        self.assertTrue(any("descarte silencioso" in line
                            for line in self._logs))

    def test_ceiling_is_configurable(self):
        gate = self.gate(FakeSpeaker(known_after=99), enabled=True,
                         max_sample_seconds=2.0)
        decision = gate.evaluate(live_voice(6.0))
        self.assertEqual([a["seconds"] for a in decision["attempts"]],
                         [1.0, 2.0])
        self.assertEqual(decision["verdict"], "unknown")

    def test_short_segment_is_indecisive_not_an_intruder(self):
        gate = self.gate(FakeSpeaker(known_after=99), enabled=True)
        decision = gate.evaluate(live_voice(0.5))
        self.assertEqual(decision["verdict"], "indecisive_short")
        self.assertTrue(decision["allowed"])
        self.assertTrue(gate.precheck(live_voice(0.5)))

    def test_authorized_guest_passes_in_both_modes(self):
        for enabled in (False, True):
            gate = self.gate(FakeSpeaker(name="Heloisa", authorized=True,
                                         known_after=0.8),
                             enabled=enabled)
            pcm = live_voice(1.5)
            decision = gate.evaluate(pcm)
            self.assertEqual(decision["verdict"], "accept")
            self.assertEqual(decision["speaker"], "Heloisa")
            self.assertTrue(gate.precheck(pcm))

    def test_known_unauthorized_is_silent_only_in_series_mode(self):
        on = self.gate(FakeSpeaker(name="Heloisa", authorized=False,
                                   known_after=0.8), enabled=True)
        pcm = live_voice(1.5)
        self.assertEqual(on.evaluate(pcm)["verdict"], "unauthorized")
        self.assertFalse(on.precheck(pcm))

        off = self.gate(FakeSpeaker(name="Heloisa", authorized=False,
                                    known_after=0.8), enabled=False)
        self.assertEqual(off.evaluate(pcm)["verdict"], "unauthorized")
        self.assertTrue(off.precheck(pcm))    # normal mode: verbal warning later


class PlaybackVetoTest(GateTestCase):
    def test_tv_line_is_discarded_without_identity(self):
        gate = self.gate(FakeSpeaker(known_after=99), enabled=True)
        decision = gate.evaluate(tv_speech(3.0))
        self.assertEqual(decision["playback"]["label"], "playback")
        self.assertEqual(decision["verdict"], "playback")
        self.assertFalse(decision["allowed"])
        self.assertFalse(gate.precheck(tv_speech(3.0)))

    def test_playback_veto_applies_in_normal_mode_too(self):
        gate = self.gate(FakeSpeaker(known_after=99), enabled=False)
        self.assertFalse(gate.precheck(tv_speech(3.0)))

    def test_live_voice_is_not_vetoed(self):
        verdict = playback_detect.classify(live_voice(2.5))
        self.assertEqual(verdict["label"], "live")

    def test_pure_music_is_not_classified_as_voice(self):
        t = np.arange(int(3.0 * SR)) / SR
        bass = _pcm(np.sin(2 * np.pi * 55 * t)
                    + 0.3 * np.sin(2 * np.pi * 110 * t))
        verdict = playback_detect.classify(bass)
        self.assertEqual(verdict["label"], "not_voice")
        self.assertFalse(verdict["speech_like"])


class GateStateTest(GateTestCase):
    def test_toggle_persists_across_instances(self):
        gate = self.gate()
        self.assertFalse(gate.enabled)
        gate.set_enabled(True)
        self.assertTrue(gate.enabled)
        data = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertTrue(data["enabled"])

        reopened = series_gate.SeriesGate(speaker=FakeSpeaker(),
                                          state_path=self.state)
        self.assertTrue(reopened.enabled)
        reopened.set_enabled(False)
        again = series_gate.SeriesGate(speaker=FakeSpeaker(),
                                       state_path=self.state)
        self.assertFalse(again.enabled)

    def test_decision_for_only_matches_the_same_pcm(self):
        gate = self.gate(FakeSpeaker(known_after=0.8), enabled=True)
        pcm = live_voice(1.5)
        gate.precheck(pcm)
        self.assertEqual(gate.decision_for(pcm)["verdict"], "accept")
        self.assertIsNone(gate.decision_for(live_voice(1.5, seed=99)))


class ListenerPrecheckTest(unittest.TestCase):
    def test_rejected_segment_costs_no_transcription_and_no_brain(self):
        calls: list[bytes] = []
        heard: list[tuple[str, bytes]] = []
        listener = groq_stt.GroqListener(
            "key", "model", "pt",
            on_text=lambda text: heard.append((text, b"")),
            on_utterance=lambda text, pcm: heard.append((text, pcm)),
            transcribe_fn=lambda pcm: calls.append(pcm) or "fala da série",
            precheck=lambda pcm: False,
        )
        try:
            listener._process_utterance(tv_speech(3.0))
        finally:
            listener.stop()
        self.assertEqual(calls, [])
        self.assertEqual(heard, [])

    def test_accepted_segment_reaches_the_transcriber(self):
        calls: list[bytes] = []
        heard: list[tuple[str, bytes]] = []
        pcm = live_voice(1.5)
        listener = groq_stt.GroqListener(
            "key", "model", "pt",
            on_text=lambda text: None,
            on_utterance=lambda text, got: heard.append((text, got)),
            transcribe_fn=lambda clip: calls.append(clip) or "Gino, tá aí?",
            precheck=lambda clip: True,
        )
        try:
            with mock.patch("core.usage.note", lambda *a, **k: None):
                listener._process_utterance(pcm)
        finally:
            listener.stop()
        self.assertEqual(len(calls), 1)
        self.assertEqual(heard, [("Gino, tá aí?", pcm)])

    def test_precheck_failure_fails_open(self):
        calls: list[bytes] = []
        listener = groq_stt.GroqListener(
            "key", "model", "pt", on_text=lambda text: None,
            transcribe_fn=lambda clip: calls.append(clip) or "ok",
            precheck=lambda clip: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        try:
            with mock.patch("core.usage.note", lambda *a, **k: None):
                listener._process_utterance(live_voice(1.0))
        finally:
            listener.stop()
        self.assertEqual(len(calls), 1)


class _UI:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_log(self, line: str) -> None:
        self.lines.append(line)


class MainIntegrationTest(GateTestCase):
    def _obj(self, gate: series_gate.SeriesGate) -> object:
        obj = object.__new__(main.JarvisLive)
        obj._series_gate = gate
        obj._speaker = gate._speaker
        obj._current_pcm = None
        obj.ui = _UI()
        obj.said = []
        obj._say_now = lambda text, log=True: obj.said.append(text)
        obj._log_heard = lambda text: None
        obj._owner_voice = lambda pcm, device=None: (True, "ok")
        return obj

    def test_spoken_toggle_on_and_off_persists(self):
        gate = self.gate()
        obj = self._obj(gate)
        obj._series_decision_for = lambda pcm: {"verdict": "accept",
                                                "speaker": "Anthero"}
        self.assertTrue(main.JarvisLive._series_mode_gate(
            obj, "Gino, modo série", b"\x01\x00" * 16000))
        self.assertTrue(gate.enabled)
        self.assertTrue(any("ligado" in line for line in obj.ui.lines))

        self.assertTrue(main.JarvisLive._series_mode_gate(
            obj, "sai do modo série", b"\x01\x00" * 16000))
        self.assertFalse(gate.enabled)
        self.assertTrue(any("desligado" in line for line in obj.ui.lines))

    def test_toggle_refused_for_unrecognized_voice(self):
        gate = self.gate()
        obj = self._obj(gate)
        obj._series_decision_for = lambda pcm: {"verdict": "unknown"}
        self.assertTrue(main.JarvisLive._series_mode_gate(
            obj, "modo série", b"\x01\x00" * 16000))
        self.assertFalse(gate.enabled)
        self.assertTrue(any("Só uma voz autorizada" in s for s in obj.said))

    def test_question_about_the_mode_is_not_a_toggle(self):
        gate = self.gate()
        obj = self._obj(gate)
        self.assertFalse(main.JarvisLive._series_mode_gate(
            obj, "o que é o modo série?", None))
        self.assertFalse(gate.enabled)

    def test_tv_turn_dies_before_qoder_and_says_nothing(self):
        class Bomber:
            def ask(self, *a, **k):
                raise AssertionError("o cérebro não pode ser chamado")

        class AnalysisSpeaker(FakeSpeaker):
            def analyze_utterance(self, pcm, transcribe=None, full_text=""):
                return {"labeled_text": full_text,
                        "primary": {"name": None, "score": 0.1,
                                    "known": False, "authorized": False},
                        "primary_name": "desconhecido",
                        "primary_embedding": [], "unknown": None}

        obj = object.__new__(main.JarvisLive)
        obj._last_user_speech = time.monotonic()
        obj._qoder_retry_used = False
        obj._restart = None
        obj._power = None
        obj._mirror_show_gate = lambda text: False
        obj._try_action_consent = lambda *a, **k: False
        obj._series_mode_gate = lambda *a, **k: False
        obj._qoder_lock = threading.RLock()
        obj._qoder_current_id = None
        obj._voice_id_enabled = True
        obj._speaker = AnalysisSpeaker(known_after=99)
        obj._series_decision_for = lambda pcm: {"verdict": "playback",
                                                "allowed": False}
        obj._series_mode_on = lambda: True
        obj._cinema_mode_enabled = False    # this test is about the series gate
        obj._last_speaker = {}
        obj._last_unknown_pcm = obj._last_unknown_emb = None
        obj._last_unknown_at = 0.0
        obj.ui = _UI()
        obj.said = []
        obj._say_now = lambda text, log=True: obj.said.append(text)
        obj._qoder_live = Bomber()

        main.JarvisLive._qoder_turn(obj, "fala da série", tv_speech(3.0))
        self.assertEqual(obj.said, [])
        self.assertTrue(any("descartada em silêncio" in line
                            for line in obj.ui.lines))


_OWNER_WAKE_TEXT = "Gino."
_OWNER_WAKE_SCORE = 0.403          # measured on the real "Gino." clip
_OWNER_WAKE_VOICED = 1.13
# 20:01:58: a 10.3 s TV dialogue line, not the owner, no wake word. The old
# single-owner indulgence answered it as the owner; the score was never logged
# by that build, so a value far below the gray floor is the fixture here.
_TV_TEXT = ("Eu também vou deixar vocês em seu tabu da Turan, porque eu "
            "tenho um carro. Regina, eu não estou falando nada, é o som da TV.")
_TV_SCORE = 0.12
_TV_VOICED = 9.0


def _analysis(verdict: str, effective: float = 0.0, voiced: float = 1.0,
              text: str = "", name: str | None = None, known: bool | None = None,
              authorized: bool = False, segments: list | None = None) -> dict:
    if known is None:
        known = verdict == "known"
    return {
        "labeled_text": text,
        "primary": {
            "name": name,
            "score": effective,
            "sample_score": effective,
            "effective_score": effective,
            "known": known,
            "authorized": authorized,
            "verdict": verdict,
        },
        "primary_name": name,
        "primary_embedding": [],
        "primary_pcm": b"",
        "unknown": (None if known else {"pcm": b"", "embedding": []}),
        "unauthorized": None,
        "verdict": verdict,
        "voiced_seconds": voiced,
        "segments": segments or [],
    }


class _AnalysisSpeaker(FakeSpeaker):
    def __init__(self, analysis: dict, records: list | None = None) -> None:
        super().__init__()
        self.analysis = analysis
        self._records = (records if records is not None else
                         [{"name": "Anthero", "owner": True,
                           "authorized": True}])

    def analyze_utterance(self, pcm, transcribe=None, full_text="") -> dict:
        return dict(self.analysis)

    def list(self) -> list[dict]:
        return list(self._records)


class _RecordingQoder:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def ask(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))


class ConversationalGateTest(unittest.TestCase):
    """The _qoder_turn gate: who is answered, who is refused, who is silent."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cinema_state = Path(self._tmp.name) / "cinema_mode.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _obj(self, analysis: dict, records: list | None = None):
        obj = object.__new__(main.JarvisLive)
        # Fluxos desligados: este arquivo mede o voiceprint e a indulgência.
        # A frente local de conversa curta (05/11/2026) responderia "oi" na
        # boca antes do ask; o portão testado aqui é outro (e o teste fica
        # hermético: nada sai à máquina).
        obj._flows_rt = False
        obj._last_user_speech = time.monotonic()
        obj._qoder_retry_used = False
        obj._restart = None
        obj._power = None
        obj._mirror_show_gate = lambda text: False
        obj._try_action_consent = lambda *a, **k: False
        obj._series_mode_gate = lambda *a, **k: False
        obj._cinema_mode_gate = lambda *a, **k: False
        obj._qoder_lock = threading.RLock()
        obj._qoder_current_id = None
        obj._voice_id_enabled = True
        obj._speaker = _AnalysisSpeaker(analysis, records)
        obj._series_decision_for = lambda pcm: None
        obj._series_mode_on = lambda: False
        obj._last_speaker = {}
        obj._last_unknown_pcm = obj._last_unknown_emb = None
        obj._last_unknown_at = 0.0
        obj._last_known_emb = None
        obj._last_known_at = 0.0
        obj._auto_enroll_voice = lambda *a, **k: None
        obj.ui = _UI()
        obj.ui.set_state = lambda *a, **k: None
        obj.said: list[str] = []
        obj._say_now = lambda text, log=True: obj.said.append(text)
        obj._qoder_live = _RecordingQoder()
        obj._asst_name = "Gino"
        obj._voice_indulgence_gray_floor = 0.38
        obj._cinema_mode_enabled = True
        obj._cinema_state_file = self.cinema_state
        obj._cinema_active = False
        obj._cinema_loaded = True
        obj._cinema_since = 0.0
        obj._cinema_last_audio = 0.0
        obj._cinema_events = []
        obj._cinema_counted = {}
        obj._cinema_prompted_at = 0.0
        obj._cinema_pending = False
        obj._cinema_timer = None
        obj._cinema_arm_timer = lambda: None
        obj._cinema_lock = threading.RLock()
        # Everything _qoder_turn touches after a turn is accepted.
        obj._qoder_ignore = False
        obj._interrupted = False
        obj._cancel_narration = lambda: None
        obj._narration_spoken = False
        obj._qoder_first_sentence = False
        obj._turn_t0 = 0.0
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
        return obj

    def test_owner_wake_gray_band_is_indulged_and_answered(self):
        analysis = _analysis("unknown", _OWNER_WAKE_SCORE,
                             _OWNER_WAKE_VOICED, _OWNER_WAKE_TEXT)
        obj = self._obj(analysis)

        main.JarvisLive._qoder_turn(obj, _OWNER_WAKE_TEXT,
                                    b"\x01\x00" * 16000)

        self.assertEqual(len(obj._qoder_live.calls), 1,
                         "a fala do dono não pode ser recusada")
        self.assertEqual(obj.said, [])
        logs = "\n".join(obj.ui.lines)
        self.assertIn("indulgência: único cadastrado é o dono "
                      "(score=0.40 s=1.1)", logs)

    def test_indecisive_short_owner_only_is_answered(self):
        analysis = _analysis("indecisive_short", 0.0, 0.5, "oi")
        obj = self._obj(analysis)

        main.JarvisLive._qoder_turn(obj, "oi", b"\x01\x00" * 16000)

        self.assertEqual(len(obj._qoder_live.calls), 1)
        self.assertEqual(obj.said, [])
        self.assertIn("indulgência: único cadastrado é o dono",
                      "\n".join(obj.ui.lines))

    def test_tv_sentence_below_the_gray_band_is_not_indulged(self):
        analysis = _analysis("unknown", _TV_SCORE, _TV_VOICED, _TV_TEXT)
        obj = self._obj(analysis)

        main.JarvisLive._qoder_turn(obj, _TV_TEXT, b"\x01\x00" * 16000)

        self.assertEqual(obj._qoder_live.calls, [],
                         "a fala longa da TV não pode virar turno do dono")
        self.assertEqual(len(obj.said), 1)
        self.assertIn("Não reconheço essa voz", obj.said[0])
        logs = "\n".join(obj.ui.lines)
        self.assertIn("voz desconhecida — não respondi", logs)
        self.assertIn("score=0.12", logs)
        self.assertNotIn("indulgência", logs)

    def test_tv_sentence_low_score_is_silent_in_cinema_mode(self):
        analysis = _analysis("unknown", _TV_SCORE, _TV_VOICED, _TV_TEXT)
        obj = self._obj(analysis)
        obj._cinema_active = True

        main.JarvisLive._qoder_turn(obj, _TV_TEXT, b"\x01\x00" * 16000)

        self.assertEqual(obj._qoder_live.calls, [])
        self.assertEqual(obj.said, [], "cinema: nenhuma recusa falada")
        logs = "\n".join(obj.ui.lines)
        self.assertIn("modo cinema — fala descartada em silêncio", logs)
        self.assertNotIn("Não reconheço essa voz", logs)

    def test_known_but_unauthorized_never_gets_the_indulgence(self):
        analysis = _analysis("known", 0.91, 2.0, "Gino, me responde",
                             name="Heloisa", known=True, authorized=False)
        obj = self._obj(analysis)

        main.JarvisLive._qoder_turn(obj, "Gino, me responde",
                                    b"\x01\x00" * 16000)

        self.assertEqual(obj._qoder_live.calls, [])
        self.assertIn("Heloisa", obj.said[0])
        self.assertIn("autorizada", obj.said[0])
        self.assertNotIn("indulgência", "\n".join(obj.ui.lines))

    def test_gray_floor_is_the_only_knob(self):
        # Not a bare address (more than four words), score above 0.38.
        text = "Gino, como está o tempo hoje?"
        analysis = _analysis("unknown", _OWNER_WAKE_SCORE, 2.0, text)
        obj = self._obj(analysis)
        main.JarvisLive._qoder_turn(obj, text, b"\x01\x00" * 16000)
        self.assertEqual(len(obj._qoder_live.calls), 1)

        obj = self._obj(analysis)
        obj._voice_indulgence_gray_floor = 0.50
        main.JarvisLive._qoder_turn(obj, text, b"\x01\x00" * 16000)
        self.assertEqual(obj._qoder_live.calls, [])
        self.assertIn("Não reconheço essa voz", obj.said[0])

    def test_unproven_clip_skips_voiceprint_but_speech_still_runs(self):
        """A válvula de energia sustentada preserva a fala, não a identidade.

        `voice_proven=False` é o clipe de ~1.3 s que só abriu por energia
        sustentada: pode ser voz baixa real (por isso segue no pipeline), mas
        não tem prova de voz para alimentar o voiceprint — era a porta do
        clique de teclado virar "voz" aprendida.
        """
        analysis = _analysis("unknown", _TV_SCORE, _TV_VOICED, _TV_TEXT)
        obj = self._obj(analysis)
        analyzed: list = []
        original = obj._speaker.analyze_utterance

        def _spy(*args, **kwargs):
            analyzed.append(1)
            return original(*args, **kwargs)

        obj._speaker.analyze_utterance = _spy
        clip = groq_stt.HeardClip(live_voice(1.30))
        clip.voice_proven = False

        main.JarvisLive._qoder_turn(obj, "Gino, que horas são?", clip)

        self.assertEqual(analyzed, [],
                         "clipe sem prova não pode alimentar o voiceprint")
        self.assertEqual(len(obj._qoder_live.calls), 1,
                         "a fala legítima tem de seguir no pipeline")
        self.assertEqual(obj.said, [],
                         "sem prova não vira recusa falada")
        self.assertNotIn("Não reconheço essa voz",
                         "\n".join(obj.ui.lines))


class _DecisionGate:
    """SeriesGate stand-in for the listener precheck (no ONNX, no audio)."""

    def __init__(self, decision: dict) -> None:
        self.decision = dict(decision)

    def precheck(self, pcm) -> bool:
        return bool(self.decision.get("allowed"))

    def decision_for(self, pcm) -> dict:
        return dict(self.decision)


class CinemaPrecheckTest(unittest.TestCase):
    """Cinema raises strictness before transcription, owner gray band aside."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cinema_state = Path(self._tmp.name) / "cinema_mode.json"
        self.logs: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _obj(self, decision: dict, active: bool = True):
        obj = object.__new__(main.JarvisLive)
        obj._series_gate = _DecisionGate(decision)
        obj._series_gate_log = self.logs.append
        obj._only_owner_enrolled = lambda: True
        obj._voice_indulgence_gray_floor = 0.38
        obj._cinema_mode_enabled = True
        obj._cinema_state_file = self.cinema_state
        obj._cinema_active = active
        obj._cinema_loaded = True
        obj._cinema_since = 0.0
        obj._cinema_last_audio = time.monotonic()
        obj._cinema_events = []
        obj._cinema_counted = {}
        obj._cinema_prompted_at = 0.0
        obj._cinema_pending = False
        obj._cinema_timer = None
        obj._cinema_arm_timer = lambda: None
        obj._cinema_lock = threading.RLock()
        obj.ui = _UI()
        obj.said = []
        obj._say_now = lambda text, log=True: obj.said.append(text)
        return obj

    def test_tv_unknown_is_dropped_before_transcription(self):
        obj = self._obj({"verdict": "unknown", "allowed": True,
                         "score": _TV_SCORE})
        self.assertFalse(main.JarvisLive._series_precheck(obj, b"tv"))
        self.assertTrue(any("modo cinema" in line for line in self.logs))
        self.assertEqual(obj.said, [])

    def test_owner_gray_band_reaches_the_conversational_gate(self):
        obj = self._obj({"verdict": "unknown", "allowed": True,
                         "score": _OWNER_WAKE_SCORE})
        self.assertTrue(main.JarvisLive._series_precheck(obj, b"owner"))

    def test_indecisive_short_and_accept_pass_in_cinema(self):
        for verdict in ("indecisive_short", "accept"):
            obj = self._obj({"verdict": verdict, "allowed": True, "score": 0.9})
            self.assertTrue(main.JarvisLive._series_precheck(obj, b"short"))

    def test_playback_is_still_dropped(self):
        # The series gate already logs its own silent discard; cinema adds no
        # spoken line and keeps the segment away from the brain.
        obj = self._obj({"verdict": "playback", "allowed": False, "score": 0.1})
        self.assertFalse(main.JarvisLive._series_precheck(obj, b"tv"))
        self.assertEqual(obj.said, [])

    def test_normal_mode_unknown_lines_count_toward_the_trigger(self):
        # Name gate off or on: three long unknown lines are what a TV dialogue
        # looks like to the gate, even before cinema is active.
        obj = self._obj({"verdict": "unknown", "allowed": True,
                         "score": _TV_SCORE, "seconds": 10.3},
                        active=False)
        for i in range(3):
            main.JarvisLive._series_precheck(obj, b"tv-" + bytes([i]))
        self.assertEqual(obj.said,
                         ["Você está assistindo alguma coisa? Quer que eu "
                          "ative o modo cinema?"])

    def test_owner_gray_band_does_not_count_as_tv(self):
        obj = self._obj({"verdict": "unknown", "allowed": True,
                         "score": _OWNER_WAKE_SCORE, "seconds": 1.2},
                        active=False)
        for i in range(3):
            main.JarvisLive._series_precheck(obj, b"owner-" + bytes([i]))
        self.assertEqual(obj.said, [])
        self.assertFalse(obj._cinema_pending)


class CinemaModeTest(unittest.TestCase):
    """Auto-trigger, owner-only activation, persistence and idle exit."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "cinema_mode.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj.ui = _UI()
        obj.said: list[str] = []
        obj._say_now = lambda text, log=True: obj.said.append(text)
        obj._log_heard = lambda text: obj.said.append(f"heard:{text}")
        obj._owner_voice_ok = lambda pcm, allow_single=True, device=None: True
        obj._voice_indulgence_gray_floor = 0.38
        obj._cinema_mode_enabled = True
        obj._cinema_state_file = self.state
        obj._cinema_active = False
        obj._cinema_loaded = False
        obj._cinema_since = 0.0
        obj._cinema_last_audio = 0.0
        obj._cinema_events = []
        obj._cinema_counted = {}
        obj._cinema_prompted_at = 0.0
        obj._cinema_pending = False
        obj._cinema_timer = None
        obj._cinema_trigger_count = 3
        obj._cinema_trigger_window_s = 60.0
        obj._cinema_idle_exit_s = 600.0
        obj._cinema_prompt_ttl_s = 60.0
        obj._cinema_prompt_cooldown_s = 600.0
        obj._cinema_arm_timer = lambda: None
        obj._cinema_lock = threading.RLock()
        return obj

    def test_three_suspicious_lines_ask_once(self):
        obj = self._obj()
        for i in range(3):
            obj._cinema_observe("unknown", b"tv-line-" + bytes([i]))
        self.assertEqual(obj.said,
                         ["Você está assistindo alguma coisa? Quer que eu "
                          "ative o modo cinema?"])
        self.assertTrue(obj._cinema_pending)
        obj._cinema_observe("unknown", b"tv-line-4")
        self.assertEqual(len(obj.said), 1, "não pode repetir a pergunta")
        self.assertFalse(obj._cinema_active)

    def test_owner_confirmation_resets_the_window(self):
        obj = self._obj()
        obj._cinema_observe("unknown", b"tv-1")
        obj._cinema_observe("unknown", b"tv-2")
        obj._cinema_observe("accept", b"owner")
        obj._cinema_observe("unknown", b"tv-3")
        self.assertEqual(obj.said, [])
        self.assertFalse(obj._cinema_pending)

    def test_activation_requires_an_owner_yes(self):
        obj = self._obj()
        obj._cinema_pending = True
        obj._cinema_prompted_at = time.monotonic()
        obj._owner_voice_ok = lambda pcm, allow_single=True, device=None: False

        self.assertTrue(main.JarvisLive._cinema_mode_gate(
            obj, "Sim", b"tv-voice"))
        self.assertFalse(obj._cinema_active)
        self.assertIn("ativação recusada", "\n".join(obj.ui.lines))

        obj._owner_voice_ok = lambda pcm, allow_single=True, device=None: True
        self.assertTrue(main.JarvisLive._cinema_mode_gate(
            obj, "Sim", b"owner-voice"))
        self.assertTrue(obj._cinema_active)
        data = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertTrue(data["active"])
        self.assertTrue(data["updated_at"])

    def test_typed_yes_is_deliberate_and_activates(self):
        obj = self._obj()
        obj._cinema_pending = True
        obj._cinema_prompted_at = time.monotonic()
        self.assertTrue(main.JarvisLive._cinema_mode_gate(obj, "pode"))
        self.assertTrue(obj._cinema_active)

    def test_consent_phrase_is_not_a_cinema_yes(self):
        # "pode enviar" answers another gate; it must not switch cinema on.
        obj = self._obj()
        obj._cinema_pending = True
        obj._cinema_prompted_at = time.monotonic()
        self.assertFalse(main.JarvisLive._cinema_mode_gate(
            obj, "Pode enviar"))
        self.assertFalse(obj._cinema_active)
        self.assertTrue(obj._cinema_pending)

    def test_explicit_activation_and_exit_by_owner(self):
        obj = self._obj()
        self.assertTrue(main.JarvisLive._cinema_mode_gate(
            obj, "Ativa o modo cinema"))
        self.assertTrue(obj._cinema_active)
        self.assertTrue(main.JarvisLive._cinema_mode_gate(
            obj, "Sai do modo cinema"))
        self.assertFalse(obj._cinema_active)
        self.assertIn("Modo cinema desligado.", obj.said[-1])
        data = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertFalse(data["active"])

    def test_question_about_exiting_does_not_exit(self):
        obj = self._obj()
        obj._cinema_enter()
        self.assertFalse(main.JarvisLive._cinema_mode_gate(
            obj, "Como saio do modo cinema?"))
        self.assertTrue(obj._cinema_active)

    def test_idle_timeout_exits_with_a_short_announcement(self):
        obj = self._obj()
        obj._cinema_enter()
        self.assertTrue(obj._cinema_active)
        obj._cinema_last_audio = 1000.0
        self.assertFalse(obj._cinema_check_idle(now=1500.0))
        self.assertTrue(obj._cinema_active)
        self.assertTrue(obj._cinema_check_idle(now=1601.0))
        self.assertFalse(obj._cinema_active)
        self.assertIn("Modo cinema desligado.", obj.said[-1])

    def test_state_survives_a_new_instance(self):
        obj = self._obj()
        obj._cinema_enter()
        other = self._obj()
        self.assertTrue(other._cinema_active_now())
        other._cinema_exit("teste")
        third = self._obj()
        self.assertFalse(third._cinema_active_now())


if __name__ == "__main__":
    unittest.main()
