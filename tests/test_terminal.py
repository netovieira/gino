"""``terminal_run``: argv exato do ptyxis/x-terminal-emulator e validações.

``subprocess.Popen`` é um gravador; nenhuma janela é aberta durante os testes.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import terminal as action  # noqa: E402


@pytest.fixture
def popen(monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return object()

    monkeypatch.setattr(action.subprocess, "Popen", fake_popen)
    return calls


@pytest.fixture
def only(monkeypatch):
    """Escolhe quais terminais existem, sem tocar no PATH real."""
    def install(*names):
        monkeypatch.setattr(
            action.shutil, "which",
            lambda name: f"/usr/bin/{name}" if name in names else None,
        )
    return install


# ── ptyxis (preferido) ─────────────────────────────────────────────────────

def test_ptyxis_default_hold_keeps_bash_open(popen, only):
    only("ptyxis")

    result = action.terminal_run({"command": "echo oi"})

    assert result == "Abri o terminal na tela e estou rodando: echo oi."
    assert len(popen) == 1
    argv, kwargs = popen[0]
    assert argv == ["ptyxis", "--", "bash", "-lc", "echo oi; exec bash"]
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["cwd"] is None
    assert kwargs["start_new_session"] is True
    assert "shell" not in kwargs or kwargs["shell"] is not True


def test_ptyxis_hold_false_does_not_append_exec_bash(popen, only):
    only("ptyxis")

    action.terminal_run({"command": "echo oi", "hold": False})

    assert popen[0][0] == ["ptyxis", "--", "bash", "-lc", "echo oi"]


def test_ptyxis_hold_string_false(popen, only):
    only("ptyxis")

    action.terminal_run({"command": "echo oi", "hold": "false"})

    assert popen[0][0][-1] == "echo oi"


def test_ptyxis_cwd_option_comes_before_double_dash(popen, only, tmp_path):
    only("ptyxis")

    action.terminal_run({"command": "pwd", "cwd": str(tmp_path)})

    argv, kwargs = popen[0]
    assert argv == [
        "ptyxis", "--working-directory", str(tmp_path),
        "--", "bash", "-lc", "pwd; exec bash",
    ]
    assert argv.index("--working-directory") < argv.index("--")
    assert kwargs["cwd"] == str(tmp_path)


def test_ptyxis_title_comes_before_double_dash(popen, only):
    only("ptyxis")

    action.terminal_run({"command": "tail -f log", "title": "JARVIS · tarefa"})

    argv = popen[0][0]
    assert argv[:4] == ["ptyxis", "--title", "JARVIS · tarefa", "--"]
    assert argv[-3:] == ["bash", "-lc", "tail -f log; exec bash"]


def test_command_is_a_single_argument(popen, only):
    only("ptyxis")

    action.terminal_run({"command": "echo 'a b' && ls"})

    assert popen[0][0][-1] == "echo 'a b' && ls; exec bash"


def test_open_terminal_reports_the_pid_of_the_window(monkeypatch, only):
    only("ptyxis")

    class Proc:
        pid = 4242

    monkeypatch.setattr(action.subprocess, "Popen", lambda *a, **k: Proc())

    result = action.open_terminal({"command": "tail -F log", "hold": False})

    assert result == {"ok": True, "pid": 4242,
                      "message": "Abri o terminal na tela e estou rodando: "
                                 "tail -F log."}


# ── fallback x-terminal-emulator ───────────────────────────────────────────

def test_xterm_fallback_argv(popen, only):
    only("x-terminal-emulator")

    result = action.terminal_run({"command": "ls -la"})

    assert result == "Abri o terminal na tela e estou rodando: ls -la."
    assert popen[0][0] == [
        "x-terminal-emulator", "-e", "bash", "-lc", "ls -la; exec bash",
    ]
    assert popen[0][1]["start_new_session"] is True


def test_xterm_fallback_honors_cwd(popen, only, tmp_path):
    only("x-terminal-emulator")

    action.terminal_run({"command": "pwd", "cwd": str(tmp_path)})

    argv, kwargs = popen[0]
    assert argv == [
        "x-terminal-emulator", "-e", "bash", "-lc", "pwd; exec bash",
    ]
    assert kwargs["cwd"] == str(tmp_path)


# ── validação ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", ["", "   ", None])
def test_empty_command_is_rejected(command, popen, only):
    only("ptyxis")

    result = action.terminal_run({"command": command})

    assert "Informe o comando" in result
    assert popen == []


def test_missing_cwd_is_rejected(popen, only, tmp_path):
    only("ptyxis")
    missing = tmp_path / "nao-existe"

    result = action.terminal_run({"command": "ls", "cwd": str(missing)})

    assert "Diretório não encontrado" in result
    assert str(missing) in result
    assert popen == []


def test_cwd_must_be_a_directory(popen, only, tmp_path):
    only("ptyxis")
    file_path = tmp_path / "arquivo.txt"
    file_path.write_text("x", encoding="utf-8")

    result = action.terminal_run({"command": "ls", "cwd": str(file_path)})

    assert "Diretório não encontrado" in result
    assert popen == []


def test_no_terminal_available_is_reported(popen, only):
    only()

    result = action.terminal_run({"command": "ls"})

    assert "Não achei ptyxis" in result
    assert popen == []


def test_popen_failure_is_reported(monkeypatch, only):
    only("ptyxis")
    monkeypatch.setattr(action.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("sem display")))

    result = action.terminal_run({"command": "ls"})

    assert "Não consegui abrir o terminal" in result
    assert "sem display" in result


# ── abrir janela != executar comando ───────────────────────────────────────

def test_open_terminal_without_command_opens_interactive_bash(popen, only, tmp_path):
    only("ptyxis")

    result = action.open_terminal({"cwd": str(tmp_path), "title": "JARVIS"})

    assert result["ok"] is True
    assert result["message"] == "Abri o terminal na tela."
    argv, kwargs = popen[0]
    assert argv == [
        "ptyxis", "--title", "JARVIS", "--working-directory", str(tmp_path),
        "--", "bash", "-l",
    ]
    assert kwargs["start_new_session"] is True


def test_open_terminal_without_command_xterm_fallback(popen, only):
    only("x-terminal-emulator")

    result = action.open_terminal({})

    assert result["message"] == "Abri o terminal na tela."
    assert popen[0][0] == ["x-terminal-emulator", "-e", "bash", "-l"]


def test_terminal_open_tool_never_runs_a_command(popen, only, tmp_path):
    from actions import terminal_open as opener

    only("ptyxis")

    result = opener.terminal_open({"command": "rm -rf ~", "cwd": str(tmp_path)})

    assert result == "Abri o terminal na tela."
    argv = popen[0][0]
    assert "rm -rf ~" not in argv
    assert "rm" not in argv
    assert argv[-3:] == ["--", "bash", "-l"]


def test_terminal_open_tool_contract():
    from actions import terminal_open as opener

    assert opener.TOOL["name"] == "terminal_open"
    assert opener.TOOL["handler"] is opener.terminal_open
    assert opener.TOOL["parameters"]["required"] == []
    assert opener.TOOL["scheduling"] == "SILENT"


def test_terminal_open_does_not_contradict_the_mouth_guard():
    from actions import terminal_open as opener
    from core.flows.guard import announcement, template_for

    assert opener.TOOL["scheduling"] == "SILENT"
    assert announcement("Abrindo o Terminal.") == "abrindo"
    assert template_for([], "Abrindo o Terminal.") == ""


# ── contrato do TOOL ───────────────────────────────────────────────────────

def test_tool_contract():
    assert action.TOOL["name"] == "terminal_run"
    assert action.TOOL["handler"] is action.terminal_run
    assert action.TOOL["parameters"]["required"] == ["command"]
    props = action.TOOL["parameters"]["properties"]
    assert props["command"]["type"] == "STRING"
    assert props["cwd"]["type"] == "STRING"
    assert props["hold"]["type"] == "BOOLEAN"
