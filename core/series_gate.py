"""Modo série — a progressive, per-segment voice gate before any paid step.

WHY THIS EXISTS
    The microphone hears the room; the room includes the TV. A series voice
    used to become a turn because the assistant forgave a low voiceprint score
    whenever only the owner was enrolled ("probably the microphone, answer as
    the owner"). With a show playing, every line of dialogue became a turn,
    a paid transcription and a model call. That forgiveness is gone.

WHAT THE GATE DOES
    One speech segment (the exact PCM the segmenter cut) is evaluated
    progressively:

      1. sample ~1 s and identify. A confident, AUTHORIZED voice is an
         immediate accept — "Gino, tá aí?" resolves without waiting for the
         whole window;
      2. still undecided, take another ~1 s and re-evaluate;
      3. stop at a configurable ceiling (default 3 s). With no verdict at the
         ceiling the segment is discarded in silence — no spoken refusal, no
         transcription, no model;
      4. a segment with less than ~0.8 s of usable audio is INDECISIVE, not an
         intruder: it is never rejected for being short (the caller may already
         have all the audio, and a door slam must not train the gate).

    A cheap DSP vote (core/playback_detect) runs on the same segment as a VETO
    only: high-confidence playback + a voice that does not match the owner or
    an authorized guest = silent discard. On doubt the voice wins.

MODES
    Series mode ON   unknown, known-but-unauthorized, playback and non-voice
                     segments die silently before transcription.
    Series mode OFF  only high-confidence playback is vetoed; unknown voices
                     keep the existing spoken warning after transcription.

STATE
    ``memory/series_mode.json`` — ``{"enabled": true, "updated_at": ...}``,
    written atomically, no popup. It is turned on and off by voice in main.py
    ("modo série" / "sai do modo série").

CONFIG (config/api_keys.json, all optional)
    "series_gate_max_seconds": 3.0            ceiling for the progressive sample
    "series_gate_accept_threshold": 0.45      default: the SpeakerId threshold
    "series_gate_playback_confidence": 0.68   DSP veto threshold
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from core import playback_detect
from core import speaker_id

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_STATE_PATH = (
    Path(__file__).resolve().parents[1] / "memory" / "series_mode.json"
)
DEFAULT_FIRST_SAMPLE_SECONDS = 1.0
DEFAULT_SAMPLE_STEP_SECONDS = 1.0
# Ceiling for the progressive window. Raising it gives a bad microphone more
# chances but delays nothing on the accept path (which stops at the first
# confident window) and costs one extra embedding per second on the reject
# path. Keep it small: a series line should be dropped quickly.
DEFAULT_MAX_SAMPLE_SECONDS = 3.0
DEFAULT_MIN_DECIDABLE_SECONDS = 0.8
DEFAULT_PLAYBACK_CONFIDENCE = playback_detect.DEFAULT_PLAYBACK_THRESHOLD
DEFAULT_ACCEPT_THRESHOLD = None      # None → SpeakerId.threshold
_MAX_ALLOWED_SAMPLE_SECONDS = 10.0

_EPS = 1e-12


def _audio_float(value: Any) -> np.ndarray:
    return playback_detect._audio_float(value)


def _pcm_key(pcm: bytes) -> str:
    return hashlib.blake2b(bytes(pcm), digest_size=8).hexdigest()


def _trim_silence(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Drop leading/trailing near-silence, keeping 50 ms of context."""
    if samples.size < int(0.06 * sample_rate):
        return samples
    frame = max(1, int(0.02 * sample_rate))
    hop = max(1, int(0.01 * sample_rate))
    count = 1 + (samples.size - frame) // hop
    if count <= 0:
        return samples
    index = np.arange(frame)[None, :] + hop * np.arange(count)[:, None]
    rms = np.sqrt(np.mean(samples[index] ** 2, axis=1) + _EPS)
    top = float(np.max(rms))
    if top <= 1e-6:
        return samples
    voiced = np.flatnonzero(rms >= max(0.004, 0.08 * top))
    if voiced.size == 0:
        return samples
    pad = int(0.05 * sample_rate)
    first = max(0, int(voiced[0]) * hop - pad)
    last = min(samples.size, int(voiced[-1]) * hop + frame + pad)
    return samples[first:last]


class SeriesGate:
    """Progressive voice gate + persisted ``modo série`` state."""

    def __init__(
        self,
        speaker: Any = None,
        state_path: str | Path | None = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        first_sample_seconds: float = DEFAULT_FIRST_SAMPLE_SECONDS,
        sample_step_seconds: float = DEFAULT_SAMPLE_STEP_SECONDS,
        max_sample_seconds: float = DEFAULT_MAX_SAMPLE_SECONDS,
        min_decidable_seconds: float = DEFAULT_MIN_DECIDABLE_SECONDS,
        accept_threshold: float | None = DEFAULT_ACCEPT_THRESHOLD,
        playback_confidence: float = DEFAULT_PLAYBACK_CONFIDENCE,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self._speaker = speaker
        self._state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self._sample_rate = max(8000, int(sample_rate or DEFAULT_SAMPLE_RATE))
        self._first_s = max(0.2, float(first_sample_seconds))
        self._step_s = max(0.1, float(sample_step_seconds))
        cap = float(max_sample_seconds)
        self._max_s = min(_MAX_ALLOWED_SAMPLE_SECONDS,
                          max(self._first_s, cap))
        self._min_s = max(0.2, float(min_decidable_seconds))
        self._accept = (float(accept_threshold)
                        if accept_threshold is not None else None)
        self._playback_confidence = min(1.0, max(0.0, float(playback_confidence)))
        self._log_fn = logger or (lambda _m: None)
        self._lock = threading.RLock()
        self._last: dict | None = None
        self._enabled = self._load_state()

    # ── persisted state ─────────────────────────────────────────────────────

    @property
    def state_path(self) -> Path:
        return self._state_path

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> bool:
        value = bool(enabled)
        with self._lock:
            self._enabled = value
            self._save_state_locked(value)
        return value

    def reload_state(self) -> bool:
        with self._lock:
            self._enabled = self._load_state()
            return self._enabled

    def _load_state(self) -> bool:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return bool(data.get("enabled"))
        except Exception:
            return False

    def _save_state_locked(self, enabled: bool) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"enabled": bool(enabled),
                 "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                ensure_ascii=False,
            )
            fd, tmp = tempfile.mkstemp(
                prefix=".series-mode-", suffix=".tmp",
                dir=str(self._state_path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.replace(tmp, self._state_path)
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
                raise
        except Exception as e:
            self._log(f"[SeriesGate] não consegui salvar o estado: {e}")

    # ── voice decision ──────────────────────────────────────────────────────

    @property
    def accept_threshold(self) -> float:
        threshold = getattr(self._speaker, "threshold", None)
        if threshold is None:
            threshold = 0.45
        if self._accept is None:
            return float(threshold)
        return float(self._accept)

    def _identify(self, window: np.ndarray) -> dict | None:
        if self._speaker is None or window.size == 0:
            return None
        try:
            embedding = self._speaker.embedding(window)
            # Conversational gate: the expanded (auto-enrolled) set may help
            # here. Privileged actions never pass through this gate.
            return speaker_id.identify_conversational(self._speaker, embedding)
        except Exception as e:
            self._log(f"[SeriesGate] identificação falhou: {e}")
            return None

    def evaluate(self, pcm: bytes) -> dict:
        """Evaluate one segment. Returns a decision dict; never raises."""
        raw = bytes(pcm or b"")
        key = _pcm_key(raw) if raw else ""
        with self._lock:
            enabled = self._enabled
        decision: dict = {
            "verdict": "indecisive_short",
            "allowed": True,
            "speaker": None,
            "authorized": False,
            "score": 0.0,
            "seconds": 0.0,
            "attempts": [],
            "playback": {},
            "enabled": enabled,
            "key": key,
            "reason": "",
        }
        samples = _audio_float(raw)
        if samples.size == 0:
            decision["reason"] = "áudio vazio"
            self._remember(decision)
            return decision

        useful = _trim_silence(samples, self._sample_rate)
        if useful.size == 0:
            useful = samples
        total_s = useful.size / float(self._sample_rate)
        decision["seconds"] = round(total_s, 2)
        playback = playback_detect.classify(
            useful, self._sample_rate, self._playback_confidence)
        decision["playback"] = playback
        playback_high = (playback.get("label") == "playback"
                         and float(playback.get("confidence") or 0.0)
                         >= self._playback_confidence)

        best: dict | None = None
        sample_s = self._first_s
        while True:
            take_s = min(sample_s, total_s)
            take_n = int(round(take_s * self._sample_rate))
            window = useful[:take_n]
            if (window.size >= int(self._min_s * self._sample_rate)
                    and self._speaker is not None):
                match = self._identify(window)
                if match is not None:
                    best = match
                    effective = max(float(match.get("score") or 0.0),
                                    float(match.get("sample_score") or 0.0))
                    decision["attempts"].append({
                        "seconds": round(take_s, 2),
                        "name": match.get("name"),
                        "score": round(effective, 4),
                    })
                    decision["score"] = round(effective, 4)
                    if match.get("known") and match.get("authorized"):
                        if effective >= self.accept_threshold:
                            decision.update({
                                "verdict": "accept",
                                "allowed": True,
                                "speaker": match.get("name"),
                                "authorized": True,
                                "reason": f"voz autorizada em {take_s:.1f}s",
                            })
                            self._remember(decision)
                            return decision
                    elif match.get("known") and not match.get("authorized"):
                        # A speaker artefact overrides the spoken warning even
                        # in normal mode: silent discard, by policy.
                        verdict = "playback" if playback_high else "unauthorized"
                        decision.update({
                            "verdict": verdict,
                            "allowed": (not enabled) and not playback_high,
                            "speaker": match.get("name"),
                            "reason": (f"voz de {match.get('name')} não autorizada"
                                       if not playback_high else
                                       "reprodução com voz não autorizada"),
                        })
                        self._log(
                            f"[SeriesGate] voz conhecida não autorizada "
                            f"({match.get('name')}) — "
                            + ("descarte silencioso."
                               if enabled or playback_high
                               else "modo normal: aviso permitido."))
                        self._remember(decision)
                        return decision
            if take_s >= total_s - 1e-9 or take_s >= self._max_s - 1e-9:
                break
            sample_s = min(sample_s + self._step_s, self._max_s)

        if playback_high:
            verdict, reason = "playback", "artefato de reprodução em alta confiança"
        elif total_s < self._min_s:
            verdict, reason = "indecisive_short", "áudio curto demais para decidir"
        elif not playback.get("speech_like", True):
            verdict, reason = "not_voice", "não parece voz"
        elif best is None:
            verdict, reason = "indecisive", "não consegui identificar a voz"
        else:
            verdict, reason = "unknown", "voz não reconhecida no teto de amostragem"

        allowed = True
        if verdict == "playback":
            allowed = False
        elif enabled and verdict in ("unknown", "indecisive", "not_voice"):
            allowed = False
        # indecisive_short stays allowed in both modes: too short to decide is
        # not evidence of an intruder (and playback_high was handled above).

        decision.update({
            "verdict": verdict,
            "allowed": allowed,
            "reason": reason,
            "speaker": None if verdict != "accept" else decision.get("speaker"),
        })
        self._remember(decision)
        return decision

    def _remember(self, decision: dict) -> None:
        with self._lock:
            decision["at"] = time.monotonic()
            self._last = dict(decision)

    def last_decision(self) -> dict | None:
        """Copy of the most recent decision, whatever the audio was.

        Cheap enough for the STT audio thread (one lock, one dict copy): it is
        the cached verdict the `current_speaker_is_owner` hook reads instead of
        running the embedder mid-capture.
        """
        with self._lock:
            return dict(self._last) if self._last else None

    def decision_for(self, pcm: bytes | None) -> dict | None:
        """The cached decision for this exact PCM, or None."""
        if not pcm:
            return None
        key = _pcm_key(bytes(pcm))
        with self._lock:
            last = self._last
            if last is not None and last.get("key") == key:
                return dict(last)
        return None

    def precheck(self, pcm: bytes) -> bool:
        """True when the segment may be transcribed. Logs every discard."""
        decision = self.evaluate(pcm)
        if decision.get("allowed"):
            return True
        verdict = decision.get("verdict")
        reason = decision.get("reason") or verdict
        self._log(
            f"[SeriesGate] descarte silencioso ({verdict}): {reason} "
            f"[{decision.get('seconds', 0):.1f}s]"
        )
        return False

    def _log(self, message: str) -> None:
        try:
            self._log_fn(message)
        except Exception:
            pass
