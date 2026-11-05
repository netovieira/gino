"""Unidades de fala: a boca abre curta e o resto vai agregado.

O dono mediu a boca esperando o pedido HTTP inteiro de cada frase. O
segmentador corta a PRIMEIRA unidade em 24-32 caracteres (som abre cedo) e
agrega as seguintes em 44-60 (~2 s de Fish; 220 chars custavam 6-9,6 s),
sempre em fim de frase/vírgula/palavra. Nunca
toca o Fish Audio: aqui só o corte puro é exercitado.
"""
from __future__ import annotations

import unittest

import main
from core.brain import (
    SPEECH_FIRST_MAX_CHARS,
    SPEECH_FIRST_MIN_CHARS,
    SPEECH_UNIT_MAX_CHARS,
    SPEECH_UNIT_MIN_CHARS,
    SpeechUnitSegmenter,
)

TEXT = (
    "Hoje o dia amanheceu bonito e o dono pediu uma resposta completa. "
    "Vamos revisar o relatório inteiro antes do almoço, porque o prazo "
    "aperta e ninguém quer descobrir um erro na última hora. "
    "Depois disso eu volto a falar do projeto e do que ficou pendente ontem."
)


def _norm(text: str) -> str:
    return " ".join(str(text or "").split())


class FirstUnitTest(unittest.TestCase):
    def test_primeira_unidade_abre_curta(self):
        units = SpeechUnitSegmenter().feed(TEXT, final=True)
        self.assertGreaterEqual(len(units), 2)
        self.assertGreaterEqual(len(units[0]), SPEECH_FIRST_MIN_CHARS)
        self.assertLessEqual(len(units[0]), SPEECH_FIRST_MAX_CHARS)
        self.assertTrue(_norm(TEXT).startswith(units[0]),
                        "a primeira unidade não é o começo do texto")

    def test_primeira_frase_em_streaming_sai_antes_do_resto(self):
        seg = SpeechUnitSegmenter()
        first = seg.feed("Hoje o dia amanheceu bonito.", final=False)
        self.assertEqual(len(first), 1, first)
        self.assertGreaterEqual(len(first[0]), SPEECH_FIRST_MIN_CHARS)
        self.assertLessEqual(len(first[0]), SPEECH_FIRST_MAX_CHARS)
        self.assertIn("amanheceu", first[0])
        rest = seg.feed(
            "Vamos revisar o relatório inteiro antes do almoço, porque o "
            "prazo aperta e ninguém quer descobrir um erro na última hora.",
            final=True)
        self.assertGreaterEqual(len(rest), 1, rest)
        self.assertEqual(seg.pending, "")


class AggregatedUnitTest(unittest.TestCase):
    def test_orcamento_de_sintese_da_unidade(self):
        # Medido 05/11/2026: a unidade de 220 chars custava 6 a 9,6 s de Fish
        # e a fila atrasada abria buraco audível de até 2,7 s. A ~30 chars/s
        # do modelo livre, a agregada cabe em ~2 s; a junção sem buraco
        # (_SPEECH_JUNCTION_GAP_S) continua coberta por test_say_junction_gap.
        self.assertLessEqual(SPEECH_UNIT_MAX_CHARS, 72,
                             "unidade agregada acima do orçamento de ~2 s")
        self.assertLessEqual(SPEECH_UNIT_MIN_CHARS, SPEECH_UNIT_MAX_CHARS)
        # teto da fila de texto: a primeira (32) + uma agregada
        self.assertLessEqual(
            main._SAY_UNIT_TEXT_MAX,
            SPEECH_FIRST_MAX_CHARS + SPEECH_UNIT_MAX_CHARS + 4)

    def test_unidades_seguintes_vem_agregadas(self):
        units = SpeechUnitSegmenter().feed(TEXT, final=True)
        tail = units[1:]
        self.assertGreaterEqual(len(tail), 1)
        self.assertTrue(any(len(u) >= SPEECH_UNIT_MIN_CHARS for u in tail),
                        [len(u) for u in tail])
        self.assertTrue(all(len(u) <= SPEECH_UNIT_MAX_CHARS for u in tail),
                        [len(u) for u in tail])

    def test_corte_nunca_parte_palavra(self):
        units = SpeechUnitSegmenter().feed(TEXT, final=True)
        norm = _norm(TEXT)
        pos = 0
        for unit in units:
            self.assertEqual(unit, unit.strip())
            self.assertTrue(norm.startswith(unit, pos), (unit, pos))
            pos += len(unit)
            if pos < len(norm):
                # Todo corte cai exatamente antes de um espaço: a palavra
                # seguinte começa inteira, nunca no meio.
                self.assertEqual(norm[pos], " ", (unit, norm[pos:pos + 20]))
                pos += 1
        self.assertEqual(pos, len(norm))

    def test_texto_sem_pontuacao_corta_em_espaco(self):
        text = " ".join(["palavra"] * 90)
        units = SpeechUnitSegmenter().feed(text, final=True)
        self.assertGreaterEqual(len(units), 3)
        for unit in units[:-1]:
            self.assertTrue(unit.endswith("palavra"), unit[-20:])
        self.assertEqual(" ".join(units), _norm(text))

    def test_espacos_e_linhas_nao_viram_buraco(self):
        units = SpeechUnitSegmenter().feed(
            "  Primeira   frase   aqui. \n\n Segunda frase com   espaços.  ",
            final=True)
        self.assertEqual(" ".join(units),
                         "Primeira frase aqui. Segunda frase com espaços.")


if __name__ == "__main__":
    unittest.main()
