"""Sinais sintéticos de áudio compartilhados pelos testes do segmentador.

Desde o portão de transitório (`core.groq_stt.voice_proof`) um senoide puro é
— corretamente — classificado como NÃO voz: energia concentrada em poucos
bins derruba a entropia espectral e o tom deixa de abrir turno. Por isso os
testes de VAD/segmentação não podem mais usar `sin(2π·440·t)` como "fala
sintética": eles passam a usar `voice_block`, um bloco com as três provas
medidas no próprio portão (fração de energia na banda de voz, envelope largo
e entropia espectral), no nível de amplitude pedido.

Determinístico de propósito: testes comparam bytes (`_pre[-1] == block`), então
a mesma chamada devolve sempre os mesmos samples.
"""
from __future__ import annotations

import functools

import numpy as np

SR = 16000
BLOCK = 1024          # CHUNK_SIZE de main.py: 64 ms a 16 kHz


@functools.lru_cache(maxsize=8)
def _wave(phase: int, seed: int) -> np.ndarray:
    """Fala sintética normalizada por RMS 1.0: harmônicos + ar, sem DC."""
    rng = np.random.default_rng(seed)
    t = (np.arange(BLOCK) + phase) / SR
    x = np.zeros(BLOCK)
    for k in range(1, 32):
        freq = 140.0 * k
        if freq > 3600.0:
            break
        weight = (np.exp(-((freq - 600.0) / 300.0) ** 2)
                  + 0.7 * np.exp(-((freq - 1600.0) / 500.0) ** 2)
                  + 0.35)
        x += (weight / k ** 0.85) * np.sin(2 * np.pi * freq * t + k * 0.9)
    x /= np.sqrt(np.mean(x * x)) + 1e-9
    # Modulação de sílaba: o envelope não pode ser uma reta.
    x *= 0.8 + 0.2 * np.sin(2 * np.pi * 3.7 * t + 0.4)
    # Ar da sala: é o que dá entropia espectral a uma voz real.
    x += 0.15 * rng.normal(size=BLOCK)
    x -= x.mean()
    x /= np.sqrt(np.mean(x * x)) + 1e-9
    return x


def voice_block(amplitude: int = 6000, phase: int = 0, seed: int = 11) -> np.ndarray:
    """Um quadro de 64 ms de fala sintética (passa no portão de voz).

    ``amplitude`` tem o mesmo sentido do antigo `sin(2πf t) * amplitude`:
    o RMS é `amplitude / √2 / 32768`, para que os limiares de nível destes
    testes continuem valendo com o mesmo número.
    """
    return np.clip(amplitude / np.sqrt(2.0) * _wave(int(phase), int(seed)),
                   -32767.0, 32767.0).astype(np.int16)


def silence(count: int = BLOCK) -> np.ndarray:
    return np.zeros(count, dtype=np.int16)
