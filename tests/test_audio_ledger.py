"""Continuous audio ledger (core/audio_ledger.py) and the WhatsApp cleanup.

The audio is synthetic; nothing here opens a microphone. Tests cover the two
things that can break a day-long recorder: rotation/pruning actually removing
old data, and ``push()`` never blocking the audio callback.
"""
from __future__ import annotations

import json
import os
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from core import audio_ledger
from core import whatsapp

RATE = 16000


def _sine(seconds: float = 1.0, sample_rate: int = RATE) -> np.ndarray:
    t = np.arange(int(seconds * sample_rate)) / float(sample_rate)
    return (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def _push_seconds(ledger, seconds: float, *, label="pessoa", rate=RATE,
                  start_ts=None, step=0.02) -> None:
    total = int(round(seconds * rate))
    chunk = int(round(step * rate))
    base = time.time() if start_ts is None else start_ts
    signal = _sine(seconds + step, rate)
    for offset in range(0, total, chunk):
        assert ledger.push(
            signal[offset:offset + chunk], label=label, confidence=0.8,
            sample_rate=rate, device_id="mic-0", verdict="near",
            timestamp=base + offset / rate)


def test_push_writes_rotated_audio_and_jsonl_events(tmp_path):
    ledger = audio_ledger.AudioLedger(
        tmp_path, block_seconds=1.0, event_seconds=0.5, keep_days=2,
        max_total_mb=100)
    encoder = ledger.encoder
    try:
        _push_seconds(ledger, 3.0)
        assert ledger.flush(5.0)
    finally:
        ledger.close()

    audio = [p for p in (tmp_path / "audio").rglob("*") if p.is_file()]
    assert len(audio) >= 3
    assert all(p.stat().st_size > 0 for p in audio)
    if encoder == audio_ledger.ENCODER_OPUS:
        assert all(p.suffix == ".ogg" for p in audio)
        assert all(p.read_bytes()[:4] == b"OggS" for p in audio)
    else:
        assert all(p.suffix == ".wav" for p in audio)
        assert all(p.read_bytes()[:4] == b"RIFF" for p in audio)

    events = []
    for path in (tmp_path / "events").glob("*.jsonl"):
        events.extend(json.loads(line) for line in path.read_text().splitlines())
    assert len(events) >= 5
    for item in events:
        assert item["device"] == "mic-0"
        assert item["label"] == "pessoa"
        assert item["verdict"] == "near"
        assert item["duration_s"] > 0
        assert -120.0 < item["rms_db"] < 20.0
        assert 0.0 <= item["confidence"] <= 1.0
        assert "text" not in item and "transcript" not in item
    assert sum(item["duration_s"] for item in events) > 2.5


def test_opus_block_decodes_when_av_is_available(tmp_path):
    ledger = audio_ledger.AudioLedger(
        tmp_path, block_seconds=1.0, keep_days=2)
    encoder = ledger.encoder
    try:
        _push_seconds(ledger, 1.0)
        ledger.flush(5.0)
    finally:
        ledger.close()
    if encoder != audio_ledger.ENCODER_OPUS:
        pytest.skip("libopus/PyAV indisponível neste ambiente")
    import av

    path = next((tmp_path / "audio").rglob("*.ogg"))
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        rate = int(stream.rate or 48000)
        decoded = sum(frame.samples for frame in container.decode(stream))
    seconds = decoded / float(rate)
    assert 0.85 <= seconds <= 1.20


def test_forced_wav_fallback_is_real_pcm(tmp_path):
    ledger = audio_ledger.AudioLedger(
        tmp_path, block_seconds=1.0, encoder=audio_ledger.ENCODER_WAV,
        keep_days=2)
    try:
        _push_seconds(ledger, 1.0)
        ledger.flush(5.0)
    finally:
        ledger.close()
    path = next((tmp_path / "audio").rglob("*.wav"))
    assert path.read_bytes()[:4] == b"RIFF"
    with wave.open(str(path), "rb") as source:
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
        assert source.getframerate() == RATE
        assert 15000 <= source.getnframes() <= 17000


def test_push_does_not_block_the_callback(tmp_path):
    ledger = audio_ledger.AudioLedger(
        tmp_path, block_seconds=30.0, keep_days=2, queue_size=4096)
    chunk = _sine(0.02)
    latencies = []
    try:
        for _ in range(600):
            started = time.perf_counter()
            assert ledger.push(chunk, label="pessoa", confidence=0.8,
                               sample_rate=RATE, device_id="mic-0")
            latencies.append(time.perf_counter() - started)
    finally:
        ledger.close()
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95)]
    assert p95 < 0.02, f"p95={p95 * 1000:.2f} ms"
    assert latencies[-1] < 0.20, f"max={latencies[-1] * 1000:.2f} ms"
    assert ledger.stats()["dropped"] == 0


def test_prune_removes_only_old_files_and_is_idempotent(tmp_path):
    ledger = audio_ledger.AudioLedger(tmp_path, keep_days=7,
                                      max_total_mb=100)
    now = time.time()
    audio_day = tmp_path / "audio" / "2020-01-01"
    audio_day.mkdir(parents=True)
    old_audio = audio_day / "old.ogg"
    old_audio.write_bytes(b"old")
    new_audio = audio_day / "new.ogg"
    new_audio.write_bytes(b"new")
    events_day = tmp_path / "events"
    events_day.mkdir(exist_ok=True)
    old_event = events_day / "old.jsonl"
    old_event.write_text("{}\n")
    new_event = events_day / "new.jsonl"
    new_event.write_text("{}\n")
    old_time = now - 10 * 86400
    for path in (old_audio, old_event):
        os.utime(path, (old_time, old_time))
    for path in (new_audio, new_event):
        os.utime(path, (now, now))

    report = ledger.prune(now=now)
    assert report["removed_age"] == 2
    assert not old_audio.exists() and not old_event.exists()
    assert new_audio.exists() and new_event.exists()

    again = ledger.prune(now=now)
    assert again["removed_age"] == 0
    assert again["removed_size"] == 0
    ledger.close()


def test_size_cap_evicts_oldest_blocks(tmp_path):
    ledger = audio_ledger.AudioLedger(tmp_path, keep_days=7,
                                      max_total_mb=0.001)
    now = time.time()
    folder = tmp_path / "audio" / "2020-01-01"
    folder.mkdir(parents=True)
    paths = []
    for index in range(5):
        path = folder / f"block-{index}.ogg"
        path.write_bytes(b"x" * 600)
        stamp = now - (100 + index) * 100
        os.utime(path, (stamp, stamp))
        paths.append(path)
    report = ledger.prune(now=now)
    assert report["removed_size"] >= 1
    remaining = [p for p in folder.iterdir() if p.is_file()]
    assert sum(p.stat().st_size for p in remaining) <= 1048
    assert paths[0].exists()               # newest survives
    ledger.close()


def test_push_after_close_is_rejected(tmp_path):
    ledger = audio_ledger.AudioLedger(tmp_path, keep_days=2)
    ledger.close()
    assert ledger.push(_sine(0.02), label="pessoa", sample_rate=RATE) is False


def test_ledger_maintenance_calls_the_whatsapp_hook(tmp_path):
    calls = []

    def hook() -> int:
        calls.append(1)
        return 4

    ledger = audio_ledger.AudioLedger(tmp_path, whatsapp_prune=hook)
    report = ledger.run_maintenance()
    assert calls == [1]
    assert report["whatsapp_media_removed"] == 4
    ledger.close()


def test_default_bridge_calls_whatsapp_prune(monkeypatch):
    calls = []

    def fake_prune(**kwargs) -> int:
        calls.append(kwargs)
        return 7

    monkeypatch.setattr(whatsapp, "prune_media", fake_prune)
    assert audio_ledger._prune_whatsapp_media() == 7
    assert calls == [{}]


def test_whatsapp_prune_media_is_conservative(tmp_path, monkeypatch):
    media = tmp_path / "whatsapp_media"
    media.mkdir()
    sub = media / "subdir"
    sub.mkdir()
    now = time.time()
    old = media / "old.jpg"
    old.write_bytes(b"old")
    fresh = media / "fresh.jpg"
    fresh.write_bytes(b"fresh")
    nested = sub / "nested.jpg"
    nested.write_bytes(b"nested")
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    link = media / "link.jpg"
    link.symlink_to(outside)
    for path in (old, nested):
        stamp = now - 30 * 86400
        os.utime(path, (stamp, stamp))

    acct = whatsapp._Account("principal", "legacy-test")
    acct.media_dir = media
    monkeypatch.setattr(whatsapp, "_ensure_accounts", lambda: None)
    monkeypatch.setattr(whatsapp, "_accounts", {acct.uuid: acct})

    assert whatsapp.prune_media(account=acct.name, now=now) == 1
    assert not old.exists()
    assert fresh.exists()
    assert nested.exists()                 # only direct children
    assert outside.exists() and link.exists()   # symlinks untouched
    assert whatsapp.prune_media(account=acct.name, now=now) == 0
