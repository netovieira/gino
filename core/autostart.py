"""
Iniciar JARVIS junto com a sessão do usuário.

No Linux não existe "registry", existe uma pasta: ~/.config/autostart. O GNOME,
o KDE e o XFCE leem os arquivos .desktop dali no login, e o mesmo arquivo
aparece na aba "Aplicativos de inicialização" das configurações do sistema.
Então ligar e desligar é criar e apagar um arquivo — sem root, sem systemd de
usuário, sem serviço escondido.

O Exec chama run-jarvis.sh com --tray: no login o assistente entra direto na
bandeja, com a janela escondida, ouvindo o "Hey Jarvis". Abrir uma janela em
cima de tudo às 8 da manhã é o comportamento que faz as pessoas desligarem o
autostart no segundo dia.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

FILE_NAME = "jarvis.desktop"


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def supported() -> bool:
    """Só o Linux está implementado — e é o sistema desta máquina."""
    return sys.platform.startswith("linux")


def entry_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / FILE_NAME


def is_enabled() -> bool:
    try:
        return entry_path().is_file()
    except Exception:
        return False


def _exec_line() -> str:
    root = _base_dir()
    script = root / "run-jarvis.sh"
    if script.is_file():
        cmd = f'"{script}" --tray'
    else:                       # instalação sem o .sh: chama o python do venv
        py = root / ".venv" / "bin" / "python"
        exe = py if py.is_file() else Path(sys.executable)
        cmd = f'"{exe}" "{root / "main.py"}" --tray'
    return cmd


def _entry_text() -> str:
    root = _base_dir()
    icon = root / "config" / "jarvis.ico"
    icon_line = f"Icon={icon}\n" if icon.is_file() else ""
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=JARVIS\n"
        "Comment=Assistente de voz (fica na bandeja; diga 'Jarvis' para acordar)\n"
        f"Exec={_exec_line()}\n"
        f"Path={root}\n"
        f"{icon_line}"
        "Terminal=false\n"
        "StartupNotify=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def enable() -> tuple[bool, str]:
    """Cria a entrada de autostart. Devolve (ok, mensagem)."""
    if not supported():
        return False, f"autostart ainda não implementado neste sistema ({sys.platform})."
    path = entry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_entry_text(), encoding="utf-8")
        try:
            path.chmod(0o755)
        except OSError:
            pass
    except Exception as e:
        return False, f"não consegui escrever {path}: {e}"
    return True, ("JARVIS vai abrir junto com o sistema, direto na bandeja. "
                  "Desligue em Configurações → Aplicativos de inicialização.")


def disable() -> tuple[bool, str]:
    if not supported():
        return False, f"autostart não implementado em {sys.platform}."
    path = entry_path()
    try:
        path.unlink(missing_ok=True)
    except Exception as e:
        return False, f"não consegui apagar {path}: {e}"
    return True, "JARVIS não abre mais sozinho no login."


def toggle() -> tuple[bool, str]:
    return disable() if is_enabled() else enable()
