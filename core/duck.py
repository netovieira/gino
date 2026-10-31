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
import time

DUCK_FACTOR = 0.20          # each app keeps a fifth of its current volume
# Safety net for the "duck stuck at 20%" class of bugs: every path that opens
# the duck (segmenter, set_speaking) is supposed to close it when the exchange
# ends, but a held turn (_qoder_hold), an early return or an exception in the
# utterance handler can skip that close. The owner's rule is exact — apps sit
# at 20% ONLY while the user speaks or the assistant speaks — so a duck with no
# sign of either for longer than this grace is released by the watchdog. This
# grace only has to survive the gaps between TTS chunks and the mic beat — the
# owner's spec is "give the volume back when the speech STOPS", not when the
# background work finishes.
DUCK_STALE_S = 3.0          # no live speech for this long ⇒ restore volumes
DUCK_CHECK_S = 1.0          # how often the watchdog looks
_SAVED_LOCK = threading.Lock()
_saved: dict[str, int] = {}     # sink-input index → volume percent to restore
_beat: float = 0.0              # monotonic time of the last sign of speech
_probe = None                   # optional external "someone is speaking" test
_watchdog: threading.Thread | None = None
_wake = threading.Event()       # set only to end the watchdog (tests/shutdown)

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


def heartbeat() -> None:
    """Mark that the duck is justified RIGHT NOW: the user is mid-sentence or
    a reply is being fed. Called from the audio callback, so it must stay a
    bare float assignment — no lock, no I/O, never a dropout."""
    global _beat
    _beat = time.monotonic()


def register_speech_probe(fn) -> None:
    """Optional external liveness test (returns True while real speech is
    happening). The built-in check — our own playback stream exists — already
    covers the assistant; a host can add the user side without the watchdog
    ever having to duck anything itself."""
    global _probe
    _probe = fn


def _own_stream_active() -> bool:
    """The assistant's playback stream is alive: a reply is coming out of the
    speakers right now (the stream is opened per playback, so absent = silent).
    A stray second Python mono-24 kHz stream would only ever extend the grace
    by one check interval — the watchdog never lowers a volume."""
    try:
        for _idx, _pid, _pct, name, spec in _inputs():
            if _is_own(name, spec):
                return True
    except Exception:
        pass
    return False


def _speech_alive() -> bool:
    if _own_stream_active():
        return True
    try:
        if _probe is not None and _probe():
            return True
    except Exception:
        pass
    return False


def _watchdog_loop() -> None:
    while True:
        if _wake.wait(DUCK_CHECK_S):      # globals re-read each cycle
            return                         # (tests tighten the timings)
        with _SAVED_LOCK:
            ducked = bool(_saved)
            stale = time.monotonic() - _beat
        if not ducked or stale <= DUCK_STALE_S:
            continue
        if _speech_alive():
            heartbeat()
            continue
        print(f"[Duck] watchdog: {stale:.0f}s sem fala ativa e o duck ainda "
              f"preso — devolvendo os volumes originais")
        up()


def _ensure_watchdog() -> None:
    global _watchdog
    if _watchdog is None or not _watchdog.is_alive():
        _wake.clear()
        _watchdog = threading.Thread(target=_watchdog_loop,
                                     daemon=True, name="duck-watchdog")
        _watchdog.start()


def down() -> None:
    """Every other application to 20% of its current volume. Idempotent."""
    def _run() -> None:
        with _SAVED_LOCK:
            global _beat
            if _saved:
                _beat = time.monotonic()  # re-assert: the exchange is still on
                return                    # already ducked: keep the originals
            own = str(os.getpid())
            for idx, pid, pct, name, spec in _inputs():
                if pid == own or _is_own(name, spec):
                    continue                # the assistant's own voice stays
                target = max(1, int(round(pct * DUCK_FACTOR)))
                _pactl("set-sink-input-volume", idx, f"{target}%")
                _saved[idx] = pct
            if _saved:
                _beat = time.monotonic()
                _ensure_watchdog()
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
