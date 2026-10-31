"""Silero VAD — voice activity detection on ONNX Runtime.

WHY THIS EXISTS
    core/groq_stt.py opens an utterance when the RMS goes above a multiple of
    the room floor. That works for a quiet room and fails for a fan, music, a
    keyboard, or a TV — the exact cases where the assistant starts transcribing
    noise. Silero VAD is a tiny ONNX model (MIT) that returns speech probability
    per 32 ms window in under a millisecond on one CPU thread. It is the VAD
    that Smart Turn is designed to work with.

LICENSE
    MIT, https://github.com/snakers4/silero-vad. The ONNX file lives in models/
    (git-ignored); this wrapper is ours.

USAGE
    vad = SileroVAD()
    prob = vad.process(block_int16)       # max probability in this block
    if vad.is_speech(block_int16): ...
    vad.reset()                           # new utterance / stream
"""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
MODEL_PATH = BASE_DIR / "models" / "silero_vad.onnx"
SAMPLE_RATE = 16_000
WINDOW = 512          # 32 ms at 16 kHz — the model's expected window
CONTEXT = 64          # samples carried from the previous window (v5/v6)
STATE_SHAPE = (2, 1, 128)

_LOCK = threading.RLock()
_SESSION = None


def available() -> bool:
    return MODEL_PATH.is_file()


def _session():
    global _SESSION
    with _LOCK:
        if _SESSION is not None:
            return _SESSION
        if not MODEL_PATH.is_file():
            raise FileNotFoundError(f"Silero VAD model not found: {MODEL_PATH}")
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        _SESSION = ort.InferenceSession(
            str(MODEL_PATH), sess_options=so,
            providers=["CPUExecutionProvider"],
        )
        return _SESSION


class SileroVAD:
    """Stateful VAD: feed consecutive blocks, get the max probability."""

    def __init__(self, threshold: float = 0.5) -> None:
        self.threshold = float(threshold)
        self._state = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)
        self._pending = np.empty(0, dtype=np.float32)

    def reset(self) -> None:
        with _LOCK:
            self._state = np.zeros(STATE_SHAPE, dtype=np.float32)
            self._context = np.zeros(CONTEXT, dtype=np.float32)
            self._pending = np.empty(0, dtype=np.float32)

    @staticmethod
    def _to_float32(audio) -> np.ndarray:
        if isinstance(audio, (bytes, bytearray, memoryview)):
            x = np.frombuffer(bytes(audio), dtype=np.int16).astype(np.float32) / 32768.0
        else:
            x = np.asarray(audio, dtype=np.float32).ravel()
            if x.size and float(np.max(np.abs(x))) > 1.5:
                x = x / 32768.0
        return x

    def process(self, audio) -> float:
        """Return the highest speech probability across the windows in `audio`."""
        x = self._to_float32(audio)
        if not x.size:
            return 0.0
        with _LOCK:
            if self._pending.size:
                x = np.concatenate((self._pending, x))
            n = (x.size // WINDOW) * WINDOW
            self._pending = x[n:].copy()
            session = _session()
            best = 0.0
            for start in range(0, n, WINDOW):
                chunk = x[start:start + WINDOW]
                # v5/v6 expect the previous 64 samples concatenated to the
                # 512-sample window; without this context the model returns
                # near-zero probabilities even on clear speech.
                framed = np.concatenate((self._context, chunk)).reshape(1, -1)
                out, self._state = session.run(
                    None,
                    {"input": framed, "state": self._state,
                     "sr": np.array(SAMPLE_RATE, dtype=np.int64)},
                )
                self._context = framed[0, -CONTEXT:].copy()
                best = max(best, float(np.asarray(out).reshape(-1)[0]))
            return best

    def is_speech(self, audio) -> bool:
        return self.process(audio) >= self.threshold


def warmup() -> None:
    SileroVAD().process(np.zeros(WINDOW, dtype=np.float32))
