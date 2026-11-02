"""
Speaker diarization and voiceprint identification (sherpa-onnx).

Who said what, and whether we have heard that voice before. Diarization
splits one utterance into speaker turns (pyannote segmentation + ERes2Net
embeddings + fast clustering, all ONNX, no torch). Identification compares an
embedding against the centroids enrolled under ``memory/voices/index.json``.

SECURITY POLICY — READ BEFORE USING ``authorized``:
    A voice is an *identity hint*, never an authentication factor. ``authorized``
    only means "this sounds like somebody the owner allowed us to answer".
    Privileged actions (restart, shutdown, deleting files, changing secrets or
    persona) must additionally require an explicit spoken confirmation from the
    owner — ideally with a freshly re-checked voiceprint — and must never be
    granted on the strength of a similarity score alone. A recording, a good
    impression or a converted voice can all cross the threshold.

Storage: only L2-normalized embeddings and their centroid are kept, as plain
JSON-safe lists, in ``<store_dir>/index.json`` (default ``memory/voices``).
Raw audio is never stored. Writes are atomic (temp file + ``os.replace``) and
guarded by a re-entrant lock; the directory is created ``0700`` and the index
``0600`` because a voiceprint is personal data.

Two sets per person, same file:
    ``samples``       trusted, hand-enrolled material (manual UI, guided
                      training, "registra essa voz"). The centroid follows it.
    ``auto_samples``  the EXPANDED set grown by ``auto_enroll`` from
                      confidently identified speech. It never replaces or
                      reweights ``samples`` and is only consulted when a caller
                      asks ``identify(..., include_auto=True)``. The security
                      checks (restart/shutdown/send confirmations, unlock) call
                      ``identify`` with the default ``include_auto=False``, so
                      adaptive material can never widen the privileged gate.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
DIARIZATION_DIR = _ROOT / "models" / "diarization"
DEFAULT_SEGMENTATION_MODEL = (
    DIARIZATION_DIR / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
)
DEFAULT_EMBEDDING_MODEL = DIARIZATION_DIR / "3dspeaker_eres2net_base.onnx"
DEFAULT_THRESHOLD = 0.45
DEFAULT_STORE_DIR = _ROOT / "memory" / "voices"
DEFAULT_OWNER = "Anthero"
SAMPLE_RATE = 16000

# Enough samples to survive a change of tone or microphone without letting the
# index grow forever.
MAX_SAMPLES = 20

# ── continuous (adaptive) enrollment ────────────────────────────────────────
# A matched, authorized utterance above this similarity may be appended to the
# speaker's EXPANDED set. It sits well above DEFAULT_THRESHOLD (0.45) so only a
# confident match feeds the profile; the base threshold still decides who is
# answered from the trusted samples.
DEFAULT_AUTO_ENROLL_THRESHOLD = 0.75
DEFAULT_AUTO_ENROLL_MAX = 50
# Below this an utterance is too short for a trustworthy embedding.
DEFAULT_AUTO_ENROLL_MIN_SECONDS = 0.8
# The expanded set may only nudge an utterance that still resembles the
# trusted centroid at least this much. It prevents a drifted auto sample from
# vouching for a completely different voice.
DEFAULT_AUTO_ANCHOR_FLOOR = 0.30
# Two samples this close add no information; skip them instead of filling the
# cap with the owner repeating the same phrase.
_AUTO_ENROLL_DUPLICATE = 0.995
_AUTO_AUDIO_MIN_RMS = 0.004       # below this is silence/noise floor
_AUTO_AUDIO_MAX_RMS = 0.6         # above this the microphone is hot/clipped
_AUTO_AUDIO_MAX_CLIPPED = 0.02    # fraction of samples pinned at full scale

# Below this a segment is too short for a trustworthy embedding.
_MIN_EMBED_SECONDS = 0.05

# ── live fingerprint hygiene ────────────────────────────────────────────────
# The embedding of a live utterance must be computed from the VOICED span: the
# pre-roll padding and the quiet boost of the listener otherwise drag a clean
# owner clip towards room tone. Below this much voiced audio the embedding is
# not comparable with the enrollment clips, so the verdict is deliberately
# neutral ("indecisive_short") instead of "unknown".
_MIN_VOICED_SECONDS = 0.8
VERDICT_KNOWN = "known"
VERDICT_UNKNOWN = "unknown"
VERDICT_INDECISIVE = "indecisive_short"

# Robust centroid: a sample joins the coherent cluster when it is at least this
# close to the cluster mean. Singletons never leave the index — they just stop
# pulling the centroid away from the voices that actually agree.
_CENTROID_CLUSTER = 0.5
_CENTROID_MIN_SAMPLES = 4      # below this a mean is safer than a cluster
_CENTROID_TRIM_MIN = 5         # from here one worst member is trimmed
_GRAY_BAND_LOW = 0.35          # debug line before a likely rejection

_EPS = 1e-12

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def available(
    segmentation_model: str | Path | None = None,
    embedding_model: str | Path | None = None,
) -> bool:
    """True when both ONNX models are on disk (they are git-ignored)."""
    seg = Path(segmentation_model or DEFAULT_SEGMENTATION_MODEL)
    emb = Path(embedding_model or DEFAULT_EMBEDDING_MODEL)
    return seg.is_file() and emb.is_file()


def _slugify(name: str) -> str:
    """Accent- and case-insensitive stable key for a person's name."""
    raw = unicodedata.normalize("NFKD", str(name))
    plain = "".join(ch for ch in raw if not unicodedata.combining(ch))
    slug = _SLUG_RE.sub("-", plain.casefold()).strip("-")
    return slug or "speaker"


def _normalize(vector: Any) -> np.ndarray:
    """One finite, non-zero vector scaled to unit length."""
    arr = np.asarray(vector, dtype=np.float64).reshape(-1)
    if arr.size == 0:
        raise ValueError("embedding vazio")
    if not np.all(np.isfinite(arr)):
        raise ValueError("embedding contém NaN ou infinito")
    norm = float(np.linalg.norm(arr))
    if norm <= 1e-12:
        raise ValueError("embedding de norma zero")
    return arr / norm


def _audio_float(pcm16: Any) -> np.ndarray:
    """int16 mono PCM (bytes, int array) or float samples → float32 in [-1, 1]."""
    if isinstance(pcm16, (bytes, bytearray, memoryview)):
        raw = bytes(pcm16)
        if len(raw) % 2:
            raw = raw[:-1]
        if not raw:
            return np.zeros(0, dtype=np.float32)
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    arr = np.asarray(pcm16)
    if arr.ndim != 1:
        arr = arr.reshape(-1)
    if arr.dtype.kind in "iu":
        return arr.astype(np.float32) / 32768.0
    out = arr.astype(np.float32)
    if out.size and float(np.max(np.abs(out))) > 1.5:
        out = out / 32768.0
    return out


def _pcm_bytes(value: Any) -> bytes:
    """Normalize bytes/int16/float samples to little-endian int16 PCM bytes."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return raw[:-1] if len(raw) % 2 else raw
    arr = np.asarray(value)
    if arr.ndim != 1:
        arr = arr.reshape(-1)
    if arr.dtype.kind in "iu":
        return arr.astype("<i2").tobytes()
    out = arr.astype(np.float32)
    if out.size and float(np.max(np.abs(out))) > 1.5:
        out = out / 32768.0
    return (np.clip(out, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _is_pcm(value: Any) -> bool:
    """True for raw audio; float arrays and lists are treated as embeddings."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return True
    if isinstance(value, np.ndarray):
        return value.ndim == 1 and value.dtype.kind in "iu"
    return False


def _embedding_rows(value: Any) -> list[np.ndarray]:
    """One embedding or a batch of them, each L2-normalized."""
    arr = np.asarray(value, dtype=np.float64)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    if arr.ndim != 2:
        raise ValueError("esperava um embedding (1-D) ou uma matriz de embeddings (2-D)")
    if arr.shape[0] == 0 or arr.shape[1] == 0:
        raise ValueError("embedding vazio")
    return [_normalize(row) for row in arr]


def _audio_quality(pcm16: Any, sample_rate: int = SAMPLE_RATE,
                   min_seconds: float = DEFAULT_AUTO_ENROLL_MIN_SECONDS) -> dict:
    """Cheap gate for adaptive enrollment: loud enough, long enough, no clipping.

    Auto-enrollment must never learn from a door slam, a silent window or a
    saturated microphone. Pure DSP, no model.
    """
    samples = _audio_float(pcm16)
    seconds = samples.size / float(sample_rate or SAMPLE_RATE)
    result = {"ok": False, "reason": "", "seconds": round(seconds, 2),
              "rms": 0.0, "clipped": 0.0}
    if samples.size == 0:
        result["reason"] = "áudio vazio"
        return result
    if seconds < float(min_seconds):
        result["reason"] = f"curto demais ({seconds:.2f}s)"
        return result
    levels = np.abs(samples)
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    clipped = float(np.mean(levels >= 0.999))
    result.update({"rms": round(rms, 4), "clipped": round(clipped, 4)})
    if rms < _AUTO_AUDIO_MIN_RMS:
        result["reason"] = f"baixo demais (rms={rms:.3f})"
        return result
    if clipped > _AUTO_AUDIO_MAX_CLIPPED:
        result["reason"] = f"áudio saturado ({clipped:.1%} no teto)"
        return result
    if rms > _AUTO_AUDIO_MAX_RMS:
        result["reason"] = f"alto demais (rms={rms:.2f})"
        return result
    result["ok"] = True
    return result


def _trim_to_voiced(
    samples: np.ndarray, sample_rate: int
) -> tuple[np.ndarray, float]:
    """Voiced span of ``samples`` and how many seconds of voice it holds.

    Adapted from ``core.series_gate._trim_silence`` (kept here as a copy so
    this module never imports the gate — the gate already imports us). Drops
    leading/trailing near-silence keeping 50 ms of context. The second value
    counts only the voiced frames (the context pads are not "voice"), which is
    what the minimum-length guard must look at: a 1 s clip that is 90 % room
    tone is not a 1 s utterance.
    """
    arr = np.asarray(samples, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return arr, 0.0
    seconds = arr.size / float(sample_rate or SAMPLE_RATE)
    if arr.size < int(0.06 * sample_rate):
        return arr, float(seconds)
    frame = max(1, int(0.02 * sample_rate))
    hop = max(1, int(0.01 * sample_rate))
    count = 1 + (arr.size - frame) // hop
    if count <= 0:
        return arr, float(seconds)
    index = np.arange(frame)[None, :] + hop * np.arange(count)[:, None]
    rms = np.sqrt(np.mean(arr[index] ** 2, axis=1) + _EPS)
    top = float(np.max(rms))
    if top <= 1e-6:
        return arr, 0.0
    voiced = np.flatnonzero(rms >= max(0.004, 0.08 * top))
    if voiced.size == 0:
        return arr, 0.0
    pad = int(0.05 * sample_rate)
    first = max(0, int(voiced[0]) * hop - pad)
    last = min(arr.size, int(voiced[-1]) * hop + frame + pad)
    trimmed = arr[first:last]
    # Counting is more permissive than trimming (2 % of the peak instead of
    # 8 %): quiet speech inside a loud utterance is still voice, it is not a
    # reason to call the clip indecisive.
    voiced = np.flatnonzero(rms >= max(0.004, 0.02 * top))
    voiced_seconds = min(
        float(trimmed.size) / float(sample_rate),
        float(voiced.size * hop) / float(sample_rate),
    )
    return trimmed, voiced_seconds


def _centroid_from_samples(
    vectors: list[Any] | np.ndarray,
    cluster_threshold: float = _CENTROID_CLUSTER,
) -> tuple[np.ndarray, list[int]]:
    """Robust centroid and the indices of the samples that built it.

    Greedy clustering: the medoid (sample with the highest mean similarity to
    everybody) seeds the cluster; samples at least ``cluster_threshold`` close
    to the cluster mean join it, refined twice. The centroid is the trimmed
    mean of that cluster (the single worst member is dropped once the cluster
    is large enough), re-normalized. Samples outside the cluster are never
    removed from the store — they simply stop dragging the centroid.

    Small sets (fewer than ``_CENTROID_MIN_SAMPLES``) keep the plain mean: with
    two or three samples there is no statistical basis to call one junk.
    """
    arr = np.asarray([_normalize(v) for v in vectors], dtype=np.float64)
    if arr.ndim != 2 or arr.shape[0] == 0:
        raise ValueError("embedding vazio")
    every = list(range(arr.shape[0]))
    if arr.shape[0] < _CENTROID_MIN_SAMPLES:
        return _normalize(arr.mean(axis=0)), every
    sims = arr @ arr.T
    seed = int(np.argmax(sims.mean(axis=1)))
    keep = [int(i) for i in np.flatnonzero(sims[seed] >= cluster_threshold)]
    for _ in range(2):
        mean = _normalize(arr[keep].mean(axis=0))
        refined = [i for i in keep if float(arr[i] @ mean) >= cluster_threshold]
        if len(refined) < 2 or refined == keep:
            break
        keep = refined
    if len(keep) < 2:
        keep = every
    member = arr[keep]
    mean = _normalize(member.mean(axis=0))
    if len(member) >= _CENTROID_TRIM_MIN:
        order = np.argsort(member @ mean)
        member = member[order[1:]]
        keep = [keep[int(i)] for i in order[1:]]
    return _normalize(member.mean(axis=0)), keep


def _match_effective(match: dict | None) -> float:
    """Effective score of an ``identify`` result (centroid or best sample)."""
    if not match:
        return -1.0
    try:
        if match.get("effective_score") is not None:
            return float(match["effective_score"])
        return max(float(match.get("score") or 0.0),
                   float(match.get("sample_score") or 0.0))
    except (TypeError, ValueError):
        return -1.0


def _verdict_result(verdict: str, score: float = 0.0,
                    sample_score: float = 0.0) -> dict:
    """Neutral ``identify``-shaped result for a path with no usable embedding."""
    return {
        "name": None,
        "score": float(score),
        "sample_score": float(sample_score),
        "effective_score": float(max(score, sample_score)),
        "known": False,
        "authorized": False,
        "verdict": verdict,
    }


def identify_conversational(speaker: Any, embedding: Any,
                            voiced_seconds: float | None = None) -> dict:
    """Identify for the conversational gate, using the EXPANDED set.

    Kept out of the security path on purpose: callers that decide a privileged
    action must call ``speaker.identify(embedding)`` directly. Tolerates a
    stand-in speaker whose ``identify`` takes a single argument.
    """
    kwargs: dict[str, Any] = {"include_auto": True}
    if voiced_seconds is not None:
        kwargs["voiced_seconds"] = float(voiced_seconds)
    try:
        return speaker.identify(embedding, **kwargs)
    except TypeError:
        return speaker.identify(embedding)


class SpeakerId:
    """Diarize utterances and enroll/identify voices.

    Diarization models are loaded lazily, so enrollment and identification can
    be used (and tested) without sherpa-onnx or the ONNX files.
    """

    def __init__(
        self,
        segmentation_model: str | Path | None = None,
        embedding_model: str | Path | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        store_dir: str | Path | None = None,
        owner_name: str = DEFAULT_OWNER,
        provider: str = "cpu",
        num_threads: int = 1,
        cluster_threshold: float = 0.5,
        sample_rate: int = SAMPLE_RATE,
        auto_enroll_enabled: bool = True,
        auto_enroll_threshold: float = DEFAULT_AUTO_ENROLL_THRESHOLD,
        auto_enroll_max: int = DEFAULT_AUTO_ENROLL_MAX,
        auto_enroll_min_seconds: float = DEFAULT_AUTO_ENROLL_MIN_SECONDS,
        auto_anchor_floor: float = DEFAULT_AUTO_ANCHOR_FLOOR,
        debug_scores: bool = False,
    ) -> None:
        self.segmentation_model = Path(segmentation_model or DEFAULT_SEGMENTATION_MODEL)
        self.embedding_model = Path(embedding_model or DEFAULT_EMBEDDING_MODEL)
        threshold = float(threshold)
        if not -1.0 <= threshold <= 1.0:
            raise ValueError("threshold deve estar entre -1 e 1")
        self._threshold = threshold
        self._cluster_threshold = float(cluster_threshold)
        self._provider = str(provider or "cpu")
        self._num_threads = max(1, int(num_threads))
        self._sample_rate = int(sample_rate or SAMPLE_RATE)
        self._store_dir = Path(store_dir) if store_dir else DEFAULT_STORE_DIR
        self._index_path = self._store_dir / "index.json"
        self._lock = threading.RLock()
        self._speakers: dict[str, dict[str, Any]] = {}
        self._owner_name = str(owner_name or DEFAULT_OWNER).strip() or DEFAULT_OWNER

        # Adaptive enrollment. ``auto_enroll_enabled`` is a master switch; a
        # zero cap disables growth the same way.
        self._auto_enabled = bool(auto_enroll_enabled)
        self._auto_threshold = min(1.0, max(0.0, float(auto_enroll_threshold)))
        self._auto_max = max(0, int(auto_enroll_max))
        self._auto_min_s = max(0.2, float(auto_enroll_min_seconds))
        self._auto_anchor = min(1.0, max(-1.0, float(auto_anchor_floor)))
        # Optional one-line debug in the gray band (rejections that are almost
        # matches). Off unless asked; JARVIS_SPEAKER_DEBUG=1 turns it on
        # without touching main.py.
        self._debug_scores = bool(debug_scores) or bool(
            os.environ.get("JARVIS_SPEAKER_DEBUG"))

        # sherpa-onnx objects, built on first use.
        self._diarizer = None
        self._extractor = None
        self._segmentation_config = None
        self._embedding_config = None
        self._num_clusters = -1

        self._load()

    # ── public state ────────────────────────────────────────────────────────

    @property
    def owner_name(self) -> str:
        with self._lock:
            return self._owner_name

    @property
    def threshold(self) -> float:
        with self._lock:
            return self._threshold

    @property
    def auto_enroll_enabled(self) -> bool:
        with self._lock:
            return bool(self._auto_enabled and self._auto_threshold > 0.0
                        and self._auto_max > 0)

    @property
    def auto_enroll_max(self) -> int:
        with self._lock:
            return self._auto_max

    @property
    def auto_enroll_threshold(self) -> float:
        with self._lock:
            return self._auto_threshold

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def set_owner(self, name: str) -> str:
        """Set the owner. The owner is always authorized when identified."""
        name = str(name or "").strip()
        if not name:
            raise ValueError("nome do dono vazio")
        with self._lock:
            self._owner_name = name
            rec = self._speakers.get(_slugify(name))
            if rec is not None:
                rec["authorized"] = True
                rec["updated"] = time.time()
            self._save_locked()
            return name

    # ── enrollment store ────────────────────────────────────────────────────

    def enroll(
        self,
        name: str,
        pcm16_or_embeddings: Any,
        authorized: bool = False,
    ) -> dict:
        """Add one voice sample (audio) or several embeddings under ``name``.

        Re-enrolling an existing person merges samples and never silently
        removes an authorization already granted. Raw audio is converted to an
        embedding and discarded; only normalized vectors are persisted.

        ``authorized`` is an identity hint, not authentication — see the module
        docstring.
        """
        display = str(name or "").strip()
        if not display:
            raise ValueError("nome vazio")
        slug = _slugify(display)

        if _is_pcm(pcm16_or_embeddings):
            vectors = [_normalize(self.embedding(pcm16_or_embeddings))]
        else:
            vectors = _embedding_rows(pcm16_or_embeddings)
        dim = len(vectors[0])
        if any(len(v) != dim for v in vectors):
            raise ValueError("embeddings com dimensões diferentes")

        with self._lock:
            rec = self._speakers.get(slug)
            if rec is None and self._speakers:
                expected = len(next(iter(self._speakers.values()))["centroid"])
            elif rec is not None:
                expected = len(rec["centroid"])
            else:
                expected = dim
            if dim != expected:
                raise ValueError(
                    f"embedding com {dim} dimensões; o índice usa {expected}"
                )

            is_owner = slug == _slugify(self._owner_name)
            now = time.time()
            if rec is None:
                rec = {
                    "name": display,
                    "authorized": bool(authorized) or is_owner,
                    "samples": [],
                    "created": now,
                    "updated": now,
                }
                self._speakers[slug] = rec
            else:
                # Never deauthorize by re-enrolling with the default flag.
                rec["authorized"] = bool(rec.get("authorized")) or bool(authorized) or is_owner

            samples = list(rec.get("samples") or [])
            samples.extend(v.tolist() for v in vectors)
            if len(samples) > MAX_SAMPLES:
                samples = samples[-MAX_SAMPLES:]
            rec["samples"] = samples
            centroid, _ = _centroid_from_samples(samples)
            rec["centroid"] = centroid.tolist()
            rec["updated"] = now
            self._save_locked()
            return self._public(slug, rec)

    def auto_enroll(self, pcm16: Any = None, *,
                    embedding: Any = None) -> dict:
        """Append one confidently matched utterance to the EXPANDED set.

        Only called from the conversational gate after the utterance was
        accepted. The match is re-checked here against the trusted ``samples``
        of every enrolled voice (never against ``auto_samples``), so an
        existing drift cannot keep endorsing itself. Requirements:

        * audio passes ``_audio_quality`` (duration, silence, clipping);
        * the closest trusted voice is authorized (or the owner) and its
          similarity is at least ``auto_enroll_threshold``;
        * the vector is not a near-duplicate of an existing auto sample.

        ``samples``/``centroid`` (the privileged gate's only source) are never
        touched; the new vector goes to ``auto_samples`` and is capped at
        ``auto_enroll_max`` (oldest dropped). Returns a small result dict.
        """
        with self._lock:
            enabled = self.auto_enroll_enabled
            max_samples = self._auto_max
            min_seconds = self._auto_min_s
            threshold = self._auto_threshold
        result: dict[str, Any] = {
            "enrolled": False, "reason": "", "name": None, "score": 0.0,
            "samples": 0, "max": max_samples,
        }
        if not enabled:
            result["reason"] = "desativado"
            return result

        if pcm16 is not None:
            quality = _audio_quality(pcm16, self._sample_rate, min_seconds)
            if not quality["ok"]:
                result["reason"] = quality["reason"]
                return result
        if embedding is not None:
            try:
                vec = _normalize(embedding)
            except (ValueError, TypeError) as e:
                result["reason"] = str(e)
                return result
        elif pcm16 is not None:
            try:
                vec = _normalize(self.embedding(pcm16))
            except Exception as e:
                result["reason"] = f"embedding falhou: {e}"
                return result
        else:
            result["reason"] = "sem áudio"
            return result

        with self._lock:
            slug, score, sample_score = self._best_trusted(vec)
            effective = max(score, sample_score)
            if slug is None:
                result["reason"] = "voz desconhecida"
                return result
            rec = self._speakers.get(slug)
            if rec is None:
                result["reason"] = "voz desconhecida"
                return result
            result["name"] = rec.get("name")
            result["score"] = float(round(max(effective, 0.0), 6))
            is_owner = slug == _slugify(self._owner_name)
            if not (bool(rec.get("authorized")) or is_owner):
                result["reason"] = "voz não autorizada"
                return result
            if effective < threshold:
                result["reason"] = "confiança baixa"
                return result

            samples = [np.asarray(s, dtype=np.float64)
                       for s in rec.get("auto_samples") or []]
            if samples:
                duplicate = max(float(np.dot(vec, _normalize(s)))
                                for s in samples)
                if duplicate >= _AUTO_ENROLL_DUPLICATE:
                    result["reason"] = "amostra duplicada"
                    result["samples"] = len(samples)
                    return result
            samples.append(vec)
            if len(samples) > max_samples:
                samples = samples[-max_samples:]
            rec["auto_samples"] = [s.tolist() for s in samples]
            centroid = np.mean(
                np.asarray([_normalize(s) for s in samples], dtype=np.float64),
                axis=0,
            )
            rec["auto_centroid"] = _normalize(centroid).tolist()
            rec["auto_updated"] = time.time()
            self._save_locked()
            result.update({
                "enrolled": True,
                "reason": "",
                "samples": len(rec["auto_samples"]),
                "max": max_samples,
            })
            return result

    def authorize(self, name: str) -> dict:
        """Mark an enrolled person as allowed to be answered. Raises KeyError."""
        slug = _slugify(name)
        with self._lock:
            rec = self._speakers.get(slug)
            if rec is None:
                raise KeyError(f"voz não cadastrada: {name}")
            rec["authorized"] = True
            rec["updated"] = time.time()
            self._save_locked()
            return self._public(slug, rec)

    def list(self) -> list[dict]:
        """Enrolled voices, sorted by name. Never includes the embeddings."""
        with self._lock:
            return [
                self._public(slug, rec)
                for slug, rec in sorted(
                    self._speakers.items(), key=lambda item: item[1]["name"].casefold()
                )
            ]

    def remove(self, name: str) -> bool:
        """Forget an enrolled voice. True if something was removed."""
        slug = _slugify(name)
        with self._lock:
            if slug not in self._speakers:
                return False
            del self._speakers[slug]
            self._save_locked()
            return True

    def sample_trust(self, name: str) -> list[float]:
        """Cosine of each stored sample to the robust centroid, by index.

        A high value means the sample agrees with the person's coherent voice;
        a low one is a singleton that no longer contributes to the centroid.
        The samples themselves stay in the index either way. Raises KeyError.
        """
        slug = _slugify(name)
        with self._lock:
            rec = self._speakers.get(slug)
            if rec is None:
                raise KeyError(f"voz não cadastrada: {name}")
            centroid = _normalize(rec["centroid"])
            return [
                round(float(np.dot(_normalize(sample), centroid)), 6)
                for sample in rec.get("samples") or []
            ]

    def recompute_centroids(self, persist: bool = True) -> list[dict]:
        """Rebuild every centroid from its coherent sample cluster.

        Maintenance entry point: samples are never deleted, only ignored by
        the centroid. ``persist`` writes the new centroids to ``index.json``;
        without it the change lives only in memory. Returns one report dict
        per person with the cluster indices, per-sample trust and how far the
        centroid moved.
        """
        report: list[dict] = []
        with self._lock:
            for slug, rec in self._speakers.items():
                samples = list(rec.get("samples") or [])
                if not samples:
                    continue
                centroid, cluster = _centroid_from_samples(samples)
                previous = np.asarray(rec["centroid"], dtype=np.float64)
                trust = [
                    round(float(np.dot(_normalize(sample), centroid)), 6)
                    for sample in samples
                ]
                report.append({
                    "name": rec.get("name"),
                    "samples": len(samples),
                    "cluster": cluster,
                    "trust": trust,
                    "centroid_shift": round(float(np.dot(previous, centroid)), 6),
                })
                rec["centroid"] = centroid.tolist()
            if persist and report:
                self._save_locked()
        return report

    def identify(self, embedding: Any, include_auto: bool = False,
                 voiced_seconds: float | None = None) -> dict:
        """Compare one embedding with the enrolled centroids (cosine).

        Returns ``{"name", "score", "sample_score", "effective_score",
        "known", "authorized", "verdict"}``. ``name`` is None for an unknown
        voice; ``score`` is the cosine against the (robust) centroid,
        ``sample_score`` the best single stored sample and ``effective_score``
        the better of the two — the number the threshold actually uses.
        ``verdict`` is ``"known"``, ``"unknown"`` or ``"indecisive_short"``;
        the last one means the clip is too short in voiced audio to judge
        (``voiced_seconds``), so it must not be treated as a rejection.

        ``include_auto`` is the conversational switch: it also consults the
        EXPANDED (auto-enrolled) set. It defaults to False deliberately — the
        privileged gate (restart/shutdown/send/unlock) must keep deciding on
        the trusted, hand-enrolled samples only, so adaptive material can
        never widen it.
        """
        vec = _normalize(embedding)
        with self._lock:
            best_slug = None
            best_score = -1.0
            best_sample = -1.0
            best_effective = -1.0
            for slug, rec in self._speakers.items():
                centroid = np.asarray(rec["centroid"], dtype=np.float64)
                if centroid.shape != vec.shape:
                    # Enrolled with another embedding model: incomparable.
                    continue
                # A single sample can match a new recording better than the
                # average does — the microphone, a cold or a different tone
                # moves one sample, not the whole centroid.
                score = float(np.dot(vec, centroid))
                sample_score = -1.0
                for sample in rec.get("samples") or []:
                    sv = np.asarray(sample, dtype=np.float64)
                    if sv.shape == vec.shape:
                        sample_score = max(sample_score,
                                           float(np.dot(vec, _normalize(sv))))
                effective = max(score, sample_score)
                if include_auto and effective >= self._auto_anchor:
                    auto_score, auto_sample = self._auto_scores(vec, rec)
                    score = max(score, auto_score)
                    sample_score = max(sample_score, auto_sample)
                    effective = max(score, sample_score)
                if effective > best_effective:
                    best_slug, best_score, best_sample = slug, score, sample_score
                    best_effective = effective
            effective = best_effective
            if best_slug is None:
                best_score = 0.0
                best_sample = 0.0
                effective = 0.0
            known = best_slug is not None and effective >= self._threshold
            authorized = False
            name = None
            if known:
                name = self._speakers[best_slug]["name"]
                authorized = bool(self._speakers[best_slug].get("authorized"))
                authorized = authorized or best_slug == _slugify(self._owner_name)
            verdict = VERDICT_KNOWN if known else VERDICT_UNKNOWN
            if voiced_seconds is not None and float(voiced_seconds) < _MIN_VOICED_SECONDS:
                # Too little voice to compare: neutral, never "unknown". A
                # caller that only understands known/unknown keeps seeing
                # known=False; the new ``verdict`` says what it really is.
                known = False
                name = None
                authorized = False
                verdict = VERDICT_INDECISIVE
            if (self._debug_scores and best_slug is not None
                    and _GRAY_BAND_LOW <= effective < self._threshold):
                print(f"[SpeakerId] faixa cinzenta score={effective:.3f} "
                      f"centroid={max(best_score, 0.0):.3f} "
                      f"amostra={max(best_sample, 0.0):.3f} "
                      f"limiar={self._threshold:.2f} voz={name or '?'} "
                      f"veredito={verdict}")
            return {
                "name": name,
                "score": float(round(best_score, 6)),
                "sample_score": float(round(max(best_sample, 0.0), 6)),
                "effective_score": float(round(max(effective, 0.0), 6)),
                "known": bool(known),
                "authorized": bool(authorized),
                "verdict": verdict,
            }

    def _best_trusted(self, vec: np.ndarray) -> tuple[str | None, float, float]:
        """Closest trusted voice for ``vec``: (slug, centroid score, best sample).

        Trusted samples only — used by ``auto_enroll`` so the enrollment
        decision is anchored on hand-enrolled material, never on the expanded
        set. Call with the lock held.
        """
        best_slug = None
        best_score = -1.0
        best_sample = -1.0
        best_effective = -1.0
        for slug, rec in self._speakers.items():
            centroid = np.asarray(rec["centroid"], dtype=np.float64)
            if centroid.shape != vec.shape:
                continue
            score = float(np.dot(vec, centroid))
            sample_score = -1.0
            for sample in rec.get("samples") or []:
                sv = np.asarray(sample, dtype=np.float64)
                if sv.shape == vec.shape:
                    sample_score = max(sample_score,
                                       float(np.dot(vec, _normalize(sv))))
            effective = max(score, sample_score)
            if effective > best_effective:
                best_slug, best_score, best_sample = slug, score, sample_score
                best_effective = effective
        return best_slug, best_score, best_sample

    def _auto_scores(self, vec: np.ndarray, rec: dict) -> tuple[float, float]:
        """Best cosine in the expanded set: (centroid score, sample score).

        Returns (-1, -1) when the person has no comparable auto sample. Call
        with the lock held.
        """
        auto_score = -1.0
        auto_sample = -1.0
        centroid = rec.get("auto_centroid")
        if centroid:
            arr = np.asarray(centroid, dtype=np.float64)
            if arr.shape == vec.shape:
                auto_score = float(np.dot(vec, _normalize(arr)))
        for sample in rec.get("auto_samples") or []:
            arr = np.asarray(sample, dtype=np.float64)
            if arr.shape == vec.shape:
                auto_sample = max(auto_sample,
                                  float(np.dot(vec, _normalize(arr))))
        return auto_score, auto_sample

    # ── diarization / embeddings ────────────────────────────────────────────

    def diarize(self, pcm16: Any, num_speakers: int = -1) -> list[dict]:
        """Split one utterance into speaker segments.

        ``pcm16`` is int16 mono at ``sample_rate`` (bytes or array; float
        samples are accepted too). ``num_speakers`` forces the cluster count
        when positive; -1 lets the clustering decide.

        Returns ``[{"start", "end", "speaker", "embedding"}, ...]`` with times
        in seconds and the embedding of each segment (empty when the segment
        is too short to embed).
        """
        samples = _audio_float(pcm16)
        if samples.size == 0:
            return []
        diarizer = self._ensure_models()
        wanted = int(num_speakers) if num_speakers is not None else -1
        if wanted <= 0:
            wanted = -1
        if wanted != self._num_clusters:
            import sherpa_onnx as so

            config = so.OfflineSpeakerDiarizationConfig(
                self._segmentation_config,
                self._embedding_config,
                so.FastClusteringConfig(wanted, self._cluster_threshold),
            )
            diarizer.set_config(config)
            self._num_clusters = wanted

        result = diarizer.process(samples)
        rate = int(getattr(diarizer, "sample_rate", self._sample_rate) or self._sample_rate)
        minimum = max(1, int(_MIN_EMBED_SECONDS * rate))
        segments: list[dict] = []
        for segment in result.sort_by_start_time():
            start = float(segment.start)
            end = float(segment.end)
            first = max(0, int(round(start * rate)))
            last = min(samples.size, int(round(end * rate)))
            embedding: list[float] = []
            if last - first >= minimum:
                try:
                    embedding = self._embed_samples(samples[first:last])
                except Exception:
                    # A clipping/silent segment can fail to embed; it still
                    # belongs in the diarization result without a vector.
                    embedding = []
            segments.append({
                "start": start,
                "end": end,
                "speaker": int(segment.speaker),
                "embedding": embedding,
            })
        return segments

    def embedding(self, pcm16: Any) -> list[float]:
        """Embedding for one audio clip: the VOICED part, as float list.

        The live listener pads an utterance with 1.5 s of pre-roll and may
        boost quiet audio; embedding that raw clip mixes room tone into the
        fingerprint and costs ~0.05 cosine. Trimming here (and in
        ``analyze_utterance``) makes live and enrollment vectors comparable —
        enrollment goes through this same method.
        """
        samples = _audio_float(pcm16)
        if samples.size == 0:
            raise ValueError("áudio vazio")
        trimmed, _ = _trim_to_voiced(samples, self._sample_rate)
        if trimmed.size == 0:
            raise ValueError("áudio vazio")
        self._ensure_models()
        return self._embed_samples(trimmed)

    def analyze_utterance(self, pcm16: Any, transcribe=None,
                          full_text: str = "") -> dict:
        """Diarize one utterance, identify each segment and label the text.

        ``transcribe`` is an optional callable receiving a PCM byte slice for a
        segment and returning its text. Without it, the full transcript is
        attributed to the primary speaker.

        The fingerprint is taken from the VOICED span only (pre-roll padding
        and quiet room tone are trimmed first). Segment times are relative to
        that trimmed span. When the voiced span is shorter than
        ``_MIN_VOICED_SECONDS`` nothing is judged: the verdict is
        ``"indecisive_short"`` (never ``"unknown"``) and ``unknown`` is None.

        Returns a dict with ``segments``, ``labeled_text``, ``primary``,
        ``primary_name``, ``primary_text``, ``primary_embedding``,
        ``primary_pcm``, ``unknown``, ``unauthorized``, ``verdict`` and
        ``voiced_seconds``. The caller decides whether to answer; this method
        only measures. Matching uses the EXPANDED set (adaptive enrollment)
        because this analysis only feeds the conversational gate; privileged
        actions must call ``identify`` without ``include_auto``.
        """
        samples = _audio_float(pcm16)
        text = str(full_text or "")
        if samples.size == 0:
            return {"segments": [], "labeled_text": text,
                    "primary": None, "primary_name": None, "primary_text": "",
                    "primary_embedding": [], "primary_pcm": b"",
                    "unknown": None, "unauthorized": None,
                    "verdict": VERDICT_UNKNOWN, "voiced_seconds": 0.0}
        rate = self._sample_rate
        trimmed, voiced_seconds = _trim_to_voiced(samples, rate)
        voiced_raw = _pcm_bytes(trimmed)
        if voiced_seconds < _MIN_VOICED_SECONDS:
            # No usable voice to compare. Do NOT call the diarizer and do not
            # report an unknown speaker: a cough or a two-word "oi" must not
            # be treated as an intruder. Callers that only know known/unknown
            # keep working (known=False, unknown=None).
            return {
                "segments": [],
                "labeled_text": text,
                "primary": _verdict_result(VERDICT_INDECISIVE),
                "primary_name": None,
                "primary_text": text,
                "primary_embedding": [],
                "primary_pcm": voiced_raw,
                "unknown": None,
                "unauthorized": None,
                "verdict": VERDICT_INDECISIVE,
                "voiced_seconds": round(voiced_seconds, 3),
            }

        try:
            segments = self.diarize(trimmed)
        except Exception as e:
            print(f"[SpeakerId] diarization failed: {e}")
            segments = []

        labeled: list[str] = []
        unauthorized = None
        primary_match = None
        primary_name = None
        primary_text = "" if segments else text
        primary_embedding: list[float] = []
        primary_pcm = voiced_raw if not segments else b""
        best_segment_effective = -1.0
        for seg in segments:
            start = max(0.0, float(seg.get("start") or 0.0))
            end = max(start, float(seg.get("end") or start))
            first = max(0, int(round(start * rate)))
            last = min(trimmed.size, int(round(end * rate)))
            seg_pcm = voiced_raw[first * 2:last * 2]
            emb = seg.get("embedding") or []
            match = None
            if emb:
                try:
                    match = identify_conversational(self, emb)
                except Exception:
                    match = None
            if match is not None:
                best_segment_effective = max(best_segment_effective,
                                             _match_effective(match))
                if match.get("known") and not match.get("authorized"):
                    unauthorized = {"name": match.get("name"),
                                    "embedding": emb, "pcm": seg_pcm}
            name = (match or {}).get("name") or "desconhecido"
            seg_text = ""
            if transcribe is not None and seg_pcm:
                try:
                    seg_text = str(transcribe(seg_pcm) or "").strip()
                except Exception as e:
                    print(f"[SpeakerId] segment transcription failed: {e}")
            if seg_text:
                labeled.append(f"[FALANTE: {name}] {seg_text}")
                primary_text = seg_text
            primary_match = match
            primary_name = name
            primary_embedding = emb
            primary_pcm = seg_pcm

        # Whole trimmed clip as a retry. Diarization can cut an owner sentence
        # into 0.2 s slivers whose embeddings are useless; one embedding of the
        # full voiced span is usually right where the slivers were not. It is
        # only computed when no segment matched someone, and it replaces the
        # failed candidate only when it scores HIGHER — so a good segment (and
        # an explicit "known but unauthorized" refusal) is never second-guessed.
        whole_emb: list[float] = []
        whole_won = False
        if primary_match is None or not primary_match.get("known"):
            try:
                whole_emb = self.embedding(trimmed)
                whole_match = identify_conversational(
                    self, whole_emb, voiced_seconds=voiced_seconds)
            except Exception:
                whole_match = None
            if whole_match is not None:
                if _match_effective(whole_match) > best_segment_effective \
                        or primary_match is None:
                    primary_match = whole_match
                    primary_name = (whole_match.get("name")
                                    or ("desconhecido"
                                        if not whole_match.get("known") else None))
                    primary_embedding = whole_emb
                    primary_pcm = voiced_raw
                    whole_won = True

        if whole_won and primary_match.get("known"):
            labeled = [
                line.replace("[FALANTE: desconhecido]",
                             f"[FALANTE: {primary_name}]")
                for line in labeled
            ]

        if primary_match is None:
            primary_match = _verdict_result(VERDICT_UNKNOWN)
            unknown = {"embedding": [], "pcm": voiced_raw}
            verdict = VERDICT_UNKNOWN
        elif not primary_match.get("known"):
            # The FAILED candidate is remembered only when it really is the
            # best one; previously the last tiny segment always won, even when
            # the whole-clip retry heard the owner.
            unknown = {"embedding": primary_embedding, "pcm": primary_pcm}
            verdict = VERDICT_UNKNOWN
        else:
            unknown = None
            verdict = primary_match.get("verdict") or VERDICT_KNOWN
            if not primary_match.get("authorized"):
                unauthorized = {"name": primary_match.get("name"),
                                "embedding": primary_embedding,
                                "pcm": primary_pcm}

        if not labeled:
            if segments:
                subject = primary_name or "desconhecido"
                labeled.append(f"[FALANTE: {subject}] {text}".strip())
            else:
                # No diarization at all: keep the old behaviour of passing the
                # transcript through unlabeled instead of inventing a speaker.
                labeled.append(text)

        return {
            "segments": segments,
            "labeled_text": "\n".join(labeled),
            "primary": primary_match,
            "primary_name": primary_name,
            "primary_text": primary_text,
            "primary_embedding": primary_embedding,
            "primary_pcm": primary_pcm,
            "unknown": unknown,
            "unauthorized": unauthorized,
            "verdict": verdict,
            "voiced_seconds": round(voiced_seconds, 3),
        }

    def _embed_samples(self, samples: np.ndarray) -> list[float]:
        extractor = self._extractor
        if extractor is None:
            raise RuntimeError("extrator de embedding não inicializado")
        stream = extractor.create_stream()
        stream.accept_waveform(
            self._sample_rate,
            np.ascontiguousarray(samples, dtype=np.float32),
        )
        stream.input_finished()
        if not extractor.is_ready(stream):
            raise RuntimeError("áudio curto demais para um embedding")
        return [float(value) for value in extractor.compute(stream)]

    def _ensure_models(self):
        """Lazily build the diarizer/extractor (thread-safe)."""
        with self._lock:
            if self._diarizer is not None and self._extractor is not None:
                return self._diarizer
            if not available(self.segmentation_model, self.embedding_model):
                missing = [
                    str(p) for p in (self.segmentation_model, self.embedding_model)
                    if not Path(p).is_file()
                ]
                raise FileNotFoundError("modelo de diarização ausente: " + ", ".join(missing))
            try:
                import sherpa_onnx as so
            except ImportError as exc:  # pragma: no cover - environment issue
                raise RuntimeError(
                    "sherpa-onnx não instalado — rode '.venv/bin/pip install sherpa-onnx'"
                ) from exc

            pyannote = so.OfflineSpeakerSegmentationPyannoteModelConfig(
                str(self.segmentation_model)
            )
            segmentation = so.OfflineSpeakerSegmentationModelConfig(pyannote)
            segmentation.num_threads = self._num_threads
            segmentation.provider = self._provider

            embedding = so.SpeakerEmbeddingExtractorConfig(str(self.embedding_model))
            embedding.num_threads = self._num_threads
            embedding.provider = self._provider

            clustering = so.FastClusteringConfig(-1, self._cluster_threshold)
            config = so.OfflineSpeakerDiarizationConfig(
                segmentation, embedding, clustering
            )
            self._segmentation_config = segmentation
            self._embedding_config = embedding
            self._diarizer = so.OfflineSpeakerDiarization(config)
            self._extractor = so.SpeakerEmbeddingExtractor(embedding)
            self._num_clusters = -1
            return self._diarizer

    # ── persistence (call with the lock held) ───────────────────────────────

    def _public(self, slug: str, rec: dict) -> dict:
        return {
            "name": rec["name"],
            "authorized": bool(rec.get("authorized")),
            "owner": slug == _slugify(self._owner_name),
            "samples": len(rec.get("samples") or []),
            "auto_samples": len(rec.get("auto_samples") or []),
            "created": rec.get("created"),
            "updated": rec.get("updated"),
        }

    def _load(self) -> None:
        with self._lock:
            self._speakers = {}
            try:
                text = self._index_path.read_text(encoding="utf-8")
            except (FileNotFoundError, NotADirectoryError):
                return
            except OSError:
                return
            try:
                data = json.loads(text)
            except (ValueError, TypeError):
                self._backup_broken_index()
                return
            if not isinstance(data, dict):
                return
            speakers = data.get("speakers")
            if not isinstance(speakers, dict):
                return
            clean: dict[str, dict[str, Any]] = {}
            for slug, rec in speakers.items():
                if not isinstance(rec, dict):
                    continue
                try:
                    samples = [
                        _normalize(sample).tolist()
                        for sample in (rec.get("samples") or [])
                    ][-MAX_SAMPLES:]
                    if samples:
                        # Rebuild the centroid from the coherent cluster on
                        # load. Singletons stay in ``samples`` (owner data is
                        # sacred) but no longer drag the average; the stored
                        # centroid is only a fallback for an index without
                        # samples. Nothing is written to disk here.
                        centroid, _ = _centroid_from_samples(samples)
                        centroid = centroid.tolist()
                    else:
                        centroid = _normalize(rec.get("centroid")).tolist()
                        samples = [centroid]
                except (ValueError, TypeError):
                    continue
                # The expanded set is additive and optional: an older index or
                # a corrupt entry must never take the person's trusted samples
                # down with it.
                auto_samples: list[list[float]] = []
                for sample in rec.get("auto_samples") or []:
                    try:
                        vector = _normalize(sample).tolist()
                    except (ValueError, TypeError):
                        continue
                    if len(vector) == len(centroid):
                        auto_samples.append(vector)
                if self._auto_max > 0:
                    auto_samples = auto_samples[-self._auto_max:]
                else:
                    auto_samples = []
                clean[_slugify(slug)] = {
                    "name": str(rec.get("name") or slug).strip() or str(slug),
                    "authorized": bool(rec.get("authorized")),
                    "centroid": centroid,
                    "samples": samples[-MAX_SAMPLES:],
                    "auto_samples": auto_samples,
                    "created": rec.get("created"),
                    "updated": rec.get("updated"),
                }
                if auto_samples:
                    auto_centroid = np.mean(
                        np.asarray([_normalize(s) for s in auto_samples],
                                   dtype=np.float64),
                        axis=0,
                    )
                    clean[_slugify(slug)]["auto_centroid"] = (
                        _normalize(auto_centroid).tolist())
            self._speakers = clean

    def _backup_broken_index(self) -> None:
        """Keep a corrupted index for inspection instead of overwriting it."""
        try:
            backup = self._index_path.with_suffix(f".broken-{int(time.time())}.json")
            os.replace(self._index_path, backup)
        except OSError:
            pass

    def _save_locked(self) -> None:
        self._store_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._store_dir, 0o700)
        except OSError:
            pass
        payload = {
            "version": 1,
            "threshold": self._threshold,
            "owner": self._owner_name,
            "updated": time.time(),
            "speakers": self._speakers,
        }
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._store_dir), prefix=".index-", suffix=".json.tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            os.replace(tmp_name, self._index_path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
