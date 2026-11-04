"""Action wrapper for the memory spider web and the new tool tiers."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import memory_index as action  # noqa: E402
from core import memory_index as index  # noqa: E402
from core.tool_bridge import ToolBridge  # noqa: E402


@pytest.fixture
def temp_index(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "BASE_DIR", tmp_path / "index")
    monkeypatch.setattr(index, "_mirror", lambda _graph: None)
    index.reset()
    yield tmp_path / "index"
    index.reset()


def _payload(result: str) -> dict:
    return json.loads(result)


def test_tag_then_query_by_facets(temp_index):
    tagged = _payload(action.memory_index_action({
        "action": "tag", "subject": "corte de fala",
        "project": "jarvis", "persona": "fulana",
        "text": "investigando o áudio",
    }))
    assert tagged["status"] == "ok"
    assert tagged["active"]["subject"] == "corte-de-fala"
    found = _payload(action.memory_index_action({
        "action": "query", "project": "jarvis", "subject": "corte de fala"}))
    assert found["status"] == "ok"
    assert found["count"] >= 1
    assert found["events"][0]["persona"] == "fulana"


def test_render_and_open_use_obsidian_mirror(temp_index, monkeypatch):
    from core import obsidian
    mirrored = temp_index / "Teia.md"
    monkeypatch.setattr(obsidian, "mirror_index", lambda _text, _graph: mirrored)
    opened = []
    monkeypatch.setattr(obsidian, "open_path", lambda path: opened.append(path) or "abri")
    rendered = _payload(action.memory_index_action({"action": "render"}))
    assert rendered["status"] == "ok"
    assert rendered["path"] == str(mirrored)
    opened_payload = _payload(action.memory_index_action({"action": "open"}))
    assert opened_payload["status"] == "ok"
    assert opened == [mirrored]


def test_new_action_tiers():
    obj = object.__new__(ToolBridge)
    assert obj.risk_tier("open_in", {"path": "x"}) == "low"
    assert obj.risk_tier("media_control", {"action": "pause"}) == "low"
    assert obj.risk_tier("memory_index", {"action": "tag"}) == "low"
    assert obj.risk_tier("terminal_run", {"command": "ls"}) == "low"
    assert obj.risk_tier("terminal_run", {"command": "rm -rf ~"}) == "high"
