"""Memory spider web: graph, queries, pages and the Obsidian mirror hook."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import memory_index, obsidian  # noqa: E402


@pytest.fixture
def index(tmp_path, monkeypatch):
    """Isolated index dir + mocked Obsidian mirror (never touches ~/Jarvis)."""
    base = tmp_path / "memory" / "chat" / "index"
    monkeypatch.setattr(memory_index, "BASE_DIR", base)
    calls: list[tuple[str, dict]] = []

    def fake_mirror(markdown, graph):
        calls.append((markdown, graph))
        return tmp_path / "Teia.md"

    monkeypatch.setattr(obsidian, "mirror_index", fake_mirror)
    memory_index.reset()
    yield memory_index, base, calls
    memory_index.reset()


def graph_of(module):
    return json.loads((Path(module.BASE_DIR) / "graph.json").read_text("utf-8"))


# ── ingestion ───────────────────────────────────────────────────────────────

def test_record_event_builds_nodes_edges_and_pages(index):
    module, base, calls = index
    event = module.record_event(
        "nota", "Revisar a saúde do Jarvis", at="2026-10-31T22:15:00",
        persona="Ana", project="Jarvis", subject="Saúde",
    )
    assert event["id"].startswith("evt-")
    assert event["subject"] == "saude"
    assert event["persona"] == "ana"
    assert event["project"] == "jarvis"
    assert event["at"].startswith("2026-10-31T22:15")

    graph = graph_of(module)
    assert graph["version"] == 1
    assert event["id"] in graph["events"]
    for node in (
        f"event:{event['id']}", "subject:saude", "persona:ana",
        "project:jarvis", "time:2026-10-31/22",
    ):
        assert node in graph["nodes"]
    edges = {(e["from"], e["to"], e["kind"]) for e in graph["edges"]}
    assert (f"event:{event['id']}", "subject:saude", "about") in edges
    assert (f"event:{event['id']}", "persona:ana", "by") in edges
    assert (f"event:{event['id']}", "project:jarvis", "in") in edges
    assert (f"event:{event['id']}", "time:2026-10-31/22", "during") in edges
    assert ("subject:saude", "persona:ana", "cooccur") in edges
    assert ("persona:ana", "project:jarvis", "cooccur") in edges

    assert (base / "subjects" / "saude.md").is_file()
    assert (base / "personas" / "ana.md").is_file()
    assert (base / "projects" / "jarvis.md").is_file()
    assert (base / "hours" / "2026-10-31" / "22.md").is_file()
    assert (base / "spider.md").is_file()

    subject_page = (base / "subjects" / "saude.md").read_text("utf-8")
    assert "[[persona:ana]]" in subject_page
    assert "Revisar a saúde" in subject_page
    persona_page = (base / "personas" / "ana.md").read_text("utf-8")
    assert "[[subject:saude]]" in persona_page

    spider = (base / "spider.md").read_text("utf-8")
    for heading in ("## Por assunto", "## Por persona",
                    "## Por projeto", "## Por hora"):
        assert heading in spider
    assert "[[subject:saude]]" in spider
    assert "[[time:2026-10-31/22]]" in spider

    assert calls and "## Por assunto" in calls[-1][0]
    assert calls[-1][1]["events"]


def test_missing_subject_defaults_to_geral(index):
    module, base, _ = index
    event = module.record_event("nota", "sem assunto", at="2026-10-31T10:00")
    assert event["subject"] == "geral"
    assert (base / "subjects" / "geral.md").is_file()


def test_text_is_truncated_and_refs_bounded(index, monkeypatch):
    module, _, _ = index
    monkeypatch.setattr(module, "MAX_REFS", 2)
    event = module.record_event(
        "nota", "x" * 5000, at="2026-10-31T10:00", refs=["a", "b", "c"])
    assert len(event["text"]) == module.MAX_TEXT
    assert event["refs"] == ["a", "b"]


# ── query ───────────────────────────────────────────────────────────────────

def test_query_filters_by_facets_and_time_newest_first(index):
    module, _, _ = index
    module.record_event("nota", "cedo", at="2026-10-31T08:00",
                        persona="Ana", project="Jarvis", subject="Saúde")
    module.record_event("nota", "meio", at="2026-10-31T12:00",
                        persona="Bruno", project="Jarvis", subject="Código")
    module.record_event("nota", "tarde", at="2026-10-31T22:00",
                        persona="Ana", project="Casa", subject="Saúde")

    assert [e["text"] for e in module.query(persona="Ana")] == ["tarde", "cedo"]
    assert [e["text"] for e in module.query(project="Jarvis")] == ["meio", "cedo"]
    assert [e["text"] for e in module.query(subject="saude")] == ["tarde", "cedo"]
    assert [e["text"] for e in module.query(persona="Ana", project="Casa")] == ["tarde"]
    assert [e["text"] for e in module.query(
        since="2026-10-31T10:00", until="2026-10-31T23:00")] == ["tarde", "meio"]
    assert [e["text"] for e in module.query(limit=1)] == ["tarde"]
    assert module.query(persona="Ninguém") == []
    assert module.query(limit=0) == []


def test_query_accepts_datetime_bounds(index):
    module, _, _ = index
    module.record_event("nota", "a", at="2026-10-31T08:00")
    module.record_event("nota", "b", at="2026-10-31T12:00")
    found = module.query(since=datetime(2026, 10, 31, 10, 0))
    assert [e["text"] for e in found] == ["b"]


# ── resilience and bounds ───────────────────────────────────────────────────

def test_malformed_graph_is_repaired_to_empty(index):
    module, base, _ = index
    base.mkdir(parents=True, exist_ok=True)
    (base / "graph.json").write_text("{not json", "utf-8")

    event = module.record_event("nota", "depois do estrago",
                                at="2026-10-31T09:00", subject="Teste")
    graph = graph_of(module)
    assert graph["version"] == 1
    assert event["id"] in graph["events"]
    assert module.query(subject="teste")[0]["text"] == "depois do estrago"


def test_malformed_graph_shapes_are_sanitised(index):
    module, base, _ = index
    base.mkdir(parents=True, exist_ok=True)
    (base / "graph.json").write_text(
        json.dumps({"version": 1, "events": [], "nodes": "x",
                    "edges": [None, {"from": 1}, {"from": "a", "to": "a"}]}),
        "utf-8",
    )
    assert module.load_graph() == {"version": 1, "events": {},
                                   "nodes": {}, "edges": []}


def test_events_are_bounded(index, monkeypatch):
    module, _, _ = index
    monkeypatch.setattr(module, "MAX_EVENTS", 3)
    ids = [module.record_event("nota", f"e{i}", at=f"2026-10-31T1{i}:00")["id"]
           for i in range(5)]
    graph = graph_of(module)
    assert len(graph["events"]) == 3
    assert ids[0] not in graph["events"]
    assert f"event:{ids[0]}" not in graph["nodes"]
    assert all(e["from"] != f"event:{ids[0]}" for e in graph["edges"])


def test_page_links_are_bounded(index, monkeypatch):
    module, base, _ = index
    monkeypatch.setattr(module, "MAX_LINKS", 4)
    for i in range(10):
        module.record_event("nota", f"e{i}", at=f"2026-10-31T1{i}:00",
                            persona=f"Pessoa {i}", subject="Grande")
    page = (base / "subjects" / "grande.md").read_text("utf-8")
    connections = [line for line in page.splitlines() if line.startswith("- [[")]
    assert len(connections) <= 4


def test_mirror_failure_never_breaks_record(index, monkeypatch):
    module, _, _ = index

    def explode(markdown, graph):
        raise RuntimeError("vault offline")

    monkeypatch.setattr(obsidian, "mirror_index", explode)
    event = module.record_event("nota", "continua", at="2026-10-31T09:00")
    assert event["text"] == "continua"
    assert module.load_graph()["events"]


# ── cross-links ─────────────────────────────────────────────────────────────

def test_link_adds_deduped_cross_edge_and_page(index):
    module, base, calls = index
    module.link("subject:saude", "persona:ana")
    module.link("subject:saude", "persona:ana")
    module.link("persona:ana", "project:jarvis", kind="works-on")

    graph = module.load_graph()
    assert len(graph["edges"]) == 2
    assert any(e["kind"] == "works-on" for e in graph["edges"])
    assert "[[persona:ana]]" in (base / "subjects" / "saude.md").read_text("utf-8")
    assert "[[subject:saude]]" in (base / "personas" / "ana.md").read_text("utf-8")
    assert calls


def test_link_rejects_invalid_node(index):
    module, _, _ = index
    with pytest.raises(ValueError):
        module.link("saude", "persona:ana")
    with pytest.raises(ValueError):
        module.link("time:2026-10-31/22", "persona:ana")


def test_reset_clears_graph_and_pages(index):
    module, base, _ = index
    module.record_event("nota", "some", at="2026-10-31T09:00", subject="X")
    assert (base / "spider.md").is_file()
    module.reset()
    assert not base.exists()
    assert module.load_graph()["events"] == {}
