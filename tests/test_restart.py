"""Tests for the two-step restart gate (core/restart.py + its action).

Run from the repo root:
    .venv/bin/pytest tests/test_restart.py -v

Every manager here is created with dry_run=True unless the test is explicitly
about the live path; the live path is exercised with subprocess.Popen mocked so
no helper process and no reboot can ever happen during the suite.
"""
from __future__ import annotations

import json
import os
import sys
import time
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


@pytest.fixture(autouse=True)
def _no_real_systemd_run(monkeypatch):
    """Nenhum teste do protocolo pode tocar o systemd --user de verdade.

    O caminho preferido do helper usa subprocess.run(systemd-run ...); sem
    esta guarda um teste futuro com dry_run=False criaria uma unidade real.
    """
    run = mock.Mock(side_effect=AssertionError("subprocess.run was called"))
    monkeypatch.setattr(restart.subprocess, "run", run)
    return run


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
    assert "owner-factor-only" in result
    assert "action='confirm'" not in result

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


# ── expired request renewal (02/11/2026 late confirmation) ──────────────────

def test_confirmation_window_is_thirty_minutes_and_rate_limit_stays_five():
    # Owner's decision on 02/11/2026: a 15-minute window let a legitimate
    # "confirmo reiniciar" arrive after the TTL and turn into a silent new
    # request. The rate limit is not part of that decision and stays untouched.
    assert restart.CONFIRM_TIMEOUT_SECONDS == 1800.0
    assert restart.RATE_LIMIT_SECONDS == 300.0


def test_expired_request_is_archived_not_deleted(tmp_path, monkeypatch):
    state = tmp_path / "restart_pending.json"
    manager = RestartManager(dry_run=True, logger=mock.Mock(), state_path=state)
    manager.request_assistant(reason="correções de hoje")

    monkeypatch.setattr(restart, "CONFIRM_TIMEOUT_SECONDS", -1.0)
    assert manager.pending() is None
    assert not state.exists()

    archive = tmp_path / "restart_last_pending.json"
    assert archive.exists(), "o pedido expirado foi apagado em vez de arquivado"
    raw = json.loads(archive.read_text(encoding="utf-8"))
    assert raw["kind"] == "assistant"
    assert raw["reason"] == "correções de hoje"
    assert manager.last_known()["kind"] == "assistant"


def test_renew_expired_keeps_the_reason_and_the_phrase_can_be_repeated(tmp_path,
                                                                      monkeypatch):
    state = tmp_path / "restart_pending.json"
    logger = mock.Mock()
    manager = RestartManager(dry_run=True, logger=logger, state_path=state)
    manager.request_assistant(reason="correções de hoje")

    monkeypatch.setattr(restart, "CONFIRM_TIMEOUT_SECONDS", -1.0)
    assert manager.pending() is None
    # Back to the real window: the renewal happens in the same turn.
    monkeypatch.setattr(restart, "CONFIRM_TIMEOUT_SECONDS", 1800.0)

    renewal = manager.renew_expired()
    assert renewal["renewed"] is True
    assert renewal["kind"] == "assistant"
    assert renewal["reason"] == "correções de hoje"
    assert manager.pending() is not None
    assert not (tmp_path / "restart_last_pending.json").exists()

    logs = " ".join(str(c.args[0]) for c in logger.call_args_list)
    assert "[Restart] confirmacao tardia — pedido expirado, renovado" in logs

    assert manager.confirm("confirmo reiniciar o assistente").startswith("[DRY RUN]")


def test_renew_expired_keeps_the_pc_kind(tmp_path, monkeypatch):
    state = tmp_path / "restart_pending.json"
    manager = RestartManager(dry_run=True, logger=mock.Mock(), state_path=state)
    manager.request_pc(reason="kernel")

    monkeypatch.setattr(restart, "CONFIRM_TIMEOUT_SECONDS", -1.0)
    assert manager.pending() is None
    monkeypatch.setattr(restart, "CONFIRM_TIMEOUT_SECONDS", 1800.0)

    renewal = manager.renew_expired()
    assert renewal["renewed"] is True
    assert renewal["kind"] == "pc"
    assert renewal["phrase"] == "confirmo reiniciar o computador"
    assert renewal["reason"] == "kernel"


def test_renew_without_history_creates_nothing(manager):
    result = manager.renew_expired()
    assert result["renewed"] is False
    assert manager.pending() is None


def test_renew_expired_falls_back_to_the_last_restart_reason(tmp_path, monkeypatch):
    state = tmp_path / "restart_pending.json"
    (tmp_path / "restart_last.json").write_text(json.dumps(
        {"at": time.time() - 3600.0, "pid": 999999,
         "reason": "correções anteriores"}), encoding="utf-8")
    manager = RestartManager(dry_run=True, logger=mock.Mock(), state_path=state)
    manager.request_assistant(reason="")

    monkeypatch.setattr(restart, "CONFIRM_TIMEOUT_SECONDS", -1.0)
    assert manager.pending() is None
    monkeypatch.setattr(restart, "CONFIRM_TIMEOUT_SECONDS", 1800.0)

    renewal = manager.renew_expired()
    assert renewal["renewed"] is True
    assert renewal["reason"] == "correções anteriores"


def test_renew_expired_explicit_kind_wins_over_the_archived_pc_request(tmp_path,
                                                                      monkeypatch):
    state = tmp_path / "restart_pending.json"
    manager = RestartManager(dry_run=True, logger=mock.Mock(), state_path=state)
    manager.request_pc(reason="kernel")

    monkeypatch.setattr(restart, "CONFIRM_TIMEOUT_SECONDS", -1.0)
    assert manager.pending() is None
    monkeypatch.setattr(restart, "CONFIRM_TIMEOUT_SECONDS", 1800.0)

    renewal = manager.renew_expired(kind="assistant")
    assert renewal["renewed"] is True
    assert renewal["kind"] == "assistant"
    assert renewal["phrase"] == "confirmo reiniciar o assistente"


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


# ── origem do reinício (nunca atribuir ordem ao dono) ───────────────────────

def _logged(manager) -> str:
    return " ".join(str(call.args[0]) for call in manager._logger.call_args_list)


def test_confirm_owner_without_origin_records_unknown_origin(manager):
    seen = []
    manager._run_assistant = lambda p: seen.append(p.reason) or "ok"

    manager.confirm_owner(kind="assistant")

    assert seen == [restart.UNKNOWN_ORIGIN]
    assert restart.UNKNOWN_ORIGIN in _logged(manager)


def test_confirm_owner_logs_the_real_reason_it_received(manager):
    seen = []
    manager._run_assistant = lambda p: seen.append(p.reason) or "ok"

    manager.confirm_owner(kind="assistant", reason="testei a melhoria")

    assert seen == ["testei a melhoria"]
    assert "testei a melhoria" in _logged(manager)


def test_confirm_owner_keeps_the_parked_reason_when_none_is_given(manager):
    seen = []
    manager._run_assistant = lambda p: seen.append(p.reason) or "ok"
    manager.request_assistant(reason="correções de hoje")

    manager.confirm_owner(kind="assistant")

    assert seen == ["correções de hoje"]


def test_legacy_direct_order_label_is_recorded_as_automatic_detection(manager):
    seen = []
    manager._run_assistant = lambda p: seen.append(p.reason) or "ok"

    manager.confirm_owner(kind="assistant", reason="ordem direta do dono")

    assert seen == [restart.AUTO_DETECTION_REASON]
    assert "ordem direta do dono" not in _logged(manager)
    assert "detecção automática de intenção" in _logged(manager)


def test_recorded_origin_is_never_the_old_attribution_by_default():
    assert restart.recorded_origin() == restart.UNKNOWN_ORIGIN
    assert restart.recorded_origin("", "") == restart.UNKNOWN_ORIGIN
    assert (restart.recorded_origin("ordem direta do dono")
            == restart.AUTO_DETECTION_REASON)


# ── live path (Popen mocked) ─────────────────────────────────────────────────

def test_live_assistant_restart_spawns_helper_outside_cgroup(monkeypatch,
                                                              tmp_path):
    """The relaunch helper must escape the app cgroup (defect of 05/11/2026).

    ``start_new_session=True`` kept the helper in the app's systemd scope and
    GNOME swept it with the scope; the chosen mechanism is a transient
    ``systemd-run --user`` oneshot unit.
    """
    run = mock.Mock(return_value=mock.Mock(returncode=0))
    popen = mock.Mock(side_effect=AssertionError("subprocess.Popen was called"))
    monkeypatch.setattr(restart.subprocess, "run", run)
    monkeypatch.setattr(restart.subprocess, "Popen", popen)
    shutdown = mock.Mock()
    manager = RestartManager(dry_run=False, logger=mock.Mock(),
                             state_path=tmp_path / "restart_pending.json")
    manager.bind(shutdown_cb=shutdown)

    manager.request_assistant(reason="melhoria testada")
    result = manager.confirm("confirmo reiniciar o assistente")

    assert "Restarting the assistant now" in result
    assert run.call_count == 1
    argv = run.call_args[0][0]
    assert argv[:2] == ["systemd-run", "--user"]
    assert "--no-block" in argv and "--collect" in argv
    assert "--property=Type=oneshot" in argv
    assert any(a.startswith("--unit=jarvis-relaunch-") for a in argv)
    script = argv[-1]
    assert f"kill -0 {os.getpid()}" in script
    assert "/home/anthero/jarvis/run-jarvis.sh --tray" in script
    assert argv[-3] == "/bin/sh" and argv[-2] == "-c"
    shutdown.assert_called_once_with()
    assert manager.pending() is None


def test_live_assistant_restart_falls_back_to_setsid_leaf(monkeypatch, tmp_path):
    """systemd-run unavailable → setsid + explicit migration to leaf cgroup."""
    monkeypatch.setattr(restart.shutil, "which",
                        lambda _name: None)  # sem systemd-run no PATH
    popen = mock.Mock(return_value=mock.Mock(pid=424242))
    monkeypatch.setattr(restart.subprocess, "Popen", popen)
    moved = []
    monkeypatch.setattr(restart, "_move_pid_out_of_app_cgroup",
                        lambda pid: moved.append(pid) or True)
    manager = RestartManager(dry_run=False, logger=mock.Mock(),
                             state_path=tmp_path / "restart_pending.json")
    manager.bind(shutdown_cb=mock.Mock())

    manager.request_assistant()
    result = manager.confirm("confirmo reiniciar o assistente")

    assert "Restarting the assistant now" in result
    assert moved == [424242]
    args, kwargs = popen.call_args
    assert args[0][:2] == ["/bin/sh", "-c"]
    assert kwargs["start_new_session"] is True


def test_live_assistant_restart_last_resort_keeps_in_scope_helper(monkeypatch,
                                                                  tmp_path):
    """Nem systemd-run nem folha: o helper antigo ainda é agendado e o log avisa."""
    monkeypatch.setattr(restart.shutil, "which", lambda _name: None)
    popen = mock.Mock(return_value=mock.Mock(pid=424242))
    monkeypatch.setattr(restart.subprocess, "Popen", popen)
    monkeypatch.setattr(restart, "_move_pid_out_of_app_cgroup",
                        lambda _pid: False)
    logger = mock.Mock()
    manager = RestartManager(dry_run=False, logger=logger,
                             state_path=tmp_path / "restart_pending.json")
    manager.bind(shutdown_cb=mock.Mock())

    manager.request_assistant()
    result = manager.confirm("confirmo reiniciar o assistente")

    assert "Restarting the assistant now" in result
    assert popen.call_count == 1
    logged = " ".join(str(c) for c in logger.call_args_list)
    assert "DENTRO do cgroup" in logged


def test_live_assistant_restart_without_shutdown_hook_refuses(monkeypatch,
                                                              tmp_path):
    popen = mock.Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(restart.subprocess, "Popen", popen)
    manager = RestartManager(dry_run=False, logger=mock.Mock(),
                             state_path=tmp_path / "restart_pending.json")
    manager.bind(pc_restart=mock.Mock())

    manager.request_assistant()
    result = manager.confirm("confirmo reiniciar o assistente")

    assert "shutdown hook" in result
    assert "Nothing was done" in result
    assert manager.pending() is None


def test_live_pc_restart_calls_the_callback(monkeypatch, tmp_path):
    popen = mock.Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(restart.subprocess, "Popen", popen)
    pc = mock.Mock()
    manager = RestartManager(dry_run=False, logger=mock.Mock(),
                             state_path=tmp_path / "restart_pending.json")
    manager.bind(pc_restart=pc)

    manager.request_pc()
    result = manager.confirm("confirmo reiniciar o computador")

    assert "Restarting the computer now" in result
    pc.assert_called_once_with()
    assert manager.pending() is None


def test_live_pc_restart_defaults_to_computer_settings(monkeypatch, tmp_path):
    fake = types.ModuleType("actions.computer_settings")
    pc = mock.Mock()
    fake.restart_computer = pc
    monkeypatch.setitem(sys.modules, "actions.computer_settings", fake)
    monkeypatch.setattr(restart.subprocess, "Popen",
                        mock.Mock(side_effect=AssertionError("must not spawn")))

    manager = RestartManager(dry_run=False, logger=mock.Mock(),
                             state_path=tmp_path / "restart_pending.json")
    manager.request_pc()
    # Asking is not doing.
    pc.assert_not_called()

    result = manager.confirm("confirmo reiniciar o computador")
    assert "Restarting the computer now" in result
    pc.assert_called_once_with()


def test_live_pc_restart_failure_does_not_claim_success(monkeypatch, tmp_path):
    monkeypatch.setattr(restart.subprocess, "Popen",
                        mock.Mock(side_effect=AssertionError("must not spawn")))
    pc = mock.Mock(side_effect=RuntimeError("polkit denied"))
    manager = RestartManager(dry_run=False, logger=mock.Mock(),
                             state_path=tmp_path / "restart_pending.json")
    manager.bind(pc_restart=pc)

    manager.request_pc()
    result = manager.confirm("confirmo reiniciar o computador")

    assert "failed" in result.lower()
    assert "polkit denied" in result
    assert "Restarting the computer now" not in result


def test_bridge_aliases_reach_the_same_protocol(manager):
    """Dono, 10/11/2026: a ponte acende o poder do processo; a máquina parka.

    O alias `assistant` deixou de ser um pedido a confirmar; `pc` continua no
    portão antigo, que é o que separa os dois caminhos.
    """
    result = manager.assistant(reason="ponte")
    assert "[DRY RUN]" in result
    assert "[CONFIRMATION_PENDING]" not in result
    status = manager.status()
    assert status is None
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


def test_action_assistant_is_a_power_and_pc_keeps_the_phrase(manager, no_popen):
    """Reinício do processo: executa, sem frase. Da máquina: parka e espera."""
    action.bind(manager)

    result = action.restart_assistant(
        {"action": "assistant", "reason": "auto-melhoria"})
    assert result.startswith("[DRY RUN]")
    assert "confirmo reiniciar" not in result
    assert manager.pending() is None

    # O teto de 5 minutos é o que trava o próximo, não uma janela de "sim".
    limited = action.restart_assistant({"action": "assistant"})
    assert ("não reiniciei" in limited.lower()
            and "teto" in limited.lower())
    assert manager.pending() is None

    # O modelo continua sem poder confirmar nada pelo dono.
    rejected = action.restart_assistant({"action": "confirm", "phrase": "sim"})
    assert "owner-factor-only" in rejected.lower()

    parked = action.restart_assistant({"action": "pc", "reason": "kernel"})
    assert "confirmo reiniciar o computador" in parked
    status = action.restart_assistant({"action": "status"})
    assert "pc restart" in status and "awaiting" in status

    # A fala exata do dono é o que libera a máquina.
    done = manager.confirm("confirmo reiniciar o computador")
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
    assert "owner-factor-only" in result.lower()
    assert "no restart awaiting confirmation" in manager.confirm(
        "confirmo reiniciar").lower()
    no_popen.assert_not_called()


# ── persistence across turns / instances ─────────────────────────────────────

def test_pending_request_survives_a_new_manager_instance(tmp_path):
    state = tmp_path / "restart_pending.json"
    first = RestartManager(dry_run=True, logger=mock.Mock(), state_path=state)
    first.request_assistant(reason="correções de hoje")

    second = RestartManager(dry_run=True, logger=mock.Mock(), state_path=state)
    p = second.pending()
    assert p is not None
    assert p["kind"] == "assistant"
    assert p["reason"] == "correções de hoje"
    assert "correções de hoje" in state.read_text(encoding="utf-8")

    result = second.confirm("confirmo reiniciar o assistente")
    assert result.startswith("[DRY RUN]")
    assert not state.exists()


def test_pending_request_from_a_previous_process_is_discarded(tmp_path):
    """Um pedido salvo antes deste processo nascer já foi cumprido pelo boot.

    Era assim que o reinício voltava em loop: o processo novo lia o arquivo da
    sessão anterior e anunciava "reinício continua estacionado" outra vez.
    """
    state = tmp_path / "restart_pending.json"
    state.write_text(json.dumps({
        "kind": "assistant",
        "reason": "pedido da sessão anterior",
        "phrase": "confirmo reiniciar o assistente",
        "created_at": time.time() - 3600.0,
    }), encoding="utf-8")

    manager = RestartManager(dry_run=True, logger=mock.Mock(), state_path=state)

    assert manager.pending() is None
    assert not state.exists(), "o pedido antigo continuou em disco"
    assert "no restart awaiting" in manager.confirm(
        "confirmo reiniciar o assistente").lower()


def test_cancel_removes_the_persisted_request(tmp_path):
    state = tmp_path / "restart_pending.json"
    manager = RestartManager(dry_run=True, logger=mock.Mock(), state_path=state)
    manager.request_pc(reason="kernel")
    assert state.exists()

    manager.cancel()
    assert not state.exists()
    assert manager.pending() is None


def test_status_reports_expired_instead_of_nothing_pending(tmp_path, monkeypatch):
    state = tmp_path / "restart_pending.json"
    manager = RestartManager(dry_run=True, logger=mock.Mock(), state_path=state)
    action.bind(manager)
    manager.request_assistant(reason="teste de expiração")

    monkeypatch.setattr(restart, "CONFIRM_TIMEOUT_SECONDS", -1.0)
    status = action.restart_assistant({"action": "status"})

    assert "expired" in status.lower()
    assert "No restart is awaiting" not in status
    assert not state.exists()
    assert manager.last_expired()["kind"] == "assistant"
