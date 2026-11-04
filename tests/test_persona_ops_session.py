"""Sessão e partição de memória: cerca de caminho, orçamento e prazo."""
from __future__ import annotations

import json

import pytest

from core.flow_ops.registry import LifecycleRegistry
from core.persona_ops.autonomy import PersonaGrant
from core.persona_ops.session import (MemoryError, PersonaMemory,
                                      PersonaSession, safe_slug)


def _grant(max_tokens=100, ttl_s=60.0):
    return PersonaGrant(
        persona="aline", tools=frozenset({"read", "memory_note"}),
        deny_tools=frozenset(), read_roots=(), requires_confirm=frozenset(),
        max_tokens=max_tokens, session_ttl_s=ttl_s)


def test_memory_partition_is_own_and_never_the_gino_store(tmp_path):
    chat = tmp_path / "chat"
    chat.mkdir()
    (chat / "hot.md").write_text("segredo do Gino", encoding="utf-8")

    aline = PersonaMemory(tmp_path, "aline")
    arnaldo = PersonaMemory(tmp_path, "arnaldo")
    aline.write("state.md", "# Aline\nfoco: UI")
    aline.note("card do player revisado")

    assert "foco: UI" in aline.context()
    assert "card do player" in aline.context()
    assert arnaldo.context() == ""          # outra persona não vê
    assert "segredo do Gino" not in aline.context()
    assert aline.search("Gino") == []       # search não sai da partição
    assert aline.search("player")[0].path == "notes.md"
    assert aline.partition()["persona"] == "aline"
    assert (tmp_path / "aline" / "state.md").is_file()


def test_memory_refuses_escape_and_absolute_paths(tmp_path):
    memory = PersonaMemory(tmp_path, "aline")
    for bad in ("../arnaldo/state.md", "/etc/passwd", "a/../../b.md"):
        with pytest.raises(MemoryError):
            memory.write(bad, "x")
    with pytest.raises(MemoryError):
        memory.read("../chat/hot.md")


def test_memory_keeps_existing_records(tmp_path):
    memory = PersonaMemory(tmp_path, "aline")
    memory.ensure()
    existing = tmp_path / "aline" / "state.md"
    existing.write_text("registro antigo", encoding="utf-8")
    memory.note("nota nova")
    assert existing.read_text(encoding="utf-8") == "registro antigo"


def test_session_charges_tokens_and_registers_lifecycle(tmp_path):
    registry = LifecycleRegistry(tmp_path / "registry")
    clock = [1000.0]
    session = PersonaSession("aline", _grant(max_tokens=10), registry,
                             memory_root=tmp_path / "memory",
                             clock=lambda: clock[0])
    assert session.alive() is True
    assert session.charge(5).alive is True
    assert session.charge(5).alive is True
    over = session.charge(1)
    assert over.alive is False
    assert over.reason == "orcamento_de_tokens"
    session.finish()
    record = json.loads((tmp_path / "registry" / "persona:aline.json")
                        .read_text(encoding="utf-8"))
    assert record["kind"] == "persona"
    assert record["used_tokens"] == 11
    assert record["state"]["status"] == "done"
    assert record["pid"] == 0               # nunca sinaliza o processo principal


def test_session_expires_by_deadline(tmp_path):
    registry = LifecycleRegistry(tmp_path / "registry")
    clock = [1000.0]
    session = PersonaSession("aline", _grant(max_tokens=0, ttl_s=30.0),
                             registry, memory_root=tmp_path / "memory",
                             clock=lambda: clock[0])
    clock[0] = 1010.0
    assert session.alive() is True
    clock[0] = 1040.0
    state = session.budget_state()
    assert state.alive is False
    assert state.reason == "prazo_de_sessao"


def test_session_context_is_only_its_own_memory(tmp_path):
    registry = LifecycleRegistry(tmp_path / "registry")
    memory = PersonaMemory(tmp_path / "memory", "aline")
    memory.write("state.md", "contexto próprio")
    other = PersonaMemory(tmp_path / "memory", "arnaldo")
    other.write("state.md", "contexto alheio")
    session = PersonaSession("aline", _grant(), registry,
                             memory_root=tmp_path / "memory")
    assert "contexto próprio" in session.context()
    assert "contexto alheio" not in session.context()


def test_memory_search_does_not_follow_a_symlink_out(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("conteudo fora da particao", encoding="utf-8")
    memory = PersonaMemory(tmp_path / "memory", "aline")
    memory.ensure()
    (memory.root / "link.md").symlink_to(outside)
    assert memory.search("fora") == []


def test_slug_is_validated():
    assert safe_slug("Aline") == "aline"
    for bad in ("", "../x", "a/b", "x y"):
        with pytest.raises(MemoryError):
            safe_slug(bad)
