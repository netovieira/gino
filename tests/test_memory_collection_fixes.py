"""Regression tests for the memory collection/indexing audit of 05/11.

The owner's evidence: ``memory_search("latência boca local prefill")`` and
``memory_search("prompt detalhado fluxo ... tecnologias")`` returned nothing
while the store held the lines. Root causes and their tests:

(a) search used an exact AND of every token in one line  -> partial recall
    with inverse-frequency ranking must surface the right line;
(b) ``note()`` only wrote to state.md, where ``MAX_NOTES`` rotation silently
    destroyed old decisions                                  -> the note must
    also land in the hour file and be searchable immediately;
(c) the in-process RLock does not protect two processes (live app +
    background agents) doing read-modify-write on the same store -> flock
    serialisation, proven with a real two-process test;
(d) the levels of the owner's policy (SKILL.md) were not all searchable:
    hot.md, sleep/ and the spider pages were invisible to ``search``  ->
    coverage test.
"""
import multiprocessing
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.chat_memory import ChatMemory  # noqa: E402


NOW = datetime(2026, 11, 5, 14, 30)


@pytest.fixture()
def mem(tmp_path):
    return ChatMemory(base_dir=tmp_path / "chat")


# ── (a) recall must not require every token in one line ─────────────────────

def test_partial_recall_finds_split_terms(mem):
    mem.append_turn("user", "quanto de latência tem a boca?", at=NOW)
    mem.append_turn("assistant", "a boca local ganhou prefill novo", at=NOW)
    found = mem.search("latência boca local prefill", limit=10)
    texts = " ".join(r["text"] for r in found)
    assert "prefill" in texts, "busca AND-exata continua cortando o trecho"


def test_rare_token_outranks_generic(mem):
    for i in range(5):
        mem.append_turn("user", f"a boca falou {i} de novo na boca", at=NOW)
    mem.append_turn("assistant", "prefill do cérebro podado", at=NOW)
    found = mem.search("boca prefill", limit=10)
    assert found
    assert "prefill" in found[0]["text"]


def test_phrase_match_wins(mem):
    mem.append_turn("user", "prefill isolado", at=NOW)
    mem.append_turn("user", "a boca local pré-fil não, o prefill da boca local",
                    at=NOW)
    found = mem.search("boca local prefill", limit=10)
    assert "boca local" in found[0]["text"]


def test_single_token_behaviour_unchanged(mem):
    slug = mem.write_topic("Música Favorita", "# Música\n\n- bossa nova\n")
    assert slug
    found = mem.search("bossa")
    assert any("musica-favorita" in r["source"] for r in found)


# ── (b) written notes are immediately searchable and never lost ─────────────

def test_note_is_searchable_immediately(mem):
    mem.note("decisão: prefill do cérebro podado ficou para o cérebro remoto",
             kind="decision")
    found = mem.search("prefill podado", limit=5)
    assert found and "decision" in " ".join(r["text"] + r["source"]
                                            for r in found)


def test_note_survives_max_notes_rotation(mem):
    mem.note("primeira decisão gravada", kind="decision")
    for i in range(40):
        mem.note(f"nota de rotação {i}", kind="note")
    found = mem.search("primeira decisão gravada")
    assert found, "nota antiga foi destruída pela rotação de MAX_NOTES"


# ── (c) two processes writing the same store must not lose turns ────────────

def _hammer(args):
    base, worker, n = args
    m = ChatMemory(base_dir=base)
    for i in range(n):
        m.append_turn("user", f"worker {worker} turno {i}")
        time.sleep(0.001)
    return worker


def _notes(args):
    base, worker, n = args
    m = ChatMemory(base_dir=base)
    for i in range(n):
        m.note(f"nota-{worker}-{i} gravada", kind="decision")


def _pool():
    return multiprocessing.get_context("spawn")


def test_concurrent_processes_do_not_lose_turns(tmp_path):
    base = tmp_path / "chat"
    ChatMemory(base_dir=base)  # layout
    n = 25
    with _pool().Pool(2) as pool:
        pool.map(_hammer, [(str(base), w, n) for w in ("a", "b")])
    hours = list((base / "hours").glob("*/*.md"))
    text = "".join(p.read_text(encoding="utf-8") for p in hours)
    a = sum(1 for i in range(n) if f"worker a turno {i}" in text)
    b = sum(1 for i in range(n) if f"worker b turno {i}" in text)
    assert a == n and b == n, f"corrida comeu turnos: a={a} b={b} esperados={n}"


def test_concurrent_notes_are_not_lost(tmp_path):
    base = tmp_path / "chat"
    ChatMemory(base_dir=base)
    ctx = _pool()
    procs = [ctx.Process(target=_notes, args=((str(base), w, 10),))
             for w in ("x", "y")]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    m = ChatMemory(base_dir=base)
    for w in ("x", "y"):
        for i in range(10):
            assert m.search(f"nota-{w}-{i}"), f"nota {w}-{i} perdida na corrida"


# ── (d) every level of the owner's policy must be searchable ────────────────

def test_sleep_and_hot_levels_are_searchable(mem):
    sleep_dir = mem.base_dir / "sleep"
    sleep_dir.mkdir(parents=True, exist_ok=True)
    (sleep_dir / "2026-11-04.md").write_text(
        "# Ontem\n\n- a noite registrou o cadastro de Sonia\n",
        encoding="utf-8")
    found = mem.search("cadastro sonia", limit=5)
    assert any("sleep" in r["source"] for r in found), \
        "nível sono/compactado não é coberto pela busca"

    mem.append_turn("user", "palavra quentíssima única aqui", at=datetime.now())
    found = mem.search("quentíssima única", limit=5)
    assert found, "turno do hot/hours não é achado"


def test_spider_pages_are_searchable(mem):
    spider_hours = mem.base_dir / "index" / "hours" / "2026-11-05"
    spider_hours.mkdir(parents=True)
    (spider_hours / "18.md").write_text(
        "# Hora\n\n- 2026-11-05 18:12 — evento raro mediu primeira_unidade "
        "do cérebro\n", encoding="utf-8")
    found = mem.search("primeira_unidade", limit=5)
    assert any("index/hours" in r["source"] for r in found), \
        "eventos da teia ficam invisíveis para a busca"
