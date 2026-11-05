"""Construção do helper de relançamento fora do cgroup do app (defeito 1).

Prova que o mecanismo escolhido é a unidade transitória ``systemd-run --user``
(Type=oneshot, --no-block, --collect) com o script certo, e que as fugas
alternativas têm o formato esperado. Nada aqui lança processo real.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.restart as restart  # noqa: E402
from core.restart import (RestartManager, _cgroup_escape_leaf,  # noqa: E402
                          _helper_command, _helper_unit_name,
                          _move_pid_out_of_app_cgroup,
                          systemd_run_helper_argv)


def test_helper_command_still_waits_for_the_app_pid_and_relaunches():
    script = _helper_command(4242)
    assert "kill -0 4242" in script
    assert f"exec {shlex.quote(restart.RUN_SCRIPT)} --tray" in script


def test_helper_unit_name_is_unique_per_invocation_and_marked():
    a = _helper_unit_name(4242)
    b = _helper_unit_name(4242)
    assert a.startswith("jarvis-relaunch-4242-")
    assert isinstance(a, str)


def test_systemd_run_argv_is_the_chosen_escape_mechanism():
    script = _helper_command(4242)
    argv = systemd_run_helper_argv(script, "jarvis-relaunch-4242-1")
    assert argv[0] == "systemd-run"
    assert "--user" in argv and "--no-block" in argv and "--quiet" in argv
    assert "--collect" in argv
    assert "--property=Type=oneshot" in argv
    assert "--unit=jarvis-relaunch-4242-1" in argv
    # comando real: /bin/sh -c <script> no fim do argv
    assert argv[-3:] == ["/bin/sh", "-c", script]


def test_manager_builds_helper_with_systemd_run(tmp_path):
    """(a) o helper é construído com o mecanismo de escape escolhido."""
    run = mock.Mock(return_value=mock.Mock(returncode=0))
    with mock.patch.object(restart.subprocess, "run", run), \
            mock.patch.object(restart.subprocess, "Popen") as popen:
        manager = RestartManager(dry_run=False, logger=mock.Mock(),
                                 state_path=tmp_path / "p.json")
        manager.bind(shutdown_cb=mock.Mock())
        manager.confirm_owner(reason="teste de mecanismo")
    popen.assert_not_called()
    argv = run.call_args[0][0]
    assert argv[:2] == ["systemd-run", "--user"]
    assert argv[-1] == _helper_command(__import__("os").getpid())


def test_cgroup_leaf_path_is_under_delegated_user_manager():
    leaf = _cgroup_escape_leaf(4242)
    parts = str(leaf)
    assert parts.startswith("/sys/fs/cgroup/user.slice/user-")
    assert "/user@1000.service/gino-relaunch-4242-" in parts or \
           "/user@0.service/gino-relaunch-4242-" in parts or \
           f"/user@{__import__('os').getuid()}.service/" in parts


def test_move_pid_out_of_app_cgroup_fails_quietly_without_sysfs():
    """Sem a árvore delegada (ou sem privilégio) a fuga devolve False, nunca estoura."""
    with mock.patch.object(restart, "_cgroup_escape_leaf",
                           lambda _pid: Path("/nonexistent-gino/leaf")):
        assert _move_pid_out_of_app_cgroup(1234) is False
