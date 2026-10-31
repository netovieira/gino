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
# Below this a segment is too short for a trustworthy embedding.
_MIN_EMBED_SECONDS = 0.05

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
            centroid = np.mean(
                np.asarray([_normalize(s) for s in samples], dtype=np.float64),
                axis=0,
            )
            rec["centroid"] = _normalize(centroid).tolist()
            rec["updated"] = now
            self._save_locked()
            return self._public(slug, rec)

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

    def identify(self, embedding: Any) -> dict:
        """Compare one embedding with the enrolled centroids (cosine).

        Returns ``{"name", "score", "known", "authorized"}``. ``name`` is None
        for an unknown voice; ``score`` is the best cosine similarity found.
        A score above the threshold makes it ``known``; ``authorized`` is only
        true when that voice was explicitly authorized (or is the owner).
        """
        vec = _normalize(embedding)
        with self._lock:
            best_slug = None
            best_score = -1.0
            best_sample = -1.0
            for slug, rec in self._speakers.items():
                centroid = np.asarray(rec["centroid"], dtype=np.float64)
                if centroid.shape != vec.shape:
                    # Enrolled with another embedding model: incomparable.
                    continue
                score = float(np.dot(vec, centroid))
                if score > best_score:
                    best_slug, best_score = slug, score
                    # A single sample can match a new recording better than the
                    # average does — the microphone, a cold or a different tone
                    # moves one sample, not the whole centroid.
                    best_sample = -1.0
                    for sample in rec.get("samples") or []:
                        sv = np.asarray(sample, dtype=np.float64)
                        if sv.shape == vec.shape:
                            best_sample = max(best_sample,
                                              float(np.dot(vec, _normalize(sv))))
            effective = max(best_score, best_sample)
            known = best_slug is not None and effective >= self._threshold
            authorized = False
            name = None
            if known:
                name = self._speakers[best_slug]["name"]
                authorized = bool(self._speakers[best_slug].get("authorized"))
                authorized = authorized or best_slug == _slugify(self._owner_name)
            if best_slug is None:
                best_score = 0.0
                best_sample = 0.0
            return {
                "name": name,
                "score": float(round(best_score, 6)),
                "sample_score": float(round(max(best_sample, 0.0), 6)),
                "known": bool(known),
                "authorized": bool(authorized),
            }

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
        """Embedding for one audio clip, as a plain list of floats."""
        samples = _audio_float(pcm16)
        if samples.size == 0:
            raise ValueError("áudio vazio")
        self._ensure_models()
        return self._embed_samples(samples)

    def analyze_utterance(self, pcm16: Any, transcribe=None,
                          full_text: str = "") -> dict:
        """Diarize one utterance, identify each segment and label the text.

        ``transcribe`` is an optional callable receiving a PCM byte slice for a
        segment and returning its text. Without it, the full transcript is
        attributed to the primary speaker.

        Returns a dict with ``segments``, ``labeled_text``, ``primary``,
        ``unknown`` and ``unauthorized``. The caller decides whether to answer;
        this method only measures.
        """
        samples = _audio_float(pcm16)
        if samples.size == 0:
            return {"segments": [], "labeled_text": str(full_text or ""),
                    "primary": None, "primary_name": None, "primary_text": "",
                    "primary_embedding": [], "primary_pcm": b"",
                    "unknown": None, "unauthorized": None}
        raw = _pcm_bytes(pcm16)
        rate = self._sample_rate
        try:
            segments = self.diarize(samples)
        except Exception as e:
            print(f"[SpeakerId] diarization failed: {e}")
            segments = []

        if not segments:
            try:
                emb = self.embedding(samples)
                match = self.identify(emb)
            except Exception:
                emb, match = [], None
            return {
                "segments": [],
                "labeled_text": str(full_text or ""),
                "primary": match,
                "primary_name": (match or {}).get("name") or "desconhecido",
                "primary_text": str(full_text or ""),
                "primary_embedding": emb,
                "primary_pcm": raw,
                "unknown": ({"embedding": emb, "pcm": raw}
                            if match is None or not match.get("known") else None),
                "unauthorized": ({"name": match.get("name"), "embedding": emb,
                                  "pcm": raw}
                                 if match and match.get("known")
                                 and not match.get("authorized") else None),
            }

        labeled: list[str] = []
        unknown = None
        unauthorized = None
        primary_match = None
        primary_name = None
        primary_text = ""
        primary_embedding: list[float] = []
        primary_pcm = b""
        for seg in segments:
            start = max(0.0, float(seg.get("start") or 0.0))
            end = max(start, float(seg.get("end") or start))
            first = max(0, int(round(start * rate)))
            last = min(samples.size, int(round(end * rate)))
            seg_pcm = raw[first * 2:last * 2]
            emb = seg.get("embedding") or []
            match = None
            if emb:
                try:
                    match = self.identify(emb)
                except Exception:
                    match = None
            name = (match or {}).get("name") or "desconhecido"
            text = ""
            if transcribe is not None and seg_pcm:
                try:
                    text = str(transcribe(seg_pcm) or "").strip()
                except Exception as e:
                    print(f"[SpeakerId] segment transcription failed: {e}")
            if text:
                labeled.append(f"[FALANTE: {name}] {text}")
                primary_text = text
            if match is None or not match.get("known"):
                unknown = {"embedding": emb, "pcm": seg_pcm}
            elif not match.get("authorized"):
                unauthorized = {"name": match.get("name"), "embedding": emb,
                                "pcm": seg_pcm}
            primary_match = match
            primary_name = name
            primary_embedding = emb
            primary_pcm = seg_pcm

        if not labeled:
            labeled.append(f"[FALANTE: {primary_name or 'desconhecido'}] "
                           f"{str(full_text or '').strip()}".strip())

        # A short or noisy segment can fail to embed on its own while the whole
        # utterance still matches an authorized voice. Use that as a fallback
        # only for an unknown primary; an explicitly unauthorized known voice
        # must keep its refusal.
        if primary_match is None or not primary_match.get("known"):
            try:
                whole = self.embedding(samples)
                whole_match = self.identify(whole)
                if whole_match.get("known") and whole_match.get("authorized"):
                    primary_match = whole_match
                    primary_name = whole_match.get("name") or primary_name
                    primary_embedding = whole
                    labeled = [
                        line.replace("[FALANTE: desconhecido]",
                                     f"[FALANTE: {primary_name}]")
                        for line in labeled
                    ]
            except Exception:
                pass

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
                    centroid = _normalize(rec.get("centroid")).tolist()
                    samples = [
                        _normalize(sample).tolist()
                        for sample in (rec.get("samples") or [])
                    ]
                except (ValueError, TypeError):
                    continue
                if not samples:
                    samples = [centroid]
                clean[_slugify(slug)] = {
                    "name": str(rec.get("name") or slug).strip() or str(slug),
                    "authorized": bool(rec.get("authorized")),
                    "centroid": centroid,
                    "samples": samples[-MAX_SAMPLES:],
                    "created": rec.get("created"),
                    "updated": rec.get("updated"),
                }
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
