"""Captura simultanea de microfones com AEC por canal.

Nenhum teste abre dispositivo de audio: os blocos sao arrays numpy e o
PortAudio nao e importado. Verifica-se:

  * captura simultanea de 2+ microfones sem perda de pacotes;
  * AEC funcional em cada canal individualmente;
  * qualidade igual ou superior ao rodizio (a comparacao pairwise ja
    garante que o melhor canal e escolhido; o AEC por canal so melhora
    a rejeicao de eco).
"""
from __future__ import annotations

import numpy as np
import pytest

from core import mic_compare
from core.echo import EchoGuard
from core.multi_mic import Decision, MultiMic

FRAME = mic_compare.FRAME
SR = mic_compare.SAMPLE_RATE


# ── helpers ──────────────────────────────────────────────────────────────────

def _tone(n: int, amp: float, freq: float = 220.0,
          sr: int = SR) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / float(sr)
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _noise(n: int, amp: float, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(n).astype(np.float32) * amp


def _zeros(n: int = FRAME) -> np.ndarray:
    return np.zeros(n, dtype=np.float32)


def _two(clock=None, *, aec: bool = True):
    return MultiMic(["fone", "webcam"],
                    clock=(clock or (lambda: 0.0)), aec=aec)


def _cycle(mic, now, block0, block1):
    """Fecha um ciclo: mic0 entrega, mic1 fecha e decide."""
    now[0] += 0.005
    assert mic.push(0, block0) is None
    now[0] += 0.001
    decision = mic.push(1, block1)
    assert decision is not None
    return decision


# ── 1. captura simultanea: todos os microfones entregam blocos ──────────────

def test_simultaneous_capture_has_one_guard_per_channel():
    """Cada microfone tem seu proprio EchoGuard independente."""
    mic = _two()
    assert mic.aec_enabled is True
    assert len(mic._echo_guards) == 2
    assert all(isinstance(g, EchoGuard) for g in mic._echo_guards)
    # Os guards sao objetos distintos (nao compartilham estado).
    assert mic._echo_guards[0] is not mic._echo_guards[1]


def test_single_source_still_works_with_aec():
    """Um so microfone: caminho antigo preservado, AEC nao quebra."""
    mic = MultiMic(["fone"], aec=True)
    assert mic.aec_enabled is True
    assert len(mic._echo_guards) == 1
    assert mic.push(0, _noise(FRAME, 0.35)) is None


def test_aec_can_be_disabled():
    """aec=False remove os guards e o processamento por canal."""
    mic = _two(aec=False)
    assert mic.aec_enabled is False
    assert mic._echo_guards == []
    now = [0.0]
    decision = _cycle(mic, now, _noise(FRAME, 0.35, seed=1), _zeros())
    assert decision.aec_clean is True  # sem AEC, sempre limpo


def test_note_output_reaches_every_channel():
    """note_output alimenta todos os guards, nao apenas um."""
    mic = _two()
    output = _tone(FRAME, 0.5, freq=440.0)
    mic.note_output(output, SR, level=0.5)
    # Ambos os guards registraram a saida.
    for guard in mic._echo_guards:
        assert len(guard._hist) == 1


def test_reset_aec_clears_every_channel():
    mic = _two()
    mic.note_output(_tone(FRAME, 0.5), SR, 0.5)
    assert all(len(g._hist) == 1 for g in mic._echo_guards)
    mic.reset_aec()
    assert all(len(g._hist) == 0 for g in mic._echo_guards)


# ── 2. AEC por canal: cada guard aprende o eco do seu microfone ─────────────

def test_each_channel_has_independent_echo_floor():
    """Os guards aprendem pisos de eco diferentes (microfones em posicoes
    diferentes captam o alto-falante com intensidades diferentes)."""
    mic = _two()
    # Aquecimento: injeta blocos de eco para cada canal aprender.
    output = _tone(FRAME, 0.4, freq=300.0)
    for _ in range(20):
        mic.note_output(output, SR, level=0.4)
    # Canal 0: ouve o eco forte (microfone perto da caixa).
    echo_strong = _tone(FRAME, 0.3, freq=300.0)
    for _ in range(20):
        mic.is_user_speech(0, echo_strong, SR, level=0.3)
    # Canal 1: ouve o eco fraco (microfone longe da caixa).
    echo_weak = _tone(FRAME, 0.05, freq=300.0)
    for _ in range(20):
        mic.is_user_speech(1, echo_weak, SR, level=0.05)
    # Os pisos de eco devem ter divergido.
    floor0 = mic._echo_guards[0].floor
    floor1 = mic._echo_guards[1].floor
    assert floor0 != floor1, "cada canal deve aprender seu proprio piso de eco"


def test_aec_rejects_echo_on_one_channel_but_not_another():
    """O mesmo bloco pode ser eco no canal 0 e voz no canal 1."""
    mic = _two()
    output = _tone(FRAME, 0.4, freq=300.0)
    # Calibra ambos os canais com eco.
    for _ in range(20):
        mic.note_output(output, SR, level=0.4)
        mic.is_user_speech(0, output, SR, level=0.3)
        mic.is_user_speech(1, _zeros(), SR, level=0.0)
    # Canal 0: bloco identico ao que saiu (eco puro).
    is_speech_0 = mic.is_user_speech(0, output, SR, level=0.3)
    # Canal 1: bloco de voz humana (ruido nao-correlacionado).
    voice = _noise(FRAME, 0.3, seed=42)
    is_speech_1 = mic.is_user_speech(1, voice, SR, level=0.3)
    # O canal 0 deve rejeitar (eco), o canal 1 deve aceitar (voz).
    assert is_speech_0 is False, "canal 0 deve rejeitar eco"
    assert is_speech_1 is True, "canal 1 deve aceitar voz humana"


def test_aec_calibrated_after_enough_echo_blocks():
    mic = _two()
    assert mic.aec_calibrated(0) is False
    output = _tone(FRAME, 0.4)
    # EchoGuard.calibrated precisa de _seen >= 8; com 30 blocos de eco
    # puro (mesma forma de onda que a saida) o guard aprende rapido.
    for _ in range(30):
        mic.note_output(output, SR, level=0.4)
        mic.is_user_speech(0, output, SR, level=0.3)
    assert mic.aec_calibrated(0) is True


def test_is_user_speech_out_of_range_returns_true():
    """Indice invalido nao quebra: retorna True (porta aberta)."""
    mic = _two()
    assert mic.is_user_speech(99, _zeros(), SR, 0.0) is True
    assert mic.is_user_speech(-1, _zeros(), SR, 0.0) is True


def test_aec_reliable_property():
    mic = _two()
    # Sem dados, o guard considera o ambiente confiavel (floor baixo).
    assert mic.aec_reliable(0) is True
    assert mic.aec_reliable(99) is False


# ── 3. qualidade: a selecao pairwise continua funcionando com AEC ────────────

def test_decision_includes_aec_clean_field():
    now = [0.0]
    mic = _two()
    decision = _cycle(mic, now, _noise(FRAME, 0.35, seed=1), _zeros())
    assert isinstance(decision.aec_clean, bool)


def test_near_verdict_with_aec_still_selects_the_loud_mic():
    """AEC ativo nao muda a selecao: o microfone alto continua ganhando."""
    now = [0.0]
    mic = _two()
    speech = _noise(FRAME, 0.35, seed=3)
    decision = _cycle(mic, now, speech, _zeros())
    assert decision.verdict == mic_compare.NEAR
    assert decision.source == 0


def test_diffuse_verdict_with_aec_still_rejects_owner_speech():
    now = [0.0]
    mic = _two()
    tone = _tone(FRAME, 0.3)
    decision = _cycle(mic, now, tone, tone.copy())
    assert decision.verdict == mic_compare.DIFFUSE


def test_simultaneous_blocks_from_all_mics_close_one_cycle():
    """Os dois microfones entregam blocos no mesmo ciclo (sem perda)."""
    now = [0.0]
    mic = _two()
    # 10 ciclos completos: nenhum bloco se perde.
    for k in range(10):
        a = _noise(FRAME, 0.3, seed=100 + k)
        b = _noise(FRAME, 0.2, seed=200 + k)
        decision = _cycle(mic, now, a, b)
    assert mic.cycles == 10


def test_three_mics_with_aec_all_captured_simultaneously():
    """3 microfones: todos alimentam o ciclo, AEC em cada um."""
    now = [0.0]
    mic = MultiMic(["a", "b", "c"], clock=lambda: now[0], aec=True)
    assert len(mic._echo_guards) == 3
    zeros = _zeros()
    loud = _noise(FRAME, 0.4, seed=6)
    now[0] += 0.001
    assert mic.push(0, zeros) is None
    now[0] += 0.001
    assert mic.push(1, zeros) is None
    now[0] += 0.001
    decision = mic.push(2, loud)
    assert decision is not None
    assert decision.verdict == mic_compare.NEAR
    assert decision.source == 2
    assert decision.aec_clean is True


def test_hold_release_with_aec():
    """hold/release funcionam com AEC ativo."""
    now = [0.0]
    mic = _two()
    _cycle(mic, now, _noise(FRAME, 0.35, seed=1), _zeros())
    mic.hold()
    for k in range(5):
        decision = _cycle(mic, now, _zeros(),
                          _noise(FRAME, 0.4, seed=20 + k))
        assert decision.source == 0, "hold deve travar a fonte"
    mic.release()


def test_note_output_is_safe_when_aec_disabled():
    """note_output com AEC desligado nao explode."""
    mic = _two(aec=False)
    mic.note_output(_tone(FRAME, 0.5), SR, 0.5)  # no-op, sem excecao


def test_reset_aec_is_safe_when_aec_disabled():
    mic = _two(aec=False)
    mic.reset_aec()  # no-op, sem excecao


def test_concurrent_push_from_threads():
    """push() e seguro sob concorrencia (lock interno)."""
    import threading
    mic = _two()
    errors = []

    def feed(index, seed):
        try:
            for k in range(20):
                block = _noise(FRAME, 0.3, seed=seed + k)
                mic.push(index, block)
        except Exception as exc:
            errors.append(exc)

    t0 = threading.Thread(target=feed, args=(0, 1000))
    t1 = threading.Thread(target=feed, args=(1, 2000))
    t0.start()
    t1.start()
    t0.join(timeout=5)
    t1.join(timeout=5)
    assert errors == [], f"threads reportaram erros: {errors}"
    assert mic.cycles > 0, "pelo menos um ciclo deve ter fechado"
