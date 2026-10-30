"""
Duck every OTHER application while the user talks to the assistant or the
assistant talks to the user.

The request is exact: when a conversation starts, every open application's
volume drops to 20% of what it was AT THAT MOMENT, and comes back to exactly
that value when the exchange ends — not the master volume, and not the
assistant's own stream, which keeps playing at full level.

PulseAudio/PipeWire exposes one sink-input per playing stream, with the owner's
PID in its properties, so "every app except this one" is exactly "every
sink-input whose application.process.id is not ours". Volumes are read and
written with pactl, the same tool actions/computer_settings.py already uses.

Calls run on their own threads: the callers include the audio callback and the
STT worker, where waiting on a spawned process would be a dropout. down() and
up() are idempotent — a second down() while ducked keeps the ORIGINAL volumes
saved, so overlapping triggers (utterance opens, then the reply starts) cannot
stack two ducks into 4% or lose the restore point.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading

DUCK_FACTOR = 0.20          # each app keeps a fifth of its current volume
_SAVED_LOCK = threading.Lock()
_saved: dict[str, int] = {}     # sink-input index → volume percent to restore

_INPUT_RE = re.compile(r"Sink Input #(\d+)")
_PID_RE = re.compile(r'application\.process\.id\s*=\s*"(\d+)"')
_VOL_RE = re.compile(r"Volume:.*?(\d+)%")
_NAME_RE = re.compile(r'application\.name\s*=\s*"([^"]*)"')
_SPEC_RE = re.compile(r"Sample Specification:.*?(\d+)ch\s*(\d+)Hz")


def _pactl(*args: str) -> str:
    # Forced C locale: the field labels are translated ("Entrada do destino"
    # here), and the parser below reads them by name.
    import os as _os
    env = dict(_os.environ, LANG="C", LC_ALL="C")
    try:
        r = subprocess.run(["pactl", *args], capture_output=True, timeout=5, env=env)
        return r.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def _inputs() -> list[tuple[str, str, int, str, str]]:
    """(index, owner pid, volume percent, app name, sample spec) per stream."""
    out = []
    for block in _pactl("list", "sink-inputs").split("Sink Input #")[1:]:
        m_idx = _INPUT_RE.match("Sink Input #" + block)   # keep the API honest
        idx = m_idx.group(1) if m_idx else block.split("\n", 1)[0].strip()
        pid = _PID_RE.search(block)
        vol = _VOL_RE.search(block)
        name = _NAME_RE.search(block)
        spec = _SPEC_RE.search(block)
        if vol:
            out.append((idx, pid.group(1) if pid else "",
                        int(vol.group(1)),
                        name.group(1) if name else "",
                        spec.group(0) if spec else ""))
    return out


def _is_own(name: str, spec: str) -> bool:
    """The assistant's own playback stream.

    PortAudio does not publish application.process.id on PipeWire (measured:
    the property is simply absent), so the PID alone cannot identify us — and
    ducking our own voice is exactly what made her inaudible at 20%, a volume
    PipeWire then remembered per application name. The signature below is what
    our stream uniquely is: this process's Python, mono, at the 24 kHz the
    playback queue uses.
    """
    return "python" in name.lower() and "24000Hz" in spec and "1ch" in spec


def down() -> None:
    """Every other application to 20% of its current volume. Idempotent."""
    def _run() -> None:
        with _SAVED_LOCK:
            if _saved:
                return                      # already ducked: keep the originals
            own = str(os.getpid())
            for idx, pid, pct, name, spec in _inputs():
                if pid == own or _is_own(name, spec):
                    continue                # the assistant's own voice stays
                target = max(1, int(round(pct * DUCK_FACTOR)))
                _pactl("set-sink-input-volume", idx, f"{target}%")
                _saved[idx] = pct
            if _saved:
                print(f"[Duck] {len(_saved)} aplicativo(s) a 20% enquanto "
                      f"a conversa acontece")
    threading.Thread(target=_run, daemon=True, name="duck-down").start()


def up() -> None:
    """Give every ducked application back the volume it had. Idempotent."""
    def _run() -> None:
        with _SAVED_LOCK:
            if not _saved:
                return
            for idx, pct in _saved.items():
                _pactl("set-sink-input-volume", idx, f"{pct}%")
            print(f"[Duck] volume devolvido a {len(_saved)} aplicativo(s)")
            _saved.clear()
    threading.Thread(target=_run, daemon=True, name="duck-up").start()
