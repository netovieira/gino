"""Tests for core.noise_filter -- RNNoise pre-filter (FASE2-TASK1).

Metrics of success (from the task spec):
  - Latency < 1 ms per 64 ms block (lightweight spectral gate)
  - Does not corrupt clean audio
  - Graceful fallback when noisereduce is not installed
"""
from __future__ import annotations

import time
import numpy as np
import pytest

from core import noise_filter


# -- availability --------------------------------------------------------------

def test_available_always_true():
    """``available()`` returns True (lightweight gate always works)."""
    assert noise_filter.available() is True


def test_nr_available_returns_bool():
    """``nr_available()`` returns True or False depending on noisereduce."""
    result = noise_filter.nr_available()
    assert isinstance(result, bool)


# -- filter_block: basic contract ----------------------------------------------

def test_filter_block_returns_bytes():
    """Output is always bytes, regardless of input type."""
    pcm = (np.sin(np.linspace(0, 2 * np.pi * 440, 1024)) * 10000).astype(np.int16)
    result = noise_filter.filter_block(pcm.tobytes(), sample_rate=16000)
    assert isinstance(result, bytes)


def test_filter_block_preserves_length():
    """Output has the same byte length as the input."""
    pcm = np.random.randint(-5000, 5000, size=2048, dtype=np.int16)
    raw = pcm.tobytes()
    result = noise_filter.filter_block(raw, sample_rate=16000)
    assert len(result) == len(raw)


def test_filter_block_short_passthrough():
    """Very short blocks (< 128 samples = 256 bytes) are returned unchanged."""
    short = b"\x00\x01" * 50  # 100 bytes = 50 samples
    result = noise_filter.filter_block(short, sample_rate=16000)
    assert result == short


def test_filter_block_accepts_ndarray():
    """An int16 ndarray is accepted and converted to bytes."""
    pcm = np.zeros(1024, dtype=np.int16)
    result = noise_filter.filter_block(pcm, sample_rate=16000)
    assert isinstance(result, bytes)
    assert len(result) == 2048  # 1024 samples * 2 bytes


def test_filter_block_silence_passthrough():
    """Very quiet audio (below RMS gate) passes through unchanged."""
    noise_filter.reset_noise_estimate()
    pcm = np.ones(1024, dtype=np.int16) * 5  # RMS ~ 0.00015
    raw = pcm.tobytes()
    result = noise_filter.filter_block(raw, sample_rate=16000)
    assert result == raw
    noise_filter.reset_noise_estimate()


def test_filter_block_clean_audio_not_corrupted():
    """A clean sine wave should remain mostly intact after filtering."""
    noise_filter.reset_noise_estimate()
    sr = 16000
    t = np.linspace(0, 0.064, int(sr * 0.064), endpoint=False)
    clean = (np.sin(2 * np.pi * 440 * t) * 15000).astype(np.int16)
    for _ in range(5):
        result_bytes = noise_filter.filter_block(clean.tobytes(), sample_rate=sr)
    result = np.frombuffer(result_bytes, dtype=np.int16)
    rms_in = float(np.sqrt(np.mean(clean.astype(np.float32) ** 2)))
    rms_out = float(np.sqrt(np.mean(result.astype(np.float32) ** 2)))
    assert rms_out > rms_in * 0.15, (
        f"Filter attenuated too much: rms_in={rms_in:.0f}, rms_out={rms_out:.0f}"
    )
    noise_filter.reset_noise_estimate()


# -- filter_array: float32 variant ---------------------------------------------

def test_filter_array_returns_float32():
    """Output is float32 ndarray."""
    audio = np.sin(np.linspace(0, 2 * np.pi * 440, 16000)).astype(np.float32) * 0.3
    result = noise_filter.filter_array(audio, sample_rate=16000)
    assert result.dtype == np.float32
    assert result.shape == audio.shape


def test_filter_array_short_passthrough():
    """Very short arrays (< 128 samples) are returned unchanged."""
    short = np.zeros(50, dtype=np.float32)
    result = noise_filter.filter_array(short, sample_rate=16000)
    np.testing.assert_array_equal(result, short)


def test_filter_array_output_bounded():
    """Output values are within [-1, 1] (clipped)."""
    noise_filter.reset_noise_estimate()
    audio = np.random.randn(8000).astype(np.float32) * 0.3
    result = noise_filter.filter_array(audio, sample_rate=16000)
    assert float(np.max(np.abs(result))) <= 1.0 + 1e-6
    noise_filter.reset_noise_estimate()


# -- latency -------------------------------------------------------------------

def test_latency_under_1ms_per_block():
    """Processing a 64 ms block must take < 1 ms (task metric)."""
    noise_filter.reset_noise_estimate()
    sr = 16000
    block_size = int(sr * 0.064)
    pcm = np.random.randint(-5000, 5000, size=block_size, dtype=np.int16)
    raw = pcm.tobytes()
    for _ in range(5):
        noise_filter.filter_block(raw, sample_rate=sr)
    n_blocks = 50
    t0 = time.monotonic()
    for _ in range(n_blocks):
        noise_filter.filter_block(raw, sample_rate=sr)
    elapsed_ms = (time.monotonic() - t0) * 1000.0 / n_blocks
    assert elapsed_ms < 1.0, f"Latency per block: {elapsed_ms:.3f} ms (expected < 1 ms)"
    noise_filter.reset_noise_estimate()


# -- noise estimate reset ------------------------------------------------------

def test_reset_noise_estimate():
    """``reset_noise_estimate`` clears the running estimate without error."""
    noise_filter.reset_noise_estimate()
    assert noise_filter._noise_estimate is None


# -- measure_latency helper ----------------------------------------------------

def test_measure_latency_returns_positive_float():
    """``measure_latency`` returns a positive number of milliseconds."""
    noise_filter.reset_noise_estimate()
    lat = noise_filter.measure_latency(n_blocks=10)
    assert isinstance(lat, float)
    assert lat > 0.0
    noise_filter.reset_noise_estimate()
