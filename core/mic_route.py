"""
core/mic_route.py — which PHYSICAL microphone JARVIS is hearing, and how to
keep the PipeWire AEC on that same hardware.

WHY THIS EXISTS
    The voice profile used to be keyed by the *name in the config*, not by the
    hardware. With ``input_device = "echo_cancel_source"`` every sample landed
    under the slug of that virtual source (or under ``pc`` when the config was
    empty), so a headset and a webcam shared one centroid. Recognition then
    compared the headset's voice with the webcam's profile and the score
    collapsed — correctly, because it was the wrong profile.

    The stable key must be the PHYSICAL capture node behind whatever filter
    sits in front of it. This module answers that question two ways:

      * a configured plain name is matched against the PipeWire/PulseAudio
        sources (``pactl``);
      * a configured virtual AEC name is resolved through the filter-chain
        configuration to its ``capture.props.target.object`` — the hardware it
        actually captures.

    Scoring/identity stays in core/speaker_id.py; this module only names the
    chain and, when asked, retargets JARVIS's OWN managed AEC chain.

AEC THAT FOLLOWS THE MIC
    ``~/.config/pipewire/filter-chain.conf.d/50-echo-cancel.conf`` is the
    owner's file, pinned to the C920 webcam. It is NEVER modified here. When
    the owner runs the installer (``python -m core.mic_route install``), this
    module writes a SEPARATE file (``90-jarvis-aec-follow.conf``) whose virtual
    source is ``jarvis_aec_source`` and whose capture target is the physical
    microphone chosen then. Because the node name is stable, the app can
    rewrite only the target later and restart only the filter-chain daemon
    (``systemctl --user restart filter-chain.service``), which does not touch
    the system's audio. Nothing auto-writes a filter chain for the owner's
    legacy node: that would mean two modules claiming one node name.

    ONE MODULE PER MICROPHONE
    The same managed file can declare several echo-cancel modules, one per
    physical microphone — ``jarvis_aec_source``, ``_2``, ``_3``… — so every
    microphone gets its own canceller and its own stream (core/mic_compare.py
    compares the separate signals; nothing is ever summed). The first module
    keeps the bare name so the saved ``input_device`` and the voice profile key
    do not move.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_FILTER_DIR = Path.home() / ".config" / "pipewire" / "filter-chain.conf.d"
LEGACY_CONF_NAME = "50-echo-cancel.conf"
MANAGED_CONF_NAME = "90-jarvis-aec-follow.conf"
MANAGED_NODE = "jarvis_aec_source"
MANAGED_CAPTURE = "jarvis_aec_capture"
DEFAULT_RATE = 32000
DEFAULT_CHANNELS = 2

_SLUG_RE = re.compile(r"[^a-z0-9]+")
# The first module keeps the bare name so the saved input_device and the voice
# profile key never move; the extra microphones are _2, _3, … .
_MANAGED_NODE_RE = re.compile(r"^" + re.escape(MANAGED_NODE) + r"(?:_\d+)?$")


def slug(text: Any) -> str:
    """Accent/case-insensitive stable key — the same rule the voice index uses."""
    raw = unicodedata.normalize("NFKD", str(text or ""))
    plain = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return _SLUG_RE.sub("-", plain.casefold()).strip("-")


def _tokens(text: Any) -> set[str]:
    return {tok for tok in slug(text).split("-") if len(tok) > 1}


def _pactl(*args: str) -> str:
    """Run one pactl query in the C locale; never raises, never blocks long.

    Everything here goes through pactl (a separate process). Calling
    sounddevice/PortAudio from the event loop while the mic stream is open is
    what tripped PulseAudio's threaded-mainloop assertion before — see
    core/audio_devices.py.
    """
    env = dict(os.environ, LANG="C", LC_ALL="C")
    try:
        out = subprocess.run(
            ["pactl", *args], capture_output=True, timeout=5, env=env, check=False
        )
        return out.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


# ── sources as the server sees them ─────────────────────────────────────────


def parse_sources(text: str) -> list[dict[str, Any]]:
    """Parse ``pactl list short sources`` into rows (pure; testable).

    Columns: index, name, driver, sample spec, state. Trailing columns may be
    absent on older servers, so only the first two are mandatory.
    """
    rows: list[dict[str, Any]] = []
    for line in str(text or "").splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 2 or not parts[1].strip():
            continue
        name = parts[1].strip()
        spec = parts[3].strip() if len(parts) > 3 else ""
        rows.append({
            "name": name,
            "driver": parts[2].strip() if len(parts) > 2 else "",
            "spec": spec,
            "state": parts[4].strip() if len(parts) > 4 else "",
            "monitor": name.lower().endswith(".monitor"),
            "rate": _spec_rate(spec),
        })
    return rows


def _spec_rate(spec: str) -> int:
    m = re.search(r"(\d+)\s*Hz", str(spec or ""))
    try:
        rate = int(m.group(1)) if m else 0
    except ValueError:
        rate = 0
    return rate if 8000 <= rate <= 192000 else 0


def _spec_channels(spec: str) -> int:
    m = re.search(r"(\d+)\s*ch\b", str(spec or ""))
    try:
        channels = int(m.group(1)) if m else 0
    except ValueError:
        channels = 0
    return channels if 1 <= channels <= 8 else 0


def list_sources() -> list[dict[str, Any]]:
    """Capture sources currently offered by the server (never raises)."""
    return parse_sources(_pactl("list", "short", "sources"))


def default_source() -> str:
    """The server's default capture source, or "" when unavailable."""
    name = _pactl("get-default-source").strip()
    if name:
        return name
    for line in _pactl("info").splitlines():
        low = line.lower()
        if low.startswith("default source") or low.startswith("fonte padrão"):
            return line.split(":", 1)[1].strip()
    return ""


# ── filter chains: which hardware feeds a virtual AEC source ────────────────


def parse_filter_chains(text: str) -> list[dict[str, Any]]:
    """Echo-cancel modules declared in one filter-chain file (pure).

    Each module is identified by ``libpipewire-module-echo-cancel``. The
    virtual source is the ``node.name`` that is not the capture node; the
    hardware is its ``target.object``.
    """
    chains: list[dict[str, Any]] = []
    blocks = re.split(r"libpipewire-module-echo-cancel", str(text or ""))[1:]
    for block in blocks:
        # Stop at the next module declaration inside the same file.
        block = re.split(r"context\.modules|libpipewire-module-", block)[0]
        target = _first(r'target\.object\s*=\s*"([^"]+)"', block)
        names = re.findall(r'node\.name\s*=\s*"([^"]+)"', block)
        if not target or not names:
            continue
        description = _first(r'node\.description\s*=\s*"([^"]+)"', block)
        capture = next((n for n in names if "capture" in n.lower()), names[0])
        source = next((n for n in names if n != capture), capture)
        rate_m = re.search(r"audio\.rate\s*=\s*(\d+)", block)
        channels_m = re.search(r"audio\.channels\s*=\s*(\d+)", block)
        chains.append({
            "node_name": source,
            "capture_node": capture,
            "target": target,
            "description": description,
            "rate": int(rate_m.group(1)) if rate_m else 0,
            "channels": int(channels_m.group(1)) if channels_m else 0,
        })
    return chains


def _first(pattern: str, text: str) -> str:
    m = re.search(pattern, text or "")
    return m.group(1).strip() if m else ""


def read_filter_chains(directory: str | Path | None = None) -> list[dict[str, Any]]:
    """Every AEC chain declared in the filter-chain drop-in directory."""
    folder = Path(directory) if directory else DEFAULT_FILTER_DIR
    chains: list[dict[str, Any]] = []
    try:
        files = sorted(folder.glob("*.conf"))
    except OSError:
        return chains
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for chain in parse_filter_chains(text):
            chain = dict(chain)
            chain["path"] = str(path)
            chain["managed"] = is_managed_node(chain.get("node_name"))
            chains.append(chain)
    return chains


# ── naming the physical microphone ──────────────────────────────────────────


def is_aec_source(name: str) -> bool:
    """Does this configured/source name denote an echo-cancelled virtual mic?"""
    low = str(name or "").strip().lower()
    return ("echo_cancel" in low or "echo-cancel" in low
            or "microfone aec" in low or low == "aec"
            or is_managed_node(name))


def is_managed_node(name: Any) -> bool:
    """Is this node name one of JARVIS's own AEC sources?

    ``jarvis_aec_source`` is the first (and for a single microphone, the only)
    module. ``_2``, ``_3``… are the extra microphones rendered into the same
    file — see render_managed_conf(). The legacy ``echo_cancel_source`` belongs
    to the owner's file and never matches."""
    return bool(_MANAGED_NODE_RE.match(str(name or "").strip()))


def managed_node_names(count: int) -> list[str]:
    """Virtual sources for N microphones: bare first, then _2, _3, …"""
    count = max(1, int(count))
    return [MANAGED_NODE] + [f"{MANAGED_NODE}_{i}" for i in range(2, count + 1)]


def managed_capture_names(count: int) -> list[str]:
    """Capture (hardware-facing) node names for N echo-cancel modules."""
    count = max(1, int(count))
    return [MANAGED_CAPTURE] + [f"{MANAGED_CAPTURE}_{i}" for i in range(2, count + 1)]


def _match_source(name: str, sources: Iterable[Mapping[str, Any]]) -> str:
    """Best real source name for a PortAudio/config label; "" if none."""
    wanted = str(name or "").strip()
    if not wanted:
        return ""
    rows = [row for row in sources if not row.get("monitor")]
    for row in rows:
        if row.get("name") == wanted:
            return str(row["name"])
    want_slug = slug(wanted)
    for row in rows:
        if slug(row.get("name")) == want_slug:
            return str(row["name"])
    want_tokens = _tokens(wanted)
    if not want_tokens:
        return ""
    best, best_score = "", 0.0
    for row in rows:
        tokens = _tokens(row.get("name"))
        if not tokens:
            continue
        overlap = len(want_tokens & tokens) / float(len(want_tokens))
        if overlap > best_score:
            best, best_score = str(row["name"]), overlap
    # 80 % of the saved label's words must appear in the node name: a full name
    # matches, "Microfone (C920)" matches, an unrelated mic does not.
    return best if best_score >= 0.8 else ""


def physical_source(
    configured: str,
    *,
    sources: list[dict[str, Any]] | None = None,
    chains: list[dict[str, Any]] | None = None,
    default: str | None = None,
) -> dict[str, Any]:
    """Resolve the configured capture chain to its PHYSICAL hardware node.

    Returns ``{"configured", "source", "physical", "key", "aec", "chain"}``.
    ``source`` is the server-side source actually read (an AEC virtual node
    when one is involved); ``physical`` is the hardware behind it.
    """
    cfg = str(configured or "").strip()
    rows = list_sources() if sources is None else list(sources)
    declared = read_filter_chains() if chains is None else list(chains)
    fallback = default_source() if default is None else str(default or "")

    chain_names = {str(c.get("node_name") or ""): c for c in declared}

    source = ""
    chain: dict[str, Any] | None = None
    if not cfg:
        source = fallback
        chain = chain_names.get(source)
    else:
        # 1) the configured value names a filter-chain virtual source.
        for node, item in chain_names.items():
            if cfg == node or slug(cfg) == slug(node):
                source, chain = node, item
                break
        # 2) or its human description ("Microfone AEC (C920)").
        if chain is None and is_aec_source(cfg):
            for node, item in chain_names.items():
                desc = str(item.get("description") or "")
                if desc and (slug(cfg) in slug(desc) or slug(desc) in slug(cfg)):
                    source, chain = node, item
                    break
        # 3) otherwise it is / should be a real source.
        if chain is None:
            source = _match_source(cfg, rows) or cfg

    physical = str(chain.get("target") or "") if chain else ""
    if not physical:
        # The default source may itself be an AEC node that another file
        # declares (or that this one did not parse); follow one level only.
        nested = chain_names.get(source)
        if nested is not None and nested is not chain:
            chain = nested
            physical = str(nested.get("target") or "")
    if not physical:
        physical = source
    return {
        "configured": cfg,
        "source": source,
        "physical": physical,
        "key": slug(physical) or "pc",
        "aec": bool(chain),
        "chain": str(chain.get("node_name") or "") if chain else "",
    }


def profile_key(configured: str, **kwargs: Any) -> str:
    """Voice-profile key for a configured microphone (physical hardware slug)."""
    return physical_source(configured, **kwargs)["key"]


# ── JARVIS's own AEC chain (never the owner's legacy file) ──────────────────


def managed_path(directory: str | Path | None = None) -> Path:
    folder = Path(directory) if directory else DEFAULT_FILTER_DIR
    return folder / MANAGED_CONF_NAME


def managed_installed(directory: str | Path | None = None) -> bool:
    return managed_path(directory).is_file()


def _clean_targets(target: Any) -> list[str]:
    """One or more physical microphone names, validated and de-duplicated.

    N modules pointing at the same hardware would fight for the same node; a
    stale config that lists the same microphone twice collapses to one module
    instead of failing to load.
    """
    if isinstance(target, (str, bytes)):
        raw = [target.decode("utf-8", "replace") if isinstance(target, bytes) else target]
    else:
        try:
            raw = [item.decode("utf-8", "replace") if isinstance(item, bytes) else item
                   for item in target]
        except TypeError as exc:
            raise ValueError("microfone alvo inválido") from exc
    cleaned: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if not name:
            raise ValueError("microfone alvo vazio")
        if name.lower().endswith(".monitor"):
            raise ValueError("monitor do alto-falante não pode ser microfone de AEC")
        if name not in cleaned:
            cleaned.append(name)
    if not cleaned:
        raise ValueError("microfone alvo vazio")
    return cleaned


def _series(value: Any, count: int, default: Any) -> list[Any]:
    """Per-module parameter: a scalar repeats, a sequence is padded."""
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        return [value] * count
    items = list(value)[:count]
    return items + [default] * (count - len(items))


def _module_block(
    target: str,
    *,
    rate: int,
    channels: int,
    node: str,
    capture: str,
    label: str,
) -> str:
    position = "[ FL FR ]" if channels >= 2 else "[ MONO ]"
    return f"""    {{ name = libpipewire-module-echo-cancel
        flags = [ nofail ]
        args = {{
            library.name   = aec/libspa-aec-webrtc
            audio.rate     = {rate}
            node.latency   = 1024/{rate}
            monitor.mode   = true
            audio.channels = {channels}
            audio.position = {position}

            capture.props = {{
                node.name     = "{capture}"
                target.object = "{target}"
            }}

            source.props = {{
                node.name        = "{node}"
                node.description = "Microfone AEC (JARVIS: {label})"
                media.class      = "Audio/Source"
            }}

            aec.args = {{
                webrtc.noise_suppression = true
                webrtc.high_pass_filter  = true
            }}
        }}
    }}"""


_SINGLE_HEADER = """# AEC gerenciado pelo JARVIS — gerado por core/mic_route.py.
#
# NAO EDITE A MAO: `python -m core.mic_route follow` reescreve este arquivo
# quando o microfone ativo muda. O arquivo 50-echo-cancel.conf do dono nao e
# tocado. O daemon do filtro e separado do audio do sistema
# (systemctl --user restart filter-chain.service).
#
# Topologia:
#   captura   : {target}
#   referencia: monitor do sink DEFAULT
#   fonte     : {node} (Audio/Source) -> escolha este nome em input_device
context.modules = [
"""


def _multi_header(targets: list[str], nodes: list[str]) -> str:
    lines = "\n".join(
        f"#   [{index}] captura: {target} -> fonte {node}"
        for index, (target, node) in enumerate(zip(targets, nodes), start=1))
    return f"""# AEC gerenciado pelo JARVIS — gerado por core/mic_route.py.
#
# NAO EDITE A MAO: `python -m core.mic_route follow` reescreve este arquivo
# quando o microfone ativo muda. O arquivo 50-echo-cancel.conf do dono nao e
# tocado. O daemon do filtro e separado do audio do sistema
# (systemctl --user restart filter-chain.service).
#
# Topologia: {len(targets)} microfones, um modulo e uma fonte POR microfone.
#   O JARVIS abre um stream em cada fonte e COMPARA os sinais — nunca soma
#   nem mixa os canais; somar dois microfones com atrasos diferentes
#   comb-filtra a voz e dobra o ruido da sala.
{lines}
#   referencia: monitor do sink DEFAULT (por modulo)
#
# O primeiro modulo mantem o nome {MANAGED_NODE} para nao mudar a config
# salva nem a chave do perfil de voz; os demais sao {MANAGED_NODE}_2, _3, ...
context.modules = [
"""


def render_managed_conf(
    target: Any,
    *,
    rate: Any = DEFAULT_RATE,
    channels: Any = DEFAULT_CHANNELS,
    node: str = MANAGED_NODE,
    short: Any = "",
) -> str:
    """The filter-chain file JARVIS owns, one echo-cancel module per microphone.

    ``target`` may be one name or a sequence; ``rate``, ``channels`` and
    ``short`` take a scalar (applied to all) or one value per microphone. The
    first module is rendered with the historic node names (``jarvis_aec_source``
    / ``jarvis_aec_capture``) so an existing install, its saved ``input_device``
    and its voice profile key keep working unchanged.

    Pure text generation: nothing is written and no service is touched here.
    """
    targets = _clean_targets(target)
    count = len(targets)
    rates = [int(value or DEFAULT_RATE)
             for value in _series(rate, count, DEFAULT_RATE)]
    chans = [int(value or DEFAULT_CHANNELS)
             for value in _series(channels, count, DEFAULT_CHANNELS)]
    labels = [str(value or "") for value in _series(short, count, "")]
    nodes = managed_node_names(count)
    captures = managed_capture_names(count)
    if count > 1 and node != MANAGED_NODE:
        # A caller-supplied base name keeps its indexing, but the bare name is
        # still the first module so nothing that points at it breaks.
        nodes = [node] + [f"{node}_{i}" for i in range(2, count + 1)]

    blocks = [
        _module_block(
            targets[index],
            rate=rates[index],
            channels=chans[index],
            node=nodes[index],
            capture=captures[index],
            label=(labels[index] or targets[index])[:60],
        )
        for index in range(count)
    ]
    body = ",\n".join(blocks)
    header = (_SINGLE_HEADER.format(target=targets[0], node=nodes[0])
              if count == 1 else _multi_header(targets, nodes))
    return header + body + "\n]\n"


def write_managed_conf(
    target: Any,
    *,
    directory: str | Path | None = None,
    rate: Any = DEFAULT_RATE,
    channels: Any = DEFAULT_CHANNELS,
    short: Any = "",
) -> dict[str, Any]:
    """Atomically write JARVIS's managed AEC file. No other file is touched.

    ``target`` may be one microphone or several; each gets its own echo-cancel
    module in this one file (see render_managed_conf). Refuses a monitor source
    (that is the machine's own playback), a missing target and — defensively —
    any path that is not the managed name.

    This is the only side effect allowed here: generate text and write OUR
    file. It does not restart the filter-chain daemon and does not touch
    ``~/.config/pipewire`` unless the caller pointed `directory` there.
    """
    targets = _clean_targets(target)
    path = managed_path(directory)
    if path.name != MANAGED_CONF_NAME:
        raise ValueError(f"arquivo gerenciado inesperado: {path.name}")
    content = render_managed_conf(targets, rate=rate, channels=channels,
                                  short=short)
    previous = ""
    if path.is_file():
        try:
            previous = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            previous = ""
    if previous == content:
        return {"path": str(path), "changed": False, "target": targets[0],
                "targets": targets}
    path.parent.mkdir(parents=True, exist_ok=True)
    if previous:
        # Our own previous generation, kept one step back for a manual undo.
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            backup.write_text(previous, encoding="utf-8")
        except OSError:
            pass
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".jarvis-aec-",
                                    suffix=".conf.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return {"path": str(path), "changed": True, "target": targets[0],
            "targets": targets}


def restart_filter_chain(timeout: int = 15) -> dict[str, Any]:
    """Restart ONLY the filter-chain user service (system audio untouched)."""
    if not shutil.which("systemctl"):
        return {"ok": False, "detail": "systemctl ausente"}
    try:
        out = subprocess.run(
            ["systemctl", "--user", "restart", "filter-chain.service"],
            capture_output=True, timeout=timeout, check=False,
        )
    except Exception as e:  # noqa: BLE001 - reported, never raised
        return {"ok": False, "detail": str(e)[:200]}
    detail = (out.stderr or out.stdout or b"").decode("utf-8", "replace").strip()
    return {"ok": out.returncode == 0, "detail": detail[:300]}


def _description_label(chain: Mapping[str, Any]) -> str:
    """The 'C920' part of ``Microfone AEC (JARVIS: C920)``."""
    match = re.search(r"JARVIS:\s*([^)]+)\)", str(chain.get("description") or ""))
    return match.group(1).strip() if match else ""


def _managed_modules(directory: str | Path | None = None) -> list[dict[str, Any]]:
    """Echo-cancel modules JARVIS declared in its own file, in file order.

    Only ``render_managed_conf`` output is understood: the owner's legacy
    ``50-echo-cancel.conf`` never participates here, even if it sits in the same
    directory."""
    path = managed_path(directory)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    modules: list[dict[str, Any]] = []
    for chain in parse_filter_chains(text):
        if not is_managed_node(chain.get("node_name")):
            continue
        modules.append({
            "node": str(chain.get("node_name") or ""),
            "target": str(chain.get("target") or ""),
            "rate": int(chain.get("rate") or DEFAULT_RATE),
            "channels": int(chain.get("channels") or DEFAULT_CHANNELS),
            "short": _description_label(chain),
        })
    return modules


def follow_managed(
    configured: str,
    *,
    target: str = "",
    directory: str | Path | None = None,
    restart: bool = True,
) -> dict[str, Any]:
    """Retarget JARVIS's AEC at the physical mic behind ``configured``.

    Only acts when the configured chain IS one of the managed nodes — the bare
    ``jarvis_aec_source`` or one of the indexed ``_2``, ``_3``… sources. A
    legacy chain (the owner's 50-echo-cancel.conf) is reported, never
    rewritten: replacing it would require a backup decision that belongs to him.

    With several modules in the file, only the module carrying the configured
    chain is retargeted; the other microphones keep their hardware and their
    node names. ``target`` is the NEW physical source (the one that just became
    the default capture); without it the current chain target would be kept,
    which is the microphone that was just abandoned.
    """
    route = physical_source(configured, chains=read_filter_chains(directory))
    if not is_managed_node(route["chain"]):
        return {"written": False, "restarted": False, "route": route,
                "reason": "AEC não gerenciado pelo JARVIS "
                          f"(cadeia={route['chain'] or route['source'] or '?'})"}
    physical = str(target or route.get("physical") or "")
    if (not physical or physical.lower().endswith(".monitor")
            or is_aec_source(physical)):
        return {"written": False, "restarted": False, "route": route,
                "reason": "microfone físico não resolvido"}
    rows = list_sources()
    row = next((r for r in rows if r.get("name") == physical), None)
    rate = int((row or {}).get("rate") or DEFAULT_RATE)
    channels = _spec_channels((row or {}).get("spec") or "") or DEFAULT_CHANNELS

    modules = _managed_modules(directory)
    if not modules:
        modules = [{"node": MANAGED_NODE, "target": physical, "rate": rate,
                    "channels": channels, "short": physical}]
    else:
        hit = next((m for m in modules if m["node"] == route["chain"]), None)
        if hit is None:
            hit = modules[0]
        hit["target"] = physical
        hit["rate"] = rate
        hit["channels"] = channels
        hit["short"] = physical
    kept: list[dict[str, Any]] = []
    for module in modules:
        if not any(other["target"] == module["target"] for other in kept):
            kept.append(module)

    written = write_managed_conf(
        [module["target"] for module in kept],
        directory=directory,
        rate=[module["rate"] for module in kept],
        channels=[module["channels"] for module in kept],
        short=[module["short"] for module in kept],
    )
    restarted: dict[str, Any] = {"ok": False, "detail": "sem alteração"}
    if written["changed"] and restart:
        restarted = restart_filter_chain()
    return {"written": bool(written["changed"]), "restarted": bool(restarted.get("ok")),
            "restart_detail": restarted.get("detail", ""), "route": route,
            "path": written["path"], "target": physical,
            "targets": [module["target"] for module in kept], "rate": rate,
            "channels": channels, "reason": ""}


# ── status for the UI/command ───────────────────────────────────────────────


def status_rows(
    *,
    configured: str = "",
    profile_keys: Iterable[str] = (),
    directory: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Every capture source with its physical key, activity and enrolment."""
    rows = list_sources()
    active = default_source()
    route = physical_source(configured, sources=rows,
                            chains=read_filter_chains(directory), default=active)
    known = {str(key) for key in profile_keys}
    out: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("name") or "")
        if not name or row.get("monitor"):
            continue
        item = physical_source(name, sources=rows,
                               chains=read_filter_chains(directory), default=active)
        out.append({
            "name": name,
            "key": item["key"],
            "physical": item["physical"],
            "aec": item["aec"],
            "active": bool(name == active),
            "in_use": bool(name == route["source"] or item["key"] == route["key"]),
            "profile": item["key"] in known,
            "rate": row.get("rate") or 0,
            "state": row.get("state") or "",
        })
    return out


def describe(rows: list[dict[str, Any]]) -> str:
    """One spoken/loggable sentence for the status command."""
    if not rows:
        return "não consegui listar as fontes de captura"
    parts = []
    for row in rows:
        tag = "ativa" if row.get("active") else ("em uso" if row.get("in_use") else "ociosa")
        profile = "com cadastro" if row.get("profile") else "sem cadastro"
        parts.append(f"{row['name']} ({tag}, {profile})")
    return "; ".join(parts)


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="python -m core.mic_route",
        description="Mostra/segue o microfone físico usado pelo JARVIS.")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status", help="lista fontes, ativa e cadastro de voz")
    inst = sub.add_parser("install", help="cria a cadeia de AEC gerenciada")
    inst.add_argument("--device", default="",
                      help="fonte física (padrão: fonte padrão do sistema)")
    inst.add_argument("--no-restart", action="store_true")
    follow = sub.add_parser("follow", help="aponta o AEC gerenciado para a fonte ativa")
    follow.add_argument("--device", default="")
    follow.add_argument("--no-restart", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "status":
        configured = os.environ.get("JARVIS_INPUT_DEVICE", "")
        print(json.dumps(status_rows(configured=configured), ensure_ascii=False, indent=2))
        return 0
    if args.cmd in ("install", "follow"):
        target = args.device.strip() or default_source()
        if not target:
            print("sem fonte de captura disponível")
            return 2
        route = physical_source(target)
        physical = route["physical"]
        rows = list_sources()
        row = next((r for r in rows if r.get("name") == physical), None)
        rate = int((row or {}).get("rate") or DEFAULT_RATE)
        channels = _spec_channels((row or {}).get("spec") or "") or DEFAULT_CHANNELS
        written = write_managed_conf(physical, rate=rate, channels=channels,
                                     short=physical)
        restarted = {"ok": False, "detail": "sem alteração"}
        if written["changed"] and not args.no_restart:
            restarted = restart_filter_chain()
        print(json.dumps({"path": written["path"], "changed": written["changed"],
                          "target": physical, "rate": rate, "channels": channels,
                          "restarted": restarted}, ensure_ascii=False, indent=2))
        print(f"\nPara usar: input_device = {MANAGED_NODE!r} no config.")
        return 0 if written["changed"] and (restarted.get("ok") or args.no_restart) else 3
    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(_main())
