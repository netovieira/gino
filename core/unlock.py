"""Opt-in voice unlock: retrieve a stored credential and type it for the user.

SECURITY MODEL
    This does not bypass the lock screen. The user stores the account password
    once, explicitly, in the desktop keyring (Secret Service). On a confirmed
    voice request from the owner, this module retrieves that secret and types it
    into the active session. The OS lock screen still decides whether to accept
    it; on many setups synthetic input is rejected, which is a feature.

    The secret is never written to config, logs, argv or the repository. The
    keyring item is readable by any process running in this user session while
    the login keyring is unlocked — that is the price of delegation. Prefer a
    fingerprint or the OS password prompt when real security matters.

PREREQUISITES
    - keyring (Secret Service backend; installed in .venv)
    - one of: ydotool (Wayland/uinput, needs /dev/uinput access), xdotool (X11),
      wtype (Wayland virtual keyboard; may be refused by the locker)
    - loginctl (systemd)
    Run scripts/setup-voice-unlock.sh once to store the credential.
"""
from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import time
from typing import Optional

SERVICE = "jarvis-unlock"


def _user() -> str:
    return (os.environ.get("USER") or os.environ.get("USERNAME") or "user").strip()


def _session_id() -> str:
    sid = str(os.environ.get("XDG_SESSION_ID") or "").strip()
    if sid:
        return sid
    try:
        out = subprocess.run(["loginctl", "list-sessions", "--no-legend"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == _user():
                return parts[0]
    except Exception:
        pass
    return ""


def session_locked() -> Optional[bool]:
    """True/False when logind can tell us, None when it cannot."""
    sid = _session_id()
    if not sid:
        return None
    try:
        out = subprocess.run(
            ["loginctl", "show-session", sid, "-p", "LockedHint", "--value"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip().lower()
    except Exception:
        return None
    if out in ("yes", "true", "1"):
        return True
    if out in ("no", "false", "0"):
        return False
    return None


def typing_tools() -> list[str]:
    return [tool for tool in ("ydotool", "xdotool", "wtype") if shutil.which(tool)]


def _run(cmd: list[str], input_text: Optional[str] = None, timeout: float = 20.0) -> None:
    r = subprocess.run(cmd, input=input_text, capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(f"{cmd[0]} falhou: {detail[:200]}")


def type_secret(secret: str) -> None:
    """Type the secret + Return without putting it in argv."""
    if not secret:
        raise RuntimeError("credencial vazia")
    if shutil.which("ydotool"):
        # uinput-level virtual keyboard; the lock screen accepts it if the
        # device permission is configured.
        _run(["ydotool", "type", "--file", "-"], input_text=secret)
        _run(["ydotool", "key", "28"])
        return
    if shutil.which("xdotool"):
        _run(["xdotool", "type", "--clearmodifiers", "--file", "-"], input_text=secret)
        _run(["xdotool", "key", "Return"])
        return
    if shutil.which("wtype"):
        _run(["wtype", "-"], input_text=secret)
        _run(["wtype", "-k", "Return"])
        return
    raise RuntimeError("nenhum injetor de teclado instalado (ydotool, xdotool ou wtype)")


def get_secret() -> str:
    try:
        import keyring
        # Do not strip: spaces are part of a password.
        return str(keyring.get_password(SERVICE, _user()) or "")
    except Exception:
        return ""


def configured() -> bool:
    return bool(get_secret())


def store_secret(secret: str) -> None:
    secret = str(secret or "")
    if not secret:
        raise ValueError("senha vazia")
    import keyring
    keyring.set_password(SERVICE, _user(), secret)


def store_from_prompt() -> None:
    first = getpass.getpass("Senha da conta (não será exibida): ")
    if not first:
        print("Nada foi armazenado.")
        return
    second = getpass.getpass("Repita a senha: ")
    if first != second:
        print("As senhas não coincidem; nada foi armazenado.")
        return
    try:
        store_secret(first)
    except Exception as e:
        print(f"Não consegui armazenar no keyring: {e}")
        return
    finally:
        first = second = ""
    print("Credencial armazenada no keyring (serviço jarvis-unlock).")
    print("O desbloqueio por voz ainda depende de o locker aceitar a digitação;")
    print("para Wayland instale ydotool e libere /dev/uinput para o seu usuário.")


def delete_secret() -> None:
    try:
        import keyring
        keyring.delete_password(SERVICE, _user())
        print("Credencial removida do keyring.")
    except Exception as e:
        print(f"Não havia credencial ou não consegui remover: {e}")


def unlock() -> str:
    """Try logind first; if still locked, type the stored credential."""
    locked = session_locked()
    if locked is False:
        return "A sessão já está desbloqueada."
    try:
        sid = _session_id()
        cmd = ["loginctl", "unlock-session"] + ([sid] if sid else [])
        _run(cmd, timeout=10)
        time.sleep(0.7)
        locked = session_locked()
        if locked is False:
            return "Tela desbloqueada."
    except Exception:
        locked = session_locked()

    # Never type a password when we cannot positively confirm the screen is
    # locked: an unknown state could type it into whatever window is focused.
    if locked is not True:
        raise RuntimeError("não consegui confirmar que a tela está bloqueada; "
                           "não vou digitar a senha no escuro")

    secret = get_secret()
    if not secret:
        raise RuntimeError("desbloqueio por voz não configurado; rode "
                           "scripts/setup-voice-unlock.sh no terminal")
    try:
        type_secret(secret)
    finally:
        secret = ""
    time.sleep(0.8)
    if session_locked() is False:
        return "Tela desbloqueada."
    raise RuntimeError("o locker não aceitou a digitação; verifique ydotool e "
                       "/dev/uinput (ou use a senha/fingerprint)")


def status() -> dict:
    return {
        "user": _user(),
        "session": _session_id(),
        "locked": session_locked(),
        "credential": configured(),
        "typing_tools": typing_tools(),
        "loginctl": bool(shutil.which("loginctl")),
    }


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "store":
        store_from_prompt()
    elif cmd == "delete":
        delete_secret()
    elif cmd == "status":
        import json
        print(json.dumps(status(), indent=2))
    elif cmd == "unlock":
        try:
            print(unlock())
        except Exception as e:
            print(f"Falhou: {e}")
            return 1
    else:
        print("uso: python -m core.unlock [store|delete|status|unlock]")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
