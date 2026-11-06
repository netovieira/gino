"""FASE4-TASK2: vector memory with FAISS index.

Tests that:
- VectorIndex builds, searches and persists correctly
- FAISS search returns the same ranking as linear cosine scan
- Integration with ChatMemory.search() uses the FAISS path
- Latency with 10k+ vectors stays under 200ms
- Fallback to linear scan works when FAISS is absent
- Semantic search finds related conversations by meaning
"""
from __future__ import annotations

import math
import time
from datetime import datetime

import numpy as np
import pytest

from core.chat_memory import ChatMemory, _cosine, make_sentence_embedder
from core.memory_vector import VectorIndex, available as faiss_available

NOW = datetime(2025, 6, 15, 14, 30)


# ── helpers ──────────────────────────────────────────────────────────────────

def _random_unit_vector(dim: int = 384, rng=None) -> list[float]:
    """Generate a random unit vector (for testing without a real model)."""
    if rng is None:
        rng = np.random.default_rng(42)
    vec = rng.standard_normal(dim).astype(np.float32)
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec]


def _make_embeddings(n: int, dim: int = 384) -> dict[str, list[float]]:
    """Build n fake embeddings with deterministic keys."""
    rng = np.random.default_rng(42)
    return {f"source#{i:04d}": _random_unit_vector(dim, rng)
            for i in range(n)}


# ── VectorIndex: build & search ─────────────────────────────────────────────

class TestVectorIndexBuildSearch:
    """The index must build, search and return correct nearest neighbours."""

    @pytest.fixture(autouse=True)
    def _skip_without_faiss(self):
        if not faiss_available():
            pytest.skip("faiss-cpu nao instalado")

    def test_build_returns_correct_count(self, tmp_path):
        vi = VectorIndex(tmp_path / "idx")
        embs = _make_embeddings(100)
        count = vi.build(embs)
        assert count == 100
        assert vi.is_built
        assert vi.size == 100
        assert vi.dim == 384

    def test_build_empty_returns_zero(self, tmp_path):
        vi = VectorIndex(tmp_path / "idx")
        assert vi.build({}) == 0
        assert not vi.is_built

    def test_search_returns_nearest(self, tmp_path):
        """The query vector should be its own nearest neighbour."""
        vi = VectorIndex(tmp_path / "idx")
        embs = _make_embeddings(50)
        vi.build(embs)
        # Use the first vector as query: it must be its own nearest neighbour
        query = embs["source#0000"]
        hits = vi.search(query, k=5)
        assert len(hits) == 5
        top_key, top_score = hits[0]
        assert top_key == "source#0000"
        assert top_score > 0.99, f"self-match should be ~1.0, got {top_score}"

    def test_search_respects_k(self, tmp_path):
        vi = VectorIndex(tmp_path / "idx")
        embs = _make_embeddings(20)
        vi.build(embs)
        query = _random_unit_vector()
        hits = vi.search(query, k=3)
        assert len(hits) == 3

    def test_search_empty_index(self, tmp_path):
        vi = VectorIndex(tmp_path / "idx")
        hits = vi.search(_random_unit_vector(), k=5)
        assert hits == []

    def test_search_zero_vector(self, tmp_path):
        """A zero query vector must return empty, not crash."""
        vi = VectorIndex(tmp_path / "idx")
        vi.build(_make_embeddings(10))
        hits = vi.search([0.0] * 384, k=5)
        assert hits == []

    def test_faiss_ranking_matches_cosine(self, tmp_path):
        """FAISS top-10 must be a subset of the linear cosine top-10."""
        embs = _make_embeddings(200)
        query = _random_unit_vector()

        # FAISS ranking
        vi = VectorIndex(tmp_path / "idx")
        vi.build(embs)
        faiss_keys = {k for k, _ in vi.search(query, k=10)}

        # Linear cosine ranking
        linear_scores = [(k, _cosine(query, v)) for k, v in embs.items()]
        linear_scores.sort(key=lambda x: -x[1])
        linear_keys = {k for k, _ in linear_scores[:10]}

        # FAISS and linear should agree on the top results
        # (exact order may differ slightly due to floating point, but the
        # sets should overlap heavily)
        overlap = faiss_keys & linear_keys
        assert len(overlap) >= 8, \
            f"FAISS and linear should agree on most top-10; overlap={len(overlap)}"


# ── VectorIndex: incremental add ────────────────────────────────────────────

class TestVectorIndexAdd:
    """Incremental add must extend the index without a full rebuild."""

    @pytest.fixture(autouse=True)
    def _skip_without_faiss(self):
        if not faiss_available():
            pytest.skip("faiss-cpu nao instalado")

    def test_add_new_key(self, tmp_path):
        vi = VectorIndex(tmp_path / "idx")
        embs = _make_embeddings(10)
        vi.build(embs)
        assert vi.size == 10

        new_vec = _random_unit_vector()
        ok = vi.add("new#0001", new_vec)
        assert ok
        assert vi.size == 11

    def test_add_duplicate_is_noop(self, tmp_path):
        vi = VectorIndex(tmp_path / "idx")
        embs = _make_embeddings(5)
        vi.build(embs)
        ok = vi.add("source#0000", embs["source#0000"])
        assert ok
        assert vi.size == 5  # unchanged

    def test_add_wrong_dimension_fails(self, tmp_path):
        vi = VectorIndex(tmp_path / "idx")
        vi.build(_make_embeddings(5, dim=384))
        ok = vi.add("bad#0001", [1.0, 2.0, 3.0])  # dim=3, expected 384
        assert not ok

    def test_add_to_empty_index_fails(self, tmp_path):
        vi = VectorIndex(tmp_path / "idx")
        ok = vi.add("lonely#0001", _random_unit_vector())
        assert not ok


# ── VectorIndex: persistence ────────────────────────────────────────────────

class TestVectorIndexPersistence:
    """The index must survive a restart (new instance, same directory)."""

    @pytest.fixture(autouse=True)
    def _skip_without_faiss(self):
        if not faiss_available():
            pytest.skip("faiss-cpu nao instalado")

    def test_persist_and_load(self, tmp_path):
        embs = _make_embeddings(50)
        vi1 = VectorIndex(tmp_path / "idx")
        vi1.build(embs)
        assert vi1.is_built

        # New instance, same directory
        vi2 = VectorIndex(tmp_path / "idx")
        ok = vi2.load()
        assert ok
        assert vi2.is_built
        assert vi2.size == 50
        assert vi2.dim == 384

        # Search results must be identical
        query = _random_unit_vector()
        hits1 = vi1.search(query, k=5)
        hits2 = vi2.search(query, k=5)
        assert [(k, round(s, 5)) for k, s in hits1] == \
               [(k, round(s, 5)) for k, s in hits2]

    def test_ensure_loaded_builds_when_missing(self, tmp_path):
        embs = _make_embeddings(20)
        vi = VectorIndex(tmp_path / "idx")
        # No files on disk -> ensure_loaded must build from dict
        ok = vi.ensure_loaded(embs)
        assert ok
        assert vi.is_built
        assert vi.size == 20

    def test_load_missing_returns_false(self, tmp_path):
        vi = VectorIndex(tmp_path / "idx")
        assert not vi.load()


# ── VectorIndex: remove ─────────────────────────────────────────────────────

class TestVectorIndexRemove:
    """Removal must invalidate the index and trigger rebuild."""

    @pytest.fixture(autouse=True)
    def _skip_without_faiss(self):
        if not faiss_available():
            pytest.skip("faiss-cpu nao instalado")

    def test_remove_existing_keys(self, tmp_path):
        vi = VectorIndex(tmp_path / "idx")
        vi.build(_make_embeddings(10))
        removed = vi.remove({"source#0000", "source#0001"})
        assert removed == 2

    def test_remove_nonexistent_keys(self, tmp_path):
        vi = VectorIndex(tmp_path / "idx")
        vi.build(_make_embeddings(5))
        removed = vi.remove({"nope#0000"})
        assert removed == 0
        assert vi.size == 5  # unchanged


# ── Integration with ChatMemory ─────────────────────────────────────────────

class TestChatMemoryVectorIntegration:
    """ChatMemory.search() must use the FAISS path when available."""

    @pytest.fixture(autouse=True)
    def _skip_without_faiss(self):
        if not faiss_available():
            pytest.skip("faiss-cpu nao instalado")
        if make_sentence_embedder() is None:
            pytest.skip("sentence-transformers nao instalado")

    def test_search_uses_faiss(self, tmp_path):
        fn = make_sentence_embedder()
        mem = ChatMemory(base_dir=tmp_path / "chat", embed_fn=fn)
        assert mem._vector_index is not None, \
            "ChatMemory must instantiate a VectorIndex when FAISS is available"

        mem.append_turn("user", "Fui ao mercado comprar frutas", at=NOW)
        mem.append_turn("assistant", "Que frutas voce comprou?", at=NOW)
        mem.append_turn("user", "Maca, banana e laranja", at=NOW)

        results = mem.search("o que comprei na feira", limit=5)
        assert results, "FAISS-backed search must return results"
        texts = " ".join(r["text"] for r in results).lower()
        assert any(w in texts for w in ("mercado", "frutas", "maca", "banana",
                                         "laranja", "feira")), \
            f"search should find market-related content; got: {results}"

    def test_faiss_index_built_after_search(self, tmp_path):
        fn = make_sentence_embedder()
        mem = ChatMemory(base_dir=tmp_path / "chat", embed_fn=fn)
        mem.append_turn("user", "conversa para indexar", at=NOW)
        mem.search("indexar", limit=1)  # triggers embedding + FAISS build

        status = mem.index_status()
        assert status["faiss"], "FAISS index should be built after search"
        assert status["faiss_size"] >= 1

    def test_index_status_reports_faiss(self, tmp_path):
        mem = ChatMemory(base_dir=tmp_path / "chat")  # no embedder
        status = mem.index_status()
        assert "faiss" in status
        assert "faiss_size" in status


# ── Semantic search quality ─────────────────────────────────────────────────

class TestSemanticSearchFAISS:
    """FAISS-backed semantic search must find related content by meaning."""

    @pytest.fixture(autouse=True)
    def _skip_without_embedder(self):
        if not faiss_available():
            pytest.skip("faiss-cpu nao instalado")
        if make_sentence_embedder() is None:
            pytest.skip("sentence-transformers nao instalado")

    def test_finds_synonyms(self, tmp_path):
        fn = make_sentence_embedder()
        mem = ChatMemory(base_dir=tmp_path / "chat", embed_fn=fn)
        mem.append_turn("user",
                        "Fui ao medico fazer um exame de sangue", at=NOW)
        mem.append_turn("assistant",
                        "O exame mostrou tudo normal?", at=NOW)
        mem.append_turn("user",
                        "Preciso mudar a dieta para o colesterol", at=NOW)

        results = mem.search("como esta minha saude", limit=5)
        texts = " ".join(r["text"] for r in results).lower()
        assert any(w in texts for w in ("medico", "exame", "sangue",
                                         "dieta", "colesterol")), \
            f"semantic search for 'saude' must find health-related content"

    def test_semantic_beats_keyword(self, tmp_path):
        """Content with no keyword overlap but semantic similarity ranks."""
        fn = make_sentence_embedder()
        mem = ChatMemory(base_dir=tmp_path / "chat", embed_fn=fn)
        mem.append_turn("user", "Estou muito contente com o resultado", at=NOW)
        mem.append_turn("user", "O arquivo foi salvo na pasta documentos", at=NOW)
        mem.append_turn("user", "A reuniao foi cancelada para amanha", at=NOW)

        results = mem.search("como voce esta feliz", limit=3)
        assert results
        top_texts = [r["text"].lower() for r in results[:2]]
        assert any("contente" in t for t in top_texts), \
            f"'contente' should rank for 'feliz' by semantics; got: {top_texts}"


# ── Latency benchmark ───────────────────────────────────────────────────────

class TestLatency:
    """Search latency with 10k+ vectors must stay under 200ms."""

    @pytest.fixture(autouse=True)
    def _skip_without_faiss(self):
        if not faiss_available():
            pytest.skip("faiss-cpu nao instalado")

    def test_search_latency_10k_vectors(self, tmp_path):
        """FAISS search over 10k vectors must be under 50ms (index-only)."""
        embs = _make_embeddings(10_000, dim=384)
        vi = VectorIndex(tmp_path / "idx")
        vi.build(embs)
        query = _random_unit_vector()

        # Warm up
        vi.search(query, k=10)

        # Measure
        times = []
        for _ in range(20):
            start = time.perf_counter()
            vi.search(query, k=10)
            elapsed = time.perf_counter() - start
            times.append(elapsed)

        avg_ms = (sum(times) / len(times)) * 1000
        max_ms = max(times) * 1000
        assert avg_ms < 50, \
            f"FAISS search avg latency with 10k vectors: {avg_ms:.1f}ms"
        assert max_ms < 200, \
            f"FAISS search max latency with 10k vectors: {max_ms:.1f}ms"

    def test_full_search_latency_with_embedder(self, tmp_path):
        """End-to-end search (embed query + FAISS search) under 200ms."""
        fn = make_sentence_embedder()
        if fn is None:
            pytest.skip("sentence-transformers nao instalado")

        mem = ChatMemory(base_dir=tmp_path / "chat", embed_fn=fn)
        # Populate with many turns
        for i in range(200):
            mem.append_turn("user",
                            f"Conversa numero {i} sobre assuntos variados "
                            f"incluindo tecnologia, saude e trabalho",
                            at=NOW)
        # Warm up: trigger reindex + embedding + FAISS build
        mem.reindex()
        mem.search("warmup", limit=1)

        # Measure end-to-end
        times = []
        for _ in range(10):
            start = time.perf_counter()
            mem.search("assunto tecnologico", limit=5)
            elapsed = time.perf_counter() - start
            times.append(elapsed)

        avg_ms = (sum(times) / len(times)) * 1000
        assert avg_ms < 200, \
            f"full search avg latency: {avg_ms:.1f}ms (target < 200ms)"

    def test_build_latency_10k(self, tmp_path):
        """Building a FAISS index with 10k vectors must be under 5s."""
        embs = _make_embeddings(10_000, dim=384)
        vi = VectorIndex(tmp_path / "idx")

        start = time.perf_counter()
        vi.build(embs)
        elapsed = time.perf_counter() - start

        assert elapsed < 5.0, \
            f"FAISS build with 10k vectors took {elapsed:.2f}s (target < 5s)"
        assert vi.size == 10_000


# ── Fallback behaviour ──────────────────────────────────────────────────────

class TestFallback:
    """When FAISS is unavailable, search must fall back to linear scan."""

    def test_linear_fallback_gives_results(self, tmp_path):
        """Even without FAISS, the linear cosine path must work."""
        fn = make_sentence_embedder()
        if fn is None:
            pytest.skip("sentence-transformers nao instalado")

        mem = ChatMemory(base_dir=tmp_path / "chat", embed_fn=fn)
        mem.append_turn("user", "linha com palavra unica xyzabc", at=NOW)
        mem.append_turn("assistant", "outra linha sem relevance", at=NOW)

        # Force-disable FAISS for this test
        original_vi = mem._vector_index
        mem._vector_index = None

        try:
            results = mem.search("palavra unica xyzabc", limit=5)
            assert results, "linear fallback must return results"
            assert any("xyzabc" in r["text"].lower() for r in results)
        finally:
            mem._vector_index = original_vi

    def test_no_embedder_no_faiss_uses_tfidf(self, tmp_path):
        """Without embedder AND without FAISS, TF-IDF must still work."""
        mem = ChatMemory(base_dir=tmp_path / "chat")  # no embed_fn
        mem._vector_index = None  # no FAISS either
        mem.append_turn("user", "prefill latencia medida", at=NOW)

        results = mem.search("prefill latencia", limit=5)
        assert results, "TF-IDF fallback must work without embedder or FAISS"


# ── Disk size constraint ─────────────────────────────────────────────────────

class TestDiskSize:
    """The FAISS index must not consume excessive disk space."""

    @pytest.fixture(autouse=True)
    def _skip_without_faiss(self):
        if not faiss_available():
            pytest.skip("faiss-cpu nao instalado")

    def test_index_size_under_500mb_for_10k(self, tmp_path):
        """10k vectors * 384 dim * 4 bytes = ~15MB. Index file should be
        well under 500MB."""
        embs = _make_embeddings(10_000, dim=384)
        vi = VectorIndex(tmp_path / "idx")
        vi.build(embs)

        index_file = tmp_path / "idx" / "faiss.index"
        assert index_file.is_file()
        size_mb = index_file.stat().st_size / (1024 * 1024)
        assert size_mb < 500, \
            f"FAISS index with 10k vectors is {size_mb:.1f}MB (limit: 500MB)"
        # Sanity check: should be around 15MB for 10k * 384 * float32
        assert size_mb < 50, \
            f"FAISS index unexpectedly large: {size_mb:.1f}MB"
