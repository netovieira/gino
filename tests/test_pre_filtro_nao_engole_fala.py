"""O pre-filtro não pode engolir a voz (PONTA 2 / F1 F2 F5 F6, 10/11/2026).

Causa medida das quatro falhas velhas de áudio: `core/noise_filter.filter_block`
era aplicado a TODO o bloco do microfone antes de ir para o anel e para o
segmento, e dois erros o faziam cortar fala limpa:

  (1) a Hann era aplicada na entrada E na saída, sem overlap-add — o bloco
      ficava multiplicado por w(t)^2. Medido com o sinal do banco de testes:
      RMS 0,12946 -> 0,06040 (-6,5 dB) num bloco de voz sem ruído nenhum;
  (2) o piso usado para subtrair era o quartil INFERIOR do espectro do próprio
      bloco — num bloco com sinal, esse quartil É sinal, então a subtração
      comia a fala junto.

Consequência medida: o anel do preroll guardava silêncio no lugar do tom
(F2), os quadros retidos não chegavam byte a byte ao STT (F1), a fronteira de
segmento entregava zeros (F5) e a fala baixa de verdade parou de abrir turno
(F6 — o falso negativo que o dono não aceita).

O conserto é a guarda `_PROTECT_SNR`: bloco cujo nível está bem acima do piso
aprendido volta CRU, byte por byte. A estimativa continua sendo aprendida (o
reset no reopen e o log "peak raw x filtrado" de 07/11 seguem valendo), e o
corte continua acontecendo onde só há ruído. Estes testes travam os dois lados
— sem eles a próxima "melhora" de ruído volta a deixar o assistente surdo.
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from core import groq_stt, noise_filter
from tests.audio_fixtures import BLOCK, SR, voice_block

_FRAME = BLOCK
_SILENCE = np.zeros(_FRAME, dtype=np.int16)

VOCES = (450, 1200, 4000, 6000, 8000, 12000)   # da fala baixa à voz cheia


def _rms_int16(raw: bytes) -> float:
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean((x / 32768.0) ** 2)))


def _ratio(block: np.ndarray) -> float:
    """mean(|X|) / mean(piso aprendido) — o número que decide a guarda."""
    noise_filter.reset_noise_estimate("teste")
    raw = np.asarray(block, dtype=np.int16).tobytes()
    for _ in range(6):
        noise_filter.filter_block(raw, SR)
    mag = np.abs(np.fft.rfft(np.frombuffer(raw, dtype=np.int16)
                             .astype(np.float32) / 32768.0))
    piso = noise_filter.noise_estimate_mean()
    assert piso is not None and piso > 0.0
    return float(np.mean(mag)) / float(piso)


# ── (1) voz limpa passa cru, byte por byte ───────────────────────────────────

@pytest.mark.parametrize("amplitude", VOCES)
def test_clean_voice_block_passes_through_byte_for_byte(amplitude):
    noise_filter.reset_noise_estimate("teste")
    block = voice_block(amplitude)
    raw = block.tobytes()
    # Primeiro bloco: sem piso aprendido ainda.
    assert noise_filter.filter_block(raw, SR) == raw
    # Depois de aprender (e repetir o mesmo quadro, como os testes de anel
    # fazem), continua cru.
    for phase in (0, 1024, 2048):
        voiced = voice_block(amplitude, phase=phase).tobytes()
        assert noise_filter.filter_block(voiced, SR) == voiced, (
            f"voz de amplitude {amplitude} foi cortada pelo pre-filtro")


def test_quiet_voice_does_not_lose_level():
    """A fala baixa (RMS ~0,0097) não pode cair ao meio: é o falso negativo."""
    noise_filter.reset_noise_estimate("teste")
    quiet = voice_block(450)
    out = noise_filter.filter_block(quiet.tobytes(), SR)
    rms_in, rms_out = _rms_int16(quiet.tobytes()), _rms_int16(out)
    assert rms_out > rms_in * 0.98, (
        f"pre-filtro derrubou a fala baixa: {rms_in:.5f} -> {rms_out:.5f}")


def test_the_guard_sits_between_noise_and_voice_measured():
    """A constante é a medição, não um chute: ruído abaixo, voz acima."""
    rng = np.random.default_rng(7)
    noise_ratios = [_ratio(np.round(rng.normal(scale=level, size=_FRAME)))
                    for level in (300, 900, 1500)]
    voice_ratios = [_ratio(voice_block(amp)) for amp in (450, 6000, 12000)]
    assert max(noise_ratios) < noise_filter._PROTECT_SNR, (
        f"ruído puro sendo protegido: {noise_ratios}")
    assert min(voice_ratios) >= noise_filter._PROTECT_SNR, (
        f"voz sendo cortada: {voice_ratios}")


# ── (2) o corte continua existindo onde só há ruído ─────────────────────────

def test_noise_only_block_is_still_attenuated():
    """Sem corte nenhum a guarda viraria "desligar o filtro"."""
    rng = np.random.default_rng(11)
    noise_filter.reset_noise_estimate("teste")
    blocks = [np.round(rng.normal(scale=260.0, size=_FRAME)).astype(np.int16)
              for _ in range(40)]
    rms_in = [_rms_int16(b.tobytes()) for b in blocks[:20]]
    rms_out = [_rms_int16(noise_filter.filter_block(b.tobytes(), SR))
               for b in blocks[20:]]
    media_in, media_out = float(np.mean(rms_in)), float(np.mean(rms_out))
    assert media_out < media_in * 0.9, (
        f"ruído puro não está sendo suprimido: {media_in:.5f} -> {media_out:.5f}")


def test_protected_block_still_learns_the_room_floor():
    """A guarda não desliga o aprendizado: o piso continua sendo medido."""
    noise_filter.reset_noise_estimate("teste")
    assert not noise_filter.has_noise_estimate()
    noise_filter.filter_block(voice_block(8000).tobytes(), SR)
    assert noise_filter.has_noise_estimate()


# ── (3) o caminho do microfone guarda o áudio cru ───────────────────────────

def _listener(transcribe):
    listener = groq_stt.GroqListener(
        "", "test", "pt",
        on_text=lambda _t: None,
        on_utterance=lambda _t, _p: None,
        on_status=lambda _m: None,
        sample_rate=SR,
        transcribe_fn=transcribe,
        use_vad=False,  # testes usam fala sintética, não o modelo Silero
    )
    listener._endpoint_complete = lambda _pcm: True
    return listener


def test_listener_ring_holds_the_microphone_bytes_not_the_filter_output():
    listener = _listener(lambda _pcm: "")
    try:
        for _ in range(40):
            listener.feed(_SILENCE)
        tone = voice_block(8000)
        listener.feed(tone, preroll_only=True)
        assert listener._pre[-1] == tone.tobytes()
    finally:
        listener.stop()


def test_low_voice_still_opens_a_turn_and_reaches_the_stt():
    """Reprodução do F6: fala baixa real abrindo turno com o filtro ligado."""
    clips: list[bytes] = []
    listener = _listener(lambda pcm: clips.append(pcm) or "fala")
    try:
        for _ in range(40):
            listener.feed(_SILENCE)
        quiet = voice_block(450)
        for _ in range(20):
            listener.feed(quiet)
        assert listener.speaking, "a fala baixa deixou de abrir turno"
        for _ in range(groq_stt._END_BLOCKS):
            listener.feed(_SILENCE)
        deadline = time.monotonic() + 3.0
        while not clips and time.monotonic() < deadline:
            time.sleep(0.02)
        assert clips, "a fala baixa não chegou ao STT"
    finally:
        listener.stop()
