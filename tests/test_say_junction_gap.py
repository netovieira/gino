"""Junção entre unidades: folga curta quando o áudio seguinte já está pronto.

A boca não espera o cérebro, mas também não pode emendar duas unidades com um
buraco de silêncio. Se a unidade N+1 ficou pronta antes de N terminar, entra a
folga de 140 ms (respiração) e nenhum buraco maior; se N+1 atrasou, o buraco é
medido e NENHUMA folga é somada — nunca se aumenta um buraco. Fish Audio não é
chamado: a síntese é um fake local.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

import main
from core import brain
from tests.test_speech_tail import _make_obj

TEXT = "Hoje o dia amanheceu bonito. O resto vem depois com calma."
JUNCTION_BYTES = int(main.RECEIVE_SAMPLE_RATE
                     * main._SPEECH_JUNCTION_GAP_S) * 2


def _set_units_middle(items):
    """Devolve (pcm1, silêncio, pcm2) a partir dos itens enfileirados."""
    data = b"".join(bytes(x) for x in items)
    pcm = b"\x00\x01" * 24000          # 0,5 s de áudio fake
    first = data.find(pcm)
    second = data.find(pcm, first + 1)
    return data[first:first + len(pcm)], data[first + len(pcm):second], \
        data[second:second + len(pcm)]


class JunctionReadyTest(unittest.TestCase):
    def test_proxima_unidade_pronta_usa_folga_de_140ms(self):
        obj = _make_obj()
        pcm = b"\x00\x01" * 24000       # 0,5 s: a segunda unidade chega antes
        calls: list[str] = []

        def fake_synth(text, _st, **_kw):
            calls.append(str(text))
            return pcm

        brain.reset_speech_stats()
        with mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.brain.synth_pcm24k", side_effect=fake_synth):
            obj._say_blocking(TEXT, log=False, epoch=0, origin="qoder")

        self.assertEqual(len(calls), 2, calls)
        self.assertLessEqual(len(calls[0]), 32)
        self.assertGreater(len(calls[1]), len(calls[0]))
        middle = _set_units_middle(obj.audio_in_queue.items)[1]
        self.assertEqual(len(middle), JUNCTION_BYTES,
                         "a folga não tem exatamente o tamanho medido")
        self.assertEqual(set(middle), {0}, "a folga não é silêncio")
        stats = brain.speech_stats()
        self.assertEqual(stats["units"], 2)
        self.assertAlmostEqual(stats["junction_s_total"],
                               main._SPEECH_JUNCTION_GAP_S, places=3)
        self.assertEqual(stats["gap_s_total"], 0.0,
                         "houve buraco mesmo com a próxima unidade pronta")
        self.assertLessEqual(main._SPEECH_JUNCTION_GAP_S, 0.18,
                             "a folga de junção passou do alvo de 120-180 ms")

    def test_unidade_atrasada_nao_ganha_folga(self):
        obj = _make_obj()
        pcm = b"\x00\x01" * 2400        # 0,1 s: a segunda unidade atrasa
        calls: list[str] = []

        def fake_synth(text, _st, **_kw):
            calls.append(str(text))
            if len(calls) > 1:
                time.sleep(0.18)
            return pcm

        brain.reset_speech_stats()
        with mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.brain.synth_pcm24k", side_effect=fake_synth):
            obj._say_blocking(TEXT, log=False, epoch=0, origin="qoder")

        data = b"".join(bytes(x) for x in obj.audio_in_queue.items)
        self.assertEqual(len(data), len(pcm) * 2,
                         "folga somada onde já existia buraco")
        stats = brain.speech_stats()
        self.assertEqual(stats["units"], 2)
        self.assertEqual(stats["junction_s_total"], 0.0)
        self.assertGreater(stats["gap_s_total"], 0.0,
                           "o buraco real não foi medido")


if __name__ == "__main__":
    unittest.main()
