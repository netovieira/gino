"""core/boot_audit.py — uma linha de proveniência do processo no Activity Log.

O reinício manual não deixava rastro: o dono fechou o app, clicou no ícone do
GNOME às 18:09 e nada nos logs do app registrava aquele nascimento — só o
journald sabia. É o que transformou discussão de horário em acusação de
mentira. No boot, a linha::

    SYS: boot origem=... pid=... hora=... ppid=... pai=... cgroup=...

resolve a pergunta "quem abriu este processo" com dados que já são públicos
(em ``ps``), no mesmo formato das linhas ``Gino online`` de ``logs/activity.log``.

Regras: uma linha só; nenhum valor de variável de ambiente; nenhum segredo;
sem ``/proc`` (sistema restrito, container sem procfs) não crasha — as peças
desaparecidas viram ``?`` e a origem vira ``desconhecida``.

Origem inferida (nesta ordem):
  * cgroup contém ``jarvis-relaunch``/``gino-relaunch`` (unidade transitória
    criada por core/restart.py) ou pai é ``systemd`` → relançamento automático;
  * pai é ``gnome-shell`` (ou cgroup do scope GNOME do app) → aberto pelo
    usuário pelo ícone;
  * pai é ``run-jarvis.sh``/shell → manual por script/terminal.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

PROC_ROOT = "/proc"

UNKNOWN_ORIGIN = "origem desconhecida"


def _read_text(path: Path) -> str:
    """Texto de um arquivo de procfs; peça que falta some, nunca estoura."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def parent_comm(ppid: int, proc_root: str = PROC_ROOT) -> str:
    """Nome do processo pai (``/proc/<ppid>/comm``); '' quando não há."""
    return _read_text(Path(proc_root, str(int(ppid)), "comm"))


def self_cgroup(proc_root: str = PROC_ROOT) -> str:
    """cgroup atual (primeira linha de ``/proc/self/cgroup``); '' se não há."""
    text = _read_text(Path(proc_root, "self", "cgroup"))
    return text.splitlines()[0] if text else ""


def infer_origin(parent_comm_name: str = "", cgroup: str = "") -> str:
    """Quem abriu este processo, pelo pai e pelo cgroup — nunca um palpite."""
    pc = str(parent_comm_name or "").lower()
    cg = str(cgroup or "").lower()
    if "jarvis-relaunch" in cg or "gino-relaunch" in cg or pc == "systemd":
        return "relançamento automático (helper)"
    if "gnome-shell" in pc or "gnome-shell" in cg or "app-gnome-jarvis" in cg:
        return "aberto pelo usuário (GNOME)"
    if "run-jarvis" in pc or pc in ("bash", "sh", "dash", "zsh"):
        return "manual por script (run-jarvis.sh)"
    return UNKNOWN_ORIGIN


def provenance_line(pid: int | None = None, now: float | None = None,
                    proc_root: str = PROC_ROOT) -> str:
    """A linha de proveniência, sem o prefixo ``SYS:`` nem timestamp do log.

    Contém PID, hora, PPID, nome do pai, cgroup e origem inferida. Funciona
    sem ``/proc``: cada leitura que falha vira ``?`` e a origem, o rótulo
    honesto de desconhecida.
    """
    pid = os.getpid() if pid is None else int(pid)
    try:
        ppid = os.getppid()
    except Exception:
        ppid = 0
    hora = time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(time.time() if now is None else now))
    pai = parent_comm(ppid, proc_root=proc_root)
    cg = self_cgroup(proc_root=proc_root)
    origem = infer_origin(pai, cg)
    return (f"boot origem={origem} pid={pid} hora={hora} ppid={ppid} "
            f"pai={pai or '?'} cgroup={cg or '?'}")
