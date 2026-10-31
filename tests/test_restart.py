"""Tests for the two-step restart gate (core/restart.py + its action).

Run from the repo root:
    .venv/bin/pytest tests/test_restart.py -v

Every manager here is created with dry_run=True unless the test is explicitly
about the live path; the live path is exercised with subprocess.Popen mocked so
no helper process and no reboot can ever happen during the suite.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.restart as restart  # noqa: E402
from actions import restart_assistant as action  # noqa: E402
from core.restart import RestartManager  # noqa: E402


@pytest.fixture(autouse=True)
def _unbound_action():
    action.bind(None)
    yield
    action.bind(None)


@pytest.fixture
def manager():
    return RestartManager(dry_run=True, logger=mock.Mock())


@pytest.fixture
def no_popen(monkeypatch):
    """Fail loudly if anything tries to spawn a process."""
    popen = mock.Mock(side_effect=AssertionError("subprocess.Popen was called"))
    monkeypatch.setattr(restart.subprocess, "Popen", popen)
    return popen


# ── request / confirm ────────────────────────────────────────────────────────

def test_request_assistant_parks_and_asks_for_spoken_confirmation(manager):
    result = manager.request_assistant(reason="testei a melhoria")

    assert "[CONFIRMATION_PENDING]" in result
    assert "confirmo reiniciar o assistente" in result
    assert "Nothing has restarted" in result
    assert "action='confirm'" in result

    p = manager.pending()
    assert p is not None
    assert p["kind"] == "assistant"
    assert p["reason"] == "testei a melhoria"
    assert p["phrase"] == "confirmo reiniciar o assistente"
    assert isinstance(p["expires_in"], int)


def test_pending_is_plain_data(manager):
    manager.request_assistant()
    p = manager.pending()
    assert p is not None
    assert all(isinstance(v, (str, int)) for v in p.values())


def test_confirm_rejects_phrase_without_the_marker(manager, no_popen):
    manager.request_assistant()

    for phrase in ("", "sim", "confirmo", "pode reiniciar", "reinicie por favor"):
        result = manager.confirm(phrase)
        assert "not confirmed" in result.lower()
        assert "confirmo reiniciar o assistente" in result
        assert manager.pending() is not None, phrase

    no_popen.assert_not_called()


def test_confirm_accepts_full_and_case_variants(manager, no_popen):
    manager.request_assistant()
    result = manager.confirm("  CONFIRMO   Reiniciar  o assistente? ")

    assert result.startswith("[DRY RUN]")
    assert manager.pending() is None
    no_popen.assert_not_called()


def test_confirm_returns_the_exact_dry_run_command(manager, no_popen):
    manager.request_assistant()
    result = manager.confirm("confirmo reiniciar")

    assert "/bin/sh -c" in result
    assert "run-jarvis.sh --tray" in result
    assert str(os.getpid()) in result
    assert "Nothing was executed" in result
    no_popen.assert_not_called()


def test_dry_run_never_calls_shutdown(manager, no_popen):
    shutdown = mock.Mock()
    manager.bind(shutdown_cb=shutdown)
    manager.request_assistant()
    manager.confirm("confirmo reiniciar o assistente")

    shutdown.assert_not_called()
    no_popen.assert_not_called()


def test_confirm_without_pending_is_refused(manager, no_popen):
    result = manager.confirm("confirmo reiniciar o assistente")
    assert "no restart awaiting confirmation" in result.lower()
    assert "Nothing was done" in result
    no_popen.assert_not_called()


def test_second_request_does_not_replace_the_pending_one(manager, no_popen):
    manager.request_assistant(reason="primeiro")
    result = manager.request_assistant(reason="segundo")

    assert "already pending" in result
    p = manager.pending()
    assert p is not None and p["reason"] == "primeiro"
    no_popen.assert_not_called()


def test_request_while_pc_pending_is_also_refused(manager, no_popen):
    manager.request_pc(reason="kernel")
    result = manager.request_assistant(reason="melhoria")

    assert "already pending" in result
    p = manager.pending()
    assert p is not None and p["kind"] == "pc"
    no_popen.assert_not_called()


def test_expired_request_is_dropped(manager, no_popen, monkeypatch):
    monkeypatch.setattr(restart, "CONFIRM_TIMEOUT_SECONDS", -1.0)
    manager.request_assistant()

    assert manager.pending() is None
    result = manager.confirm("confirmo reiniciar o assistente")
    # pending() already discarded it, so confirm finds nothing.
    assert "no restart awaiting confirmation" in result.lower()
    no_popen.assert_not_called()


def test_expired_request_reported_by_confirm(manager, no_popen, monkeypatch):
    manager.request_assistant()
    monkeypatch.setattr(restart, "CONFIRM_TIMEOUT_SECONDS", -1.0)

    result = manager.confirm("confirmo reiniciar o assistente")
    assert "expired" in result.lower()
    assert manager.pending() is None
    no_popen.assert_not_called()


# ── cancel ───────────────────────────────────────────────────────────────────

def test_cancel_clears_pending(manager, no_popen):
    manager.request_pc()
    assert manager.pending() is not None

    result = manager.cancel()
    assert "cancelled" in result.lower()
    assert "Nothing was done" in result
    assert manager.pending() is None
    no_popen.assert_not_called()


def test_cancel_with_nothing_pending_is_harmless(manager):
    result = manager.cancel()
    assert "no restart waiting" in result.lower()
    assert "Nothing was done" in result


# ── rate limit ───────────────────────────────────────────────────────────────

def test_assistant_restart_is_rate_limited_to_one_per_five_minutes(manager, no_popen):
    manager.request_assistant()
    assert manager.confirm("confirmo reiniciar o assistente").startswith("[DRY RUN]")

    result = manager.request_assistant(reason="de novo")
    assert "rate-limited" in result
    assert "5 minutes" in result
    assert manager.pending() is None
    no_popen.assert_not_called()


def test_rate_limit_does_not_apply_to_pc(manager, no_popen):
    manager.request_assistant()
    manager.confirm("confirmo reiniciar o assistente")

    result = manager.request_pc(reason="kernel")
    assert "rate-limited" not in result
    assert manager.pending() is not None
    no_popen.assert_not_called()


# ── PC restart ───────────────────────────────────────────────────────────────

def test_pc_request_names_the_computer_phrase(manager, no_popen):
    result = manager.request_pc(reason="atualização")
    assert "confirmo reiniciar o computador" in result
    assert "spoken confirmation is needed" in result

    p = manager.pending()
    assert p is not None and p["kind"] == "pc"


def test_pc_confirm_is_dry_run_and_never_calls_the_callback(manager, no_popen):
    pc = mock.Mock()
    manager.bind(pc_restart=pc)
    manager.request_pc()

    result = manager.confirm("confirmo reiniciar o computador")
    assert result.startswith("[DRY RUN]")
    assert "PC restart" in result
    pc.assert_not_called()
    assert manager.pending() is None
    no_popen.assert_not_called()


def test_pc_confirm_requires_the_phrase(manager, no_popen):
    pc = mock.Mock()
    manager.bind(pc_restart=pc)
    manager.request_pc()

    result = manager.confirm("reinicie o pc")
    assert "not confirmed" in result.lower()
    pc.assert_not_called()
    assert manager.pending() is not None


# ── live path (Popen mocked) ─────────────────────────────────────────────────

def test_live_assistant_restart_spawns_detached_helper_then_shuts_down(monkeypatch):
    popen = mock.Mock(return_value=mock.Mock())
    monkeypatch.setattr(restart.subprocess, "Popen", popen)
    shutdown = mock.Mock()
    manager = RestartManager(dry_run=False, logger=mock.Mock())
    manager.bind(shutdown_cb=shutdown)

    manager.request_assistant(reason="melhoria testada")
    result = manager.confirm("confirmo reiniciar o assistente")

    assert "Restarting the assistant now" in result
    assert popen.call_count == 1
    args, kwargs = popen.call_args
    argv = args[0]
    assert argv[0] == "/bin/sh" and argv[1] == "-c"
    script = argv[2]
    assert f"kill -0 {os.getpid()}" in script
    assert "/home/anthero/jarvis/run-jarvis.sh --tray" in script
    assert kwargs["start_new_session"] is True
    shutdown.assert_called_once_with()
    assert manager.pending() is None


def test_live_assistant_restart_without_shutdown_hook_refuses(monkeypatch):
    popen = mock.Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(restart.subprocess, "Popen", popen)
    manager = RestartManager(dry_run=False, logger=mock.Mock())
    manager.bind(pc_restart=mock.Mock())

    manager.request_assistant()
    result = manager.confirm("confirmo reiniciar o assistente")

    assert "shutdown hook" in result
    assert "Nothing was done" in result
    assert manager.pending() is None


def test_live_pc_restart_calls_the_callback(monkeypatch):
    popen = mock.Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(restart.subprocess, "Popen", popen)
    pc = mock.Mock()
    manager = RestartManager(dry_run=False, logger=mock.Mock())
    manager.bind(pc_restart=pc)

    manager.request_pc()
    result = manager.confirm("confirmo reiniciar o computador")

    assert "Restarting the computer now" in result
    pc.assert_called_once_with()
    assert manager.pending() is None


def test_live_pc_restart_defaults_to_computer_settings(monkeypatch):
    fake = types.ModuleType("actions.computer_settings")
    pc = mock.Mock()
    fake.restart_computer = pc
    monkeypatch.setitem(sys.modules, "actions.computer_settings", fake)
    monkeypatch.setattr(restart.subprocess, "Popen",
                        mock.Mock(side_effect=AssertionError("must not spawn")))

    manager = RestartManager(dry_run=False, logger=mock.Mock())
    manager.request_pc()
    # Asking is not doing.
    pc.assert_not_called()

    result = manager.confirm("confirmo reiniciar o computador")
    assert "Restarting the computer now" in result
    pc.assert_called_once_with()


def test_live_pc_restart_failure_does_not_claim_success(monkeypatch):
    monkeypatch.setattr(restart.subprocess, "Popen",
                        mock.Mock(side_effect=AssertionError("must not spawn")))
    pc = mock.Mock(side_effect=RuntimeError("polkit denied"))
    manager = RestartManager(dry_run=False, logger=mock.Mock())
    manager.bind(pc_restart=pc)

    manager.request_pc()
    result = manager.confirm("confirmo reiniciar o computador")

    assert "failed" in result.lower()
    assert "polkit denied" in result
    assert "Restarting the computer now" not in result


def test_bridge_aliases_reach_the_same_protocol(manager):
    result = manager.assistant(reason="ponte")
    assert "[CONFIRMATION_PENDING]" in result
    status = manager.status()
    assert status is not None and status["kind"] == "assistant"
    manager.cancel()
    assert manager.pc(reason="ponte").startswith("[CONFIRMATION_PENDING]")


# ── action handler ───────────────────────────────────────────────────────────

def test_action_without_manager_returns_clear_error():
    result = action.restart_assistant({"action": "assistant"})
    assert "not available" in result
    assert "RestartManager" in result
    assert "Nothing was done" in result


def test_action_status_and_unknown_action(manager):
    action.bind(manager)
    assert "No restart is awaiting" in action.restart_assistant({"action": "status"})

    result = action.restart_assistant({"action": "explode"})
    assert "Unknown action" in result
    assert "Nothing was done" in result


def test_action_request_then_confirm_with_phrase(manager, no_popen):
    action.bind(manager)

    result = action.restart_assistant(
        {"action": "assistant", "reason": "auto-melhoria"})
    assert "[CONFIRMATION_PENDING]" in result
    assert "confirmo reiniciar o assistente" in result

    status = action.restart_assistant({"action": "status"})
    assert "assistant restart" in status
    assert "awaiting" in status

    rejected = action.restart_assistant({"action": "confirm", "phrase": "sim"})
    assert "not confirmed" in rejected.lower()

    # The pending request survives a wrong phrase.
    assert manager.pending() is not None
    assert "awaiting" in action.restart_assistant({"action": "status"})

    done = action.restart_assistant(
        {"action": "confirm", "phrase": "confirmo reiniciar o assistente"})
    assert done.startswith("[DRY RUN]")
    assert manager.pending() is None
    no_popen.assert_not_called()


def test_action_pc_cancel_and_no_false_claim(manager, no_popen):
    action.bind(manager)

    requested = action.restart_assistant({"action": "pc", "reason": "kernel"})
    assert "confirmo reiniciar o computador" in requested
    assert "Nothing has restarted" in requested

    cancelled = action.restart_assistant({"action": "cancel"})
    assert "cancelled" in cancelled.lower()
    assert manager.pending() is None

    result = action.restart_assistant({"action": "confirm", "phrase": "confirmo reiniciar"})
    assert "no restart awaiting confirmation" in result.lower()
    no_popen.assert_not_called()
