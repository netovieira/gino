"""Teto de segmento: fala contínua é cortada em trechos transcrevíveis.

A TV que fala sem pausa mantinha o VAD aberto até o teto duro de 600 quadros
(~38,4 s). Um blob de 38 s não é transcrevível — o Whisper devolvia nada e as
palavras do dono que estavam dentro dele iam junto. Passado o teto de voz
(`_UTTERANCE_CAP_S`, configurável em plugin_config.groq.max_utterance_s), o
segmento é cortado e enviado, e outro começa na mesma respiração: nenhum
quadro é perdido, repetido ou reordenado na fronteira. Com o gancho
`current_speaker_is_owner` dizendo "não é o dono", o teto cai para
`_NON_OWNER_CAP_S`.
"""
from __future__ import annotations

import threading
import time
import unittest

import numpy as np

from core import groq_stt
from tests.audio_fixtures import voice_block

_SR = 16000
_FRAME = 1024          # CHUNK_SIZE de main.py: 64 ms a 16 kHz
_SILENCE = np.zeros(_FRAME, dtype=np.int16)


def _tone(amplitude: int = 4800) -> np.ndarray:
    """Um quadro de fala sintética — um senoide puro não abre mais turno."""
    return voice_block(amplitude)


class SegmentCapTest(unittest.TestCase):
    def setUp(self):
        self._orig_up = groq_stt._duck.up
        self._orig_down = groq_stt._duck.down
        groq_stt._duck.up = lambda: None
        groq_stt._duck.down = lambda: None
        self._listeners = []
        self.got: list[bytes] = []
        self._lock = threading.Lock()

    def tearDown(self):
        for listener in self._listeners:
            listener.stop()
        groq_stt._duck.up = self._orig_up
        groq_stt._duck.down = self._orig_down

    def _listener(self, **kwargs):
        def _transcribe(pcm: bytes) -> str:
            with self._lock:
                self.got.append(pcm)
            return f"trecho {len(self.got)}"

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

    def _wait_for(self, count: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.got) >= count:
                    return
            time.sleep(0.02)
        with self._lock:
            got = len(self.got)
        self.fail(f"esperava {count} trechos no STT, chegaram {got}")

    def _chunks(self) -> list[bytes]:
        with self._lock:
            return list(self.got)

    def test_continuous_voiced_stream_past_the_cap_forces_a_boundary(self):
        listener = self._listener()
        # O teto padrão é 15 s de VOZ: curto o bastante para a TV virar trechos
        # transcrevíveis, longo o bastante para não picar uma fala do dono.
        self.assertEqual(listener.utterance_seconds, groq_stt._UTTERANCE_CAP_S)
        self.assertEqual(listener._owner_cap_blocks,
                         int(round(groq_stt._UTTERANCE_CAP_S
                                   / groq_stt._BLOCK_SECONDS)))
        self._feed(listener, 5, _SILENCE)          # pré-roll
        self._feed(listener, 400, _tone())         # ~25,6 s sem nenhuma pausa
        self._wait_for(1)
        first = self._chunks()[0]
        secs = len(first) / (2 * _SR)
        self.assertGreaterEqual(secs, groq_stt._UTTERANCE_CAP_S - 0.5)
        self.assertLessEqual(secs, groq_stt._UTTERANCE_CAP_S + 0.5)
        # O corte é um LIMITE de trecho, não o fim do turno: o VAD continua
        # aberto e a fala seguinte começa um segmento novo.
        self.assertTrue(listener.speaking, "o VAD fechou no corte de tempo")

    def test_boundary_preserves_every_block_in_order(self):
        listener = self._listener()
        # Cada quadro com amplitude própria: qualquer perda ou troca de ordem
        # muda os bytes entregues ao STT.
        speech = [_tone(4000 + 8 * i) for i in range(400)]
        fed = ([_SILENCE.tobytes()] * 5
               + [block.tobytes() for block in speech]
               + [_SILENCE.tobytes()] * groq_stt._END_BLOCKS)
        self._feed(listener, 5, _SILENCE)
        for block in speech:
            listener.feed(block)
        self._wait_for(1)
        self._feed(listener, groq_stt._END_BLOCKS, _SILENCE)
        self._wait_for(2)
        self.assertEqual(b"".join(self._chunks()), b"".join(fed),
                         "a fronteira perdeu, duplicou ou reordenou quadros")

    def test_forced_chunk_is_not_glued_back_by_endpointing(self):
        # Smart Turn dizendo "frase incompleta" não pode juntar dois trechos
        # forçados: colados, eles voltam a ser o blob que o corte evita.
        listener = self._listener()
        listener._endpoint_complete = lambda _pcm: False
        self._feed(listener, 5, _SILENCE)
        self._feed(listener, 500, _tone())     # ~32 s de fala contínua
        self._wait_for(2, timeout=3.0)
        self.assertLessEqual(
            max(len(c) for c in self._chunks()) / (2 * _SR),
            groq_stt._UTTERANCE_CAP_S + 0.5)

    def test_a_normal_sentence_is_never_chopped(self):
        listener = self._listener()
        seconds = 9.0            # turno típico do dono, abaixo do teto de 15 s
        self._feed(listener, int(seconds / groq_stt._BLOCK_SECONDS), _tone())
        self._feed(listener, groq_stt._END_BLOCKS, _SILENCE)
        self._wait_for(1)
        time.sleep(0.2)          # nada mais pode ser cortado
        chunks = self._chunks()
        self.assertEqual(len(chunks), 1)
        self.assertGreaterEqual(len(chunks[0]), int(seconds * _SR * 2))

    def test_non_owner_hook_tightens_the_cap_to_eight_seconds(self):
        listener = self._listener()
        listener.current_speaker_is_owner = lambda: False
        self.assertEqual(listener._non_owner_cap_blocks,
                         int(round(groq_stt._NON_OWNER_CAP_S
                                   / groq_stt._BLOCK_SECONDS)))
        self._feed(listener, 5, _SILENCE)
        self._feed(listener, 400, _tone())
        self._wait_for(1)
        secs = len(self._chunks()[0]) / (2 * _SR)
        self.assertGreaterEqual(secs, groq_stt._NON_OWNER_CAP_S - 0.5)
        self.assertLessEqual(secs, groq_stt._NON_OWNER_CAP_S + 0.4)

    def test_owner_hook_keeps_the_time_cap(self):
        listener = self._listener()
        listener.current_speaker_is_owner = lambda: True
        self._feed(listener, 5, _SILENCE)
        self._feed(listener, 400, _tone())
        self._wait_for(1)
        secs = len(self._chunks()[0]) / (2 * _SR)
        self.assertGreaterEqual(secs, groq_stt._UTTERANCE_CAP_S - 0.5)
        self.assertLessEqual(secs, groq_stt._UTTERANCE_CAP_S + 0.5)

    def test_broken_hook_falls_back_to_the_time_cap(self):
        listener = self._listener()
        listener.current_speaker_is_owner = lambda: (_ for _ in ()).throw(
            RuntimeError("boom"))
        self._feed(listener, 5, _SILENCE)
        self._feed(listener, 400, _tone())
        self._wait_for(1)
        secs = len(self._chunks()[0]) / (2 * _SR)
        self.assertGreaterEqual(secs, groq_stt._UTTERANCE_CAP_S - 0.5)

    def test_cap_comes_from_one_config_key_and_is_clamped(self):
        self.assertEqual(
            groq_stt.utterance_cap_from_config({"max_utterance_s": 12}), 12.0)
        # Um valor absurdo nunca pica uma frase (piso) nem devolve o blob de 38 s.
        self.assertEqual(
            groq_stt.utterance_cap_from_config({"max_utterance_s": "x"}),
            groq_stt._UTTERANCE_CAP_S)
        self.assertEqual(
            groq_stt.utterance_cap_from_config({"max_utterance_s": 0}),
            groq_stt._MIN_UTTERANCE_S)
        self.assertEqual(
            groq_stt.utterance_cap_from_config({"max_utterance_s": 999}),
            groq_stt._MAX_UTTERANCE_S)
        cfg = groq_stt.settings(lambda _ns: {"max_utterance_s": 12.0})
        self.assertEqual(cfg["max_utterance_s"], 12.0)
        listener = self._listener(max_utterance_s=cfg["max_utterance_s"])
        self.assertEqual(listener.utterance_seconds, 12.0)
        self.assertEqual(listener._owner_cap_blocks,
                         int(round(12.0 / groq_stt._BLOCK_SECONDS)))
        # O teto de não-dono nunca é MAIOR que o configurado.
        self.assertEqual(listener._non_owner_cap_blocks,
                         int(round(min(12.0, groq_stt._NON_OWNER_CAP_S)
                                   / groq_stt._BLOCK_SECONDS)))
        tight = self._listener(max_utterance_s=6.0)
        self.assertEqual(tight._non_owner_cap_blocks,
                         int(round(6.0 / groq_stt._BLOCK_SECONDS)))


if __name__ == "__main__":
    unittest.main()
