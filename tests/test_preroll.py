"""Pre-roll de áudio: os 1,5 s antes do primeiro fonema chegam ao STT.

O segmentador abre uma utterance só depois de `_START_BLOCKS` quadros acima do
limiar. Sem anel, esses ~190 ms mais a rampa do primeiro fonema eram perdidos
("Firmo reiniciar" no lugar de "Confirmo reiniciar"). O anel é contínuo,
limitado por `plugin_config.groq.preroll_s` e nunca cresce sem teto.
"""
from __future__ import annotations

import threading
import unittest

import numpy as np

from core import groq_stt
from tests.audio_fixtures import voice_block

_SR = 16000
_FRAME = 1024          # CHUNK_SIZE de main.py: 64 ms a 16 kHz
_SILENCE = np.zeros(_FRAME, dtype=np.int16)


def _tone(amplitude: int = 8000) -> np.ndarray:
    """Um quadro de FALA sintética — o portão de voz recusa um senoide puro.

    Estes testes são do pré-roll/segmentação, não do detector de voz: o sinal
    só precisa cruzar o limiar E passar no portão de transitório (banda,
    envelope, entropia) como uma voz passaria.
    """
    return voice_block(amplitude)


class PrerollTest(unittest.TestCase):
    def setUp(self):
        self._orig_up = groq_stt._duck.up
        self._orig_down = groq_stt._duck.down
        self._orig_start = groq_stt._START_BLOCKS
        groq_stt._duck.up = lambda: None
        groq_stt._duck.down = lambda: None
        self._listeners = []
        self.got: list[bytes] = []
        self.done = threading.Event()

    def tearDown(self):
        for listener in self._listeners:
            listener.stop()
        groq_stt._duck.up = self._orig_up
        groq_stt._duck.down = self._orig_down
        groq_stt._START_BLOCKS = self._orig_start

    def _listener(self, **kwargs):
        def _transcribe(pcm: bytes) -> str:
            self.got.append(pcm)
            self.done.set()
            return ""

        listener = groq_stt.GroqListener(
            "", "test", "pt",
            on_text=lambda _t: None,
            on_utterance=lambda _t, _p: None,
            on_status=lambda _m: None,
            sample_rate=_SR,
            transcribe_fn=_transcribe,
            **kwargs,
        )
        # Endpointing é um modelo à parte: aqui o corte fixo de silêncio basta.
        listener._endpoint_complete = lambda _pcm: True
        self._listeners.append(listener)
        return listener

    @staticmethod
    def _feed(listener, count: int, block: np.ndarray) -> None:
        for _ in range(count):
            listener.feed(block)

    @staticmethod
    def _leading_silent_seconds(pcm: bytes) -> float:
        x = np.frombuffer(pcm, dtype=np.int16)
        nonzero = np.flatnonzero(x)
        if nonzero.size == 0:
            return len(x) / _SR
        return int(nonzero[0]) / _SR

    def _utterance(self, listener):
        # Depois da fala, 11 quadros de silêncio fecham a utterance (o mesmo
        # `_END_BLOCKS` do segmentador) e o worker entrega o PCM ao STT.
        self._feed(listener, groq_stt._END_BLOCKS, _SILENCE)
        self.assertTrue(self.done.wait(3.0), "a utterance nunca chegou ao STT")
        return self.got[-1]

    def test_payload_starts_1_5s_before_the_first_voiced_frame(self):
        listener = self._listener()
        self.assertEqual(listener.preroll_seconds, groq_stt._PREROLL_S)
        self.assertEqual(listener.preroll_blocks,
                         groq_stt.ring_blocks(groq_stt._PREROLL_S))
        # Enche o anel com silêncio, depois a fala começa.
        self._feed(listener, 40, _SILENCE)
        self._feed(listener, 6, _tone())
        pcm = self._utterance(listener)
        # 24 quadros de pré-roll puro = 1,536 s antes do primeiro fonema.
        self.assertGreaterEqual(self._leading_silent_seconds(pcm), 1.4)
        # A cauda de 700 ms continua no fim: nada é encurtado.
        x = np.frombuffer(pcm, dtype=np.int16)
        tail = len(x) - 1 - int(np.flatnonzero(x)[-1])
        self.assertGreaterEqual(tail, (groq_stt._END_BLOCKS - 1) * _FRAME)

    def test_ring_size_comes_from_the_single_config_key(self):
        cfg = groq_stt.settings(lambda _ns: {"preroll_s": 2.0})
        self.assertEqual(cfg["preroll_s"], 2.0)
        listener = self._listener(preroll_s=cfg["preroll_s"])
        self.assertEqual(listener.preroll_seconds, 2.0)
        self.assertEqual(listener.preroll_blocks, groq_stt.ring_blocks(2.0))
        self.assertGreater(listener.preroll_blocks,
                           groq_stt.ring_blocks(groq_stt._PREROLL_S))
        # Um valor absurdo é limitado, nunca estoura a memória do anel.
        self.assertEqual(groq_stt.preroll_from_config({"preroll_s": "x"}),
                         groq_stt._PREROLL_S)
        self.assertEqual(groq_stt.preroll_from_config({"preroll_s": -9}), 0.0)
        self.assertEqual(groq_stt.preroll_from_config({"preroll_s": 999}), 5.0)

    def test_ring_never_exceeds_its_configured_size(self):
        listener = self._listener()
        cap = listener.preroll_blocks
        for _ in range(500):
            listener.feed(_SILENCE)
            self.assertLessEqual(len(listener._pre), cap)
        self.assertEqual(len(listener._pre), cap)
        self.assertLessEqual(sum(len(b) for b in listener._pre),
                             listener.preroll_bytes)
        # 1,5 s + folga do detector: dezenas de KB, nunca megabytes.
        self.assertLess(listener.preroll_bytes, 1_000_000)

    def test_preroll_accounts_for_a_slow_detector(self):
        # Um detector que exige mais quadros para disparar não pode comer o
        # orçamento do pré-roll: o anel cresce junto com `_START_BLOCKS`.
        groq_stt._START_BLOCKS = 8
        listener = self._listener()
        self.assertEqual(listener.preroll_blocks,
                         groq_stt.ring_blocks(groq_stt._PREROLL_S))
        self._feed(listener, 60, _SILENCE)
        self._feed(listener, 12, _tone())
        pcm = self._utterance(listener)
        self.assertGreaterEqual(self._leading_silent_seconds(pcm), 1.4)

    def test_preroll_degrades_gracefully_with_fewer_frames_than_the_ring(self):
        # O stream acabou de abrir: só há 5 quadros antes da fala. O pré-roll
        # entrega tudo o que existe, sem inventar áudio nem estourar o anel.
        listener = self._listener()
        self._feed(listener, 5, _SILENCE)
        self._feed(listener, 6, _tone())
        pcm = self._utterance(listener)
        leading = self._leading_silent_seconds(pcm)
        self.assertGreater(leading, 0.0)
        self.assertLess(leading, groq_stt._PREROLL_S)

    def test_preroll_only_fills_the_ring_without_opening_a_segment(self):
        # Durante a própria fala do assistente (ou dormindo, no caminho cloud)
        # só o anel roda: nenhuma utterance abre, mas o áudio fica guardado.
        listener = self._listener()
        self._feed(listener, 40, _SILENCE)
        for _ in range(3):
            listener.feed(_tone(), preroll_only=True)
        self.assertFalse(listener.speaking)
        self.assertEqual(len(listener._pre), listener.preroll_blocks)
        self.assertEqual(listener._pre[-1], _tone().tobytes())
        self.assertEqual(listener._pre[-3], _tone().tobytes())
        # Mesmo com muito áudio, o anel descarta sozinho e fica no teto.
        for _ in range(100):
            listener.feed(_tone(), preroll_only=True)
        self.assertEqual(len(listener._pre), listener.preroll_blocks)


if __name__ == "__main__":
    unittest.main()
