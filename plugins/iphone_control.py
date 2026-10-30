"""iPhone pela USB: ver a tela, ler o syslog, puxar crash reports, checar pareamento.

Com Developer Mode LIGADO no iPhone (Ajustes → Privacidade & Segurança → Modo
do Desenvolvedor), o `libimobiledevice` que já está instalado nesta máquina
(`idevice_id`, `ideviceinfo`, `idevicescreenshot`, `idevicesyslog`,
`idevicecrashreport`, `idevicepair`) enxerga o aparelho por USB sem precisar de
Xcode nem Mac. O `usbmuxd` é ativado via udev no plug, então não precisa subir
nada na mão.

O que NÃO dá pra fazer daqui: injetar toque/teclado no iPhone. A Apple só
expõe isso pelo DVT do Xcode, e o `pymobiledevice3` cobre uma parte em macOS —
no Linux o controle é fora de alcance. "Ver" e "ler" sim.

Ações expostas ao cérebro:
    status     — está plugado? pareado? iOS/modelo/número de série
    info       — dump completo do DeviceInfo em chave=valor
    pair       — dispara o pareamento; o iPhone mostra o diálogo "Confiar?"
    screenshot — captura a tela; se houver visão local (qwen2.5vl) ligada,
                 descreve o que vê. Aceita `query` para perguntar algo específico
    logs       — últimas N linhas do syslog em tempo real (aceita `query`
                 como filtro por substring, case-insensitive)
    crashes    — baixa os crash reports para memory/iphone/crashes/ e lista
                 os 5 mais recentes; com `query` traz o corpo do último match
    apps       — lista instalada (bundle id + nome), se ideviceinstaller
                 estiver presente
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

_NS = "iphone"


# ── o contrato da ferramenta ────────────────────────────────────────────────

PLUGIN = {
    "name": "iphone_control",
    "description": (
        "The user's iPhone plugged in by USB on this PC. Read-only + capture: "
        "For the screen of THIS computer use `screen_process` instead — this "
        "plugin is only for the phone. "
        "check status/pairing, take a screenshot of the phone (with the local "
        "vision model describing it), read the live syslog, download crash "
        "reports, list installed apps, or trigger the 'Trust this computer' "
        "pairing dialog. CANNOT tap, swipe or type into the iPhone — Apple "
        "does not expose touch injection outside macOS/Xcode. Use this "
        "whenever the user says iPhone / 'meu iphone' / 'iOS' / 'celular da "
        "Apple' and asks what is on its screen, whether it crashed, for the "
        "logs, or to see it. Prefer this over computer_control for anything "
        "on the phone itself."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "status | info | pair | screenshot | logs | crashes | apps"
                ),
            },
            "query": {
                "type": "STRING",
                "description": (
                    "screenshot: what to look for or describe on the phone's "
                    "screen (default: everything visible). logs/crashes: "
                    "substring to keep (case-insensitive)."
                ),
            },
            "limit": {
                "type": "INTEGER",
                "description": (
                    "logs: how many lines (default 40, max 200). crashes: how "
                    "many recent entries to list (default 5)."
                ),
            },
        },
        "required": ["action"],
    },
}


# ── helpers ──────────────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: float = 10.0, env_extra: dict | None = None
         ) -> tuple[int, str, str]:
    """subprocess.run com (rc, stdout, stderr) em texto, tolerando timeout."""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.returncode, r.stdout or "", r.stderr or ""
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} não está instalado"
    except subprocess.TimeoutExpired as e:
        # `timeout` CLI manda SIGTERM e devolve o stdout parcial — mas se o
        # próprio subprocess estourar, ainda queremos o que saiu.
        return 124, (e.stdout or "") if isinstance(e.stdout, str) else "", \
               (e.stderr or "") if isinstance(e.stderr, str) else ""
    except Exception as e:
        return 1, "", str(e)


def _has_udid() -> str:
    rc, out, _ = _run(["idevice_id", "-l"], timeout=5)
    if rc != 0:
        return ""
    for line in out.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _info_kv() -> dict[str, str]:
    rc, out, _ = _run(["ideviceinfo"], timeout=8)
    fields: dict[str, str] = {}
    if rc != 0:
        return fields
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            fields[k.strip()] = v.strip()
    return fields


def _dir_out() -> Path:
    d = Path(__file__).resolve().parent.parent / "memory" / "iphone"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── as ações ─────────────────────────────────────────────────────────────────

def _do_status() -> tuple[str, str]:
    udid = _has_udid()
    if not udid:
        return ("nenhum iPhone conectado por USB agora — conecta o cabo e "
                "confere se o aparelho está desbloqueado", "sem device")
    rc, out, _ = _run(["idevicepair", "validate"], timeout=6)
    paired = rc == 0
    fields = _info_kv()
    name = fields.get("DeviceName", "iPhone")
    ver  = fields.get("ProductVersion", "?")
    model = fields.get("ProductType", "?")
    if not paired:
        return (f"{name} ({model}, iOS {ver}) está plugado mas ainda não "
                "pareado — pede pra eu disparar o pareamento e toca em "
                "'Confiar' no iPhone",
                f"plugado, não pareado: {udid}")
    line = f"{name} pareado — iOS {ver}, modelo {model},udid {udid[:8]}…"
    return line, line


def _do_pair() -> tuple[str, str]:
    if not _has_udid():
        return ("não tem iPhone plugado pra eu parear", "sem device")
    _run(["idevicepair", "unpair"], timeout=5)   # ignora se não havia
    rc, out, err = _run(["idevicepair", "pair"], timeout=20)
    if rc == 0:
        return ("pareado — o iPhone agora confia neste PC.", out or "ok")
    if "pair" in (out + err).lower() or "trust" in (out + err).lower():
        return ("apareceu o diálogo 'Confiar neste computador?' no iPhone — "
                "desbloqueia a tela e toca em Confiar, depois me avisa",
                out + err)
    return (f"não consegui parear: {(err or out).strip()[:200]}", out + err)


def _do_info() -> tuple[str, str]:
    if not _has_udid():
        return ("nenhum iPhone conectado — pluga o cabo pra eu ler as "
                "informações", "sem device")
    fields = _info_kv()
    if not fields:
        return ("o iPhone está plugado mas eu não consegui ler o DeviceInfo "
                "dele — provavelmente ainda não pareado", "info vazio")
    keep = ["DeviceName", "DeviceClass", "ProductType", "ProductVersion",
            "BuildVersion", "CPUArchitecture", "TotalDiskCapacity",
            "AvailableCapacity", "BatteryLevel", "BatteryIsCharging",
            "WiFiAddress", "BluetoothAddress", "SerialNumber", "TimeZone"]
    picked = [f"{k}={fields[k]}" for k in keep if k in fields]
    body = "\n".join(picked) if picked else "\n".join(
        f"{k}={v}" for k, v in list(fields.items())[:15])
    return (body.replace("=", " é ").replace("\n", ". ") + ".",
            body)


def _do_screenshot(query: str) -> tuple[str, str]:
    if not _has_udid():
        return ("nenhum iPhone plugado pra eu tirar screenshot", "sem device")
    path = _dir_out() / f"tela-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    rc, _out, err = _run(["idevicescreenshot", str(path)], timeout=20)
    if rc != 0 or not path.exists():
        return (f"o iPhone não deixou capturar a tela ({err.strip()[:120] or 'rc=%d' % rc}) "
                "— confirma se o Modo do Desenvolvedor está ligado",
                f"screenshot rc={rc} {err}")
    body = f"screenshot salva em {path}"
    if not query:
        return (f"tirei a screenshot da tela do iPhone e salvei em {path.name}",
                body)
    # Vision local igual ao computer_control.screen_find — a imagem NUNCA sai
    # da máquina, e se o VL não estiver disponível o usuário ainda recebe o
    # caminho do arquivo.
    try:
        from core import brain as _b
        from memory.config_manager import get_plugin_config
        st = _b.settings(get_plugin_config)
        if not st.get("vision_local", st.get("provider") == "custom"):
            return (f"screenshot salva em {path.name} (visão local disponível "
                    "só quando o cérebro é o Ollama)", body)
        data = path.read_bytes()
        q = (f"{query}\n\nResponda em português, em uma frase curta, com o que "
             "você REALMENTE vê nesta imagem; se não tiver certeza, diz que não "
             "conseguiu ler.")
        desc = _b.vision_describe(data, "image/png", q, st) or ""
    except Exception as e:
        return (f"screenshot salva em {path.name} mas a visão local falhou: {e}",
                f"{body}\nvision error: {e}")
    if not desc:
        return (f"screenshot salva em {path.name}, o modelo de visão não respondeu",
                body)
    return (desc.strip(), f"{body}\nvisão: {desc.strip()}")


def _do_logs(query: str, limit: int) -> tuple[str, str]:
    if not _has_udid():
        return ("nenhum iPhone plugado pra eu escutar o syslog", "sem device")
    limit = max(5, min(200, int(limit or 40)))
    window = 6  # segundos coletando — suficiente pra pegar as linhas mais quentes
    # `timeout` manda SIGTERM e fecha o pipe; subprocess.TimeoutExpired é o
    # fallback caso o `timeout` não exista no PATH (Windows).
    cmd = ["timeout", str(window), "idevicesyslog"]
    if shutil_timeout_missing():
        cmd = ["idevicesyslog"]
    rc, out, err = _run(cmd, timeout=window + 4)
    lines = [ln for ln in (out or "").splitlines() if ln.strip()]
    if query:
        low = query.lower()
        lines = [ln for ln in lines if low in ln.lower()]
    if not lines:
        return ("não vi nenhuma linha de log do iPhone nesse momento — "
                "deixa a tela dele ativa e tenta de novo",
                f"syslog rc={rc} lines=0 err={err[:200]}")
    keep = lines[-limit:]
    body = "\n".join(keep)
    spoken = (f"peguei {len(keep)} linhas de log do iPhone"
              + (f" filtrando por '{query}'" if query else "") + ": "
              + keep[-1])
    return spoken, body


def shutil_timeout_missing() -> bool:
    import shutil
    return shutil.which("timeout") is None


def _do_crashes(query: str, limit: int) -> tuple[str, str]:
    if not _has_udid():
        return ("nenhum iPhone plugado pra eu puxar crash reports", "sem device")
    dst = _dir_out() / "crashes"
    dst.mkdir(parents=True, exist_ok=True)
    rc, out, err = _run(["idevicecrashreport", str(dst)], timeout=45)
    if rc != 0 and not any(dst.iterdir()):
        return (f"não consegui baixar os crash reports ({err.strip()[:120] or 'rc=%d' % rc})",
                out + err)
    files = sorted([p for p in dst.rglob("*") if p.is_file()],
                   key=lambda p: p.stat().st_mtime, reverse=True)
    limit = max(1, min(50, int(limit or 5)))
    recent = files[:limit]
    if query:
        low = query.lower()
        for p in recent:
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if low in p.name.lower() or low in txt.lower():
                head = "\n".join(txt.splitlines()[:25])
                return (f"o crash report mais recente combinando com '{query}' "
                        f"é {p.name}: " + head.splitlines()[0],
                        head)
    if not recent:
        return ("o iPhone não tem crash reports pra me passar agora", "sem crash")
    names = "\n".join(p.name for p in recent)
    return (f"peguei {len(recent)} crash report(s) recentes, o mais novo é "
            f"{recent[0].name}", names)


def _do_apps() -> tuple[str, str]:
    if not _has_udid():
        return ("nenhum iPhone plugado pra eu listar apps", "sem device")
    rc, out, err = _run(["ideviceinstaller", "-l"], timeout=30)
    if rc != 0:
        return ("o ideviceinstaller não rodou — instala com "
                f"'sudo apt install ideviceinstaller' (detalhe: {err[:120]})",
                out + err)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return (f"{len(lines)} apps instalados no iPhone. Alguns: "
            + ", ".join(lines[:6]),
            "\n".join(lines))


# ── teste do botão ⚙ ────────────────────────────────────────────────────────

def _connect_action(values: dict | None = None) -> tuple[bool, str]:
    """Botão 'TESTAR CONEXÃO' em ⚙ → PLUGIN SETTINGS → IPHONE."""
    if not _has_udid():
        return False, "nenhum iPhone plugado por USB (usbmuxd não vê nada)"
    rc, out, err = _run(["idevicepair", "validate"], timeout=6)
    if rc != 0:
        return False, f"plugado mas não pareado — {out.strip() or err.strip() or 'toca em Confiar no iPhone'}"
    fields = _info_kv()
    return True, (f"{fields.get('DeviceName', 'iPhone')} · iOS "
                  f"{fields.get('ProductVersion', '?')} · "
                  f"{fields.get('ProductType', '?')}")


# ── a entrada da ferramenta ──────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "").strip().lower()
    query  = str(params.get("query") or "").strip()
    limit  = params.get("limit") or 0

    try:
        if action in ("status", "estado", "conectado", "conexao", "conexão"):
            spoken, body = _do_status()
            title = "IPHONE — status"
        elif action in ("info", "detalhes"):
            spoken, body = _do_info()
            title = "IPHONE — info"
        elif action in ("pair", "parear", "confiar", "trust"):
            spoken, body = _do_pair()
            title = "IPHONE — pareamento"
        elif action in ("screenshot", "tela", "print", "snapshot"):
            spoken, body = _do_screenshot(query)
            title = "IPHONE — screenshot"
        elif action in ("logs", "syslog", "log"):
            spoken, body = _do_logs(query, int(limit))
            title = "IPHONE — syslog"
        elif action in ("crashes", "crash", "trava", "fechou"):
            spoken, body = _do_crashes(query, int(limit))
            title = "IPHONE — crash reports"
        elif action in ("apps", "instalados", "aplicativos"):
            spoken, body = _do_apps()
            title = "IPHONE — apps instalados"
        else:
            return ("não entendi a ação do iPhone — usa status, pair, "
                    "screenshot, logs, crashes, apps ou info")
    except Exception as e:
        return f"iPhone falhou: {e}"

    if player:
        try:
            player.write_log(f"JARVIS: {spoken[:400]}")
            import tempfile
            tmp = Path(tempfile.gettempdir()) / "jarvis-iphone-last.txt"
            tmp.write_text(body, encoding="utf-8")
        except Exception:
            pass
    return spoken


PLUGIN_SETTINGS = {
    "namespace": _NS,
    "title": "IPHONE (USB, libimobiledevice)",
    "fields": [
        {"key": "notes", "label":
            "Requer iPhone com Developer Mode ligado, plugado por USB e "
            "desbloqueado. Na primeira vez use a action 'pair' e toque em "
            "Confiar no aparelho. Toque/gesto por USB NÃO é suportado em "
            "Linux (limitação Apple).",
         "type": "info"},
    ],
    "action": {"label": "TESTAR CONEXÃO", "run": _connect_action},
}
