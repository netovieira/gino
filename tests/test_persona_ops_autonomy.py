"""Autonomia declarativa: tabela, precedência do dono e bloqueio de ferramenta."""
from __future__ import annotations

import json

import pytest

from core.persona_ops.autonomy import (AutonomyError, AutonomyTable,
                                       load_autonomy, tool_key)


def _table(tmp_path, payload=None):
    path = tmp_path / "autonomy.json"
    path.write_text(json.dumps(payload or {
        "version": 1,
        "defaults": {"autonomy": {
            "tools": ["Read", "Glob", "Grep", "memory_read"],
            "read_roots": [], "requires_confirm": [],
            "max_tokens": 1000, "session_ttl_s": 60,
        }},
        "rules": [{
            "id": "persona-fulana", "priority": 10,
            "match": {"route": "fulana"},
            "autonomy": {
                "tools": ["Read", "Grep", "memory_read", "memory_note"],
                "read_roots": ["/projects/therotv"],
                "max_tokens": 2000,
            },
        }],
    }), encoding="utf-8")
    return AutonomyTable.from_file(path)


def test_repository_table_loads_real_personas():
    table = load_autonomy()
    assert "aline" in table.personas()
    assert "arnaldo" in table.personas()
    grant = table.grant("aline")
    assert "read" in grant.tools
    assert "memory_note" in grant.tools
    assert grant.read_roots == ("/projects/therotv",)
    assert grant.max_tokens == 12000
    assert grant.session_ttl_s == 900.0


def test_owner_override_denies_write_even_when_a_rule_allows_it(tmp_path):
    table = _table(tmp_path, {
        "version": 1,
        "defaults": {"autonomy": {"tools": ["Read"]}},
        "overrides": [{
            "id": "dono-veta-bash", "priority": 999,
            "match": {"route": "fulana"},
            "autonomy": {"deny_tools": ["Bash"]},
        }],
        "rules": [{
            "id": "fulana-libera-tudo", "priority": 10,
            "match": {"route": "fulana"},
            "autonomy": {"tools": ["Read", "Bash"]},
        }],
    })
    grant = table.grant("fulana")
    assert "bash" in grant.tools  # a regra liberou...
    assert grant.check_tool("Bash").allowed is False  # ...o dono negou
    assert grant.check_tool("Bash").reason == "negado_pelo_dono"
    # nem a confirmação explícita fura a negação do dono
    assert grant.check_tool("Bash", confirm=True).allowed is False


def test_owner_override_wins_on_budget_and_read_roots(tmp_path):
    table = _table(tmp_path, {
        "version": 1,
        "defaults": {"autonomy": {"tools": ["Read"], "max_tokens": 100}},
        "overrides": [{
            "id": "dono-fixa-orcamento", "priority": 1,
            "match": {"route": "fulana"},
            "autonomy": {"max_tokens": 777, "read_roots": ["/tmp/permitido"]},
        }],
        "rules": [{
            "id": "fulana-pede-mais", "priority": 50,
            "match": {"route": "fulana"},
            "autonomy": {"max_tokens": 9999, "read_roots": ["/tmp/outro"]},
        }],
    })
    grant = table.grant("fulana")
    assert grant.max_tokens == 777
    assert grant.read_roots == ("/tmp/permitido",)


def test_tool_outside_the_list_is_blocked(tmp_path):
    table = _table(tmp_path)
    grant = table.grant("fulana")
    decision = grant.check_tool("Bash")
    assert decision.allowed is False
    assert decision.requires_confirm is False
    assert decision.reason == "fora_da_lista_de_autonomia"
    assert grant.check_tool("Write").allowed is False
    assert grant.check_tool("Read", path="/projects/therotv/app").allowed


def test_read_fence_blocks_outside_and_allows_inside(tmp_path):
    table = _table(tmp_path)
    grant = table.grant("fulana")
    inside = grant.check_tool("Grep", path="/projects/therotv/src/ui")
    outside = grant.check_tool("Read", path="/projects/outro/segredo")
    traversal = grant.check_tool("Read", path="/projects/therotv/../outro")
    assert inside.allowed is True
    assert outside.allowed is False
    assert outside.reason == "leitura_fora_dos_diretorios"
    assert traversal.allowed is False


def test_requires_confirm_blocks_until_the_owner_confirms(tmp_path):
    table = _table(tmp_path, {
        "version": 1,
        "defaults": {"autonomy": {
            "tools": ["Read", "memory_note"],
            "requires_confirm": ["memory_note"],
        }},
        "rules": [],
    })
    grant = table.grant("fulana")
    pending = grant.check_tool("memory_note")
    assert pending.allowed is False
    assert pending.requires_confirm is True
    assert pending.reason == "exige_confirmacao_do_dono"
    confirmed = grant.check_tool("memory_note", confirm=True)
    assert confirmed.allowed is True


def test_data_reloads_without_code_or_restart(tmp_path):
    table = _table(tmp_path)
    assert table.grant("fulana").max_tokens == 2000
    data = json.loads((tmp_path / "autonomy.json").read_text(encoding="utf-8"))
    data["rules"][0]["autonomy"]["max_tokens"] = 4321
    (tmp_path / "autonomy.json").write_text(json.dumps(data), encoding="utf-8")
    assert table.reload_if_changed() is True
    assert table.grant("fulana").max_tokens == 4321
    assert table.reload_if_changed() is False


def test_unknown_autonomy_key_is_refused_at_load(tmp_path):
    path = tmp_path / "autonomy.json"
    path.write_text(json.dumps({
        "version": 1,
        "defaults": {"autonomy": {"tools": ["Read"]}},
        "rules": [{
            "id": "x", "match": {"route": "fulana"},
            "autonomy": {"toolz": ["Read"]},
        }],
    }), encoding="utf-8")
    with pytest.raises(AutonomyError):
        AutonomyTable.from_file(path)


def test_tool_key_canonicalizes_mcp_names():
    assert tool_key("mcp__persona__memory_read") == "memory_read"
    assert tool_key("mcp__jarvis__jarvis_call") == "jarvis_call"
    assert tool_key("WebFetch") == "webfetch"
