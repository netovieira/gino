"""Smart Turn v3.2 — decide whether a spoken turn is complete.

WHY THIS EXISTS
    The energy VAD in core/groq_stt.py can only tell "sound" from "silence". It
    cannot tell a finished sentence from a pause in the middle of one, so a
    fixed ~700 ms of silence ends the turn — and a user who stops to think is
    cut off. Smart Turn is a small (8 MB, int8) Whisper-Tiny-based ONNX model
    that looks at the last 8 s of audio and answers "complete?" with a
    probability. It supports 23 languages, including Portuguese.

LICENSE
    Model and code: BSD-2-Clause, from https://github.com/pipecat-ai/smart-turn
    and https://huggingface.co/pipecat-ai/smart-turn-v3.
    The ONNX file lives in models/ (git-ignored); this wrapper is ours.

LATENCY
    ~10-100 ms on CPU for one prediction. It only runs when the VAD sees a
    pause, so it never sits in the per-block audio path.
"""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
MODEL_PATH = BASE_DIR / "models" / "smart-turn-v3.2-cpu.onnx"
SAMPLE_RATE = 16_000
WINDOW_SECONDS = 8

_LOCK = threading.RLock()
_SESSION = None
_FEATURE_EXTRACTOR = None


def available() -> bool:
    return MODEL_PATH.is_file()


def _load():
    global _SESSION, _FEATURE_EXTRACTOR
    with _LOCK:
        if _SESSION is not None:
            return _SESSION, _FEATURE_EXTRACTOR
        if not MODEL_PATH.is_file():
            raise FileNotFoundError(f"Smart Turn model not found: {MODEL_PATH}")
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor

        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.intra_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        _SESSION = ort.InferenceSession(
            str(MODEL_PATH), sess_options=so,
            providers=["CPUExecutionProvider"],
        )
        _FEATURE_EXTRACTOR = WhisperFeatureExtractor(chunk_length=WINDOW_SECONDS)
        return _SESSION, _FEATURE_EXTRACTOR


def _to_float32(audio) -> np.ndarray:
    if isinstance(audio, (bytes, bytearray, memoryview)):
        x = np.frombuffer(bytes(audio), dtype=np.int16).astype(np.float32) / 32768.0
    else:
        x = np.asarray(audio, dtype=np.float32).ravel()
        if x.size and float(np.max(np.abs(x))) > 1.5:
            x = x / 32768.0
    return x


def predict_endpoint(audio) -> dict:
    """Predict whether the turn in `audio` is complete.

    `audio` is 16 kHz mono, as int16 bytes or float32 samples. Returns
    {"prediction": 1|0, "probability": float}. The model sees at most the last
    8 s; shorter audio is left-padded, exactly as the upstream reference does.
    """
    x = _to_float32(audio)
    n = WINDOW_SECONDS * SAMPLE_RATE
    if x.size > n:
        x = x[-n:]
    elif x.size < n:
        x = np.pad(x, (n - x.size, 0))

    session, fe = _load()
    features = fe(
        x, sampling_rate=SAMPLE_RATE, return_tensors="np",
        padding="max_length", max_length=n, truncation=True, do_normalize=True,
    ).input_features.astype(np.float32)
    with _LOCK:
        out = session.run(None, {"input_features": features})[0]
    value = float(np.asarray(out).reshape(-1)[0])
    # The published model returns a sigmoid probability; a future export could
    # return logits, so clamp only when the value is outside the probability
    # range instead of applying sigmoid twice.
    probability = value if 0.0 <= value <= 1.0 else 1.0 / (1.0 + np.exp(-value))
    return {"prediction": 1 if probability > 0.5 else 0,
            "probability": probability}


def warmup() -> None:
    """Load the model once, off the conversation path."""
    _load()
    predict_endpoint(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))
