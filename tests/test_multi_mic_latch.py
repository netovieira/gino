"""O latch da fonte do microfone: travado por frase, nunca argmax a cada ciclo.

Nenhum teste abre microfone nem usa relógio wall-clock: a classe aceita
``clock=`` injetável e os blocos são arrays numpy sintéticos. A defesa é contra
o defeito real: webcam e headphone quase empatados faziam o ``argmax`` virar a
cada ciclo, o callback reimprimia ``[Mic] fonte selecionada`` e o cadastro de
voz do dono não fechava.
"""
from __future__ import annotations

import numpy as np

from core import mic_compare
from core.multi_mic import MultiMic

FRAME = mic_compare.FRAME


def _noise(n: int, amp: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(n).astype(np.float32) * amp)


def _zeros() -> np.ndarray:
    return np.zeros(FRAME, dtype=np.float32)


def _two():
    now = [0.0]
    mic = MultiMic(["fone", "webcam"], clock=lambda: now[0])
    return mic, now


def _cycle(mic, now, block0, block1):
    """Fecha um ciclo completo: mic0 entrega, depois mic1 fecha e decide."""
    now[0] += 0.005
    assert mic.push(0, block0) is None
    now[0] += 0.001
    decision = mic.push(1, block1)
    assert decision is not None
    return decision


def _near(mic, now, loud_index, seed):
    """Ciclo com vencedor provado: um microfone alto, o outro mudo."""
    loud = _noise(FRAME, 0.35, seed=seed)
    if loud_index == 0:
        return _cycle(mic, now, loud, _zeros())
    return _cycle(mic, now, _zeros(), loud)


def _ambiguous(mic, now, seed):
    """Ciclo sem prova: 3 dB de diferença e nada de correlação."""
    return _cycle(mic, now, _noise(FRAME, 0.30, seed=seed),
                  _noise(FRAME, 0.21, seed=seed + 100))


# (a) vencedores alternando a cada ciclo não podem trocar a fonte
def test_alternating_winners_never_flip_the_latch():
    mic, now = _two()
    first = _near(mic, now, 0, seed=1)
    assert first.source == 0 and first.verdict == mic_compare.NEAR
    for k in range(6):
        decision = _near(mic, now, (k % 2), seed=10 + k)
        assert decision.source == 0, "o latch não pode virar com alternância"
    # nenhum ciclo ambíguo também derruba o latch (era aqui que o argmax nascia)
    for k in range(4):
        decision = _ambiguous(mic, now, seed=50 + k)
        assert decision.source == 0


def test_alternating_cycles_raise_nothing():
    mic, now = _two()
    _near(mic, now, 0, seed=1)
    for k in range(10):
        _near(mic, now, k % 2, seed=70 + k)
    assert mic.cycles >= 10


# (b) vencedor novo sustentado troca exatamente uma vez e estabiliza
def test_sustained_new_winner_switches_exactly_once():
    mic, now = _two()
    assert mic.latch_wins == 3
    assert _near(mic, now, 0, seed=1).source == 0
    # um ciclo e dois ciclos de prova ainda não compram a troca
    assert _near(mic, now, 1, seed=2).source == 0
    assert _near(mic, now, 1, seed=3).source == 0
    # o terceiro ciclo consecutivo move o latch uma única vez
    switched = _near(mic, now, 1, seed=4)
    assert switched.source == 1
    assert switched.verdict == mic_compare.NEAR
    for k in range(5):
        assert _near(mic, now, 1, seed=10 + k).source == 1
    # ciclos sem vencedor mantêm o eleito (sem cair em argmax)
    for k in range(3):
        assert _ambiguous(mic, now, seed=40 + k).source == 1


def test_pending_cycles_are_ambiguous_for_the_gate():
    """Enquanto a troca não é paga, o portão não abre turno na fonte antiga."""
    mic, now = _two()
    _near(mic, now, 0, seed=1)
    pending = _near(mic, now, 1, seed=2)
    assert pending.source == 0
    assert pending.verdict == mic_compare.AMBIGUOUS


# (c) hold pineia a fonte; release reabre a eleição após latch_wins
def test_hold_pins_the_source_while_the_assistant_speaks():
    mic, now = _two()
    assert _near(mic, now, 0, seed=1).source == 0
    mic.hold()
    for k in range(10):
        assert _near(mic, now, 1, seed=20 + k).source == 0
    mic.release()
    assert _near(mic, now, 1, seed=40).source == 0
    assert _near(mic, now, 1, seed=41).source == 0
    assert _near(mic, now, 1, seed=42).source == 1


def test_hold_before_the_first_decision_is_harmless():
    mic, _now = _two()
    mic.hold()          # nada eleito ainda: não pode travar nem explodir
    mic.release()


# (d) uma única fonte: caminho antigo preservado, push devolve None
def test_single_source_never_enters_the_latch():
    mic = MultiMic(["fone"])
    assert mic.push(0, _noise(FRAME, 0.35, seed=1)) is None
    assert mic.push(0, _zeros()) is None
    assert mic.cycles == 0
