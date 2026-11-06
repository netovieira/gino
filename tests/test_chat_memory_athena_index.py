"""Testes da receita athena transplantada para o índice do chat (05/11).

Cobrem os cinco itens do contrato do dono, adaptados ao store markdown:

1. manifesto sha256 por arquivo + assinatura de diretório, persistido;
2. poda pelo touched-set: só o que mudou é reindexado, o que sumiu sai;
3. TF-IDF com IDF suavizado e ranking determinístico (desempate por id);
4. embedding como caminho preferido com fallback obrigatório para TF-IDF
   e guarda de dimensão contra índice corrompido;
5. gate de custo: ``max_files`` por varredura e ``dry_run`` que só relata.

A recência, que o athena não tem, aqui pesa — e há teste para isso.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.chat_memory import ChatMemory  # noqa: E402

NOW = datetime(2026, 11, 5, 14, 30)


@pytest.fixture()
def mem(tmp_path):
    return ChatMemory(base_dir=tmp_path / "chat")


@pytest.fixture()
def store(tmp_path):
    """Store with three files in three levels: two hours + one topic."""
    m = ChatMemory(base_dir=tmp_path / "chat")
    m.append_turn("user", "latência da boca local no turno de voz", at=NOW)
    m.append_turn("assistant", "prefill do cérebro podado em unidades", at=NOW)
    m.write_topic("Latencia do turno de voz",
                  "# Latência do turno de voz\n\n"
                  "- medições do turno: boca, prefill, unidades\n",
                  set_current=False)
    return m


# ── 1) manifesto sha256 persistido ──────────────────────────────────────────

def test_manifest_persists_sha256_per_file(store):
    stats = store.reindex()
    assert stats["added"] >= 3 and not stats["capped"]
    manifest = store.manifest_path
    assert manifest.is_file()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["files"] and data["dirs"]
    for entry in data["files"].values():
        assert len(entry["hash"]) == 64  # hex sha256
    # hora e tópico entram cada um com sua chave relativa
    keys = set(data["files"])
    assert any(k.startswith("hours/") for k in keys)
    assert "topics/latencia-do-turno-de-voz.md" in keys


def test_second_sweep_unchanged_and_no_rehash(store):
    store.reindex()
    stats = store.reindex()
    assert stats["changed"] == 0 and stats["added"] == 0
    assert stats["unchanged"] >= 3


# ── 2) poda pelo touched-set ────────────────────────────────────────────────

def test_touched_set_reindexes_only_changed(store):
    store.reindex()
    data = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    topic_before = data["files"]["topics/latencia-do-turno-de-voz.md"]["hash"]
    other_keys = [k for k, v in data["files"].items()
                  if k.startswith("hours/")]
    hours_before = {k: data["files"][k]["hash"] for k in other_keys}

    topic_file = store.topics_dir / "latencia-do-turno-de-voz.md"
    topic_file.write_text(
        "# Latência do turno de voz\n\n- linha nova do índice\n",
        encoding="utf-8")
    stats = store.reindex()
    assert stats["changed"] == 1, "só o arquivo tocado deveria reindexar"

    data = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    assert data["files"]["topics/latencia-do-turno-de-voz.md"]["hash"] \
        != topic_before, "hash do modificado não atualizou"
    for k, old_hash in hours_before.items():
        assert data["files"][k]["hash"] == old_hash, \
            "arquivo intacto foi reescrito no manifesto"
    # o conteúdo novo está pesquisável
    assert any("linha nova do índice" in r["text"]
               for r in store.search("linha nova indice"))


def test_deleted_file_is_pruned(store):
    topic_file = store.topics_dir / "latencia-do-turno-de-voz.md"
    store.reindex()
    assert "topics/latencia-do-turno-de-voz.md" in store._units_cache
    topic_file.unlink()
    stats = store.reindex()
    assert stats["removed"] >= 1
    data = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    assert "topics/latencia-do-turno-de-voz.md" not in data["files"]


# ── 3) determinismo do ranking ──────────────────────────────────────────────

def test_ranking_is_deterministic_across_two_runs(store):
    first = store.search("latência boca turno", limit=10)
    second = store.search("latência boca turno", limit=10)
    assert first, "busca não pode voltar vazia no store do teste"
    assert [(r["source"], r["text"]) for r in first] == \
        [(r["source"], r["text"]) for r in second]


def test_ranking_deterministic_in_fresh_process(store):
    store.search("prefill unidades", limit=5)  # grava manifesto
    again = ChatMemory(base_dir=store.base_dir)  # outro "processo"
    a = [(r["source"], r["text"]) for r in store.search("prefill unidades", limit=5)]
    b = [(r["source"], r["text"]) for r in again.search("prefill unidades", limit=5)]
    assert a and a == b


def test_phrase_beats_scattered_tokens(store):
    found = store.search("boca local", limit=5)
    assert found and "boca local" in found[0]["text"]


def test_rare_token_outranks_common(store):
    for i in range(6):
        store.append_turn("user", f"a boca falou boca {i} na boca", at=NOW)
    store.append_turn("assistant", "xyzzy mediu o prefill", at=NOW)
    found = store.search("boca xyzzy", limit=10)
    assert found and "xyzzy" in found[0]["text"], \
        "IDF suavizado deve pôr o termo raro no topo"


# ── recência pesa no ranking (não copiar a ausência do athena) ──────────────

def test_recency_breaks_tie_between_equal_scores(tmp_path):
    m = ChatMemory(base_dir=tmp_path / "chat")
    m.append_turn("user", "medição do marcador quorum-x",
                  at=datetime(2026, 10, 1, 10, 0))
    m.append_turn("user", "medição do marcador quorum-x",
                  at=datetime(2026, 11, 5, 10, 0))
    # linhas idênticas são deduplicadas (regra da base); usa textos quase
    # iguais com o mesmo peso léxico, datas diferentes
    m.append_turn("user", "quorum-y medido ontem", at=datetime(2026, 10, 1, 10, 0))
    m.append_turn("user", "quorum-y medido agora", at=datetime(2026, 11, 5, 10, 0))
    found = m.search("quorum-y medido", limit=5)
    assert found and "agora" in found[0]["text"], \
        "a linha mais recente deve ganhar o empate"


# ── 4) embedding preferido, fallback TF-IDF, guarda de dimensão ─────────────

def words_embed(text):
    """Fake embedder: bag of folded words in a wide space (few collisions)."""
    import unicodedata
    folded = unicodedata.normalize("NFKD", str(text).lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    vec = [0.0] * 256
    for word in folded.split():
        vec[sum(map(ord, word)) % 256] += 1.0
    return vec


def test_vector_path_preferred_and_persisted(tmp_path):
    plain = ChatMemory(base_dir=tmp_path / "a")
    plain.append_turn("user", "alpha beta", at=NOW)
    plain.append_turn("user", "gamma delta", at=NOW)

    vec = ChatMemory(base_dir=tmp_path / "a", embed_fn=words_embed)
    found = vec.search("alpha beta", limit=1)
    assert found and "alpha beta" in found[0]["text"]
    assert vec.embeddings_path.is_file(), \
        "embeddings preguiçosos devem ser persistidos"
    status = vec.index_status()
    assert status["embeddings"] >= 1


def test_tfidf_fallback_when_embedder_unavailable(tmp_path):
    base = tmp_path / "chat"
    ref = ChatMemory(base_dir=base)
    ref.append_turn("user", "o prefill podado mediu latência", at=NOW)
    ref.append_turn("assistant", "a boca local respondeu primeiro", at=NOW)
    expected = [(r["source"], r["text"]) for r in
                ref.search("prefill latência", limit=5)]

    down = ChatMemory(base_dir=base, embed_fn=lambda text: None)
    found = [(r["source"], r["text"]) for r in
             down.search("prefill latência", limit=5)]
    assert found == expected, "embedder morto deve cair no MESMO TF-IDF"

    def explode(text):
        raise RuntimeError("ollama fora do ar")

    boom = ChatMemory(base_dir=base, embed_fn=explode)
    assert [(r["source"], r["text"]) for r in
            boom.search("prefill latência", limit=5)] == expected


def test_dimension_guard_falls_back(tmp_path):
    base = tmp_path / "chat"
    m = ChatMemory(base_dir=base)
    m.append_turn("user", "linha guardada do índice", at=NOW)
    expected = [(r["source"], r["text"]) for r in
                m.search("linha guardada", limit=5)]

    def wrong_dim(text):
        # consulta num modelo, unidades noutro: o guarda de dimensão tem de
        # perceber e devolver o comando ao TF-IDF
        if text == "linha guardada":
            return [0.1] * 3
        return [0.2] * 9

    g = ChatMemory(base_dir=base, embed_fn=wrong_dim)
    found = [(r["source"], r["text"]) for r in g.search("linha guardada", limit=5)]
    assert found == expected, "dimensão incompatível deve acionar o fallback"

    # e o arquivo de embeddings corrompido não quebra a busca seguinte
    g.embeddings_path.parent.mkdir(parents=True, exist_ok=True)
    g.embeddings_path.write_text("{isso não é json", encoding="utf-8")
    g._embeddings = None
    found = [(r["source"], r["text"]) for r in g.search("linha guardada", limit=5)]
    assert found == expected


# ── 5) gate de custo: max_files + dry-run ───────────────────────────────────

def test_max_files_caps_the_sweep(tmp_path):
    m = ChatMemory(base_dir=tmp_path / "chat")
    for i in range(6):
        m.write_topic(f"topico {i}", f"# Tópicos {i}\n\n- linha única {i}\n",
                      set_current=False)
    stats = m.reindex(max_files=2)
    assert stats["capped"] is True
    assert stats["skipped"] >= 1
    assert stats["added"] <= 2 + 2  # hot/state também competem; nunca o todo


def test_dry_run_reports_without_writing(store):
    store.reindex()  # manifesto de referência existe
    before = store.manifest_path.read_bytes()
    topic_file = store.topics_dir / "latencia-do-turno-de-voz.md"
    topic_file.write_text(
        "# Latência do turno de voz\n\n- mudança só relatada\n",
        encoding="utf-8")
    stats = store.reindex(max_files=None, dry_run=True)
    assert stats["changed"] == 1, "dry-run precisa enxergar o que mudaria"
    assert store.manifest_path.read_bytes() == before, \
        "dry-run não pode escrever o manifesto"
    # nem o cache de unidades: o conteúdo velho continua sendo o indexado
    cached_lines = [u[2] for u in
                    store._units_cache["topics/latencia-do-turno-de-voz.md"]]
    assert "mudança só relatada" not in cached_lines
    assert any("medições do turno" in t for t in cached_lines)
