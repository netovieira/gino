"""Run a command as root — authorised by the user's voice, not a typed password.

HOW IT IS AUTHORISED
    The user asks the assistant to do something that needs root, and says
    "pode usar sudo" (or "autorizo o sudo", "pode usar root"). That phrase is
    matched by core/rootgate.py BEFORE any model sees it and opens a short
    window (two minutes, single use). While the window is open, this tool runs
    `sudo -n` — passwordless, because the user already authorised it — and then
    consumes the window.

    No password is typed, stored or read. The sudoers line that makes `sudo -n`
    work is written once by ./autorizar-sudo.sh, which the user runs themselves.

THE RISK, STATED PLAINLY
    With passwordless sudo the ONLY gates are the phrase and this module's
    blocklist. The microphone hears the room, so the phrase window is kept short
    and single-use.

WHAT THIS MODULE REFUSES
    A short list of commands that destroy a machine outright (formatting a disk,
    wiping /, fork bombs). Those are refused before anything runs, because a
    spoken "yes" is not a safety net for `mkfs`.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess

from core import rootgate

# Root commands are executed as an argv list — there is no shell in the middle,
# so a model cannot smuggle `; cmd` or `$(cmd)` into the command string. The
# first token is restricted to tools that make sense for this assistant; a
# pipeline or shell syntax is refused and must be done manually.
_ALLOWED_ROOT_COMMANDS = {
    "apt", "apt-get", "dpkg", "snap", "flatpak", "systemctl", "service",
    "journalctl", "nmcli", "ufw", "iptables", "nft", "mount", "umount",
    "modprobe", "sysctl", "hostnamectl", "timedatectl", "localectl",
    "sed", "cp", "mv", "mkdir", "install", "chmod", "chown", "ln",
    "update-grub", "grub-install", "dconf",
}
_SHELL_SYNTAX = re.compile(r"[;&|`$<>\n\r]")

# The command waiting for the user's spoken "yes". Kept here because the whole
# point of the flow is that the request and the authorisation arrive as two
# separate utterances: Alice asks, the user answers, the command runs — without
# the user having to repeat the request.
_pending: dict | None = None


def has_pending() -> bool:
    return _pending is not None


def take_pending() -> tuple[str, str] | None:
    """Pop the waiting (command, reason), or None."""
    global _pending
    p, _pending = _pending, None
    return (p.get("command", ""), p.get("reason", "")) if p else None


def clear_pending() -> None:
    global _pending
    _pending = None

# Catastrophic shapes refused before anything runs. Deliberately narrow: this is
# a guard against the obviously unrecoverable, not a policy engine.
_REFUSE = (
    r"\brm\s+-[a-z]*[rf][a-z]*\s+/\s*$",     # rm -rf /
    r"\brm\s+-[a-z]*[rf][a-z]*\s+/\*",       # rm -rf /*
    r"\bmkfs\b",                              # format a filesystem
    r"\bdd\b.*\bof=/dev/",                    # write raw to a disk
    r":\s*\(\s*\)\s*\{",                      # fork bomb
    r">\s*/dev/[sh]d[a-z]",                   # clobber a disk
    r"\bchmod\s+-R\s+777\s+/(\s|$)",          # world-writable /
    r"\bchown\s+-R\b.*\s+/(\s|$)",
)


def _command_argv(command: str) -> list[str]:
    """Validate a root command and return its argv, without a shell."""
    raw = " ".join(str(command or "").splitlines()).strip()
    if not raw:
        raise ValueError("comando vazio")
    if "\x00" in raw:
        raise ValueError("comando com byte nulo")
    if _SHELL_SYNTAX.search(raw):
        raise ValueError("comandos com sintaxe de shell (; & | ` $ < >) "
                         "precisam ser executados manualmente no terminal")
    try:
        argv = shlex.split(raw)
    except ValueError as e:
        raise ValueError(f"aspas inválidas: {e}") from e
    if not argv:
        raise ValueError("comando vazio")
    name = os.path.basename(argv[0]).lower()
    if name == "sudo":
        raise ValueError("não inclua 'sudo'; esta ferramenta já executa como root")
    if name not in _ALLOWED_ROOT_COMMANDS:
        raise ValueError(f"comando '{name}' não está na lista de comandos root "
                         "permitidos")
    return argv


def _run_root(command: str) -> str:
    """Run one validated root command as argv — never through a shell."""
    try:
        argv = _command_argv(command)
    except ValueError as e:
        return f"recusei o comando: {e}"
    try:
        r = subprocess.run(["sudo", "-n", "--", *argv],
                           capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return f"o comando passou de 10 minutos e eu o interrompi: {command}"
    out = ((r.stdout or "") + ("\n" + r.stderr if r.stderr else "")).strip()
    if r.returncode != 0:
        low = out.lower()
        if "password is required" in low or "no password was provided" in low or "a terminal is required" in low:
            return ("o sudo sem senha ainda não está configurado — rode uma vez "
                    "./autorizar-sudo.sh (pede a sua senha só naquela hora).")
        return f"falhou (código {r.returncode}): {out[:600] or 'sem saída'}"
    return f"concluído como root: {out[:800] or 'sem saída'}"


def _looks_catastrophic(command: str) -> str:
    low = " ".join(str(command or "").lower().split())
    for pat in _REFUSE:
        if re.search(pat, low):
            return pat
    return ""


# ── a ferramenta ─────────────────────────────────────────────────────────────

def root_exec(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    command = str(params.get("command") or "").strip()
    reason = str(params.get("reason") or "").strip()

    if not command:
        return "qual comando eu rodo como root? diga o comando."
    if _looks_catastrophic(command):
        return (f"não executo esse comando nem com autorização sua: ele apaga ou "
                f"corrompe o sistema de forma irreversível ({command[:80]}).")
    try:
        _command_argv(command)
    except ValueError as e:
        return f"não executo esse comando: {e}"

    if not rootgate.authorized():
        global _pending
        _pending = {"command": command, "reason": reason}
        if reason:
            return (f"preciso da sua autorização para isto: {reason}. Diga "
                    f"'pode executar' ou 'tem minha autorização' e eu faço na hora.")
        return ("preciso da sua autorização para rodar isso como root. Diga "
                "'pode executar' ou 'tem minha autorização' e eu faço na hora.")

    result = _run_root(command)
    rootgate.consume()          # a spoken "yes" authorises one command
    return result


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────

TOOL = {
    "name": "root_exec",
    "description": (
        "Runs ONE shell command as root (administrator). It works ONLY while the "
        "user has just authorised it by saying 'pode usar sudo' / 'autorizo o "
        "sudo' — if they have not, this returns a sentence asking them to say it, "
        "and nothing runs. Use it for what genuinely needs root: installing or "
        "removing packages, editing files under /etc, restarting system "
        "services, changing system settings. One command at a time, without "
        "shell pipelines or redirections; those must be run manually. Never "
        "claim the command ran until the result says it did. Do NOT use it for "
        "things a normal tool can do: opening apps, volume, files in the home "
        "directory."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "command": {
                "type": "STRING",
                "description": "The exact shell command to run as root, without "
                               "the leading 'sudo' (e.g. 'apt-get install -y adb').",
            },
            "reason": {
                "type": "STRING",
                "description": "One short sentence, in the user's language, saying "
                               "what this command does and why it needs root. It "
                               "is shown on the confirmation banner.",
            },
        },
        "required": ["command"],
    },
    "handler": root_exec,
}
