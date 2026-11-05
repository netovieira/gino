"""Linha de proveniência do boot no Activity Log (defeito 3).

O clique manual no ícone do GNOME não deixava rastro nenhum nos logs do app.
A linha tem PID, hora, PPID, nome do pai, cgroup e origem inferida — e não
pode estourar quando /proc não existe.
"""
from __future__ import annotations

import os
import re
import time

from core import boot_audit


def test_provenance_line_has_pid_hour_and_origin():
    line = boot_audit.provenance_line()
    assert f"pid={os.getpid()}" in line
    assert "hora=" in line
    assert "origem=" in line
    assert "ppid=" in line
    assert re.search(r"hora=\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", line)


def test_provenance_line_without_proc_never_crashes():
    """(d) sem /proc disponível: peças viram '?' e a origem, desconhecida."""
    line = boot_audit.provenance_line(proc_root="/nonexistent-gino-proc")
    assert "pai=?" in line
    assert "cgroup=?" in line
    assert "origem=" in line
    assert "pid=" in line and "hora=" in line


def test_provenance_line_reads_fake_proc_tree(tmp_path, monkeypatch):
    (tmp_path / "4242").mkdir()
    (tmp_path / "4242" / "comm").write_text("gnome-shell\n")
    (tmp_path / "self").mkdir()
    (tmp_path / "self" / "cgroup").write_text(
        "0::/user.slice/user-1000.slice/"
        "user@1000.service/app.slice/app-gnome-jarvis-9.scope\n")
    monkeypatch.setattr(os, "getppid", lambda: 4242)
    line = boot_audit.provenance_line(pid=99, now=time.time(),
                                      proc_root=str(tmp_path))
    assert "pid=99" in line
    assert "ppid=4242" in line
    assert "pai=gnome-shell" in line
    assert "origem=aberto pelo usuário (GNOME)" in line
    assert "app-gnome-jarvis-9.scope" in line


def test_infer_origin_covers_the_three_real_cases():
    assert boot_audit.infer_origin(
        "systemd", "0::/user.slice/.../jarvis-relaunch-9-1.service") == \
        "relançamento automático (helper)"
    assert boot_audit.infer_origin("gnome-shell", "") == \
        "aberto pelo usuário (GNOME)"
    assert boot_audit.infer_origin("run-jarvis.sh", "") == \
        "manual por script (run-jarvis.sh)"
    assert boot_audit.infer_origin("python", "") == \
        boot_audit.UNKNOWN_ORIGIN


def test_line_has_no_env_values_and_is_one_line():
    line = boot_audit.provenance_line()
    assert "\n" not in line
    for token in ("PATH", "HOME=", "DISPLAY=", "XAUTH"):
        assert token not in line
