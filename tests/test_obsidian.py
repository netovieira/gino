"""Obsidian vault mirror: folders, plan copies, index files and open calls."""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import obsidian  # noqa: E402


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "Jarvis"
    plans = tmp_path / "qoder" / "plans"
    plans.mkdir(parents=True)
    monkeypatch.setattr(obsidian, "VAULT", root)
    monkeypatch.setattr(obsidian, "PLANS_DIR", plans)
    monkeypatch.setattr(obsidian, "OBSIDIAN_BIN", "/snap/bin/obsidian")
    # UP-7: abrir nota grava event=obsidian_opened; o activity.log do dono
    # não pode virar alvo de teste.
    monkeypatch.setattr(obsidian, "ACTIVITY_LOG", tmp_path / "activity.log")
    monkeypatch.setattr(obsidian, "NEXTCLOUD_DIR", tmp_path / "Nextcloud")
    return root, plans


# ── vault layout ────────────────────────────────────────────────────────────

def test_ensure_vault_creates_layout_and_is_idempotent(vault):
    root, _ = vault
    assert obsidian.ensure_vault() == root
    for name in ("Planos", "Projetos", "Personas", "Indices", "Topicos"):
        assert (root / name).is_dir()
    app = root / ".obsidian" / "app.json"
    assert json.loads(app.read_text("utf-8")) == {}

    marker = root / "Planos" / "keep.md"
    marker.write_text("keep", "utf-8")
    assert obsidian.ensure_vault() == root
    assert marker.read_text("utf-8") == "keep"


# ── plans ───────────────────────────────────────────────────────────────────

def test_mirror_plans_copies_only_when_content_differs(vault):
    root, plans = vault
    first = plans / "alfa.md"
    first.write_text("# alfa\n", "utf-8")
    (plans / "beta.md").write_text("# beta\n", "utf-8")

    destinations = obsidian.mirror_plans()
    assert [p.name for p in destinations] == ["alfa.md", "beta.md"]
    assert all(p.parent == root / "Planos" for p in destinations)
    assert (root / "Planos" / "alfa.md").read_text("utf-8") == "# alfa\n"

    # Freeze the mtime; an unchanged plan must not be rewritten.
    frozen = 1_000_000_000
    os.utime(destinations[0], ns=(frozen, frozen))
    assert obsidian.mirror_plans() == destinations
    assert destinations[0].stat().st_mtime_ns == frozen

    first.write_text("# alfa 2\n", "utf-8")
    obsidian.mirror_plans()
    assert destinations[0].read_text("utf-8") == "# alfa 2\n"
    assert destinations[0].stat().st_mtime_ns != frozen


def test_mirror_plans_missing_source_is_empty(vault, tmp_path):
    assert obsidian.mirror_plans(tmp_path / "nope") == []


def test_mirror_plan_missing_file_raises(vault):
    with pytest.raises(ValueError):
        obsidian.mirror_plan(Path("/definitely/not/here.md"))


def test_mirror_plan_refuses_files_outside_plans_and_vault(vault, tmp_path):
    stray = tmp_path / "stray.md"
    stray.write_text("# stray", "utf-8")
    with pytest.raises(ValueError):
        obsidian.mirror_plan(stray)


def test_mirror_plan_accepts_file_already_in_vault(vault):
    root, _ = vault
    inside = root / "Topicos" / "topico.md"
    inside.parent.mkdir(parents=True)
    inside.write_text("# topico", "utf-8")
    destination = obsidian.mirror_plan(inside)
    assert destination == root / "Planos" / "topico.md"
    assert destination.read_text("utf-8") == "# topico"


# ── index mirror ────────────────────────────────────────────────────────────

def test_mirror_index_writes_teia_and_grafo(vault):
    root, _ = vault
    graph = {"version": 1, "events": {}, "nodes": {}, "edges": []}
    path = obsidian.mirror_index("# Teia\n\n[[subject:saude]]", graph)
    assert path == root / "Indices" / "Teia.md"
    assert path.read_text("utf-8") == "# Teia\n\n[[subject:saude]]"
    stored = json.loads((root / "Indices" / "Grafo.json").read_text("utf-8"))
    assert stored == graph


# ── opening ─────────────────────────────────────────────────────────────────

def test_open_path_mirrors_plan_and_calls_xdg_open(vault, monkeypatch):
    root, plans = vault
    plan = plans / "plano.md"
    plan.write_text("# plano", "utf-8")
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(obsidian.subprocess, "run", fake_run)
    monkeypatch.setattr(obsidian.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("Popen must not be used"))

    message = obsidian.open_path(plan)
    target = root / "Planos" / "plano.md"
    assert target.is_file()
    assert "Abrindo" in message and "Obsidian" in message
    assert calls == [(
        ["xdg-open", "obsidian://open?path=" + quote(str(target), safe="")],
        {"timeout": 5},
    )]


def test_open_path_copies_outside_markdown_into_planos(vault, tmp_path, monkeypatch):
    root, _ = vault
    outside = tmp_path / "fora.md"
    outside.write_text("# fora", "utf-8")
    monkeypatch.setattr(obsidian.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0))
    obsidian.open_path(outside)
    copied = root / "Planos" / "fora.md"
    assert copied.read_text("utf-8") == "# fora"


def test_open_path_falls_back_to_obsidian_binary(vault, tmp_path, monkeypatch):
    root, _ = vault
    outside = tmp_path / "nota.md"
    outside.write_text("# nota", "utf-8")
    popped = []

    def broken_run(*args, **kwargs):
        raise FileNotFoundError("xdg-open")

    monkeypatch.setattr(obsidian.subprocess, "run", broken_run)
    monkeypatch.setattr(obsidian.subprocess, "Popen",
                        lambda args, **kwargs: popped.append((args, kwargs)))

    message = obsidian.open_path(outside)
    target = root / "Planos" / "nota.md"
    assert "Abrindo" in message
    assert popped and popped[0][0] == ["/snap/bin/obsidian", str(target)]


def test_open_path_missing_file_does_not_call_subprocess(vault, monkeypatch):
    monkeypatch.setattr(obsidian.subprocess, "run",
                        lambda *a, **k: pytest.fail("no subprocess for missing"))
    message = obsidian.open_path("/definitely/not/here.md")
    assert "não encontrado" in message
