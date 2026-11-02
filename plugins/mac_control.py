"""Mac bridge: a Alice no Linux fala com um MacBook pela rede via SSH.

Por quê isto existe
-------------------
O Linux cobre bem o "ver e ler" do iPhone (o plugin `iphone_control` usa
libimobiledevice daqui mesmo). Mas duas coisas só acontecem num Mac:

  1. **controlar** o iPhone de verdade — toque, swipe, digitar, lançar/matar
     app. Isso passa pelo DVT do `pymobiledevice3` (ou `xcrun devicectl`), e
     só funciona em macOS.
  2. **ver/controlar o próprio Mac** — screenshot (`screencapture`), AppleScript
     (`osascript`), `pbcopy`/`pbpaste`, `open -a`.

Este plugin é um bridge SSH: ele roda comandos no Mac com `ssh -i` e uma
conexão multiplexada (`ControlMaster` + `ControlPersist`) pra não abrir mão
de uma por ação. Nada de password — só chave. Pré-requisitos no Mac ficam
em `setup-mac.sh`; chave no Linux fica em `preparar-bridge-mac.sh`.

Config (config/api_keys.json → plugin_config.mac):
    {
      "host": "192.168.0.42"  (ou "macbook.local"),
      "user": "anthero",
      "key_path": "~/.ssh/jarvis_mac_key",
      "allow_exec": false,           // liga para liberar action='mac_exec'
      "iphone_udid": ""              // opcional, quando há mais de um iPhone
    }

Ações
-----
Mac local:      ping | status | mac_screenshot | mac_open | mac_clip_read |
                mac_clip_write | mac_applescript | mac_exec (opcional)
iPhone via Mac: iphone_status | iphone_info | iphone_pair | iphone_screenshot
                | iphone_logs | iphone_crashes | iphone_apps
iPhone tocar:   iphone_tap | iphone_swipe | iphone_type | iphone_keypress
                | iphone_launch | iphone_kill   (requer pymobiledevice3 no Mac)

Segurança: `mac_exec` é a única porta que corre shell arbitrário, e vem
desligada — só roda se `allow_exec: true` no config. As demais ações são
binários específicos com args escapados via shlex.
"""
from __future__ import annotations

import base64
import io
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path

NS = "mac"
_CONTROL_PATH = "/tmp/jarvis-mac-%r@%h:%p"
_SSH_BASE_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ControlMaster=auto",
    "-o", f"ControlPath={_CONTROL_PATH}",
    "-o", "ControlPersist=300",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=6",
    "-o", "ServerAliveInterval=15",
]


PLUGIN = {
    "name": "mac_control",
    "description": (
        "A DIFFERENT computer — the user's MacBook, reached over SSH. Never "
        "use this for 'my screen' / 'minha tela' / 'aqui' / 'this computer': "
        "that is the machine the assistant runs on, and the tool for it is "
        "`screen_process`. Use mac_control only when the user names the Mac or "
        "an Apple device explicitly. "
        "What it is for: anything Apple-side the Linux plugin cannot reach: (1) control "
        "the iPhone itself — tap, swipe, type, launch/kill an app — via "
        "pymobiledevice3 running on the Mac; (2) see or act on the Mac's own "
        "screen and clipboard — screencapture, osascript, pbcopy/pbpaste, "
        "open -a; (3) iPhone status/pairing/screenshot/syslog/crash reports. "
        "Choose this over iphone_control when the user wants to TAP, SWIPE, "
        "TYPE, LAUNCH or KILL something on the iPhone. Choose it over "
        "computer_control whenever the target is the MacBook. If the Mac is "
        "not configured or reachable, say so plainly instead of inventing a "
        "result."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "Mac: ping | status | mac_screenshot | mac_open | "
                    "mac_clip_read | mac_clip_write | mac_applescript | "
                    "mac_exec. iPhone (proxy through the Mac): iphone_status "
                    "| iphone_info | iphone_pair | iphone_screenshot | "
                    "iphone_logs | iphone_crashes | iphone_apps. iPhone "
                    "control: iphone_tap | iphone_swipe | iphone_type | "
                    "iphone_keypress | iphone_launch | iphone_kill."
                ),
            },
            "target": {
                "type": "STRING",
                "description": (
                    "varies by action — mac_open: app name (e.g. 'Safari'); "
                    "iphone_launch/iphone_kill: bundle id "
                    "(e.g. com.apple.mobilesafari); iphone_type: the text "
                    "to insert; iphone_keypress: a key name (home, volumeup, "
                    "volumeup) or a two-letter shortcut; mac_clip_write: text "
                    "to place on the clipboard; mac_applescript/mac_exec: the "
                    "script/command line to run."
                ),
            },
            "x":      {"type": "INTEGER", "description": "coordinate X for iphone_tap/swipe (points, not pixels — screen is ~390 wide on modern iPhones)"},
            "y":      {"type": "INTEGER", "description": "coordinate Y for iphone_tap/swipe"},
            "x2":     {"type": "INTEGER", "description": "swipe end X"},
            "y2":     {"type": "INTEGER", "description": "swipe end Y"},
            "query":  {"type": "STRING",  "description": "screenshot: what to look for; logs/crashes: substring filter"},
            "limit":  {"type": "INTEGER", "description": "logs: how many lines (default 40)"},
            "duration_ms": {"type": "INTEGER", "description": "swipe duration in ms (default 300)"},
        },
        "required": ["action"],
    },
}


# ── config + helpers de SSH ─────────────────────────────────────────────────

def _cfg() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        return get_plugin_config(NS) or {}
    except Exception:
        return {}


def _missing_cfg(cfg: dict) -> str:
    host = str(cfg.get("host") or "").strip()
    user = str(cfg.get("user") or "").strip()
    key  = str(cfg.get("key_path") or "").strip()
    if not host:
        return "nenhum Mac configurado — preencha plugin_config.mac.host no config"
    if not user:
        return "falta o usuário do Mac em plugin_config.mac.user"
    if not key:
        return "falta a chave SSH em plugin_config.mac.key_path"
    if not Path(key).expanduser().exists():
        return (f"a chave {key} não existe aqui — rode ./preparar-bridge-mac.sh "
                "no Linux e autorize a pública no Mac")
    return ""


def _ssh_args(cfg: dict) -> list[str]:
    key = str(Path(cfg.get("key_path") or "").expanduser())
    return ["ssh", *_SSH_BASE_OPTS, "-i", key,
            f"{cfg['user'].strip()}@{cfg['host'].strip()}"]


def _ssh_run(remote_cmd: str, timeout: float = 20.0) -> tuple[int, str, str]:
    """Executa `remote_cmd` no Mac; retorna (rc, stdout, stderr)."""
    cfg = _cfg()
    why = _missing_cfg(cfg)
    if why:
        return 78, "", why
    try:
        r = subprocess.run(
            _ssh_args(cfg) + [remote_cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or "") if isinstance(e.stdout, str) else "", \
               (e.stderr or "") if isinstance(e.stderr, str) else "timeout"
    except FileNotFoundError:
        return 127, "", "ssh não está no PATH deste ambiente"
    except Exception as e:
        return 1, "", str(e)


def _ssh_pull_binary(remote_path: str, timeout: float = 25.0) -> bytes:
    """Retorna os bytes de um arquivo remoto via base64 — sem scp."""
    rc, out, err = _ssh_run(f"base64 < {shlex.quote(remote_path)}",
                            timeout=timeout)
    if rc != 0 or not out.strip():
        return b""
    try:
        return base64.b64decode(out)
    except Exception:
        return b""


def _dir_out() -> Path:
    d = Path(__file__).resolve().parent.parent / "memory" / "mac"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── ações Mac ────────────────────────────────────────────────────────────────

def _do_ping() -> tuple[str, str]:
    rc, out, err = _ssh_run("hostname; echo ---; uptime", timeout=8)
    if rc == 78:
        return (err, err)          # sem config
    if rc != 0:
        return (f"não consegui falar com o Mac: {err.strip()[:180] or 'sem resposta'}",
                f"rc={rc} err={err}")
    lines = out.strip().splitlines()
    host = lines[0] if lines else "Mac"
    up   = lines[2] if len(lines) > 2 else ""
    return (f"Mac {host} respondeu{(' — ' + up.strip()) if up else ''}.", out)


def _do_status() -> tuple[str, str]:
    cmd = ("sw_vers -productName; sw_vers -productVersion; uname -m; "
           "sysctl -n hw.model 2>/dev/null; "
           "df -h / | awk 'NR==2{print $4, $2}'")
    rc, out, err = _ssh_run(cmd, timeout=10)
    if rc == 78:
        return (err, err)
    if rc != 0:
        return (f"não consegui ler o status do Mac: {err.strip()[:180]}", out + err)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return ("meu Mac está rodando " + ", ".join(lines[:2]) +
            f" — arquitetura {lines[2] if len(lines)>2 else '?'}; "
            "disco livre " + (lines[4] if len(lines)>4 else "?"),
            "\n".join(lines))


# Caminhos no Mac (definidos pelo finish-mac-screencapture.sh). Usamos um
# cache do `$HOME` remoto porque `shlex.quote` engole `$HOME` dentro de aspas
# simples, e o caminho tem um espaço em "Application Support".
_MAC_HOME: str | None = None


def _mac_home() -> str:
    global _MAC_HOME
    if _MAC_HOME:
        return _MAC_HOME
    rc, out, _ = _ssh_run("printf %s \"$HOME\"", timeout=6)
    if rc != 0 or not out.strip():
        return ""
    _MAC_HOME = out.strip()
    return _MAC_HOME


def _mac_state() -> tuple[str, str, str]:
    h = _mac_home()
    if not h:
        return "", "", ""
    base = f"{h}/Library/Application Support/Jarvis"
    return (f"{base}/needs-screenshot", f"{base}/screenshot.png",
            f"{base}/last-taken")


def _do_mac_screenshot(query: str) -> tuple[str, str]:
    # Sobre SSH, nem `screencapture` nem `sudo launchctl asuser ... screencapture`
    # alcançam o WindowServer no macOS moderno. O finish-mac-screencapture.sh
    # instala um LaunchAgent residente na sessão Aqua do usuário, disparado
    # por WatchPaths; aqui só tocamos o flag, esperamos o PNG aparecer, e
    # puxamos via base64.
    trig, shot, stamp_path = _mac_state()
    if not trig:
        return ("não consegui resolver $HOME no Mac", "sem home")
    rc, stamp_before, _ = _ssh_run(
        f"cat {shlex.quote(stamp_path)} 2>/dev/null || echo 0", timeout=8)
    if rc == 78:
        return (stamp_before, stamp_before)
    stamp_before = (stamp_before or "0").strip()
    rc, _o, err = _ssh_run(f"touch {shlex.quote(trig)}", timeout=8)
    if rc != 0:
        return (f"falha ao pedir a screenshot: "
                f"{(err or '').strip()[:180] or 'touch rc=' + str(rc)} — "
                "rode ./finish-mac-screencapture.sh no Mac", err)
    appeared = False
    for _ in range(16):
        time.sleep(0.5)
        rc2, after, _ = _ssh_run(
            f"cat {shlex.quote(stamp_path)} 2>/dev/null || echo 0", timeout=6)
        if rc2 == 0 and (after or "0").strip() != stamp_before:
            appeared = True
            break
    if not appeared:
        return ("o LaunchAgent do Mac não respondeu em 8 s — confere se ele "
                "está carregado (launchctl print gui/$(id -u)/com.jarvis.screenshot-watcher) "
                "e se o finish-mac-screencapture.sh terminou com ✔", "agente mudo")
    data = _ssh_pull_binary(shot, timeout=25)
    if not data:
        return ("o Mac gravou a screenshot mas não consegui baixar", "base64 vazio")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    local = _dir_out() / f"tela-mac-{stamp}.png"
    local.write_bytes(data)
    body = f"screen capture salva em {local}"
    if not query:
        return (f"capturei a tela do Mac e salvei em {local.name}", body)
    try:
        from core import brain as _b
        from memory.config_manager import get_plugin_config
        st = _b.settings(get_plugin_config)
        if not st.get("vision_enabled", True):
            return (f"screenshot salva em {local.name} (visão desligada — "
                    "ligue em ECONOMY BRAIN → Vision provider)",
                    body)
        q = (f"{query}\n\nResponda em português, em uma frase curta, apenas "
             "com o que você REALMENTE vê nesta imagem.")
        desc = _b.vision_describe(data, "image/png", q, st) or ""
    except Exception as e:
        return (f"screenshot salva em {local.name}, mas a visão falhou: {e}",
                f"{body}\nvision: {e}")
    if not desc:
        return (f"screenshot salva em {local.name}, o modelo não respondeu", body)
    return (desc.strip(), f"{body}\nvisão: {desc.strip()}")


def _do_mac_open(target: str) -> tuple[str, str]:
    if not target:
        return ("qual app abrir no Mac? diz o nome dele", "sem target")
    rc, _out, err = _ssh_run(f"open -a {shlex.quote(target)}", timeout=10)
    if rc == 78:
        return (err, err)
    if rc != 0:
        return (f"o Mac não abriu {target}: {err.strip()[:180]}", err)
    return (f"abri o {target} no Mac.", f"open -a {target} rc=0")


def _do_mac_clip_read() -> tuple[str, str]:
    rc, out, err = _ssh_run("pbpaste", timeout=8)
    if rc == 78:
        return (err, err)
    if rc != 0:
        return (f"não consegui ler a área de transferência do Mac: {err.strip()[:120]}",
                out + err)
    head = out[:280]
    return (f"a área de transferência do Mac tem {len(out)} caracteres. Começa com: {head}",
            out)


def _do_mac_clip_write(text: str) -> tuple[str, str]:
    if not text:
        return ("o que eu coloco na área de transferência do Mac?", "vazio")
    # envia via stdin sem shell-escape frágil
    cfg = _cfg()
    why = _missing_cfg(cfg)
    if why:
        return (why, why)
    try:
        p = subprocess.run(_ssh_args(cfg) + ["pbcopy"],
                           input=text.encode("utf-8"), timeout=10)
    except Exception as e:
        return (f"falhou a escrita na área de transferência: {e}", str(e))
    if p.returncode != 0:
        return (f"o Mac recusou pbcopy (rc={p.returncode})", "")
    return ("coloquei o texto na área de transferência do Mac.",
            f"pbcopy <- {len(text)} chars")


def _do_mac_applescript(script: str) -> tuple[str, str]:
    if not script:
        return ("escreve o AppleScript que você quer que eu rode", "vazio")
    rc, out, err = _ssh_run(f"osascript -e {shlex.quote(script)}", timeout=15)
    if rc == 78:
        return (err, err)
    if rc != 0:
        return (f"o AppleScript deu erro: {err.strip()[:200]}", out + err)
    return ((out.strip()[:280] or "AppleScript rodou."), out)


def _do_mac_exec(cmd: str) -> tuple[str, str]:
    cfg = _cfg()
    if not cfg.get("allow_exec"):
        return ("execução remota está desligada. Liga allow_exec em "
                "⚙ → PLUGIN SETTINGS → MAC BRIDGE pra liberar.",
                "allow_exec off")
    if not cmd:
        return ("qual comando eu rodo no Mac?", "vazio")
    rc, out, err = _ssh_run(cmd, timeout=25)
    if rc == 78:
        return (err, err)
    body = (out + ("\n" + err if err else ""))[:4000]
    return (f"rodei no Mac (rc={rc}): " + (out.strip().splitlines() or ["(sem saída)"])[0][:240],
            body)


# ── ações iPhone via Mac ─────────────────────────────────────────────────────

def _iphone_prefix(cfg: dict) -> str:
    udid = str(cfg.get("iphone_udid") or "").strip()
    return f"-u {shlex.quote(udid)} " if udid else ""


def _do_ip_status() -> tuple[str, str]:
    rc, out, err = _ssh_run("idevice_id -l", timeout=8)
    if rc == 78:
        return (err, err)
    udids = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not udids:
        return ("nenhum iPhone plugado no Mac agora", "sem device")
    cfg = _cfg()
    rc2, info, _ = _ssh_run(f"ideviceinfo -k DeviceName; ideviceinfo -k ProductVersion; ideviceinfo -k ProductType", timeout=10)
    lines = [ln.strip() for ln in (info or "").splitlines() if ln.strip()]
    name = lines[0] if lines else "iPhone"
    ver  = lines[1] if len(lines) > 1 else "?"
    model = lines[2] if len(lines) > 2 else "?"
    rc3, _o3, _e3 = _ssh_run(f"idevicepair {_iphone_prefix(cfg)}validate", timeout=6)
    paired = rc3 == 0
    tail = "pareado" if paired else "não pareado — pede pra eu disparar iphone_pair"
    return (f"{name} plugado no Mac, iOS {ver}, {model}, {tail} (udid {udids[0][:8]}…)",
            f"udids={udids}\n{info}\npaired={paired}")


def _do_ip_pair() -> tuple[str, str]:
    cfg = _cfg()
    _ssh_run(f"idevicepair {_iphone_prefix(cfg)}unpair", timeout=8)
    rc, out, err = _ssh_run(f"idevicepair {_iphone_prefix(cfg)}pair", timeout=25)
    if rc == 78:
        return (err, err)
    if rc == 0:
        return ("o Mac pareou com o iPhone — o aparelho já confia neste computador.",
                out or "ok")
    msg = (out + " " + err).lower()
    if "pair" in msg or "trust" in msg or "confirm" in msg:
        return ("apareceu o diálogo 'Confiar neste computador?' no iPhone — "
                "desbloqueia, toca em Confiar, me avisa", out + err)
    return (f"não consegui parear: {(err or out).strip()[:200]}", out + err)


def _do_ip_screenshot(query: str) -> tuple[str, str]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    remote = f"/tmp/jarvis-iphone-{stamp}.png"
    rc, _o, err = _ssh_run(f"idevicescreenshot {shlex.quote(remote)}", timeout=25)
    if rc == 78:
        return (err, err)
    if rc != 0:
        return (f"o iPhone não deixou tirar screenshot ({err.strip()[:150]}) — "
                "confere se o Modo do Desenvolvedor está ligado", err)
    data = _ssh_pull_binary(remote, timeout=30)
    _ssh_run(f"rm -f {shlex.quote(remote)}", timeout=8)
    if not data:
        return ("o Mac tirou a screenshot mas não consegui baixar o PNG do iPhone",
                "png vazio")
    local = _dir_out() / f"tela-iphone-{stamp}.png"
    local.write_bytes(data)
    body = f"screenshot salva em {local}"
    if not query:
        return (f"capturei a tela do iPhone e salvei em {local.name}", body)
    try:
        from core import brain as _b
        from memory.config_manager import get_plugin_config
        st = _b.settings(get_plugin_config)
        if not st.get("vision_enabled", True):
            return (f"png em {local.name} (visão desligada — "
                    "ligue em ECONOMY BRAIN → Vision provider)", body)
        q = (f"{query}\n\nResponda em português, em uma frase curta, apenas com "
             "o que você REALMENTE vê nesta imagem.")
        desc = _b.vision_describe(data, "image/png", q, st) or ""
    except Exception as e:
        return (f"png em {local.name}, mas a visão falhou: {e}", str(e))
    if not desc:
        return (f"png em {local.name}, o modelo de visão não respondeu", body)
    return (desc.strip(), f"{body}\nvisão: {desc.strip()}")


def _do_ip_logs(query: str, limit: int) -> tuple[str, str]:
    cfg = _cfg()
    why = _missing_cfg(cfg)
    if why:
        return (why, why)
    cmd = f"idevicesyslog {_iphone_prefix(cfg)}"
    try:
        p = subprocess.Popen(_ssh_args(cfg) + [cmd],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, bufsize=1)
    except FileNotFoundError:
        return ("ssh não está no PATH deste ambiente", "ssh missing")
    except Exception as e:
        return (f"não consegui escutar o syslog: {e}", str(e))
    lines: list[str] = []
    limit = max(5, min(200, int(limit or 40)))
    t0 = time.time()
    try:
        while time.time() - t0 < 6 and len(lines) < limit * 4:
            line = p.stdout.readline()
            if not line:
                break
            line = line.rstrip()
            if not line:
                continue
            if query and query.lower() not in line.lower():
                continue
            lines.append(line)
    finally:
        try:
            p.terminate(); p.wait(timeout=1)
        except Exception:
            try: p.kill()
            except Exception: pass
        try:
            errtxt = p.stderr.read().strip()
        except Exception:
            errtxt = ""
    if not lines:
        if p.returncode not in (0, None) and errtxt:
            return (f"não consegui escutar o syslog do iPhone via Mac: "
                    f"{errtxt[:180]}", errtxt)
        return ("não veio nenhuma linha de log do iPhone nesses segundos — "
                "deixa a tela dele ativa e tenta de novo",
                "syslog 0 lines")
    keep = lines[-limit:]
    return (f"peguei {len(keep)} linhas de log do iPhone"
            + (f" filtrando por '{query}'" if query else "") + ": " + keep[-1],
            "\n".join(keep))


def _do_ip_crashes(query: str, limit: int) -> tuple[str, str]:
    remote_dir = "/tmp/jarvis-iphone-crashes"
    _ssh_run(f"rm -rf {shlex.quote(remote_dir)} && mkdir -p {shlex.quote(remote_dir)}",
             timeout=8)
    rc, out, err = _ssh_run(f"idevicecrashreport {shlex.quote(remote_dir)}", timeout=45)
    if rc == 78:
        return (err, err)
    rc2, lst, _ = _ssh_run(f"ls -1t {shlex.quote(remote_dir)}", timeout=8)
    files = [ln.strip() for ln in (lst or "").splitlines() if ln.strip()]
    if not files:
        return ("o iPhone não tem crash reports pra me passar agora",
                out + err)
    limit = max(1, min(50, int(limit or 5)))
    recent = files[:limit]
    if query:
        low = query.lower()
        for name in recent:
            if low in name.lower():
                rc3, body, _ = _ssh_run(
                    f"head -30 {shlex.quote(f'{remote_dir}/{name}')}", timeout=8)
                return (f"crash '{name}' encontrado, começo do relatório:\n"
                        + body.splitlines()[0], body)
        return (f"nenhum crash report contendo '{query}' nos {len(recent)} mais recentes",
                "\n".join(recent))
    return (f"peguei {len(recent)} crash report(s) recentes do iPhone, "
            f"o mais novo é {recent[0]}",
            "\n".join(recent))


def _do_ip_apps() -> tuple[str, str]:
    rc, out, err = _ssh_run("ideviceinstaller -l 2>&1 | head -200", timeout=30)
    if rc == 78:
        return (err, err)
    lines = [ln for ln in (out or "").splitlines() if ln.strip()]
    if not lines:
        return ("o ideviceinstaller não retornou nada no Mac", out + err)
    return (f"{len(lines)} apps instalados no iPhone. Alguns: " + ", ".join(
        ln.split(",")[0] for ln in lines[:5]), "\n".join(lines))


# ── iPhone CONTROL — o que só dá no macOS via pymobiledevice3 ───────────────

def _dvt(subcmd: str, timeout: float = 15.0) -> tuple[int, str, str]:
    cfg = _cfg()
    udid = str(cfg.get("iphone_udid") or "").strip()
    tail = f" --udid {shlex.quote(udid)}" if udid else ""
    return _ssh_run(f"python3 -m pymobiledevice3 developer dvt {subcmd}{tail}",
                    timeout=timeout)


def _do_ip_tap(x: int, y: int) -> tuple[str, str]:
    rc, out, err = _dvt(f"tap {int(x)} {int(y)}")
    if rc == 78:
        return (err, err)
    if rc != 0:
        return (f"o iPhone recusou o toque em ({x},{y}): {err.strip()[:150]}",
                out + err)
    return (f"toquei em ({x},{y}) na tela do iPhone.", out or "ok")


def _do_ip_swipe(x: int, y: int, x2: int, y2: int, dur_ms: int) -> tuple[str, str]:
    dur_ms = max(60, min(3000, int(dur_ms or 300))) / 1000.0
    rc, out, err = _dvt(f"swipe {int(x)} {int(y)} {int(x2)} {int(y2)} {dur_ms:.2f}")
    if rc == 78:
        return (err, err)
    if rc != 0:
        return (f"não consegui deslizar no iPhone: {err.strip()[:150]}", out + err)
    return (f"deslizei de ({x},{y}) até ({x2},{y2}) no iPhone.", out or "ok")


def _do_ip_type(text: str) -> tuple[str, str]:
    if not text:
        return ("o que eu digito no iPhone?", "vazio")
    rc, out, err = _dvt(f"text {shlex.quote(text)}")
    if rc == 78:
        return (err, err)
    if rc != 0:
        return (f"não consegui digitar no iPhone: {err.strip()[:150]}", out + err)
    return (f"digitei \"{text[:40]}\" no iPhone.", out or "ok")


def _do_ip_keypress(name: str) -> tuple[str, str]:
    if not name:
        return ("qual tecla? home, volumeup, volumedown, power", "vazio")
    rc, out, err = _dvt(f"press {shlex.quote(name.lower())}")
    if rc == 78:
        return (err, err)
    if rc != 0:
        return (f"o iPhone não respondeu à tecla {name}: {err.strip()[:150]}",
                out + err)
    return (f"apertei {name} no iPhone.", out or "ok")


def _do_ip_launch(bundle: str) -> tuple[str, str]:
    if not bundle:
        return ("qual bundle id? (ex. com.apple.mobilesafari)", "vazio")
    rc, out, err = _dvt(f"launch {shlex.quote(bundle)}", timeout=20)
    if rc == 78:
        return (err, err)
    if rc != 0:
        return (f"o iPhone não abriu {bundle}: {err.strip()[:150]}", out + err)
    return (f"abri {bundle} no iPhone.", out or "ok")


def _do_ip_kill(bundle: str) -> tuple[str, str]:
    if not bundle:
        return ("qual bundle id matar?", "vazio")
    rc, out, err = _dvt(f"kill {shlex.quote(bundle)}", timeout=20)
    if rc == 78:
        return (err, err)
    if rc != 0:
        return (f"o iPhone não fechou {bundle}: {err.strip()[:150]}", out + err)
    return (f"fechei {bundle} no iPhone.", out or "ok")


# ── botão ⚙ ─────────────────────────────────────────────────────────────────

def _test_action(values: dict | None = None) -> tuple[bool, str]:
    if values:
        try:
            from memory.config_manager import save_plugin_config
            save_plugin_config(NS, values)
        except Exception:
            pass
    rc, out, err = _ssh_run("echo ok", timeout=6)
    if rc == 78:
        return False, err
    if rc != 0:
        return False, f"ssh falhou (rc={rc}): {err.strip()[:180] or 'sem resposta'}"
    # testa as ferramentas que a Alice usa remotamente
    checks = []
    for cmd, name in [
        ("idevice_id -l", "libimobiledevice"),
        ("ideviceinstaller -l 2>&1 | head -1", "ideviceinstaller"),
        ("python3 -c 'import pymobiledevice3,sys; print(pymobiledevice3.__version__)'", "pymobiledevice3"),
    ]:
        r, o, e = _ssh_run(cmd, timeout=8)
        checks.append(f"{name}: {'ok' if r == 0 else 'falta'}"
                      + (f" ({e.strip()[:40]})" if r != 0 else ""))
    return True, "ssh ok · " + " · ".join(checks)


# ── dispatcher ──────────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower()
    target = str(p.get("target") or "").strip()
    query  = str(p.get("query") or "").strip()
    try:
        limit = int(p.get("limit") or 0)
    except Exception:
        limit = 0

    def _num(name: str, default: int = 0) -> int:
        try:
            return int(p.get(name))
        except Exception:
            return default

    try:
        # Mac
        if action in ("ping", "conn", "conectar"):
            spoken, body = _do_ping()
        elif action in ("status", "estado"):
            spoken, body = _do_status()
        elif action in ("mac_screenshot", "mac_screen", "screenshot"):
            spoken, body = _do_mac_screenshot(query)
        elif action in ("mac_open", "abrir_mac"):
            spoken, body = _do_mac_open(target or query)
        elif action in ("mac_clip_read", "clipboard_read"):
            spoken, body = _do_mac_clip_read()
        elif action in ("mac_clip_write", "clipboard_write"):
            spoken, body = _do_mac_clip_write(target or p.get("text") or query)
        elif action in ("mac_applescript", "applescript", "osascript"):
            spoken, body = _do_mac_applescript(target or p.get("text") or query)
        elif action in ("mac_exec", "exec"):
            spoken, body = _do_mac_exec(target or p.get("text") or query)

        # iPhone via Mac (mesmas do iphone_control, mas rodando lá)
        elif action in ("iphone_status", "iphone_conn"):
            spoken, body = _do_ip_status()
        elif action in ("iphone_pair", "iphone_parear", "iphone_trust"):
            spoken, body = _do_ip_pair()
        elif action in ("iphone_info",):
            rc, out, err = _ssh_run("ideviceinfo", timeout=8)
            spoken = ("lendo DeviceInfo do iPhone" if rc == 0 else
                      (err if rc == 78 else "não consegui ler o DeviceInfo"))
            body = out + err
        elif action in ("iphone_screenshot", "iphone_screen", "iphone_tela"):
            spoken, body = _do_ip_screenshot(query)
        elif action in ("iphone_logs", "iphone_syslog", "iphone_log"):
            spoken, body = _do_ip_logs(query, limit)
        elif action in ("iphone_crashes", "iphone_crash"):
            spoken, body = _do_ip_crashes(query, limit)
        elif action in ("iphone_apps", "iphone_instalados"):
            spoken, body = _do_ip_apps()

        # iPhone CONTROL (DVT — só no macOS)
        elif action in ("iphone_tap", "iphone_clicar"):
            spoken, body = _do_ip_tap(_num("x"), _num("y"))
        elif action in ("iphone_swipe", "iphone_deslizar"):
            spoken, body = _do_ip_swipe(_num("x"), _num("y"), _num("x2"), _num("y2"),
                                        int(p.get("duration_ms") or 300))
        elif action in ("iphone_type", "iphone_digitar"):
            spoken, body = _do_ip_type(target or p.get("text") or query)
        elif action in ("iphone_keypress", "iphone_tecla"):
            spoken, body = _do_ip_keypress(target or query)
        elif action in ("iphone_launch", "iphone_abrir"):
            spoken, body = _do_ip_launch(target)
        elif action in ("iphone_kill", "iphone_fechar"):
            spoken, body = _do_ip_kill(target)

        else:
            return ("não entendi a ação do Mac/iPhone — usa ping, status, "
                    "mac_screenshot, mac_open, mac_clipboard_*, "
                    "mac_applescript, iphone_status/pair/screenshot/logs/"
                    "crashes/apps/tap/swipe/type/keypress/launch/kill")
    except Exception as e:
        return f"Mac bridge falhou: {e}"

    if player:
        try:
            player.write_log(f"JARVIS: {spoken[:400]}")
            if body:
                import tempfile
                Path(tempfile.gettempdir(), "jarvis-mac-last.txt").write_text(
                    body, encoding="utf-8")
        except Exception:
            pass
    return spoken


PLUGIN_SETTINGS = {
    "namespace": NS,
    "title": "MAC BRIDGE (SSH)",
    "fields": [
        {"key": "host", "label": "Host ou IP do Mac", "type": "text",
         "default": ""},
        {"key": "user", "label": "Usuário SSH no Mac", "type": "text",
         "default": ""},
        {"key": "key_path", "label": "Caminho da chave privada",
         "type": "text", "default": "~/.ssh/jarvis_mac_key"},
        {"key": "iphone_udid", "label": "UDID do iPhone (opcional, só quando "
            "há mais de um pareado)", "type": "text", "default": ""},
        {"key": "allow_exec", "label":
            "PERMITIR mac_exec (shell arbitrário remoto — perigoso)",
         "type": "toggle", "default": False},
    ],
    "action": {"label": "TESTAR CONEXÃO SSH", "run": _test_action},
}
