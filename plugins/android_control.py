"""Projetor / Android TV pelo ADB — a Alice controla de verdade.

O Android expõe, por ADB, tudo que o iPhone esconde no Linux: toque, swipe,
tecla do controle remoto, digitar texto, lançar/matar app, screenshot,
screenrecord, logcat, push/pull arquivo. Não precisa de macOS nem de túnel.

Pré-requisitos (uma vez só):
    ./instalar-adb.sh          # instala o `adb` do lado do Linux
    ./parear-android.sh        # `adb pair` + `adb connect` no projetor
                               #   (ou pluga por USB e aceita o "Permitir
                               #   depuração USB?" no projetor)

Config (config/api_keys.json → plugin_config.android):
    {
      "serial": "192.168.0.42:5555",   // quando há mais de um device
      "allow_shell": false,            // libera action='shell'
      "label": "projetor"              // opcional; como a Alice chama o device
    }

Notas
-----
• Projetores Android TV geralmente NÃO têm tela touch — `tap`/`swipe` aceitam
  mas não fazem nada. O caminho real é `dpad up|down|left|right|center` e as
  teclas do controle remoto (back/home/menu/vol+/vol-/power). A descrição da
  ferramenta deixa isso claro pro modelo, que escolhe a ação certa.
• `screenshot` usa `adb exec-out screencap -p` — retorna PNG cru direto pro
  stdout, sem arquivo intermediário no device.
• `screenrecord` grava em /sdcard/ no device, puxa pro `memory/android/` e
  apaga o original. Fica limitado a 180 s (o Android mesmo corta em 180).
• `logs` faz `logcat -d` (dump + sai) com filtro por tag e substring.
• `launch`/`kill` por package name. Detecta automaticamente o intent
  LEANBACK_LAUNCHER do Android TV quando o LAUNCHER padrão não existe.
"""
from __future__ import annotations

import io
import re
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path

NS = "android"


PLUGIN = {
    "name": "android_control",
    "description": (
        "The user's Android device — a projector / Android TV / phone / tablet "
        "— driven over ADB (USB or Wi-Fi). Real control (unlike iPhone): tap, "
        "swipe, DPAD (up/down/left/right/center), hardware keys (back, home, "
        "menu, volume, power), type text, launch/kill an app by package name, "
        "open an http/youtube URL, list installed apps, capture a screenshot "
        "(described by the configured vision backend, Qoder by default), record "
        "a short mp4 of the "
        "screen, tail logcat, push/pull files. For projectors and Android TV "
        "prefer dpad / keypress / open_url over tap — the screen usually "
        "isn't touch-sensitive. If the user says 'projetor', 'TV', 'Android "
        "TV', or names an Android device, use this tool. If no device is "
        "connected, say so plainly instead of guessing."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "status | connect | tap | swipe | dpad | keypress | type | "
                    "screenshot | screenrecord | launch | kill | apps | logs | "
                    "open_url | push | pull | power | shell"
                ),
            },
            "target": {
                "type": "STRING",
                "description": (
                    "launch/kill: package name (e.g. com.netflix.ninja). "
                    "open_url: URL (http://…, https://…, or "
                    "https://www.youtube.com/tv#/watch?v=ID). "
                    "dpad: up|down|left|right|center. keypress: back|home|menu"
                    "|power|volumeup|volumedown|mute|enter|del|tab|info|guide|"
                    "playpause|rewind|forward|next|prev. type/push/pull/shell: "
                    "the text/path/command."
                ),
            },
            "x":  {"type": "INTEGER", "description": "x for tap/swipe start"},
            "y":  {"type": "INTEGER", "description": "y for tap/swipe start"},
            "x2": {"type": "INTEGER", "description": "x for swipe end"},
            "y2": {"type": "INTEGER", "description": "y for swipe end"},
            "duration_ms": {"type": "INTEGER", "description": "swipe duration (default 300 ms)"},
            "seconds": {"type": "INTEGER", "description": "screenrecord seconds (default 6, max 180)"},
            "query":   {"type": "STRING",  "description": "screenshot: question about the image; logs: substring filter"},
            "limit":   {"type": "INTEGER", "description": "logs: how many lines (default 40)"},
            "path":    {"type": "STRING",  "description": "push/pull remote path (Android side)"},
        },
        "required": ["action"],
    },
}


# ── helpers de ADB ───────────────────────────────────────────────────────────

def _cfg() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        return get_plugin_config(NS) or {}
    except Exception:
        return {}


def _adb_bin() -> str | None:
    import shutil
    return shutil.which("adb")


def _has_adb_or_die() -> str:
    if not _adb_bin():
        return "rode ./instalar-adb.sh no Linux (falta o binário `adb`)"
    return ""


def _serial_or_none() -> str:
    """Serial pinado no config; se vazio, deixa o adb escolher (só ok com um device)."""
    return str(_cfg().get("serial") or "").strip()


def _adb_devices_raw() -> list[str]:
    """Linhas de `adb devices` já filtradas, formato 'serial\\tdevice'."""
    adb = _adb_bin()
    if not adb:
        return []
    try:
        r = subprocess.run([adb, "devices"], capture_output=True, text=True,
                           timeout=6)
    except Exception:
        return []
    lines = r.stdout.splitlines()[1:]  # pula 'List of devices attached'
    return [ln for ln in lines if ln.strip() and "\tdevice" in ln]


def _pick_serial() -> tuple[str, str]:
    """Retorna (serial, motivo_do_erro). Vazio se não houver nenhum."""
    pinned = _serial_or_none()
    devices = _adb_devices_raw()
    if not devices:
        return "", ("nenhum Android conectado — pluga por USB e aceita o diálogo "
                    "'Permitir depuração USB?', ou roda ./parear-android.sh pro "
                    "Wi-Fi")
    serials = [ln.split("\t")[0] for ln in devices]
    if pinned and pinned in serials:
        return pinned, ""
    if len(serials) == 1:
        return serials[0], ""
    if pinned:
        return "", (f"o serial '{pinned}' configurado não está conectado agora. "
                    f"Vistos: {', '.join(serials)}")
    return "", ("há mais de um Android conectado; escolhe um no ⚙ → PLUGIN "
                f"SETTINGS → ANDROID (serial). Vistos: {', '.join(serials)}")


def _adb(args: list[str], timeout: float = 15.0) -> tuple[int, bytes, str]:
    """`adb -s <serial> <args>` com stdout binário (pra screencap) e stderr texto.

    Args sem `serial` (ex.: `devices`) são chamados sem o `-s` automaticamente.
    """
    adb = _adb_bin()
    if not adb:
        return 127, b"", "adb não está instalado"
    cmd = [adb]
    if args and args[0] not in ("devices", "version", "start-server", "kill-server"):
        serial, why = _pick_serial()
        if why:
            return 78, b"", why
        cmd += ["-s", serial]
    cmd += args
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.returncode, r.stdout or b"", (r.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or b""), "timeout"
    except Exception as e:
        return 1, b"", str(e)


def _shell(cmd: str, timeout: float = 20.0) -> tuple[int, bytes, str]:
    return _adb(["shell", cmd], timeout=timeout)


def _dir_out() -> Path:
    d = Path(__file__).resolve().parent.parent / "memory" / "android"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _label() -> str:
    return str(_cfg().get("label") or "Android").strip() or "Android"


# ── ações ────────────────────────────────────────────────────────────────────

def _do_status() -> tuple[str, str]:
    why = _has_adb_or_die()
    if why:
        return (why, why)
    serial, w = _pick_serial()
    if w:
        return (w, w)
    rc, out, err = _shell("getprop ro.product.model; getprop ro.build.version.release; "
                          "getprop ro.product.manufacturer; wm size 2>/dev/null; "
                          "dumpsys power 2>/dev/null | grep -E 'mWakefulness=' | head -1",
                          timeout=10)
    lines = [ln.strip() for ln in out.decode("utf-8","replace").splitlines() if ln.strip()]
    if rc != 0 or not lines:
        return (f"{serial} conectado mas eu não consegui ler o perfil dele", err or "")
    model  = lines[0]
    versao = lines[1] if len(lines) > 1 else "?"
    fab    = lines[2] if len(lines) > 2 else "?"
    size   = next((ln.split(":")[1].strip() for ln in lines if ln.startswith("Physical size")), "?")
    wake   = next((ln.split("=")[1].strip() for ln in lines if "mWakefulness=" in ln), "?")
    body = "\n".join(lines)
    spoken = (f"{_label()} conectado: {fab} {model}, Android {versao}, tela {size}, "
              f"estado {wake} (serial {serial})")
    return (spoken, body)


def _do_tap(x: int, y: int) -> tuple[str, str]:
    rc, _o, err = _shell(f"input tap {int(x)} {int(y)}")
    if rc != 0:
        return (f"o tap em ({x},{y}) falhou: {err.strip()[:150]}", err)
    return (f"toquei em ({x},{y}).", "input tap ok")


def _do_swipe(x1: int, y1: int, x2: int, y2: int, dur: int) -> tuple[str, str]:
    dur = max(60, min(3000, int(dur or 300)))
    rc, _o, err = _shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {dur}")
    if rc != 0:
        return (f"o swipe ({x1},{y1})→({x2},{y2}) falhou: {err.strip()[:150]}", err)
    return (f"deslizei de ({x1},{y1}) até ({x2},{y2}).", "input swipe ok")


_DPAD_MAP = {
    "up": "19", "down": "20", "left": "21", "right": "22", "center": "23",
}


def _do_dpad(name: str) -> tuple[str, str]:
    key = _DPAD_MAP.get((name or "").lower().strip())
    if not key:
        return ("diz up, down, left, right ou center", "dpad inválido")
    rc, _o, err = _shell(f"input keyevent {key}")
    if rc != 0:
        return (f"não consegui mandar dpad {name}: {err.strip()[:150]}", err)
    return (f"mandei {name.upper()} no direcional.", "ok")


_KEY_MAP = {
    "back": "4", "home": "3", "menu": "82", "power": "26",
    "volumeup": "24", "volumedown": "25", "mute": "164",
    "enter": "66", "del": "67", "tab": "61",
    "info": "166", "guide": "168", "playpause": "85",
    "rewind": "88", "forward": "87", "next": "87", "prev": "88",
    "search": "84", "appswitch": "187",
}


def _do_keypress(name: str) -> tuple[str, str]:
    n = (name or "").lower().strip()
    key = _KEY_MAP.get(n)
    if not key:
        return (f"não conheço a tecla '{n}'. Uso: " +
                ", ".join(sorted(_KEY_MAP))), "key inválida"
    rc, _o, err = _shell(f"input keyevent {key}")
    if rc != 0:
        return (f"tecla {n} falhou: {err.strip()[:150]}", err)
    return (f"apertei {n}.", f"keyevent {key}")


def _do_type(text: str) -> tuple[str, str]:
    if not text:
        return ("o que eu digito?", "vazio")
    # `input text` não lida bem com espaços/acentos; faz URL-encode em %s
    safe = shlex.quote(text.replace(" ", "%s"))
    rc, _o, err = _shell(f"input text {safe}")
    if rc != 0:
        return (f"não consegui digitar: {err.strip()[:150]}", err)
    return (f"digitei \"{text[:40]}\" no campo ativo.", "ok")


def _do_screenshot(query: str) -> tuple[str, str]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rc, png, err = _adb(["exec-out", "screencap", "-p"], timeout=20)
    if rc != 0 or not png:
        return (f"não consegui tirar screenshot do {_label()}: "
                f"{err.strip()[:150] or 'sem dados'}", err)
    local = _dir_out() / f"tela-{stamp}.png"
    local.write_bytes(png)
    body = f"screenshot salva em {local}"
    if not query:
        return (f"capturei a tela do {_label()} e salvei em {local.name}", body)
    try:
        from core import brain as _b
        from memory.config_manager import get_plugin_config
        st = _b.settings(get_plugin_config)
        if not st.get("vision_enabled", True):
            return (f"png em {local.name} (visão desligada — "
                    "ligue em ECONOMY BRAIN → Vision provider)", body)
        q = (f"{query}\n\nResponda em português, em uma frase curta, apenas com "
             "o que você REALMENTE vê nesta imagem.")
        desc = _b.vision_describe(png, "image/png", q, st) or ""
    except Exception as e:
        return (f"png em {local.name}, mas a visão falhou: {e}", str(e))
    if not desc:
        return (f"png em {local.name}, o modelo de visão não respondeu", body)
    return (desc.strip(), f"{body}\nvisão: {desc.strip()}")


def _do_screenrecord(seconds: int) -> tuple[str, str]:
    secs = max(1, min(180, int(seconds or 6)))
    name = f"video-{datetime.now().strftime('%Y%m%d-%H%M%S')}.mp4"
    remote = f"/sdcard/{name}"
    # screenrecord bloqueia até `--time-limit` estourar; damos folga no timeout.
    rc, _o, err = _shell(f"screenrecord --time-limit {secs} {shlex.quote(remote)}",
                         timeout=secs + 15)
    if rc != 0:
        return (f"não consegui gravar a tela do {_label()}: {err.strip()[:150]}", err)
    # `adb pull` é mais confiável que `exec-out cat` para mp4.
    local = _dir_out() / name
    prc, _po, perr = _adb(["pull", remote, str(local)], timeout=60)
    _shell(f"rm -f {shlex.quote(remote)}", timeout=8)
    if prc != 0 or not local.exists():
        return (f"gravei {secs}s mas o pull falhou: {perr.strip()[:150]}", perr)
    kb = local.stat().st_size // 1024
    return (f"gravei {secs}s da tela do {_label()} em {local.name} ({kb} KB).",
            f"pull ok, {local}")


def _do_launch(pkg: str) -> tuple[str, str]:
    if not pkg:
        return ("qual app abrir? me dá o package name (ex.: com.netflix.ninja)",
                "sem package")
    # Android TV: tenta o intent LEANBACK_LAUNCHER primeiro; se não achar, cai
    # no LAUNCHER padrão. `monkey -p pkg 1` também funciona e escolhe o certo.
    rc, _o, err = _shell(
        f"monkey -p {shlex.quote(pkg)} -c android.intent.category.LEANBACK_LAUNCHER 1",
        timeout=10)
    if rc == 0:
        return (f"abri {pkg} no {_label()}.", "leanback ok")
    rc2, _o2, err2 = _shell(
        f"monkey -p {shlex.quote(pkg)} -c android.intent.category.LAUNCHER 1",
        timeout=10)
    if rc2 == 0:
        return (f"abri {pkg} no {_label()}.", "launcher ok")
    return (f"não consegui abrir {pkg}: {(err2 or err).strip()[:180]}",
            err + "\n" + err2)


def _do_kill(pkg: str) -> tuple[str, str]:
    if not pkg:
        return ("qual app fechar?", "sem package")
    rc, _o, err = _shell(f"am force-stop {shlex.quote(pkg)}")
    if rc != 0:
        return (f"não consegui fechar {pkg}: {err.strip()[:150]}", err)
    return (f"fechei {pkg}.", "force-stop ok")


def _do_apps() -> tuple[str, str]:
    rc, out, err = _shell("pm list packages -3", timeout=15)
    if rc != 0:
        return (f"não consegui listar apps: {err.strip()[:150]}", err)
    pkgs = [ln.split(":", 1)[1].strip() for ln in
            out.decode("utf-8","replace").splitlines() if ln.startswith("package:")]
    if not pkgs:
        return ("nenhum app de usuário instalado no device", "")
    head = ", ".join(pkgs[:8])
    return (f"{len(pkgs)} apps de usuário. Exemplos: {head}", "\n".join(pkgs))


def _do_logs(query: str, limit: int) -> tuple[str, str]:
    limit = max(5, min(500, int(limit or 40)))
    rc, out, err = _shell("logcat -d -t 200", timeout=15)
    if rc != 0:
        return (f"logcat falhou: {err.strip()[:150]}", err)
    lines = out.decode("utf-8", "replace").splitlines()
    if query:
        low = query.lower()
        lines = [ln for ln in lines if low in ln.lower()]
    keep = lines[-limit:]
    if not keep:
        return (f"nenhuma linha de log do {_label()}"
                + (f" com '{query}'" if query else "") + " agora", "")
    body = "\n".join(keep)
    spoken = (f"peguei {len(keep)} linhas de log"
              + (f" filtrando por '{query}'" if query else "") + ": " + keep[-1])
    return (spoken, body)


def _do_open_url(url: str) -> tuple[str, str]:
    if not url:
        return ("qual URL abrir?", "vazio")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", url):
        url = "https://" + url
    rc, _o, err = _shell(
        f"am start -a android.intent.action.VIEW -d {shlex.quote(url)}",
        timeout=15)
    if rc != 0:
        return (f"o device não abriu a URL: {err.strip()[:180]}", err)
    return (f"abri {url} no {_label()}.", "am start ok")


def _do_push(local_path: str, remote: str) -> tuple[str, str]:
    if not local_path or not Path(local_path).expanduser().exists():
        return (f"não achei o arquivo local '{local_path}'", "arquivo ausente")
    remote = remote or f"/sdcard/{Path(local_path).name}"
    rc, _o, err = _adb(["push", str(Path(local_path).expanduser()), remote],
                       timeout=60)
    if rc != 0:
        return (f"push falhou: {err.strip()[:180]}", err)
    return (f"enviei {Path(local_path).name} pro device em {remote}.", "push ok")


def _do_pull(remote: str, local_path: str) -> tuple[str, str]:
    if not remote:
        return ("o que eu puxo do device? me passa o caminho remoto", "sem remote")
    local_path = local_path or str(_dir_out() / Path(remote).name)
    rc, _o, err = _adb(["pull", remote, local_path], timeout=60)
    if rc != 0:
        return (f"pull falhou: {err.strip()[:180]}", err)
    return (f"baixei {remote} pra {local_path}.", "pull ok")


def _do_power(mode: str) -> tuple[str, str]:
    m = (mode or "").lower().strip()
    if m in ("on", "wake", "acender"):
        rc, _o, err = _shell("input keyevent KEYCODE_WAKEUP 224")
        tail = "acendi a tela."
    elif m in ("off", "sleep", "apagar"):
        rc, _o, err = _shell("input keyevent KEYCODE_SLEEP 223")
        tail = "apaguei a tela."
    elif m in ("toggle", "", "alternar"):
        rc, _o, err = _shell("input keyevent POWER")
        tail = "alternei o estado da tela."
    else:
        return ("power aceita on, off ou toggle", "modo inválido")
    if rc != 0:
        return (f"falha no power ({m}): {err.strip()[:150]}", err)
    return (tail, "ok")


def _do_shell(cmd: str) -> tuple[str, str]:
    if not _cfg().get("allow_shell"):
        return ("shell remoto está desligada. Liga 'allow_shell' em ⚙ → PLUGIN "
                "SETTINGS → ANDROID pra liberar.", "allow_shell off")
    if not cmd:
        return ("qual comando eu rodo?", "vazio")
    rc, out, err = _shell(cmd, timeout=25)
    body = (out.decode("utf-8","replace") + ("\n" + err if err else ""))[:4000]
    first = (out.decode("utf-8","replace").splitlines() or ["(sem saída)"])[0]
    return (f"rodei no device (rc={rc}): {first[:240]}", body)


# ── botão ⚙ ─────────────────────────────────────────────────────────────────

def _test_action(values: dict | None = None) -> tuple[bool, str]:
    if values:
        try:
            from memory.config_manager import save_plugin_config
            save_plugin_config(NS, values)
        except Exception:
            pass
    why = _has_adb_or_die()
    if why:
        return False, why
    devices = _adb_devices_raw()
    if not devices:
        return False, "nenhum Android online — pluga o USB ou roda ./parear-android.sh"
    serials = ", ".join(ln.split("\t")[0] for ln in devices)
    serial, err = _pick_serial()
    if err:
        return False, err
    rc, out, _ = _shell("getprop ro.product.model")
    model = out.decode("utf-8", "replace").strip() or "?"
    return True, f"{len(devices)} device(s): {serials}; ativo={serial} ({model})"


# ── dispatcher ──────────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower()
    target = str(p.get("target") or "").strip()
    query  = str(p.get("query") or "").strip()
    path   = str(p.get("path") or "").strip()

    def _num(name: str, default: int = 0) -> int:
        try:
            return int(p.get(name))
        except Exception:
            return default

    try:
        if action == "status":
            spoken, body = _do_status()
        elif action == "connect":
            # atalho para o user chamar via voz — só tenta reconectar ao serial
            # já configurado; pareamento de verdade continua no shell script.
            s = _serial_or_none()
            if not s:
                spoken = ("não há serial configurado; roda ./parear-android.sh "
                          "no Linux pra conectar o projetor por Wi-Fi")
                body = "sem serial"
            else:
                adb = _adb_bin() or "adb"
                r = subprocess.run([adb, "connect", s], capture_output=True,
                                   text=True, timeout=8)
                spoken = (r.stdout.strip() or r.stderr.strip() or "sem resposta")
                body = spoken
        elif action == "tap":
            spoken, body = _do_tap(_num("x"), _num("y"))
        elif action == "swipe":
            spoken, body = _do_swipe(_num("x"), _num("y"), _num("x2"), _num("y2"),
                                     int(p.get("duration_ms") or 300))
        elif action == "dpad":
            spoken, body = _do_dpad(target or query)
        elif action == "keypress":
            spoken, body = _do_keypress(target or query)
        elif action == "type":
            spoken, body = _do_type(target or p.get("text") or query)
        elif action == "screenshot":
            spoken, body = _do_screenshot(query)
        elif action == "screenrecord":
            spoken, body = _do_screenrecord(int(p.get("seconds") or 6))
        elif action == "launch":
            spoken, body = _do_launch(target)
        elif action == "kill":
            spoken, body = _do_kill(target)
        elif action == "apps":
            spoken, body = _do_apps()
        elif action == "logs":
            spoken, body = _do_logs(query, _num("limit"))
        elif action == "open_url":
            spoken, body = _do_open_url(target or query)
        elif action == "push":
            spoken, body = _do_push(target, path)
        elif action == "pull":
            spoken, body = _do_pull(target or path, "")
        elif action == "power":
            spoken, body = _do_power(target or query)
        elif action == "shell":
            spoken, body = _do_shell(target or query)
        else:
            return ("não entendi a ação do Android — usa status, connect, tap, "
                    "swipe, dpad, keypress, type, screenshot, screenrecord, "
                    "launch, kill, apps, logs, open_url, push, pull, power ou shell")
    except Exception as e:
        return f"Android falhou: {e}"

    if player:
        try:
            player.write_log(f"JARVIS: {spoken[:400]}")
            if body:
                import tempfile
                Path(tempfile.gettempdir(), "jarvis-android-last.txt").write_text(
                    body, encoding="utf-8")
        except Exception:
            pass
    return spoken


PLUGIN_SETTINGS = {
    "namespace": NS,
    "title": "ANDROID (ADB, projetor/TV/celular)",
    "fields": [
        {"key": "serial", "label":
            "Serial pinado (ex.: 192.168.0.42:5555). Deixe vazio se só houver "
            "um device.", "type": "text", "default": ""},
        {"key": "label", "label": "Como a Alice chama este device",
         "type": "text", "default": "projetor"},
        {"key": "allow_shell", "label":
            "PERMITIR action='shell' (comando arbitrário no device — perigoso)",
         "type": "toggle", "default": False},
    ],
    "action": {"label": "TESTAR CONEXÃO", "run": _test_action},
}
