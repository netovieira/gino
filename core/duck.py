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

UV-3 (prova no vivo): the exchange itself is now measurable. Every real
transition writes one canonical line to ``logs/activity.log``::

    [2026-11-07 17:40:12] duck_down: seq=7 gatilho="core.groq_stt:_open_segment" n=1 apps="Firefox:60->12"
    [2026-11-07 17:40:19] duck_up: seq=7 gatilho="core.groq_stt:_close" n=1 apps="Firefox:12->60" preso_s=7.4

``seq`` pairs the two halves of one exchange, ``gatilho`` names the exact
``módulo:função`` that opened or closed the duck (the watchdog releasing a
stuck duck shows up as ``core.duck:_watchdog_loop``), ``apps`` is the per-app
before→after percent that the owner's rule promises (20% of what the app was
playing AT THAT MOMENT, our own voice never touched). Re-entrant down() calls
are deliberately NOT logged: they re-assert the same duck, and a log full of
them would read as N exchanges. Writing never raises and never blocks a duck.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

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
_saved_names: dict[str, str] = {}   # sink-input index → app name (só o log)
_saved_targets: dict[str, int] = {}  # index → nível do duck (só o log)
_seq = 0                        # número do duck atual: casa down() com up()
_opened_at = 0.0                # monotonic de quando o duck abriu (preso_s)
_beat: float = 0.0              # monotonic time of the last sign of speech
_probe = None                   # optional external "someone is speaking" test
_watchdog: threading.Thread | None = None
_wake = threading.Event()       # set only to end the watchdog (tests/shutdown)

# UV-3: a prova da sequência vive aqui. Um caminho de módulo (e não uma
# constante congelada) para o teste poder apontar para um tmp dir.
ACTIVITY_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "activity.log"
_APPS_FIELD_CHARS = 240         # a linha do log é uma só, e cortada

_INPUT_RE = re.compile(r"Sink Input #(\d+)")
_PID_RE = re.compile(r'application\.process\.id\s*=\s*"(\d+)"')
_VOL_RE = re.compile(r"Volume:.*?(\d+)%")
_NAME_RE = re.compile(r'application\.name\s*=\s*"([^"]*)"')
_SPEC_RE = re.compile(r"Sample Specification:.*?(\d+)ch\s*(\d+)Hz")


def _stamp() -> str:
    # Hora LOCAL, como toda linha que o dono lê no activity.log (ui.py).
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _one_line(value: object, limit: int = _APPS_FIELD_CHARS) -> str:
    """Texto de uma linha só, cortado — o que vai no activity.log."""
    return " ".join(str(value if value is not None else "").split())[:limit]


def _append_activity(event: str, fields: str) -> None:
    """Uma linha ``[data hora] evento: campos`` no activity.log.

    Silenciosa por construção: registrar a prova nunca pode derrubar o duck
    (disco cheio, diretório ausente, permissão) nem o áudio da conversa.
    """
    try:
        path = Path(ACTIVITY_LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{_stamp()}] {event}: {fields}\n")
    except Exception as exc:                    # noqa: BLE001 — prova é acréscimo
        print(f"[Duck] registro no activity.log falhou: {exc}")


def _caller_tag(depth: int = 2) -> str:
    """``módulo:função`` de quem abriu/fechou o duck.

    ``depth=2`` porque a chamada sai de dentro da função instrumentada
    (down/up/register_speech_probe): frame 1 é ela, frame 2 é o chamador real.
    Lido ANTES de descer para a thread — no worker o frame do chamador já
    desapareceu. É a proveniência que o UV-3 pede ("quem mandou abaixar?"), e
    ``sys._getframe`` custa um atributo de frame, sem stack trace e sem I/O.
    """
    try:
        frame = sys._getframe(depth)
    except Exception:
        return "?"
    try:
        module = str(frame.f_globals.get("__name__", "?"))
        return _one_line(f"{module}:{frame.f_code.co_name}", 80)
    except Exception:
        return "?"


def _apps_field(antes: dict[str, int], depois: dict[str, int],
                names: dict[str, str]) -> str:
    """``nome:antes->depois`` por aplicativo.

    ``antes``/``depois`` têm as MESMAS chaves (são os dicts do mesmo duck),
    então a ordem vem de ``depois`` — a do down() é o nível aplicado, a do
    up() é o volume devolvido.
    """
    parts = []
    for idx, after in depois.items():
        name = names.get(idx) or f"sink#{idx}"
        before = antes.get(idx, after)
        parts.append(f"{_one_line(name, 40)}:{before}->{after}")
    return _one_line(" ".join(parts)) if parts else "nenhum"


def _log_duck(event: str, seq: int, gatilho: str, antes: dict[str, int],
              depois: dict[str, int], names: dict[str, str], n: int,
              held_s: float | None = None) -> None:
    """A linha do UV-3: sequência completa, pareada por ``seq``.

    Chamada FORA de _SAVED_LOCK e com os dicts já copiados por quem abriu ou
    fechou o duck — registrar a prova não segura o lock nem corre o risco de
    reentrância. ``n`` é o número de aplicativos afetados, sempre o mesmo nos
    dois lados do par.
    """
    fields = (f'seq={seq} gatilho="{gatilho}" n={n} '
              f'apps="{_apps_field(antes, depois, names)}"')
    if event == "duck_down":
        fields += f" fator={DUCK_FACTOR:.2f}"
    if held_s is not None:
        fields += f" preso_s={held_s:.1f}"
    _append_activity(event, fields)


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
    # UV-3: prova de que a casa tem sonda de fala de verdade — sem essa linha
    # o log não diz se o watchdog está sendo enganado pelo stream ócio.
    _append_activity("duck_probe", (
        f'gatilho="{_caller_tag()}" '
        f'sonda="{_one_line(getattr(fn, "__qualname__", None) or "removida", 80)}"'))


def _own_stream_active() -> bool:
    """A fallback "the assistant may be talking" signal for hosts that never
    registered a speech probe. NOT trustworthy on its own: main.py opens the
    24 kHz playback stream once per session and leaves it started, so pactl
    keeps listing it while the room is silent (measured live) — an idle stream
    is not speech. Real playback refreshes the heartbeat itself (every ≤200 ms
    batch runs set_speaking(True) → down()), which is why the watchdog asks
    the probe first and never lowers a volume on its own.
    """
    try:
        for _idx, _pid, _pct, name, spec in _inputs():
            if _is_own(name, spec):
                return True
    except Exception:
        pass
    return False


def _speech_alive() -> bool:
    # A host probe is authoritative: a playback stream can stay open while
    # silent, and "stream exists" then held the duck forever.
    if _probe is not None:
        try:
            return bool(_probe())
        except Exception:
            pass
    return _own_stream_active()


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
    # A proveniência é lida AQUI, no frame de quem chamou: dentro da thread o
    # chamador já desapareceu.
    gatilho = _caller_tag()

    def _run() -> None:
        global _seq, _opened_at
        antes: dict[str, int] = {}
        depois: dict[str, int] = {}
        nomes: dict[str, str] = {}
        seq = 0
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
                _saved_names[idx] = name
                _saved_targets[idx] = target
                antes[idx] = pct
                depois[idx] = target
                nomes[idx] = name
            if _saved:
                _beat = time.monotonic()
                _opened_at = _beat
                _seq += 1
                seq = _seq
                _ensure_watchdog()
                print(f"[Duck] {len(_saved)} aplicativo(s) a 20% enquanto "
                      f"a conversa acontece")
        if depois:
            _log_duck("duck_down", seq, gatilho, antes, depois, nomes,
                      len(depois))
    threading.Thread(target=_run, daemon=True, name="duck-down").start()


def up() -> None:
    """Give every ducked application back the volume it had. Idempotent."""
    gatilho = _caller_tag()

    def _run() -> None:
        global _seq, _opened_at
        antes: dict[str, int] = {}
        depois: dict[str, int] = {}
        nomes: dict[str, str] = {}
        seq = 0
        n = 0
        held: float | None = None
        with _SAVED_LOCK:
            if not _saved:
                return
            original = dict(_saved)
            nomes = dict(_saved_names)
            applied = dict(_saved_targets)
            seq = _seq
            n = len(original)
            held = time.monotonic() - _opened_at if _opened_at else None
            for idx, pct in original.items():
                _pactl("set-sink-input-volume", idx, f"{pct}%")
                antes[idx] = applied.get(idx, pct)
                depois[idx] = pct
            print(f"[Duck] volume devolvido a {len(original)} aplicativo(s)")
            _saved.clear()
            _saved_names.clear()
            _saved_targets.clear()
            _opened_at = 0.0
        if depois:
            _log_duck("duck_up", seq, gatilho, antes, depois, nomes, n, held)
    threading.Thread(target=_run, daemon=True, name="duck-up").start()
