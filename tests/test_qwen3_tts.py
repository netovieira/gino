"""FASE3-TASK1: Qwen3-TTS integration tests.

Tests the Qwen3-TTS local TTS engine integration, including:
- Model loading and caching
- Portuguese speech synthesis
- Latency and memory measurements
- Fallback chain mechanism
- Integration with synth_pcm24k dispatcher
"""
import time
import pytest
import numpy as np


@pytest.fixture(autouse=True)
def clear_qwen3_cache():
    """Clear Qwen3-TTS model cache before each test to avoid memory accumulation."""
    try:
        from core import brain
        brain._QWEN3_MODEL_CACHE["model"] = None
        # Force GPU memory cleanup
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
    except ImportError:
        pass
    yield
    # Cleanup after test
    try:
        from core import brain
        brain._QWEN3_MODEL_CACHE["model"] = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
    except ImportError:
        pass


def test_qwen3_tts_import():
    """Test that qwen_tts package is importable."""
    try:
        from qwen_tts import Qwen3TTSModel
        assert Qwen3TTSModel is not None
    except ImportError as e:
        pytest.skip(f"qwen_tts not installed: {e}")


def test_qwen3_tts_model_load():
    """Test Qwen3-TTS model loading (cached after first call)."""
    try:
        from core import brain
    except ImportError:
        pytest.skip("core.brain not importable")

    st = {
        "qwen3_tts_model": brain.QWEN3_TTS_MODEL,
        "qwen3_tts_speaker": brain.QWEN3_TTS_SPEAKER,
        "qwen3_tts_language": brain.QWEN3_TTS_LANGUAGE,
    }

    t0 = time.time()
    model = brain._get_qwen3_model(st)
    load_time = time.time() - t0

    assert model is not None
    print(f"\nModel load time: {load_time:.2f}s")

    # Second call should be instant (cached)
    t0 = time.time()
    model2 = brain._get_qwen3_model(st)
    cache_time = time.time() - t0

    assert model2 is model  # same object
    assert cache_time < 0.1  # instant from cache
    print(f"Cache hit time: {cache_time*1000:.1f}ms")


def test_qwen3_tts_synthesis_portuguese():
    """Test Portuguese speech synthesis with Qwen3-TTS."""
    try:
        from core import brain
    except ImportError:
        pytest.skip("core.brain not importable")

    st = {
        "qwen3_tts_model": brain.QWEN3_TTS_MODEL,
        "qwen3_tts_speaker": brain.QWEN3_TTS_SPEAKER,
        "qwen3_tts_language": brain.QWEN3_TTS_LANGUAGE,
    }

    text = "Olá, como você está hoje?"
    t0 = time.time()
    pcm = brain.tts_qwen3_pcm24k(text, st)
    synth_time = time.time() - t0

    assert isinstance(pcm, bytes)
    assert len(pcm) > 0

    # Verify it's valid int16 PCM at 24 kHz
    audio = np.frombuffer(pcm, dtype=np.int16)
    duration = len(audio) / 24000.0

    print(f"\nSynthesis time: {synth_time*1000:.1f}ms")
    print(f"Audio duration: {duration:.2f}s")
    print(f"Audio samples: {len(audio)}")
    print(f"Sample rate: 24000 Hz")

    # Basic sanity checks
    assert duration > 0.5  # at least 0.5s of audio
    assert duration < 10.0  # less than 10s for a short sentence
    assert synth_time < 30.0  # should complete in reasonable time

    # Check audio is not silent (RMS > threshold)
    rms = np.sqrt(np.mean(audio.astype(float) ** 2))
    assert rms > 100  # not silence
    print(f"Audio RMS: {rms:.1f}")


def test_qwen3_tts_varied_samples():
    """Test synthesis with varied Portuguese text samples."""
    try:
        from core import brain
    except ImportError:
        pytest.skip("core.brain not importable")

    st = {
        "qwen3_tts_model": brain.QWEN3_TTS_MODEL,
        "qwen3_tts_speaker": brain.QWEN3_TTS_SPEAKER,
        "qwen3_tts_language": brain.QWEN3_TTS_LANGUAGE,
    }

    samples = [
        "Bom dia!",
        "Como vai você?",
        "Está fazendo sol hoje.",
        "O sistema está funcionando corretamente.",
    ]

    for text in samples:
        pcm = brain.tts_qwen3_pcm24k(text, st)
        assert len(pcm) > 0
        audio = np.frombuffer(pcm, dtype=np.int16)
        duration = len(audio) / 24000.0
        print(f"\n'{text}' -> {duration:.2f}s, {len(audio)} samples")
        assert duration > 0.1


def test_qwen3_tts_memory_usage():
    """Test GPU memory usage of Qwen3-TTS."""
    try:
        import torch
        from core import brain
    except ImportError:
        pytest.skip("torch or core.brain not importable")

    if not torch.cuda.is_available():
        pytest.skip("CUDA not available")

    st = {
        "qwen3_tts_model": brain.QWEN3_TTS_MODEL,
        "qwen3_tts_speaker": brain.QWEN3_TTS_SPEAKER,
        "qwen3_tts_language": brain.QWEN3_TTS_LANGUAGE,
    }

    # Clear cache and load model (fixture already cleared, but be explicit)
    brain._QWEN3_MODEL_CACHE["model"] = None
    torch.cuda.reset_peak_memory_stats()

    model = brain._get_qwen3_model(st)
    mem_after_load = torch.cuda.memory_allocated() / 1024**3

    # Synthesize
    pcm = brain.tts_qwen3_pcm24k("Teste de memória.", st)
    mem_peak = torch.cuda.max_memory_allocated() / 1024**3

    print(f"\nMemory after load: {mem_after_load:.2f} GB")
    print(f"Peak memory: {mem_peak:.2f} GB")

    assert mem_after_load < 5.0  # should fit in 5 GB (float32 ~4 GB, bfloat16 ~2 GB)
    assert mem_peak < 6.0  # peak should stay under 6 GB


def test_synth_pcm24k_dispatch_qwen3():
    """Test that synth_pcm24k dispatches to Qwen3-TTS when configured."""
    try:
        from core import brain
    except ImportError:
        pytest.skip("core.brain not importable")

    st = {
        "tts_engine": "qwen3",
        "qwen3_tts_model": brain.QWEN3_TTS_MODEL,
        "qwen3_tts_speaker": brain.QWEN3_TTS_SPEAKER,
        "qwen3_tts_language": brain.QWEN3_TTS_LANGUAGE,
    }

    text = "Teste de dispatch."
    pcm = brain.synth_pcm24k(text, st)

    assert isinstance(pcm, bytes)
    assert len(pcm) > 0

    audio = np.frombuffer(pcm, dtype=np.int16)
    duration = len(audio) / 24000.0
    print(f"\nDispatch test: {duration:.2f}s of audio")
    assert duration > 0.1


def test_fallback_chain_order():
    """Test that fallback chain has correct priority order."""
    try:
        from core import brain
    except ImportError:
        pytest.skip("core.brain not importable")

    # Fish primary
    chain = brain._tts_fallback_chain("fish")
    assert chain == ["fish", "qwen3", "edge"]

    # Qwen3 primary
    chain = brain._tts_fallback_chain("qwen3")
    assert chain == ["qwen3", "fish", "edge"]

    # Edge primary
    chain = brain._tts_fallback_chain("edge")
    assert chain == ["edge", "fish", "qwen3"]

    # Unknown primary
    chain = brain._tts_fallback_chain("unknown")
    assert chain == ["fish", "qwen3", "edge"]


def test_fallback_mechanism():
    """Test that synth_pcm24k falls back when primary engine fails."""
    try:
        from core import brain
    except ImportError:
        pytest.skip("core.brain not importable")

    # Configure with invalid Fish key (should fail and fall back)
    st = {
        "tts_engine": "fish",
        "fish_api_key": "invalid_key_that_will_fail",
        "fish_voice_id": "",
        "fish_model": "s2.1-pro-free",
        "tts_voice": "pt-BR-AntonioNeural",
        "qwen3_tts_model": brain.QWEN3_TTS_MODEL,
        "qwen3_tts_speaker": brain.QWEN3_TTS_SPEAKER,
        "qwen3_tts_language": brain.QWEN3_TTS_LANGUAGE,
    }

    text = "Teste de fallback."

    # This should fail on Fish, then succeed on Qwen3 or Edge
    pcm = brain.synth_pcm24k(text, st)

    assert isinstance(pcm, bytes)
    assert len(pcm) > 0
    print("\nFallback mechanism works")


def test_voice_label_qwen3():
    """Test voice_label returns correct string for Qwen3-TTS."""
    try:
        from core import brain
    except ImportError:
        pytest.skip("core.brain not importable")

    st = {
        "tts_engine": "qwen3",
        "qwen3_tts_model": "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
        "qwen3_tts_speaker": "Ryan",
    }

    label = brain.voice_label(st)
    assert "Qwen3-TTS" in label
    assert "Ryan" in label
    assert "local" in label
    print(f"\nVoice label: {label}")


def test_missing_packages_qwen3():
    """Test missing_packages detects qwen-tts when engine is qwen3."""
    try:
        from core import brain
    except ImportError:
        pytest.skip("core.brain not importable")

    # With qwen3 engine
    st = {"tts_engine": "qwen3"}
    missing = brain.missing_packages(st)

    # qwen-tts should be installed, so it shouldn't be in missing
    # (unless the test environment doesn't have it)
    try:
        import qwen_tts
        assert "qwen-tts" not in missing
    except ImportError:
        assert "qwen-tts" in missing


if __name__ == "__main__":
    # Run tests manually for development
    pytest.main([__file__, "-v", "-s"])
