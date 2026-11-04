"""core/audio_ledger.py — the microphone's history: what was heard, when.

Two artifacts live under one configurable directory (default
``memory/audio_ledger/``):

``events/YYYY-MM-DD.jsonl``
    One line per (device, class) segment: epoch + ISO timestamp, device id,
    ``silencio``/``ruido``/``pessoa``/``tv``/``musica``, confidence, RMS in
    dBFS, duration in seconds and the ``mic_compare`` verdict when the caller
    has one. No transcript, no filename of third-party content, nothing else.

``audio/YYYY-MM-DD/<device>-<start>.ogg``
    Rotated 30 s blocks of the actual audio. If PyAV with libopus is present
    the block is Ogg/Opus at 16 kbps mono; otherwise it is a PCM16 WAV block
    (the fallback, much larger). The encoder actually in use is reported by
    ``ledger.encoder`` and in ``stats()``.

The capture callback only calls :meth:`AudioLedger.push`: it copies the block,
puts it on a bounded queue and returns. Encoding, JSONL writes, rotation and
pruning happen in the ledger's own daemon thread. If the queue is full a block
is dropped and counted — the callback is never allowed to block on disk.

    from core import audio_ledger
    ledger = audio_ledger.AudioLedger()
    reading = source_class.classify(block, 16000)
    ledger.push(block, label=reading.label, confidence=reading.confidence,
                sample_rate=16000, device_id="mic-0", verdict=mic_reading)

``push()`` must be safe from an audio callback at 10–25 ms intervals; measured
cost in the tests is a few microseconds for the copy plus ``Queue.put_nowait``.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

SILENCE = "silencio"
NOISE = "ruido"
PERSON = "pessoa"
TV = "tv"
MUSIC = "musica"
LABELS = (SILENCE, NOISE, PERSON, TV, MUSIC)

ENCODER_OPUS = "opus/ogg"
ENCODER_WAV = "pcm16/wav"

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DIR = BASE_DIR / "memory" / "audio_ledger"
BLOCK_SECONDS = 30.0
EVENT_SECONDS = 1.0
KEEP_DAYS = 7.0
MAX_TOTAL_MB = 1024.0
QUEUE_SIZE = 512
PRUNE_INTERVAL_S = 3600.0
OPUS_BITRATE = 16000

_LABEL_ALIASES = {
    "silencio": SILENCE, "silêncio": SILENCE, "silence": SILENCE,
    "ruido": NOISE, "ruído": NOISE, "noise": NOISE,
    "pessoa": PERSON, "person": PERSON, "voz": PERSON,
    "tv": TV, "televisao": TV, "televisão": TV,
    "musica": MUSIC, "música": MUSIC, "music": MUSIC,
}


def normalize_label(label: object) -> str:
    """Map accents/English aliases to one of the five stored classes."""
    key = str(label or "").strip().lower()
    return _LABEL_ALIASES.get(key, NOISE)


def encoder_available() -> str:
    """The best encoder usable in THIS environment (checked, not assumed)."""
    try:
        import av

        av.Codec("libopus", "w")
        return ENCODER_OPUS
    except Exception:
        return ENCODER_WAV


def _opus_encode(path: Path, pcm: np.ndarray, sample_rate: int) -> None:
    import av

    with av.open(str(path), "w", format="ogg") as container:
        stream = container.add_stream("libopus", rate=int(sample_rate))
        stream.layout = "mono"
        stream.bit_rate = OPUS_BITRATE
        frame_size = int(stream.codec_context.frame_size or 960)
        points = 0
        for start in range(0, int(pcm.size), frame_size):
            block = pcm[start:start + frame_size]
            if block.size < frame_size:
                block = np.pad(block, (0, frame_size - block.size))
            frame = av.AudioFrame.from_ndarray(
                block.reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = int(sample_rate)
            frame.pts = points
            points += frame_size
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


def _wav_encode(path: Path, pcm: np.ndarray, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(int(sample_rate))
        out.writeframes(pcm.tobytes())


@dataclass(slots=True)
class _Chunk:
    samples: np.ndarray
    sample_rate: int
    label: str
    confidence: float
    device: str
    verdict: str
    rms_db: float | None
    ts: float


@dataclass(slots=True)
class _DeviceBuffer:
    sample_rate: int = 0
    parts: list[np.ndarray] = field(default_factory=list)
    samples: int = 0
    start_ts: float = 0.0
    sequence: int = 0


@dataclass(slots=True)
class _EventAccum:
    start_ts: float = 0.0
    end_ts: float = 0.0
    label: str = ""
    power: float = 0.0
    confidence: float = 0.0
    duration: float = 0.0
    verdict: str = ""


class AudioLedger:
    """Continuous, classified audio history. Safe to call from an audio callback."""

    def __init__(
        self,
        directory: str | os.PathLike | None = None,
        *,
        block_seconds: float = BLOCK_SECONDS,
        event_seconds: float = EVENT_SECONDS,
        keep_days: float = KEEP_DAYS,
        max_total_mb: float = MAX_TOTAL_MB,
        queue_size: int = QUEUE_SIZE,
        prune_interval_s: float = PRUNE_INTERVAL_S,
        whatsapp_prune: Callable[[], int] | None = None,
        encoder: str | None = None,
    ) -> None:
        self.directory = Path(directory) if directory is not None else DEFAULT_DIR
        self.audio_dir = self.directory / "audio"
        self.events_dir = self.directory / "events"
        self.block_seconds = max(1.0, float(block_seconds))
        self.event_seconds = max(0.2, float(event_seconds))
        self.keep_days = max(0.0, float(keep_days))
        self.max_total_mb = max(0.0, float(max_total_mb))
        self.prune_interval_s = max(0.0, float(prune_interval_s))
        self._encoder = encoder or encoder_available()
        self._whatsapp_prune = whatsapp_prune or _prune_whatsapp_media
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, int(queue_size)))
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._prune_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._closing = False
        self._devices: dict[str, _DeviceBuffer] = {}
        self._events: dict[str, _EventAccum] = {}
        self._event_file = None
        self._event_day = ""
        self._next_prune = 0.0
        self._pushed = 0
        self._dropped = 0
        self._blocks = 0
        self._events_written = 0
        self._last_error = ""
        self.directory.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)

    # ── public surface ──────────────────────────────────────────────────────

    @property
    def encoder(self) -> str:
        """Actual container/codec now in use (``opus/ogg`` or fallback)."""
        return self._encoder

    def push(
        self,
        chunk: object,
        *,
        label: object = NOISE,
        confidence: float = 0.0,
        sample_rate: int | None = None,
        device_id: object = "default",
        verdict: object = None,
        rms_db: float | None = None,
        timestamp: float | None = None,
    ) -> bool:
        """Queue one block. Returns False only when dropped/closed/invalid.

        ``chunk`` may be float32 mono in -1..1 or int16 PCM; a 2-D block uses
        its first column (the same convention as ``mic_compare``). ``verdict``
        accepts a ``mic_compare.PairReading`` or a plain string.
        """
        if self._closing:
            return False
        try:
            rate = int(sample_rate or 0)
            if rate < 4000 or rate > 192000:
                return False
            arr = np.asarray(chunk)
            if arr.ndim == 2:
                arr = arr[:, 0]
            elif arr.ndim > 2:
                arr = arr.reshape(-1)
            if arr.size == 0:
                return False
            if arr.dtype == np.int16:
                samples = arr.astype(np.float32) * (1.0 / 32768.0)
            elif arr.dtype.kind in "iu":
                samples = arr.astype(np.float32) / float(np.iinfo(arr.dtype).max)
            elif arr.dtype.kind == "f":
                samples = np.array(arr, dtype=np.float32, copy=True)
            else:
                return False
            np.nan_to_num(samples, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
            np.clip(samples, -1.0, 1.0, out=samples)
        except Exception:
            return False

        self._ensure_started()
        item = _Chunk(
            samples=samples,
            sample_rate=rate,
            label=normalize_label(label),
            confidence=_clamp(confidence),
            device=_device_key(device_id),
            verdict=_verdict_text(verdict),
            rms_db=(float(rms_db) if rms_db is not None else None),
            ts=float(timestamp if timestamp is not None else time.time()),
        )
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self._dropped += 1
            return False
        self._pushed += 1
        return True

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every queued block and event has been written."""
        if self._thread is None:
            return True
        done = threading.Event()
        try:
            self._queue.put(("flush", done), timeout=max(0.1, timeout))
        except queue.Full:
            return False
        return bool(done.wait(max(0.1, timeout)))

    def close(self, timeout: float = 15.0) -> None:
        """Flush and stop the writer thread. Idempotent."""
        if self._closing and self._thread is None:
            return
        self._closing = True
        thread = self._thread
        if thread is None:
            return
        try:
            self._queue.put(("stop", None), timeout=max(0.1, timeout))
        except queue.Full:
            pass
        thread.join(max(0.1, timeout))
        if not thread.is_alive():
            self._thread = None

    def run_maintenance(self, *, now: float | None = None) -> dict:
        """Age/size prune of this ledger plus the WhatsApp media cleanup hook."""
        report = {"prune": self.prune(now=now)}
        try:
            removed = int(self._whatsapp_prune() or 0)
            report["whatsapp_media_removed"] = removed
        except Exception as exc:
            report["whatsapp_media_removed"] = 0
            report["whatsapp_media_error"] = f"{type(exc).__name__}: {exc}"[:160]
        return report

    def prune(self, *, now: float | None = None) -> dict:
        """Delete blocks/events older than ``keep_days``, then enforce size cap.

        Idempotent: a second call on an unchanged tree removes nothing. Files
        touched in the last minute are never age-pruned, and symlinks are
        ignored. The size cap only evicts old completed blocks, never one just
        written (a too-small cap is honoured on the following pass).
        """
        moment = time.time() if now is None else float(now)
        cutoff = moment - self.keep_days * 86400.0
        removed_age = 0
        removed_size = 0
        with self._prune_lock:
            for root in (self.audio_dir, self.events_dir):
                for path in root.rglob("*"):
                    if not path.is_file() or path.is_symlink():
                        continue
                    try:
                        age = moment - path.stat().st_mtime
                        if age > 60.0 and path.stat().st_mtime < cutoff:
                            path.unlink()
                            removed_age += 1
                    except OSError:
                        continue

            files: list[tuple[float, int, Path]] = []
            total = 0
            for path in self.audio_dir.rglob("*"):
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                files.append((stat.st_mtime, stat.st_size, path))
                total += stat.st_size
            cap = int(self.max_total_mb * 1024 * 1024)
            if cap > 0 and total > cap:
                files.sort(key=lambda item: item[0])
                for mtime, size, path in files:
                    if total <= cap:
                        break
                    if moment - mtime < 30.0:
                        continue          # the block just written
                    try:
                        path.unlink()
                        total -= size
                        removed_size += 1
                    except OSError:
                        continue
        return {
            "removed_age": removed_age,
            "removed_size": removed_size,
            "bytes": total,
            "files": len(files) - removed_size,
        }

    def stats(self) -> dict:
        """Snapshot for logs/UI; scanning the directory happens only here."""
        total_bytes = 0
        files = 0
        for path in self.audio_dir.rglob("*"):
            try:
                if path.is_file():
                    total_bytes += path.stat().st_size
                    files += 1
            except OSError:
                continue
        return {
            "directory": str(self.directory),
            "encoder": self._encoder,
            "block_seconds": self.block_seconds,
            "keep_days": self.keep_days,
            "max_total_mb": self.max_total_mb,
            "queued": self._queue.qsize(),
            "pushed": self._pushed,
            "dropped": self._dropped,
            "blocks": self._blocks,
            "events": self._events_written,
            "audio_files": files,
            "audio_bytes": total_bytes,
            "last_error": self._last_error,
        }

    def __enter__(self) -> "AudioLedger":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ── writer thread ───────────────────────────────────────────────────────

    def _ensure_started(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._next_prune = time.monotonic() + max(60.0, self.prune_interval_s)
            self._thread = threading.Thread(
                target=self._run, name="audio-ledger", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if isinstance(item, tuple) and item[0] == "stop":
                    self._flush_all()
                    self._close_event_file()
                    return
                if isinstance(item, tuple) and item[0] == "flush":
                    self._flush_all()
                    item[1].set()
                    continue
                self._consume(item)
                self._maybe_maintenance()
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"[:160]
            finally:
                self._queue.task_done()

    def _maybe_maintenance(self) -> None:
        if self.prune_interval_s <= 0:
            return
        now = time.monotonic()
        if now < self._next_prune:
            return
        self._next_prune = now + max(60.0, self.prune_interval_s)
        self.run_maintenance()

    def _consume(self, item: _Chunk) -> None:
        buffer = self._devices.get(item.device)
        if buffer is None or buffer.sample_rate != item.sample_rate:
            if buffer is not None and buffer.samples:
                self._write_device(item.device, buffer)
            buffer = _DeviceBuffer(sample_rate=item.sample_rate)
            self._devices[item.device] = buffer
        if not buffer.parts:
            buffer.start_ts = item.ts
        buffer.parts.append(item.samples)
        buffer.samples += int(item.samples.size)
        self._accumulate_event(item)
        target = max(1, int(round(buffer.sample_rate * self.block_seconds)))
        if buffer.samples >= target:
            self._write_device(item.device, buffer)

    def _write_device(self, device: str, buffer: _DeviceBuffer) -> None:
        if not buffer.parts:
            return
        samples = (buffer.parts[0] if len(buffer.parts) == 1
                   else np.concatenate(buffer.parts))
        buffer.parts.clear()
        buffer.samples = 0
        buffer.sequence += 1
        self._write_audio(device, buffer.sample_rate, samples,
                          buffer.start_ts, buffer.sequence)

    def _write_audio(self, device: str, sample_rate: int,
                     samples: np.ndarray, start_ts: float, sequence: int) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(start_ts))
        folder = self.audio_dir / day
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(start_ts))
        base = f"{device}-{stamp}-{int(sample_rate)}-{sequence:06d}"
        pcm = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)
        path = folder / f"{base}.ogg"
        if self._encoder == ENCODER_OPUS:
            try:
                part = path.with_suffix(".ogg.part")
                _opus_encode(part, pcm, sample_rate)
                os.replace(part, path)
            except Exception as exc:
                self._last_error = f"opus: {type(exc).__name__}: {exc}"[:160]
                self._encoder = ENCODER_WAV
                try:
                    part.unlink(missing_ok=True)
                except OSError:
                    pass
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        if self._encoder == ENCODER_WAV:
            path = folder / f"{base}.wav"
            _wav_encode(path, pcm, sample_rate)
        self._blocks += 1

    def _accumulate_event(self, item: _Chunk) -> None:
        duration = item.samples.size / float(item.sample_rate)
        power = item.rms_db
        if power is None:
            rms = float(np.sqrt(np.mean(item.samples * item.samples)))
            power = 20.0 * math.log10(max(rms, 1e-9))
        old = self._events.get(item.device)
        if (old is not None and old.label
                and old.label != item.label and old.duration > 0.0):
            self._flush_event(item.device, old)
            old = None
        if old is None:
            old = _EventAccum(start_ts=item.ts, label=item.label)
            self._events[item.device] = old
        old.end_ts = item.ts + duration
        old.duration += duration
        old.power += float(10.0 ** (float(power) / 10.0)) * duration
        old.confidence += float(item.confidence) * duration
        if item.verdict:
            old.verdict = item.verdict
        if old.duration >= self.event_seconds:
            self._flush_event(item.device, old)

    def _flush_event(self, device: str, accum: _EventAccum) -> None:
        if accum.duration <= 0.0:
            self._events.pop(device, None)
            return
        mean_power = accum.power / accum.duration
        rms_db = 10.0 * math.log10(max(mean_power, 1e-18))
        line = {
            "ts": round(accum.start_ts, 3),
            "end_ts": round(accum.end_ts, 3),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S",
                                 time.localtime(accum.start_ts)),
            "device": device,
            "label": accum.label,
            "confidence": round(accum.confidence / accum.duration, 4),
            "rms_db": round(rms_db, 2),
            "duration_s": round(accum.duration, 3),
            "verdict": accum.verdict,
        }
        fp = self._event_handle(accum.start_ts)
        fp.write(json.dumps(line, ensure_ascii=False) + "\n")
        fp.flush()
        self._events_written += 1
        self._events.pop(device, None)

    def _event_handle(self, ts: float):
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        if self._event_file is not None and self._event_day == day:
            return self._event_file
        if self._event_file is not None:
            try:
                self._event_file.close()
            except OSError:
                pass
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self._event_file = (self.events_dir / f"{day}.jsonl").open(
            "a", encoding="utf-8")
        self._event_day = day
        return self._event_file

    def _flush_all(self) -> None:
        for device, buffer in list(self._devices.items()):
            if buffer.samples:
                self._write_device(device, buffer)
        for device, accum in list(self._events.items()):
            self._flush_event(device, accum)
        if self._event_file is not None:
            try:
                self._event_file.flush()
            except OSError:
                pass

    def _close_event_file(self) -> None:
        if self._event_file is not None:
            try:
                self._event_file.close()
            except OSError:
                pass
            self._event_file = None
            self._event_day = ""


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


def _device_key(value: object) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "default")).strip("._-")
    text = text[:48] or "default"
    return text


def _verdict_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()[:32]
    try:
        return str(getattr(value, "verdict", "") or "").strip()[:32]
    except Exception:
        return ""


def _prune_whatsapp_media() -> int:
    """Lazy bridge: the ledger's hourly maintenance also trims WhatsApp media."""
    try:
        from core import whatsapp

        return int(whatsapp.prune_media() or 0)
    except Exception:
        return 0


_default_ledger: AudioLedger | None = None
_default_lock = threading.Lock()


def default_ledger() -> AudioLedger:
    """Process-wide ledger for callers that do not want to own one."""
    global _default_ledger
    if _default_ledger is None or _default_ledger._closing:
        with _default_lock:
            if _default_ledger is None or _default_ledger._closing:
                _default_ledger = AudioLedger()
    return _default_ledger


def record(chunk: object, *, label: object = NOISE, confidence: float = 0.0,
           sample_rate: int | None = None, device_id: object = "default",
           verdict: object = None, rms_db: float | None = None,
           timestamp: float | None = None) -> bool:
    """Convenience wrapper around :func:`default_ledger().push`."""
    return default_ledger().push(
        chunk, label=label, confidence=confidence, sample_rate=sample_rate,
        device_id=device_id, verdict=verdict, rms_db=rms_db, timestamp=timestamp)
