"""core/multi_mic.py — one microphone stream per source, compared, never mixed.

WHY
    The owner's instruction is literal: capture ALL microphones, each with its
    own echo cancellation, and COMPARE the signals to reject noise. Summing
    them is forbidden: a webcam 40 cm away and a headset on the ear carry the
    same voice with different delays, so adding them comb-filters the speech
    and doubles the room noise. This module does not touch audio devices and
    does not import sounddevice/PortAudio; it receives blocks from one callback
    per stream and decides WHICH stream is hearing a person.

WHAT IT DOES, PER BLOCK CYCLE
    * stores the latest block of every microphone (never adds them);
    * waits until every microphone delivered a NEW block (a cycle), so a slow
      stream can never be compared with a stale copy of itself;
    * feeds one ``core.mic_compare.PairComparator`` per pair;
    * aggregates the pairwise verdicts: a ``near`` reading votes for the side
      that is louder; a unique winner selects that microphone for the STT;
      diffuse/ambiguous readings never select a microphone as owner speech.

    The verdict is deliberately conservative: a tie, silence, or a pair that
    does not agree leaves it ``ambiguous`` — the caller feeds that to the
    transcription gate but does not open a turn as the owner's voice.

This module is fed by whoever owns the streams. ``push()`` is safe to call
from the PortAudio callbacks: it takes one lock, copies the caller's block and
does the comparison inside it (measured ~0.85 ms/frame by mic_compare).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import numpy as np

from . import mic_compare
from .echo import EchoGuard


@dataclass(slots=True)
class Decision:
    """One complete comparison cycle.

    ``source`` is the microphone the assistant should listen to: the winner
    when ``verdict`` is ``near``, the loudest one otherwise (so the
    transcription gate still receives the best signal). ``block`` is that
    microphone's whole block — always one stream's data, never a mix.
    ``aec_clean`` is True when the chosen channel's EchoGuard classified the
    block as not-echo (user speech or no output playing).
    """

    verdict: str = mic_compare.AMBIGUOUS
    source: int | None = None
    name: str = ""
    block: Any = None
    votes: tuple[int, ...] = ()
    pairs: int = 0
    compared: int = 0
    aec_clean: bool = True


class MultiMic:
    """Compares N separate microphone streams and picks the one to listen to.

    ``sources`` is a sequence of names (or ``(name, device)`` pairs — the
    device is used only for logs/opening by the caller). For one source the
    class is inert: ``push`` returns None and no comparison runs, so a machine
    with a single microphone keeps exactly the old single-stream path.
    """

    def __init__(
        self,
        sources: Iterable[Any],
        *,
        frame: int = mic_compare.FRAME,
        sample_rate: int = mic_compare.SAMPLE_RATE,
        tolerance_ms: float = 60.0,
        comparator_factory: Callable[..., Any] = mic_compare.PairComparator,
        clock: Callable[[], float] = time.monotonic,
        aec: bool = True,
    ) -> None:
        entries = list(sources)
        if not entries:
            raise ValueError("nenhum microfone")
        self.sources: list[str] = []
        self.devices: list[Any] = []
        for entry in entries:
            if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                self.sources.append(str(entry[0]))
                self.devices.append(entry[1])
            else:
                self.sources.append(str(entry))
                self.devices.append(None)
        self.frame = int(frame) or mic_compare.FRAME
        self.sample_rate = int(sample_rate) or mic_compare.SAMPLE_RATE
        self.tolerance = max(0.0, float(tolerance_ms)) / 1000.0
        self._clock = clock
        self._lock = threading.Lock()
        self._latest: list[tuple[np.ndarray, float] | None] = [None] * len(self.sources)
        self._fresh = [False] * len(self.sources)
        self._comparators = [
            (i, j, comparator_factory(sample_rate=self.sample_rate,
                                      frame=self.frame))
            for i in range(len(self.sources))
            for j in range(i + 1, len(self.sources))
        ]
        self.decision = Decision(name="", pairs=len(self._comparators))
        self.cycles = 0
        # ── AEC por canal: um EchoGuard independente por microfone ──────────
        # Cada canal tem seu próprio guard para que o eco seja cancelado
        # individualmente antes da comparação pairwise. Isso é necessário porque
        # microfones em posições diferentes captam o eco do alto-falante com
        # intensidades e delays diferentes.
        self._aec_enabled = bool(aec)
        self._echo_guards: list[EchoGuard] = (
            [EchoGuard() for _ in self.sources] if self._aec_enabled else []
        )
        # Hysteresis latch: the chosen stream is pinned per phrase. A new
        # source only wins after proving itself on ``latch_wins`` consecutive
        # cycles; an unproven cycle never falls back to argmax (that argmax
        # flip between near-tied microphones made the endless "fonte
        # selecionada" alternation and thrashed the speaker profile).
        self.latch_wins = 3
        self._latched: int | None = None
        self._candidate: int | None = None
        self._candidate_n = 0
        self._held = False

    # ── input from the callbacks ────────────────────────────────────────────
    def push(self, index: int, block: object, at: float | None = None) -> Decision | None:
        """Feed one block from microphone ``index``; None until a cycle closes."""
        if len(self.sources) < 2:
            return None
        if not 0 <= int(index) < len(self.sources):
            return None
        arr = np.asarray(block)
        if arr.ndim == 2:
            arr = arr[:, 0]
        elif arr.ndim > 2:
            arr = arr.reshape(-1)
        if arr.size < self.frame:
            return None
        sample = np.array(arr[: self.frame], copy=True)
        whole = np.array(arr[:], copy=True)
        stamp = self._clock() if at is None else float(at)
        with self._lock:
            self._latest[int(index)] = (sample, whole, stamp)
            self._fresh[int(index)] = True
            if not all(self._fresh):
                return None
            stamps = [item[2] for item in self._latest if item is not None]
            if len(stamps) != len(self.sources):
                return None
            self._fresh = [False] * len(self.sources)
            if max(stamps) - min(stamps) > self.tolerance:
                # The blocks are not from the same instant: comparing them
                # would manufacture a verdict. Wait for the next cycle.
                return None
            decision = self._decide()
            self.decision = decision
            self.cycles += 1
            return decision

    def _decide(self) -> Decision:
        votes: list[int] = []
        compared = 0
        diffuse = 0
        energies = [0.0] * len(self.sources)
        for index, item in enumerate(self._latest):
            if item is not None:
                energies[index] = float(np.dot(item[0], item[0]))
        for i, j, comparator in self._comparators:
            left, right = self._latest[i], self._latest[j]
            reading = comparator.push(left[0], right[0])
            if reading.silent:
                continue
            compared += 1
            if reading.verdict == mic_compare.NEAR:
                winner = i if reading.near == "a" else j
                votes.append(winner)
            elif reading.verdict == mic_compare.DIFFUSE:
                diffuse += 1
        winner = _unique_winner(votes)
        if winner is not None:
            verdict = mic_compare.NEAR
        elif compared and diffuse == compared:
            verdict = mic_compare.DIFFUSE
        else:
            verdict = mic_compare.AMBIGUOUS

        chosen = self._latch(winner, energies)
        if winner is not None and chosen is not None and winner != chosen:
            # The new microphone has not paid for the switch yet; for the
            # gate this cycle is not the owner's speech on the latched source.
            verdict = mic_compare.AMBIGUOUS
        # ── AEC por canal: o bloco do canal escolhido é voz humana? ────────
        aec_clean = True
        if (self._aec_enabled and chosen is not None
                and chosen < len(self._latest)
                and self._latest[chosen] is not None):
            sample, whole, stamp = self._latest[chosen]
            rms = float(np.sqrt(max(0.0,
                np.dot(sample, sample) / max(sample.size, 1))))
            aec_clean = self.is_user_speech(chosen, sample, self.sample_rate,
                                            rms, when=stamp)
        return Decision(
            verdict=verdict,
            source=chosen,
            name=(self.sources[chosen] if chosen is not None else ""),
            block=(None if chosen is None else self._latest[chosen][1]),
            votes=tuple(votes),
            pairs=len(self._comparators),
            compared=compared,
            aec_clean=aec_clean,
        )

    # ── latch histerético: a fonte é travada por frase ──────────────────────
    def _latch(self, winner: int | None, energies: list[float]) -> int | None:
        """Choose the source with a hysteresis latch. NO LOCK HERE: the
        caller ``_decide`` already runs under ``self._lock`` from ``push``.

        Semantics: while held the latched source is pinned; the first cycle
        adopts the winner (or, without proof, the loudest); an unproven cycle
        KEEPS the latch (never re-runs argmax — that flip was the defect);
        a different winner must repeat ``latch_wins`` consecutive cycles
        before the latch moves.
        """
        if self._held and self._latched is not None:
            return self._latched
        fallback = int(np.argmax(energies)) if energies else None
        if self._latched is None:
            # First cycle: adopt the proven winner, else the loudest stream.
            self._latched = winner if winner is not None else fallback
            self._candidate = None
            self._candidate_n = 0
            return self._latched
        if winner is None:
            # No proof this cycle: keep the latch. Falling back to argmax
            # here is exactly where the source alternation was born.
            self._candidate = None
            self._candidate_n = 0
            return self._latched
        if winner == self._latched:
            self._candidate = None
            self._candidate_n = 0
            return self._latched
        if self._candidate != winner:
            self._candidate = winner
            self._candidate_n = 1
        else:
            self._candidate_n += 1
        if self._candidate_n >= self.latch_wins:
            self._latched = winner
            self._candidate = None
            self._candidate_n = 0
        return self._latched

    def hold(self) -> None:
        """Pin the source while the assistant speaks: the mouth cannot change
        ears while the brain is talking (the TTS echo would re-elect)."""
        with self._lock:
            self._held = True

    def release(self) -> None:
        """Unpin after the speech turn: the silence reopens the election."""
        with self._lock:
            self._held = False

    # ── AEC por canal ────────────────────────────────────────────────────────
    def note_output(self, pcm, sr: int, level: float,
                    when: float | None = None) -> None:
        """Feed the assistant's output to EVERY per-channel EchoGuard.

        Each microphone hears the speaker differently (distance, angle, room
        reflections), so each guard learns its own echo gain and floor.
        """
        if not self._aec_enabled:
            return
        for guard in self._echo_guards:
            try:
                guard.note_output(pcm, sr, level, when=when)
            except Exception:
                pass

    def is_user_speech(self, index: int, pcm, sr: int, level: float,
                       when: float | None = None) -> bool:
        """Check one channel's EchoGuard: is this block a human voice?"""
        if not self._aec_enabled or not (0 <= int(index) < len(self._echo_guards)):
            return True
        try:
            return self._echo_guards[int(index)].is_user_speech(
                pcm, sr, level, when=when)
        except Exception:
            return False

    def reset_aec(self) -> None:
        """Drop the echo history on every channel (playback stopped)."""
        for guard in self._echo_guards:
            try:
                guard.reset()
            except Exception:
                pass

    @property
    def aec_enabled(self) -> bool:
        return self._aec_enabled

    def aec_calibrated(self, index: int) -> bool:
        """True once the channel's guard has enough echo data to be trusted."""
        if not self._aec_enabled or not (0 <= int(index) < len(self._echo_guards)):
            return False
        return self._echo_guards[int(index)].calibrated

    def aec_reliable(self, index: int) -> bool:
        """False when the channel's room acoustics are too poor to judge."""
        if not self._aec_enabled or not (0 <= int(index) < len(self._echo_guards)):
            return False
        return self._echo_guards[int(index)].reliable

    # ── introspection ───────────────────────────────────────────────────────
    @property
    def count(self) -> int:
        return len(self.sources)

    def verdict(self) -> str:
        return self.decision.verdict

    def label(self, index: int | None = None) -> str:
        """Honest Portuguese label for logs: never the raw audio level."""
        if index is None:
            index = self.decision.source
        name = self.sources[index] if index is not None and 0 <= index < len(self.sources) else "?"
        return f"{name} ({mic_compare.label_for(self.decision.verdict)})"


def _unique_winner(votes: list[int]) -> int | None:
    if not votes:
        return None
    counts: dict[int, int] = {}
    for vote in votes:
        counts[vote] = counts.get(vote, 0) + 1
    best = max(counts.values())
    winners = [key for key, count in counts.items() if count == best]
    return winners[0] if len(winners) == 1 else None


# ── which physical microphones become sources ───────────────────────────────


def physical_sources(rows: Iterable[dict[str, Any]],
                     *, is_aec: Callable[[str], bool]) -> list[dict[str, Any]]:
    """Real, non-AEC capture sources from ``pactl list short sources`` rows.

    Monitors are the machine's own playback, and an AEC/echo-cancel node is a
    virtual source (feeding it to a second AEC would cancel the cancellation).
    A source the server reports as unavailable is skipped; a suspended one is
    kept — a resting USB microphone is suspended until something opens it.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name or name in seen:
            continue
        if row.get("monitor"):
            continue
        if str(row.get("state") or "").strip().lower() == "unavailable":
            continue
        try:
            if is_aec(name):
                continue
        except Exception:
            pass
        seen.add(name)
        out.append(dict(row))
    return out


def source_details() -> str:
    """Raw ``pactl list sources`` text for the port check below.

    Never raises: if the server is unreachable the caller simply keeps every
    source it already picked. Tests inject their own text into
    ``unavailable_sources`` instead of calling this.
    """
    import os
    import subprocess
    env = dict(os.environ)
    env.update({"LC_ALL": "C", "LANG": "C"})
    try:
        out = subprocess.run(["pactl", "list", "sources"], env=env,
                             capture_output=True, timeout=5, check=False)
    except Exception:
        return ""
    return (out.stdout or b"").decode("utf-8", "replace")


def unavailable_sources(text: str) -> set[str]:
    """Sources whose ACTIVE port is explicitly reported as unavailable.

    A resting USB microphone reports SUSPENDED but keeps a usable port, so it
    must stay in the multi-microphone set. A line-in with nothing plugged in
    also reports SUSPENDED, but its active port says "not available" — opening
    it would only feed room hum into the pairwise comparison and let that hum
    vote against the owner's voice. Only the explicit port verdict removes a
    source; an unknown availability stays.
    """
    blocked: set[str] = set()
    name = ""
    active = ""
    ports: dict[str, bool] = {}

    def _flush() -> None:
        if name and active and ports.get(active) is False:
            blocked.add(name)

    for raw in str(text or "").splitlines():
        line = raw.strip()
        if line.startswith("Source #"):
            _flush()
            name, active, ports = "", "", {}
        elif line.startswith("Name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Active Port:"):
            active = line.split(":", 1)[1].strip()
        elif line.startswith("Ports:") or line.startswith("Formats:"):
            continue
        elif name and "type:" in line and ":" in line:
            key = line.split(":", 1)[0].strip()
            low = line.lower()
            ports[key] = not ("not available" in low or "não disponível" in low
                              or "indisponível" in low)
    _flush()
    return blocked


def channel_count(spec: str) -> int:
    """Channel count from a pactl sample spec ('s16le 2ch 48000Hz')."""
    import re
    match = re.search(r"(\d+)\s*ch\b", str(spec or ""))
    try:
        value = int(match.group(1)) if match else 0
    except ValueError:
        value = 0
    return value if 1 <= value <= 8 else 0


# ── turning the generated conf into live PipeWire nodes, with a safety net ──


def activate_filter_conf(
    targets: Iterable[str],
    *,
    directory: Any = None,
    rates: Iterable[int] | None = None,
    channels: Iterable[int] | None = None,
    write: Callable[..., dict] | None = None,
    restart: Callable[..., dict] | None = None,
    list_sources: Callable[[], list] | None = None,
    probe: Callable[[str], float | None] | None = None,
    required: Iterable[str] | None = None,
    rms_floor: float = 1e-4,
    node_timeout_s: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> dict:
    """Write the multi-module AEC file, restart ONLY its daemon, and verify.

    Steps, in order — each one is undone by ``_restore`` if it fails:
      1. copy the current managed file to a dated backup (``*.backup-...``);
      2. ``write_managed_conf`` (the owner's 50-echo-cancel.conf is never
         touched);
      3. ``systemctl --user restart filter-chain.service``;
      4. verify every ``jarvis_aec_source``/``_2``… node appeared;
      5. when ``probe`` is given, read one real frame per source and require
         RMS above ``rms_floor`` (the probe is injectable so tests never open
         a device — and the running app verifies from its own streams instead,
         because opening a probe next to live streams is what aborts PortAudio).

    AUDIO LEVEL IS NEVER LOGGED: the probe result is reduced to a boolean
    ("captured"/"silent"), not a level. Returns a dict with ``ok`` and the
    exact step that failed in ``failed``/``detail``.
    """
    from pathlib import Path
    import shutil

    from . import mic_route

    names = [str(name or "").strip() for name in targets]
    names = [name for name in names if name]
    if not names:
        return {"ok": False, "changed": False, "failed": "targets",
                "detail": "nenhum microfone alvo"}
    rates = [int(value or mic_route.DEFAULT_RATE) for value in list(rates or [])]
    channels = [int(value or mic_route.DEFAULT_CHANNELS) for value in list(channels or [])]
    if len(rates) < len(names):
        rates += [mic_route.DEFAULT_RATE] * (len(names) - len(rates))
    if len(channels) < len(names):
        channels += [mic_route.DEFAULT_CHANNELS] * (len(names) - len(channels))
    write = write or mic_route.write_managed_conf
    restart = restart or mic_route.restart_filter_chain
    sources_fn = list_sources or mic_route.list_sources

    path = mic_route.managed_path(directory)
    before = None
    try:
        if path.is_file():
            before = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        before = None
    backup_path = ""
    if before is not None:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
        backup_path = str(path.with_name(f"{path.name}.backup-{stamp}"))
        try:
            shutil.copy2(path, backup_path)
        except OSError as exc:
            return {"ok": False, "changed": False, "failed": "backup",
                    "detail": str(exc)[:200], "backup": ""}

    try:
        written = write(names, directory=directory, rate=rates,
                        channels=channels, short=names)
    except Exception as exc:
        return {"ok": False, "changed": False, "failed": "write",
                "detail": f"{type(exc).__name__}: {exc}"[:200],
                "backup": backup_path}

    if not written.get("changed"):
        return {"ok": True, "changed": False, "failed": "", "detail": "",
                "path": str(written.get("path") or path),
                "backup": backup_path, "targets": names, "nodes": []}

    restarted = restart()
    if not restarted.get("ok"):
        detail = str(restarted.get("detail") or "serviço não reiniciou")[:200]
        restored = _restore(path, before, restart)
        return {"ok": False, "changed": True, "failed": "restart",
                "detail": detail, "backup": backup_path, "restored": restored}

    nodes = mic_route.managed_node_names(len(names))
    if sources_fn is not None:
        # The daemon needs a moment to create the new nodes.
        deadline = time.monotonic() + max(0.0, float(node_timeout_s))
        missing: list[str] = list(nodes)
        node_error = ""
        while True:
            try:
                rows = list(sources_fn() or [])
            except Exception as exc:
                rows = []
                node_error = f"{type(exc).__name__}: {exc}"[:160]
            else:
                node_error = ""
            present = {str(row.get("name") or "") for row in rows}
            missing = [node for node in nodes if node not in present]
            if (not missing and not node_error) or time.monotonic() >= deadline:
                break
            sleep(0.2)
        if missing or node_error:
            restored = _restore(path, before, restart)
            return {"ok": False, "changed": True, "failed": "nodes",
                    "detail": node_error or ("nós ausentes: " + ", ".join(missing)),
                    "backup": backup_path, "restored": restored, "nodes": nodes}

    silent: list[str] = []
    if probe is not None:
        wanted = [str(name) for name in required] if required is not None else nodes
        for node in nodes:
            try:
                level = float(probe(node) or 0.0)
            except Exception:
                level = 0.0
            if node in wanted and level < float(rms_floor):
                silent.append(node)
        if silent:
            restored = _restore(path, before, restart)
            return {"ok": False, "changed": True, "failed": "capture",
                    "detail": "sem áudio em: " + ", ".join(silent),
                    "backup": backup_path, "restored": restored, "nodes": nodes}

    return {"ok": True, "changed": True, "failed": "", "detail": "",
            "path": str(written.get("path") or path), "backup": backup_path,
            "targets": names, "nodes": nodes}


def _restore(path: Any, before: str | None, restart: Callable[..., dict]) -> bool:
    """Put the previous file back and restart the daemon again. Best effort."""
    try:
        from pathlib import Path
        import tempfile
        if before is None:
            try:
                Path(path).unlink()
            except OSError:
                pass
        else:
            folder = Path(path).parent
            fd, tmp = tempfile.mkstemp(dir=str(folder), prefix=".jarvis-restore-",
                                       suffix=".conf.tmp")
            with __import__("os").fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(before)
                handle.flush()
                __import__("os").fsync(handle.fileno())
            __import__("os").replace(tmp, str(path))
        result = restart()
        return bool(result.get("ok"))
    except Exception:
        return False

