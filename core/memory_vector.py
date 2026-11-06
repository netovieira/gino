"""FAISS-based vector index for semantic memory search (FASE4-TASK2).

WHY THIS EXISTS
    The embedding search in ChatMemory._search_vector() does a linear scan
    over all stored vectors, computing cosine similarity for each one. With
    a few hundred embeddings this is fine, but with 10k+ conversations it
    becomes a bottleneck. FAISS replaces the O(n) scan with an optimised
    nearest-neighbour search, keeping latency under 200ms even with large
    indexes.

HOW IT WORKS
    - Vectors are normalised to unit length, so L2 distance ranks the same
      as cosine similarity (mathematically equivalent for unit vectors).
    - The index is stored on disk as ``faiss.index`` (binary) plus
      ``faiss_meta.json`` (key list mapping FAISS internal IDs to our
      content-addressed embedding keys).
    - The index is rebuilt from ``embeddings.json`` whenever it is missing
      or out of sync. Incremental adds avoid a full rebuild when only a
      few new vectors were computed.

FALLBACK
    If FAISS is not installed or the index fails to build, the caller
    (ChatMemory._search_vector) must fall back to the linear cosine scan.
    This module never raises on import; ``faiss`` absence is detected at
    runtime and reported through ``available()``.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

import numpy as np

# Lazy import: faiss-cpu may not be installed. Everything in this module
# degrades gracefully when it is absent.
_faiss = None
_faiss_import_attempted = False


def _get_faiss():
    """Import faiss on first use; return None when unavailable."""
    global _faiss, _faiss_import_attempted
    if _faiss_import_attempted:
        return _faiss
    _faiss_import_attempted = True
    try:
        import faiss
        _faiss = faiss
    except ImportError:
        print("[VectorIndex] faiss-cpu not installed; using linear scan")
        _faiss = None
    return _faiss


def available() -> bool:
    """True when FAISS is importable and ready to use."""
    return _get_faiss() is not None


class VectorIndex:
    """FAISS-backed nearest-neighbour index persisted to disk.

    The index stores normalised vectors and uses IndexFlatL2 (exact L2
    search). For unit vectors, L2 ranking is identical to cosine ranking,
    so the search results are interchangeable with the linear cosine scan
    in ChatMemory._search_vector.
    """

    def __init__(self, index_dir: Path):
        self.index_dir = Path(index_dir)
        self.index_path = self.index_dir / "faiss.index"
        self.meta_path = self.index_dir / "faiss_meta.json"
        self._index = None
        self._keys: list[str] = []
        self._key_set: set[str] = set()
        self._dim: int = 0
        self._lock = threading.Lock()

    # ── state queries ──────────────────────────────────────────────────────

    @property
    def is_built(self) -> bool:
        return self._index is not None and len(self._keys) > 0

    @property
    def size(self) -> int:
        return len(self._keys)

    @property
    def dim(self) -> int:
        return self._dim

    # ── build / rebuild ────────────────────────────────────────────────────

    def build(self, embeddings: dict[str, list[float]]) -> int:
        """Build (or rebuild) the FAISS index from a key -> vector dict.

        Returns the number of vectors indexed. Returns 0 when FAISS is
        unavailable or the dict is empty.
        """
        faiss = _get_faiss()
        if faiss is None or not embeddings:
            return 0

        keys = list(embeddings.keys())
        vectors = [embeddings[k] for k in keys]
        dim = len(vectors[0])

        # Validate: all vectors must have the same dimension
        valid_keys = []
        valid_vecs = []
        for k, v in zip(keys, vectors):
            if isinstance(v, (list, tuple)) and len(v) == dim:
                valid_keys.append(k)
                valid_vecs.append(v)
        if not valid_vecs:
            return 0

        dim = len(valid_vecs[0])
        matrix = np.array(valid_vecs, dtype=np.float32)
        # Normalise for cosine-equivalent L2 search
        faiss.normalize_L2(matrix)

        index = faiss.IndexFlatL2(dim)
        index.add(matrix)

        with self._lock:
            self._index = index
            self._keys = valid_keys
            self._key_set = set(valid_keys)
            self._dim = dim

        self._persist()
        return len(valid_keys)

    # ── incremental add ────────────────────────────────────────────────────

    def add(self, key: str, vector: list[float]) -> bool:
        """Add one vector to the existing index. Returns True on success.

        If the key is already indexed, this is a no-op (returns True).
        If the index is empty or the dimension does not match, returns
        False — the caller should trigger a full ``build()`` instead.
        """
        faiss = _get_faiss()
        if faiss is None:
            return False

        with self._lock:
            if key in self._key_set:
                return True  # already indexed
            if self._index is None:
                return False  # no index to add to
            dim = self._index.d
            if len(vector) != dim:
                return False  # dimension mismatch

            vec = np.array([vector], dtype=np.float32)
            faiss.normalize_L2(vec)
            self._index.add(vec)
            self._keys.append(key)
            self._key_set.add(key)

        self._persist()
        return True

    # ── search ─────────────────────────────────────────────────────────────

    def search(self, query_vector: list[float],
               k: int = 10) -> list[tuple[str, float]]:
        """Return up to k nearest neighbours as ``(key, score)`` pairs.

        The score is cosine similarity (derived from L2 distance on
        normalised vectors: ``cos = 1 - L2^2 / 2``), highest first.
        Returns an empty list when the index is empty or FAISS fails.
        """
        faiss = _get_faiss()
        if faiss is None or self._index is None or not self._keys:
            return []

        try:
            vec = np.array([query_vector], dtype=np.float32)
            if np.linalg.norm(vec) < 1e-10:
                return []
            faiss.normalize_L2(vec)

            k = min(k, len(self._keys))
            distances, indices = self._index.search(vec, k)

            results = []
            for dist, idx in zip(distances[0], indices[0]):
                if 0 <= idx < len(self._keys):
                    # L2 on unit vectors: cos_sim = 1 - d^2/2
                    cos_sim = 1.0 - float(dist) * float(dist) / 2.0
                    results.append((self._keys[idx], cos_sim))
            return results
        except Exception as e:
            print(f"[VectorIndex] search failed: {e}")
            return []

    # ── remove ─────────────────────────────────────────────────────────────

    def remove(self, keys_to_remove: set[str]) -> int:
        """Remove keys from the index. Returns the number actually removed.

        FAISS IndexFlat does not support individual removal, so this
        rebuilds the index without the removed keys. Cheap when the
        removal set is small relative to the index.
        """
        with self._lock:
            before = len(self._keys)
            remaining_keys = [k for k in self._keys
                              if k not in keys_to_remove]
            removed = before - len(remaining_keys)
            if removed == 0:
                return 0
            self._keys = remaining_keys
            self._key_set = set(remaining_keys)
            # Mark for rebuild on next search/build call
            self._index = None
            self._delete_files()
        return removed

    # ── persistence ────────────────────────────────────────────────────────

    def _persist(self):
        """Write the FAISS index and key metadata to disk atomically."""
        faiss = _get_faiss()
        if faiss is None or self._index is None:
            return
        try:
            self.index_dir.mkdir(parents=True, exist_ok=True)
            tmp_idx = self.index_path.with_name(
                f".faiss.index.{os.getpid()}.tmp")
            faiss.write_index(self._index, str(tmp_idx))
            os.replace(tmp_idx, self.index_path)

            tmp_meta = self.meta_path.with_name(
                f".faiss_meta.{os.getpid()}.tmp")
            meta = {"version": 1, "dim": self._dim, "keys": self._keys}
            with open(tmp_meta, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False, sort_keys=True)
                fh.write("\n")
            os.replace(tmp_meta, self.meta_path)
        except Exception as e:
            print(f"[VectorIndex] persist failed: {e}")

    def _delete_files(self):
        """Remove persisted index files (called before a rebuild)."""
        try:
            self.index_path.unlink(missing_ok=True)
            self.meta_path.unlink(missing_ok=True)
        except Exception:
            pass

    def load(self) -> bool:
        """Load a previously persisted FAISS index from disk.

        Returns True on success, False when the files are missing or
        corrupted (caller should trigger a ``build()`` instead).
        """
        faiss = _get_faiss()
        if faiss is None:
            return False
        if not self.index_path.is_file() or not self.meta_path.is_file():
            return False
        try:
            index = faiss.read_index(str(self.index_path))
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            keys = meta.get("keys", [])
            dim = meta.get("dim", index.d)
            if index.d != dim:
                return False  # dimension mismatch between index and metadata
            if index.ntotal != len(keys):
                return False  # key count mismatch
            with self._lock:
                self._index = index
                self._keys = list(keys)
                self._key_set = set(keys)
                self._dim = dim
            return True
        except Exception as e:
            print(f"[VectorIndex] load failed: {e}")
            return False

    def ensure_loaded(self, embeddings: dict[str, list[float]]) -> bool:
        """Load from disk if possible, otherwise build from the dict.

        Returns True when the index is ready for search.
        """
        if self.is_built:
            return True
        if self.load():
            return True
        n = self.build(embeddings)
        return n > 0

    def needs_rebuild(self, embeddings: dict[str, list[float]]) -> bool:
        """True when the index is missing vectors that the dict has.

        Used to decide whether to add incrementally or rebuild.
        """
        if not self.is_built:
            return True
        missing = sum(1 for k in embeddings if k not in self._key_set)
        return missing > 0
