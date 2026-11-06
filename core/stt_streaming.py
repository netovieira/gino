"""Streaming STT incremental with preroll (FASE2-TASK3).

Wraps the existing GroqListener / local-STT pipeline with a progressive
transcription layer:

* **Preroll buffer** (1.5 s): audio kept BEFORE the VAD detects speech, so the
  first phoneme is never clipped.  Already implemented in ``core.groq_stt``
  (the ring buffer); this module exposes the configuration and validates it.

* **Progressive window**: every N seconds of voiced speech, the accumulated
  audio is sent to the STT engine for a PARTIAL transcription.  Partials are
  marked ``preliminary=True`` in the activity log.

* **Final transcription**: when the VAD detects end-of-speech, the full
  utterance (preroll + speech) is transcribed once more as the DEFINITIVE
  result.  The final replaces any partials.

The module is engine-agnostic: it works with both Groq (remote Whisper) and
faster-whisper (local).  The caller injects the transcription function.

Usage::

    from core.stt_streaming import StreamingSession

    session = StreamingSession(
        transcribe_fn=my_transcribe,
        on_partial=lambda text: print(f"[partial] {text}"),
        on_final=lambda text: print(f"[final] {text}"),
        preroll_s=1.5,
        progressive_interval_s=1.5,
    )
    session.feed_pcm16(block_bytes)   # called from the audio callback
    session.end_of_speech()           # called when VAD closes the utterance
"""
from __future__ import annotations

import collections
import time
from typing import Callable

# Preroll: seconds of audio kept before the first phoneme.
DEFAULT_PREROLL_S = 1.5
MIN_PREROLL_S = 0.0
MAX_PREROLL_S = 5.0

# Progressive window: send a partial transcription every N seconds of speech.
DEFAULT_PROGRESSIVE_INTERVAL_S = 1.5
MIN_PROGRESSIVE_INTERVAL_S = 0.5
MAX_PROGRESSIVE_INTERVAL_S = 5.0

# Block size: 64 ms at 16 kHz = 1024 samples = 2048 bytes (int16 mono).
BLOCK_SAMPLES = 1024
BLOCK_BYTES = BLOCK_SAMPLES * 2
SAMPLE_RATE = 16000


def preroll_blocks(preroll_s: float) -> int:
    """Number of 64 ms blocks in the preroll buffer."""
    import math
    return int(math.ceil(max(0.0, float(preroll_s)) / 0.064))


class StreamingSession:
    """One utterance's worth of streaming STT.

    Create a new session for each utterance (or reuse via ``reset()``).

    Parameters
    ----------
    transcribe_fn : callable
        ``transcribe_fn(pcm_bytes) -> str``.  Called for partials and final.
    on_partial : callable, optional
        ``on_partial(text, is_final=False)``.  Called after each progressive
        transcription.  ``is_final=True`` on the definitive result.
    on_final : callable, optional
        ``on_final(text)``.  Called once with the definitive transcription.
        Convenience wrapper around ``on_partial(text, is_final=True)``.
    preroll_s : float
        Seconds of preroll buffer (default 1.5).
    progressive_interval_s : float
        Seconds of speech between partial transcriptions (default 1.5).
    sample_rate : int
        Audio sample rate (default 16000).
    """

    def __init__(
        self,
        transcribe_fn: Callable[[bytes], str],
        on_partial: Callable[[str, bool], None] | None = None,
        on_final: Callable[[str], None] | None = None,
        preroll_s: float = DEFAULT_PREROLL_S,
        progressive_interval_s: float = DEFAULT_PROGRESSIVE_INTERVAL_S,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        self._transcribe = transcribe_fn
        self._on_partial = on_partial or (lambda text, is_final: None)
        self._on_final = on_final or (lambda text: None)
        self._sr = int(sample_rate or SAMPLE_RATE)

        # Preroll configuration.
        self._preroll_s = min(MAX_PREROLL_S, max(MIN_PREROLL_S, float(preroll_s)))
        self._preroll_blocks = preroll_blocks(self._preroll_s)

        # Progressive window configuration.
        self._interval_s = min(MAX_PROGRESSIVE_INTERVAL_S,
                               max(MIN_PROGRESSIVE_INTERVAL_S,
                                   float(progressive_interval_s)))
        self._interval_blocks = int(self._interval_s / 0.064)

        # Buffers.
        self._ring: collections.deque[bytes] = collections.deque(
            maxlen=self._preroll_blocks)
        self._speech_blocks: list[bytes] = []
        self._blocks_since_partial = 0
        self._speech_active = False
        self._final_done = False
        self._last_partial_text = ""

    # -- configuration ---------------------------------------------------------

    @property
    def preroll_seconds(self) -> float:
        return self._preroll_s

    @property
    def preroll_capacity(self) -> int:
        """Number of blocks the ring holds."""
        return self._preroll_blocks

    @property
    def progressive_interval_s(self) -> float:
        return self._interval_s

    # -- feeding audio ---------------------------------------------------------

    def feed_pcm16(self, block: bytes, is_speech: bool = True) -> None:
        """Feed one 64 ms block of int16 PCM.

        Parameters
        ----------
        block : bytes
            Raw int16 LE PCM (2048 bytes for 64 ms at 16 kHz).
        is_speech : bool
            Whether the VAD considers this block to contain speech.
            Non-speech blocks fill the preroll ring but do not trigger
            progressive transcription.
        """
        if self._final_done:
            return

        if not is_speech:
            # Non-speech: fill the preroll ring (captures pre-speech audio).
            if not self._speech_active:
                self._ring.append(block)
            else:
                # Silence after speech: could be end-of-utterance.
                self._blocks_since_partial += 1
            return

        # Speech block.
        if not self._speech_active:
            # First speech block: flush the ring into the speech buffer.
            self._speech_active = True
            self._speech_blocks.extend(self._ring)
            self._ring.clear()
            self._speech_blocks.append(block)
            self._blocks_since_partial = 1
        else:
            self._speech_blocks.append(block)
            self._blocks_since_partial += 1

        # Check if it's time for a partial transcription.
        if self._blocks_since_partial >= self._interval_blocks:
            self._do_partial()

    def end_of_speech(self) -> str:
        """Signal end-of-utterance; returns the final transcription.

        The full speech buffer (including preroll) is transcribed once as the
        definitive result.  Subsequent calls return the same text.
        """
        if self._final_done:
            return self._last_partial_text

        self._final_done = True
        pcm = self._collect_pcm()
        if not pcm:
            self._on_final("")
            self._on_partial("", is_final=True)
            return ""

        try:
            text = self._transcribe(pcm)
        except Exception as e:
            print(f"[StreamingSTT] transcrição final falhou: {e}")
            text = ""

        text = str(text or "").strip()
        self._last_partial_text = text
        self._on_final(text)
        self._on_partial(text, is_final=True)
        return text

    def reset(self) -> None:
        """Clear all buffers for a new utterance."""
        self._ring.clear()
        self._speech_blocks.clear()
        self._blocks_since_partial = 0
        self._speech_active = False
        self._final_done = False
        self._last_partial_text = ""

    # -- internal --------------------------------------------------------------

    def _collect_pcm(self) -> bytes:
        """Concatenate all buffered blocks into one PCM byte string.

        The ring holds pre-speech audio only when speech hasn't started yet.
        Once speech is active, the ring was flushed into ``_speech_blocks``
        and is kept empty.
        """
        if self._speech_active:
            return b"".join(self._speech_blocks)
        # No speech detected: return whatever is in the preroll ring.
        return b"".join(self._ring)

    def _do_partial(self) -> None:
        """Transcribe the accumulated speech so far (progressive window)."""
        self._blocks_since_partial = 0
        pcm = b"".join(self._speech_blocks)
        if len(pcm) < BLOCK_BYTES * 3:
            # Too short for a meaningful partial (< 192 ms).
            return
        try:
            text = self._transcribe(pcm)
        except Exception as e:
            print(f"[StreamingSTT] transcrição parcial falhou: {e}")
            return
        text = str(text or "").strip()
        if text and text != self._last_partial_text:
            self._last_partial_text = text
            self._on_partial(text, is_final=False)


# -- convenience: create a session from a GroqListener -------------------------

def session_from_listener(
    listener,
    on_partial: Callable[[str, bool], None] | None = None,
    on_final: Callable[[str], None] | None = None,
    preroll_s: float = DEFAULT_PREROLL_S,
    progressive_interval_s: float = DEFAULT_PROGRESSIVE_INTERVAL_S,
) -> StreamingSession:
    """Create a ``StreamingSession`` that delegates transcription to a GroqListener.

    The listener's ``transcribe_pcm`` method is used as the transcription
    function, so the same engine (Groq or local) handles both partials and
    the final result.
    """
    return StreamingSession(
        transcribe_fn=listener.transcribe_pcm,
        on_partial=on_partial,
        on_final=on_final,
        preroll_s=preroll_s,
        progressive_interval_s=progressive_interval_s,
        sample_rate=getattr(listener, "_sr", SAMPLE_RATE),
    )
