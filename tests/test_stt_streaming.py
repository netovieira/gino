"""Tests for core.stt_streaming -- streaming STT with preroll (FASE2-TASK3).

Metrics of success:
  - Preroll of 1.5 s captured correctly
  - Partial transcriptions every 1-2 s of speech
  - Final transcription is complete and accurate
  - Latency perceived < 2 s for first partial
"""
from __future__ import annotations

import numpy as np
import pytest

from core import stt_streaming
from core.stt_streaming import StreamingSession


# -- helpers -------------------------------------------------------------------

def _silence_blocks(n: int) -> list[bytes]:
    """Generate n blocks of silent int16 PCM (64 ms each at 16 kHz)."""
    block = b"\x00\x00" * stt_streaming.BLOCK_SAMPLES
    return [block] * n


def _tone_blocks(n: int, freq: float = 440.0, amplitude: float = 0.5) -> list[bytes]:
    """Generate n blocks of sine-wave int16 PCM (64 ms each at 16 kHz)."""
    sr = stt_streaming.SAMPLE_RATE
    block_samples = stt_streaming.BLOCK_SAMPLES
    blocks = []
    for i in range(n):
        t = np.arange(block_samples) / sr + i * 0.064
        wave = (np.sin(2 * np.pi * freq * t) * amplitude * 32767).astype(np.int16)
        blocks.append(wave.tobytes())
    return blocks


class MockTranscriber:
    """Records calls and returns a fixed string."""

    def __init__(self, text: str = "transcricao"):
        self.text = text
        self.calls: list[bytes] = []

    def __call__(self, pcm: bytes) -> str:
        self.calls.append(pcm)
        return self.text


# -- preroll configuration -----------------------------------------------------

def test_default_preroll_is_1_5s():
    """Default preroll is 1.5 seconds."""
    assert stt_streaming.DEFAULT_PREROLL_S == 1.5


def test_preroll_blocks_calculation():
    """preroll_blocks returns the correct number of 64 ms blocks."""
    # 1.5 s / 0.064 s per block = 23.4375 -> ceil = 24
    assert stt_streaming.preroll_blocks(1.5) == 24
    # 0 s -> 0 blocks
    assert stt_streaming.preroll_blocks(0.0) == 0


def test_preroll_clamped():
    """Preroll is clamped to [0, 5] seconds."""
    session = StreamingSession(
        transcribe_fn=lambda pcm: "",
        preroll_s=10.0,
    )
    assert session.preroll_seconds == 5.0

    session2 = StreamingSession(
        transcribe_fn=lambda pcm: "",
        preroll_s=-1.0,
    )
    assert session2.preroll_seconds == 0.0


# -- progressive window --------------------------------------------------------

def test_progressive_interval_default():
    """Default progressive interval is 1.5 seconds."""
    assert stt_streaming.DEFAULT_PROGRESSIVE_INTERVAL_S == 1.5


def test_progressive_interval_clamped():
    """Progressive interval is clamped to [0.5, 5] seconds."""
    session = StreamingSession(
        transcribe_fn=lambda pcm: "",
        progressive_interval_s=0.1,
    )
    assert session.progressive_interval_s == 0.5


# -- session lifecycle ---------------------------------------------------------

def test_session_starts_inactive():
    """A new session has no speech buffered."""
    session = StreamingSession(transcribe_fn=lambda pcm: "")
    assert session._speech_active is False
    assert session._final_done is False
    assert len(session._speech_blocks) == 0


def test_feed_non_speech_fills_ring():
    """Non-speech blocks fill the preroll ring but don't start speech."""
    session = StreamingSession(transcribe_fn=lambda pcm: "")
    blocks = _silence_blocks(5)
    for block in blocks:
        session.feed_pcm16(block, is_speech=False)
    assert session._speech_active is False
    assert len(session._ring) == 5


def test_first_speech_block_flushes_ring():
    """When speech starts, the preroll ring is flushed into the speech buffer."""
    session = StreamingSession(transcribe_fn=lambda pcm: "", preroll_s=0.5)
    # Fill ring with non-speech blocks.
    preroll = _silence_blocks(5)
    for block in preroll:
        session.feed_pcm16(block, is_speech=False)
    assert len(session._ring) == 5

    # First speech block: ring is flushed into speech_blocks.
    tone = _tone_blocks(1)
    session.feed_pcm16(tone[0], is_speech=True)
    assert session._speech_active is True
    assert len(session._ring) == 0
    # speech_blocks should contain preroll + first speech block.
    assert len(session._speech_blocks) == 6  # 5 preroll + 1 speech


def test_partial_transcription_triggered():
    """A partial transcription is triggered after enough speech blocks."""
    transcriber = MockTranscriber("parcial")
    partials: list[tuple[str, bool]] = []

    session = StreamingSession(
        transcribe_fn=transcriber,
        on_partial=lambda text, is_final: partials.append((text, is_final)),
        preroll_s=0.0,
        progressive_interval_s=0.5,  # ~8 blocks
    )
    # Feed enough speech blocks to trigger a partial.
    tone = _tone_blocks(20)  # 20 blocks = 1.28 s
    for block in tone:
        session.feed_pcm16(block, is_speech=True)

    # At least one partial should have been emitted.
    assert len(partials) >= 1
    assert partials[0][0] == "parcial"
    assert partials[0][1] is False  # not final


def test_final_transcription():
    """end_of_speech triggers the final transcription."""
    transcriber = MockTranscriber("final")
    finals: list[str] = []

    session = StreamingSession(
        transcribe_fn=transcriber,
        on_final=lambda text: finals.append(text),
    )
    tone = _tone_blocks(30)
    for block in tone:
        session.feed_pcm16(block, is_speech=True)

    result = session.end_of_speech()
    assert result == "final"
    assert finals == ["final"]
    assert len(transcriber.calls) >= 1


def test_final_is_idempotent():
    """Calling end_of_speech twice returns the same result."""
    session = StreamingSession(
        transcribe_fn=lambda pcm: "texto",
    )
    tone = _tone_blocks(10)
    for block in tone:
        session.feed_pcm16(block, is_speech=True)

    first = session.end_of_speech()
    second = session.end_of_speech()
    assert first == second == "texto"


def test_reset_clears_state():
    """reset() clears all buffers for a new utterance."""
    session = StreamingSession(transcribe_fn=lambda pcm: "")
    tone = _tone_blocks(10)
    for block in tone:
        session.feed_pcm16(block, is_speech=True)
    session.end_of_speech()

    session.reset()
    assert session._speech_active is False
    assert session._final_done is False
    assert len(session._speech_blocks) == 0
    assert len(session._ring) == 0


def test_empty_utterance():
    """end_of_speech with no audio returns empty string."""
    finals: list[str] = []
    session = StreamingSession(
        transcribe_fn=lambda pcm: "algo",
        on_final=lambda text: finals.append(text),
    )
    result = session.end_of_speech()
    assert result == ""
    assert finals == [""]


# -- preroll capture -----------------------------------------------------------

def test_preroll_captured_in_final():
    """The preroll audio is included in the final transcription."""
    captured_pcm: list[bytes] = []

    def capture_transcribe(pcm: bytes) -> str:
        captured_pcm.append(pcm)
        return "texto"

    session = StreamingSession(
        transcribe_fn=capture_transcribe,
        preroll_s=0.5,  # ~8 blocks
        progressive_interval_s=10.0,  # disable partials
    )
    # Feed 5 non-speech blocks (preroll).
    preroll = _silence_blocks(5)
    for block in preroll:
        session.feed_pcm16(block, is_speech=False)

    # Feed 10 speech blocks.
    tone = _tone_blocks(10)
    for block in tone:
        session.feed_pcm16(block, is_speech=True)

    session.end_of_speech()

    # The final PCM should include preroll + speech.
    assert len(captured_pcm) >= 1
    final_pcm = captured_pcm[-1]
    expected_blocks = 5 + 10  # preroll + speech
    expected_bytes = expected_blocks * stt_streaming.BLOCK_BYTES
    assert len(final_pcm) == expected_bytes


# -- latency -------------------------------------------------------------------

def test_first_partial_within_2s():
    """First partial transcription happens within 2 seconds of speech start.

    With progressive_interval_s=1.5, the first partial should trigger after
    ~1.5 s of speech (24 blocks at 64 ms each).
    """
    partial_times: list[float] = []
    start_time: list[float] = []

    def on_partial(text, is_final):
        if not is_final:
            partial_times.append(time.monotonic())

    import time
    session = StreamingSession(
        transcribe_fn=lambda pcm: "parcial",
        on_partial=on_partial,
        progressive_interval_s=1.5,
    )
    start_time.append(time.monotonic())
    # Feed 30 blocks of speech (~1.92 s).
    tone = _tone_blocks(30)
    for block in tone:
        session.feed_pcm16(block, is_speech=True)

    assert len(partial_times) >= 1
    elapsed = partial_times[0] - start_time[0]
    assert elapsed < 2.0, f"First partial took {elapsed:.2f}s (expected < 2s)"
