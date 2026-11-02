"""Abre um terminal visível na tela e roda um comando de shell.

O dono vê a janela e o comando acontecendo, em vez de um ``subprocess`` mudo.
Preferência: ptyxis (GNOME), com ``--`` separando opções dos argumentos do
comando e ``--working-directory`` ANTES do ``--``. Fallback:
``x-terminal-emulator -e bash -lc``. Em ambos os casos ``hold=True`` (padrão)
termina com ``exec bash`` para a janela não fechar quando o comando acaba.

Nunca usa shell=True no Popen: o comando roda dentro do bash do terminal,
que é justamente o shell pedido. O processo é destacado em uma sessão nova.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PTYXIS = "ptyxis"
X_TERMINAL = "x-terminal-emulator"


def _available(name: str) -> str | None:
    """Nome curto se o terminal estiver no PATH, senão None."""
    return name if shutil.which(name) else None


def _hold_enabled(value) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() not in {
        "0", "false", "nao", "não", "no", "off", "",
    }


def _ptyxis_argv(binary: str, script: str, cwd: str | None,
                 title: str | None = None) -> list[str]:
    # `ptyxis [OPÇÃO…] [-- ARGUMENTOS DO COMANDO]`
    argv = [binary]
    if title:
        argv += ["--title", title]
    if cwd:
        argv += ["--working-directory", cwd]
    return argv + ["--", "bash", "-lc", script]


def _xterm_argv(binary: str, script: str,
                title: str | None = None) -> list[str]:
    argv = [binary]
    if title:
        argv += ["-T", title]
    return argv + ["-e", "bash", "-lc", script]


def open_terminal(parameters) -> dict:
    """Abre a janela e devolve ``{"ok", "message", "pid"}``.

    ``pid`` é o processo da janela que o app abriu (o cliente do ptyxis xterm).
    Quem precisa fechar a janela depois — o espelho de execução — mata esse pid
    em vez de adivinhar títulos de janela no gerenciador.
    """
    p = parameters if isinstance(parameters, dict) else {}

    command = str(p.get("command") or "").strip()
    if not command:
        return {"ok": False, "pid": None,
                "message": "Informe o comando para rodar no terminal."}

    raw_cwd = str(p.get("cwd") or "").strip()
    cwd = os.path.abspath(os.path.expanduser(raw_cwd)) if raw_cwd else None
    if cwd and not Path(cwd).is_dir():
        return {"ok": False, "pid": None,
                "message": f"Diretório não encontrado: {cwd}"}

    script = command + ("; exec bash" if _hold_enabled(p.get("hold")) else "")
    title = str(p.get("title") or "").strip()[:80] or None

    ptyxis = _available(PTYXIS)
    if ptyxis:
        argv = _ptyxis_argv(ptyxis, script, cwd, title=title)
    else:
        xterm = _available(X_TERMINAL)
        if not xterm:
            return {"ok": False, "pid": None,
                    "message": "Não achei ptyxis nem x-terminal-emulator "
                               "para abrir a tela."}
        argv = _xterm_argv(xterm, script, title=title)
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=cwd or None,
            start_new_session=True,
        )
    except Exception as exc:
        return {"ok": False, "pid": None,
                "message": f"Não consegui abrir o terminal: {exc}"}

    try:
        pid = int(getattr(proc, "pid", 0) or 0) or None
    except Exception:
        pid = None
    return {"ok": True, "pid": pid,
            "message": f"Abri o terminal na tela e estou rodando: {command}."}


def terminal_run(parameters, player=None, speak=None, session_memory=None) -> str:
    return str(open_terminal(parameters).get("message") or "")


TOOL = {
    "name": "terminal_run",
    "description": (
        "Abre um terminal visível na tela e roda um comando de shell dentro "
        "dele, para o dono acompanhar a saída ao vivo. Use quando ele pedir "
        "'abre um terminal e roda X', instalar algo, ver logs ou executar um "
        "comando demorado que precisa ficar visível. Passe cwd para rodar "
        "dentro de uma pasta e hold=false para a janela fechar ao terminar "
        "(padrão: true, deixa o bash aberto). Não use para comandos que devem "
        "ser silenciosos."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "command": {
                "type": "STRING",
                "description": "Comando de shell a executar no terminal.",
            },
            "cwd": {
                "type": "STRING",
                "description": "Pasta onde o comando roda (opcional).",
            },
            "hold": {
                "type": "BOOLEAN",
                "description": "true (padrão) deixa o bash aberto ao terminar; "
                               "false fecha a janela.",
            },
            "title": {
                "type": "STRING",
                "description": "Título da janela/aba (opcional).",
            },
        },
        "required": ["command"],
    },
    "handler": terminal_run,
    "behavior": "NON_BLOCKING",
    "scheduling": "WHEN_IDLE",
}
