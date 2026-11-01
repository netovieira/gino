"""Roteamento de ``open_in``: extensão -> app, launcher do Qoder e erros.

Nenhum aplicativo real é aberto: ``subprocess.Popen`` é substituído por um
gravador de chamadas e o módulo ``core.obsidian`` é um dublê em ``sys.modules``.
"""
from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import open_in_app as action  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_unified(monkeypatch):
    """Keep the real /opt/Qoder/qoder out of unit tests."""
    monkeypatch.setattr(action, "QODER_UNIFIED", "/nonexistent/Qoder-unified")


@pytest.fixture
def popen(monkeypatch):
    """Grava (argv, kwargs) de cada Popen em vez de abrir nada."""
    calls: list[tuple[list[str], dict]] = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return object()

    monkeypatch.setattr(action.subprocess, "Popen", fake_popen)
    return calls


@pytest.fixture
def qoder(tmp_path, monkeypatch):
    """Qoder IDE falso e executável, para o launcher ser determinístico."""
    ide = tmp_path / "qoder-ide"
    ide.write_text("#!/bin/sh\n", encoding="utf-8")
    ide.chmod(0o755)
    monkeypatch.setattr(action, "QODER_IDE", str(ide))
    monkeypatch.setattr(action, "QODER_CLI", str(tmp_path / "missing-code"))
    monkeypatch.setattr(action.shutil, "which", lambda name: None)
    return ide


@pytest.fixture
def obsidian(monkeypatch):
    module = types.ModuleType("core.obsidian")
    module.open_path = mock.Mock(return_value="Abri o plano no Obsidian.")
    monkeypatch.setitem(sys.modules, "core.obsidian", module)
    return module


def make_file(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_text("conteudo\n", encoding="utf-8")
    return path


# ── roteamento por extensão ─────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "main.py", "app.js", "view.jsx", "types.ts", "view.tsx", "package.json",
    "ci.yml", "config.yaml", "config.toml", "lib.rs", "main.go", "Main.java",
    "lib.c", "lib.h", "lib.cpp", "Program.cs", "index.php", "task.rb",
    "run.sh", "query.sql", "style.css", "index.html", "feed.xml",
    "service.dockerfile", "Dockerfile",
])
def test_code_files_go_to_qoder(name, tmp_path, popen, qoder):
    path = make_file(tmp_path, name)

    result = action.open_in({"path": str(path)})

    assert result == f"Abri {name} no Qoder IDE."
    assert len(popen) == 1
    argv, kwargs = popen[0]
    assert argv == [str(qoder), str(path)]
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert "shell" not in kwargs or kwargs["shell"] is not True


@pytest.mark.parametrize("name", ["plano.md", "PLANO.MARKDOWN"])
def test_markdown_goes_to_obsidian(name, tmp_path, popen, obsidian):
    path = make_file(tmp_path, name)

    result = action.open_in({"path": str(path)})

    assert result == "Abri o plano no Obsidian."
    obsidian.open_path.assert_called_once_with(str(path))
    assert popen == []


def test_explicit_obsidian_wins_over_extension(tmp_path, popen, obsidian):
    path = make_file(tmp_path, "script.py")

    result = action.open_in({"path": str(path), "app": "obsidian"})

    assert result == "Abri o plano no Obsidian."
    obsidian.open_path.assert_called_once_with(str(path))
    assert popen == []


def test_unknown_file_goes_to_default_app(tmp_path, popen, monkeypatch):
    path = make_file(tmp_path, "contrato.pdf")
    monkeypatch.setattr(action.shutil, "which",
                        lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None)

    result = action.open_in({"path": str(path)})

    assert result == "Abri contrato.pdf no aplicativo padrão."
    assert len(popen) == 1
    assert popen[0][0] == ["xdg-open", str(path)]
    assert popen[0][1]["stderr"] is subprocess.DEVNULL


def test_directory_opens_as_project_in_qoder(tmp_path, popen, qoder):
    folder = tmp_path / "projeto"
    folder.mkdir()

    result = action.open_in({"path": str(folder)})

    assert result == "Abri projeto no Qoder IDE."
    assert popen[0][0] == [str(qoder), str(folder)]


# ── seleção do launcher do Qoder ────────────────────────────────────────────

def test_qoder_prefers_unified_app_and_uses_open_file(tmp_path, monkeypatch, popen):
    unified = tmp_path / "Qoder"
    unified.write_text("#!/bin/sh\n", encoding="utf-8")
    unified.chmod(0o755)
    monkeypatch.setattr(action, "QODER_UNIFIED", str(unified))
    path = make_file(tmp_path, "x.py")

    result = action.open_in({"path": str(path), "app": "qoder"})

    assert result == "Abri x.py no Qoder IDE."
    assert popen[0][0] == [str(unified), "--open-file", str(path)]


def test_qoder_prefers_desktop_binary(tmp_path, monkeypatch, popen):
    ide = tmp_path / "ide"
    cli = tmp_path / "code"
    for binary in (ide, cli):
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
    monkeypatch.setattr(action, "QODER_IDE", str(ide))
    monkeypatch.setattr(action, "QODER_CLI", str(cli))
    path = make_file(tmp_path, "x.py")

    action.open_in({"path": str(path), "app": "qoder"})

    assert popen[0][0][0] == str(ide)


def test_qoder_falls_back_to_bundled_cli(tmp_path, monkeypatch, popen):
    cli = tmp_path / "code"
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    cli.chmod(0o755)
    monkeypatch.setattr(action, "QODER_IDE", str(tmp_path / "missing"))
    monkeypatch.setattr(action, "QODER_CLI", str(cli))
    path = make_file(tmp_path, "x.py")

    action.open_in({"path": str(path), "app": "qoder"})

    assert popen[0][0][0] == str(cli)


def test_qoder_falls_back_to_code_in_path(tmp_path, monkeypatch, popen):
    monkeypatch.setattr(action, "QODER_IDE", str(tmp_path / "missing-ide"))
    monkeypatch.setattr(action, "QODER_CLI", str(tmp_path / "missing-cli"))
    monkeypatch.setattr(action.shutil, "which",
                        lambda name: "/opt/bin/code" if name == "code" else None)
    path = make_file(tmp_path, "x.py")

    action.open_in({"path": str(path), "app": "qoder"})

    assert popen[0][0][0] == "/opt/bin/code"


def test_qoder_last_resort_is_literal_code(tmp_path, monkeypatch, popen):
    monkeypatch.setattr(action, "QODER_IDE", str(tmp_path / "missing-ide"))
    monkeypatch.setattr(action, "QODER_CLI", str(tmp_path / "missing-cli"))
    monkeypatch.setattr(action.shutil, "which", lambda name: None)
    path = make_file(tmp_path, "x.py")

    action.open_in({"path": str(path), "app": "qoder"})

    assert popen[0][0][0] == "code"


# ── validação e falhas ─────────────────────────────────────────────────────

def test_missing_file_returns_clear_error(tmp_path, popen):
    missing = tmp_path / "nao-existe.md"

    result = action.open_in({"path": str(missing)})

    assert "Caminho não encontrado" in result
    assert str(missing) in result
    assert popen == []


def test_blank_path_is_rejected(popen):
    result = action.open_in({"path": "   "})

    assert "Informe o caminho" in result
    assert popen == []


def test_invalid_app_is_rejected(tmp_path, popen):
    path = make_file(tmp_path, "x.py")

    result = action.open_in({"path": str(path), "app": "photoshop"})

    assert "app inválido" in result
    assert popen == []


def test_qoder_launch_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(action, "QODER_IDE", str(tmp_path / "ide"))
    path = make_file(tmp_path, "x.py")
    monkeypatch.setattr(action.subprocess, "Popen",
                        mock.Mock(side_effect=OSError("sem display")))

    result = action.open_in({"path": str(path), "app": "qoder"})

    assert "Não consegui abrir x.py no Qoder IDE" in result
    assert "sem display" in result


def test_obsidian_failure_is_reported(tmp_path, popen, monkeypatch):
    path = make_file(tmp_path, "plano.md")
    module = types.ModuleType("core.obsidian")
    module.open_path = mock.Mock(side_effect=RuntimeError("cofre fora do ar"))
    monkeypatch.setitem(sys.modules, "core.obsidian", module)

    result = action.open_in({"path": str(path)})

    assert "Não consegui abrir plano.md no Obsidian" in result
    assert "cofre fora do ar" in result
    assert popen == []


def test_tilde_is_expanded(tmp_path, popen, qoder, monkeypatch):
    path = make_file(tmp_path, "x.py")
    monkeypatch.setenv("HOME", str(tmp_path))

    action.open_in({"path": "~/x.py"})

    assert popen[0][0] == [str(qoder), str(path)]


# ── contrato do TOOL ───────────────────────────────────────────────────────

def test_tool_contract():
    assert action.TOOL["name"] == "open_in"
    assert action.TOOL["handler"] is action.open_in
    assert action.TOOL["parameters"]["required"] == ["path"]
    props = action.TOOL["parameters"]["properties"]
    assert props["app"]["enum"] == ["auto", "obsidian", "qoder", "system"]
    assert "path" in props
