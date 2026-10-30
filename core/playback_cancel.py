"""
Silence, in the microphone stream, everything the PC's speakers are playing.

The pulse/pipewire sink monitor is a capture device whose signal IS what the
machine plays — music, YouTube, JARVIS's own voice. Measured on this machine,
the mic waveform is NOT a faithful copy of it (a webcam mic with AGC and noise
shaping caps the best alignment correlation around 0.33), so subtracting a
fitted copy leaves too much. Energy, on the other hand, is faithful: while
only the speakers play, the mic/monitor RMS ratio sits on a stable room gain;
the moment the user speaks, the mic gains energy the monitor never had and
the ratio jumps.

So each mic block is classified: ratio on the room gain → the block is the
machine's own sound and is replaced by silence BEFORE the segmenter sees it,
which keeps a running song from ever opening an utterance (and from filling
the pre-roll of a real one); ratio above it → the block carries the user and
passes untouched. Ducking still lowers the music under a real utterance, and
the name gate stays as the last backstop.
"""
from __future__ import annotations

import subprocess
import threading
from collections import deque

import numpy as np

SR = 16000
BLOCK = 1024                     # 64 ms, the same block the mic callback uses
_HISTORY_BLOCKS = 8              # just enough to read the monitor's level now
_MONITOR_FLOOR = 1e-4            # below this the machine is silent
_SPEECH_MARGIN = 2.0             # ratio over the room gain that counts as voice
_MIC_FLOOR = 0.01                # ~ -40 dBFS: below this nobody is speaking
_WARMUP_BLOCKS = 30              # ~2 s to learn the room gain before gating


def _pactl(*args: str) -> str:
    # C locale: the field labels are translated here, and we parse them by name.
    import os
    env = dict(os.environ, LANG="C", LC_ALL="C")
    try:
        r = subprocess.run(["pactl", *args], capture_output=True, timeout=5, env=env)
        return r.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def _default_sink_name() -> str:
    """The sink the machine actually plays through, per the pulse server."""
    for line in _pactl("info").splitlines():
        if "default sink" in line.lower():
            return line.split(":", 1)[1].strip()
    return ""


def find_monitor_device() -> str | None:
    """The NAME of the source that captures the default sink's output.

    Asked entirely through pactl (a separate process): calling
    sounddevice.query_devices() here — from the event loop thread, while the
    mic and playback streams are open on the audio thread — tripped PulseAudio's
    threaded-mainloop assertion and aborted the whole app, intermittently, at
    startup. PortAudio stays out of this module completely.
    """
    sink = _default_sink_name()
    wanted = f"{sink}.monitor" if sink else ""
    fallback = None
    for line in _pactl("list", "short", "sources").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[1]
        if not name.lower().endswith(".monitor"):
            continue
        if wanted and name == wanted:
            return name
        if fallback is None:
            fallback = name
    return fallback


class PlaybackCanceller:
    """Reads the monitor through a `parec` pipe — no PortAudio in this process."""

    def __init__(self, device: str) -> None:
        self._device = device
        self._buf = deque(maxlen=_HISTORY_BLOCKS)
        self._lock = threading.Lock()
        self._proc = None
        self._reader: threading.Thread | None = None
        self._ratios: deque = deque(maxlen=120)
        self._warm = 0
        self.last_ratio = 0.0    # diagnostics: mic/monitor energy of last block
        self.last_gate = False   # diagnostics: True when the block was silenced

    def start(self) -> bool:
        import shutil
        if shutil.which("parec") is None:
            print("[Audio] parec não encontrado — cancelador desligado")
            return False
        try:
            self._proc = subprocess.Popen(
                ["parec", f"--device={self._device}", "--rate=16000",
                 "--channels=1", "--format=s16le", "--latency-msec=50"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"[Audio] playback canceller failed to open: {e}")
            self._proc = None
            return False
        self._reader = threading.Thread(target=self._pump, daemon=True,
                                        name="monitor-reader")
        self._reader.start()
        return True

    def _pump(self) -> None:
        """parec stdout → 64 ms float blocks in the ring, forever."""
        rest = b""
        while self._proc is not None and self._proc.poll() is None:
            chunk = self._proc.stdout.read(2048 - len(rest) % 2048) or b""
            if not chunk:
                break
            data = rest + chunk
            for i in range(0, len(data) - 2047, 2048):
                block = np.frombuffer(data[i:i + 2048], dtype=np.int16)
                with self._lock:
                    self._buf.append(block.astype(np.float32) / 32768.0)
            rest = data[len(data) // 2048 * 2048:]
        print("[Audio] monitor pipe closed — playback canceller off")

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    # ── per mic block ───────────────────────────────────────────────────────

    def clean(self, mic: np.ndarray) -> np.ndarray:
        """The mic block, or silence when the block is only the machine's sound."""
        x = np.asarray(mic, dtype=np.float32).ravel()
        with self._lock:
            if not self._buf:
                return mic
            mon = self._buf[-1]
        mon_rms = float(np.sqrt(np.mean(mon * mon)))
        if mon_rms < _MONITOR_FLOOR:
            self.last_ratio, self.last_gate = 0.0, False
            return mic                       # nothing is playing: hands off
        mic_rms = float(np.sqrt(np.mean(x * x)))
        ratio = mic_rms / mon_rms
        self.last_ratio = ratio
        if self._warm < _WARMUP_BLOCKS:
            self._warm += 1
            self._ratios.append(ratio)
            self.last_gate = False
            return mic                       # still learning the room gain
        gain = float(np.median(self._ratios))
        if ratio > gain * _SPEECH_MARGIN and mic_rms > _MIC_FLOOR:
            # Energy the monitor never had: the user, over whatever plays.
            self.last_gate = False
            return mic
        self._ratios.append(ratio)
        self.last_gate = True
        return np.zeros_like(mic)            # the machine, talking to itself


def start_if_possible() -> PlaybackCanceller | None:
    name = find_monitor_device()
    if name is None:
        print("[Audio] no sink monitor found — playback canceller off")
        return None
    c = PlaybackCanceller(name)
    if not c.start():
        return None
    print(f"[Audio] playback canceller on monitor: {name}")
    return c


if __name__ == "__main__":
    # Self-test: for a few seconds, print the mic/monitor energy ratio and
    # whether the block was silenced. With music playing and nobody speaking,
    # almost every line should read GATE; talking over it should flip to pass.
    import time
    import sounddevice as sd
    c = start_if_possible()
    if c is None:
        raise SystemExit("no monitor source available")
    seen = []

    def _mic(indata, frames, _t, status):
        c.clean(indata)
        seen.append((float(np.sqrt(np.mean(np.asarray(indata, np.float32) ** 2))),
                     c.last_ratio, c.last_gate))

    with sd.InputStream(samplerate=SR, channels=1, dtype="int16",
                        blocksize=BLOCK, callback=_mic):
        for _ in range(8):
            time.sleep(1.0)
            if seen:
                gates = sum(1 for s in seen if s[2])
                mic_rms = seen[-1][0]
                print(f"mic {mic_rms:.4f}  ratio {seen[-1][1]:.2f}  "
                      f"silenciados {gates}/{len(seen)} blocos")
            seen.clear()
    c.stop()
