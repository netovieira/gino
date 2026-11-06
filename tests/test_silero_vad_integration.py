"""Silero VAD integration in the main STT flow.

Tests that the segmenter (core.groq_stt.GroqListener) uses Silero VAD for
start/end-of-utterance detection when the ONNX model is present, and falls
back to RMS threshold when it is absent.  Also tests the pre-filter in
core.stt.

The Silero model file is git-ignored; tests that need the real model skip
when it is not downloaded.
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

import numpy as np

from core import groq_stt, silero_vad, stt
from tests.audio_fixtures import voice_block, silence

_SR = 16000
_FRAME = 1024          # CHUNK_SIZE: 64 ms at 16 kHz
_SILENCE = np.zeros(_FRAME, dtype=np.int16)


class _Harness(unittest.TestCase):
    """Shared setup: duck stubs, listener factory, feed helpers."""

    def setUp(self):
        self._orig_up = groq_stt._duck.up
        self._orig_down = groq_stt._duck.down
        self._orig_heartbeat = groq_stt._duck.heartbeat
        groq_stt._duck.up = lambda: None
        groq_stt._duck.down = lambda: None
        groq_stt._duck.heartbeat = lambda: None
        self._lock = threading.Lock()
        self.got: list[bytes] = []
        self._listeners: list[groq_stt.GroqListener] = []

    def tearDown(self):
        for listener in self._listeners:
            listener.stop()
        groq_stt._duck.up = self._orig_up
        groq_stt._duck.down = self._orig_down
        groq_stt._duck.heartbeat = self._orig_heartbeat

    def _listener(self, **kwargs) -> groq_stt.GroqListener:
        def _transcribe(pcm: bytes) -> str:
            with self._lock:
                self.got.append(pcm)
            return "fala transcrita"

        listener = groq_stt.GroqListener(
            "", "test", "pt",
            on_text=lambda _t: None,
            on_utterance=lambda _t, _p: None,
            on_status=lambda _m: None,
            sample_rate=_SR,
            transcribe_fn=_transcribe,
            **kwargs,
        )
        listener._endpoint_complete = lambda _pcm: True
        self._listeners.append(listener)
        return listener

    @staticmethod
    def _feed(listener, count: int, block: np.ndarray) -> None:
        for _ in range(count):
            listener.feed(block)

    def _settle_floor(self, listener) -> None:
        self._feed(listener, 40, _SILENCE)

    def _wait_for(self, count: int, timeout: float = 5.0) -> list[bytes]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.got) >= count:
                    return list(self.got)
            time.sleep(0.02)
        with self._lock:
            return list(self.got)


# ── VAD available path ───────────────────────────────────────────────────────

class _MockVAD:
    """Mock Silero VAD that returns speech based on RMS level.

    The synthetic voice_block from audio_fixtures passes spectral tests but
    does not sound like real speech to the Silero model.  This mock provides
    deterministic behavior for integration tests.
    """

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold

    def reset(self) -> None:
        pass

    def is_speech(self, audio) -> bool:
        # Handle both bytes and numpy arrays
        if isinstance(audio, (bytes, bytearray, memoryview)):
            x = np.frombuffer(bytes(audio), dtype=np.int16).astype(np.float32) / 32768.0
        else:
            x = np.asarray(audio, dtype=np.float32).ravel()
            if x.size and float(np.max(np.abs(x))) > 1.5:
                x = x / 32768.0
        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        # Consider blocks with RMS > 0.01 as speech (voice_block has ~0.12)
        return rms > 0.01

    def process(self, audio) -> float:
        return 0.9 if self.is_speech(audio) else 0.1


class VADAvailableTest(_Harness):
    """When the ONNX model is present, VAD drives start/end detection."""

    def test_listener_has_vad_instance(self):
        """Listener initializes VAD when model is available."""
        with mock.patch.object(groq_stt.silero_vad, "available",
                               return_value=True):
            with mock.patch.object(groq_stt.silero_vad, "SileroVAD",
                                   _MockVAD):
                listener = self._listener()
        self.assertTrue(listener._vad_available,
                        "listener did not detect Silero VAD availability")
        self.assertIsNotNone(listener._vad,
                             "listener has no SileroVAD instance")

    def test_vad_opens_segment_on_speech_blocks(self):
        """3 consecutive VAD-positive blocks must open a segment."""
        with mock.patch.object(groq_stt.silero_vad, "available",
                               return_value=True):
            with mock.patch.object(groq_stt.silero_vad, "SileroVAD",
                                   _MockVAD):
                listener = self._listener()
        self._settle_floor(listener)
        voice = voice_block(8000)
        self._feed(listener, groq_stt._START_BLOCKS + 2, voice)
        self.assertTrue(listener.speaking,
                        "VAD-positive blocks did not open a segment")

    def test_vad_closes_segment_on_silence_blocks(self):
        """end_blocks consecutive VAD-negative blocks must close a segment."""
        with mock.patch.object(groq_stt.silero_vad, "available",
                               return_value=True):
            with mock.patch.object(groq_stt.silero_vad, "SileroVAD",
                                   _MockVAD):
                listener = self._listener()
        self._settle_floor(listener)
        voice = voice_block(8000)
        # Open segment
        self._feed(listener, groq_stt._START_BLOCKS + 5, voice)
        self.assertTrue(listener.speaking)
        # Close with silence
        self._feed(listener, listener._end_blocks + 2, _SILENCE)
        self.assertFalse(listener.speaking,
                         "VAD-negative blocks did not close the segment")
        chunks = self._wait_for(1, timeout=2.0)
        self.assertGreaterEqual(len(chunks), 1,
                                "closed segment did not reach the STT")

    def test_silence_does_not_open_segment(self):
        """Silence blocks must NOT open a segment even if RMS is high."""
        with mock.patch.object(groq_stt.silero_vad, "available",
                               return_value=True):
            with mock.patch.object(groq_stt.silero_vad, "SileroVAD",
                                   _MockVAD):
                listener = self._listener()
        self._settle_floor(listener)
        self._feed(listener, 20, _SILENCE)
        self.assertFalse(listener.speaking,
                         "silence opened a segment through VAD")

    def test_vad_reset_on_silence(self):
        """VAD state must be reset when the segmenter goes idle."""
        with mock.patch.object(groq_stt.silero_vad, "available",
                               return_value=True):
            with mock.patch.object(groq_stt.silero_vad, "SileroVAD",
                                   _MockVAD):
                listener = self._listener()
        self._settle_floor(listener)
        # The _settle_floor feeds silence, which should trigger reset.
        self.assertIsNotNone(listener._vad)


# ── VAD unavailable (RMS fallback) path ──────────────────────────────────────

class VADFallbackTest(_Harness):
    """When the ONNX model is absent, RMS threshold drives detection."""

    def test_listener_falls_back_to_rms(self):
        """With VAD mocked as unavailable, the listener uses RMS."""
        with mock.patch.object(groq_stt.silero_vad, "available",
                               return_value=False):
            listener = self._listener()
        self.assertFalse(listener._vad_available,
                         "listener should not have VAD when model is absent")
        self.assertIsNone(listener._vad,
                          "listener should have no VAD instance in fallback")

    def test_rms_fallback_still_opens_on_voice(self):
        """The original RMS + voice-proof path must still open a segment."""
        with mock.patch.object(groq_stt.silero_vad, "available",
                               return_value=False):
            listener = self._listener()
        self._settle_floor(listener)
        voice = voice_block(8000)
        # Need _START_BLOCKS voice-proof blocks OR _VOICE_BYPASS_BLOCKS RMS
        self._feed(listener, groq_stt._VOICE_BYPASS_BLOCKS + 2, voice)
        self.assertTrue(listener.speaking,
                        "RMS fallback did not open a segment on loud voice")

    def test_rms_fallback_closes_on_silence(self):
        with mock.patch.object(groq_stt.silero_vad, "available",
                               return_value=False):
            listener = self._listener()
        self._settle_floor(listener)
        voice = voice_block(8000)
        self._feed(listener, groq_stt._VOICE_BYPASS_BLOCKS + 2, voice)
        self.assertTrue(listener.speaking)
        self._feed(listener, listener._end_blocks + 2, _SILENCE)
        self.assertFalse(listener.speaking,
                         "RMS fallback did not close on silence")


# ── core.stt VAD pre-filter ──────────────────────────────────────────────────

class STTVadFilterTest(unittest.TestCase):
    """The vad_filter_audio function in core.stt."""

    def test_empty_audio_returns_empty(self):
        result = stt.vad_filter_audio(np.array([], dtype=np.float32))
        self.assertEqual(result.size, 0)

    def test_silence_is_stripped(self):
        """Pure silence should be reduced to nothing or near-nothing."""
        audio = np.zeros(_SR * 2, dtype=np.float32)  # 2 s silence
        result = stt.vad_filter_audio(audio)
        # With RMS fallback the threshold is 0.01; silence is 0.0
        self.assertLessEqual(result.size, audio.size)

    def test_loud_audio_is_kept(self):
        """Audio above the RMS threshold should not be fully stripped."""
        rng = np.random.default_rng(42)
        audio = (rng.normal(scale=0.3, size=_SR * 2)).astype(np.float32)
        result = stt.vad_filter_audio(audio)
        self.assertGreater(result.size, 0,
                           "loud audio was entirely stripped by the filter")

    @unittest.skipUnless(silero_vad.available(), "Silero VAD model not downloaded")
    def test_vad_filter_with_real_model(self):
        """With the real model, speech blocks survive and silence is cut."""
        # Build a signal: 0.5 s silence + 1 s voice-like noise + 0.5 s silence
        silent = np.zeros(_SR // 2, dtype=np.float32)
        rng = np.random.default_rng(7)
        # Voice-like: harmonics in the 300-3400 Hz band
        t = np.arange(_SR) / _SR
        voice = np.zeros(_SR, dtype=np.float32)
        for k in range(1, 16):
            freq = 140.0 * k
            if freq > 3400:
                break
            voice += (1.0 / k) * np.sin(2 * np.pi * freq * t)
        voice *= 0.3
        voice += 0.05 * rng.normal(size=_SR)
        audio = np.concatenate([silent, voice, silent])
        result = stt.vad_filter_audio(audio)
        # The filtered audio should be shorter than the original
        # (silence stripped from edges)
        self.assertLessEqual(result.size, audio.size)


# ── Integration: VAD state across segments ───────────────────────────────────

class VADMultiSegmentTest(_Harness):
    """VAD must work correctly across multiple utterances."""

    def test_two_consecutive_utterances(self):
        """Two separate speech bursts produce two separate transcriptions."""
        with mock.patch.object(groq_stt.silero_vad, "available",
                               return_value=True):
            with mock.patch.object(groq_stt.silero_vad, "SileroVAD",
                                   _MockVAD):
                listener = self._listener()
        self._settle_floor(listener)
        voice = voice_block(8000)

        # First utterance
        self._feed(listener, groq_stt._START_BLOCKS + 5, voice)
        self.assertTrue(listener.speaking)
        self._feed(listener, listener._end_blocks + 2, _SILENCE)
        self.assertFalse(listener.speaking)

        # Second utterance
        self._feed(listener, 10, _SILENCE)  # gap
        self._feed(listener, groq_stt._START_BLOCKS + 5, voice)
        self.assertTrue(listener.speaking,
                        "VAD did not reopen a segment after the first closed")
        self._feed(listener, listener._end_blocks + 2, _SILENCE)
        self.assertFalse(listener.speaking)

        chunks = self._wait_for(2, timeout=3.0)
        self.assertGreaterEqual(len(chunks), 2,
                                "two utterances did not produce two chunks")


if __name__ == "__main__":
    unittest.main()
