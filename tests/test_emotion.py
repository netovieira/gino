"""Emoção na entrada: léxico pt-BR + prosódia determinística, sem rede.

Os PCM sintéticos são construídos aqui para cada eixo que o detector diz
medir — ativação (RMS/F0/variação) para cima e energia para baixo —, então
um teste que quebre aponta qual feature mudou, não "o modelo mudou".
"""
from __future__ import annotations

import unittest

import numpy as np

from core import emotion


def pcm16(signal: np.ndarray) -> bytes:
    return (np.clip(signal, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def tone(seconds: float = 2.0, hz: float = 200.0, amp: float = 0.2,
         vibrato_hz: float = 0.0, vibrato_depth: float = 0.0,
         sr: int = 16000) -> np.ndarray:
    t = np.arange(int(sr * seconds)) / sr
    freq = hz + vibrato_depth * np.sin(2 * np.pi * vibrato_hz * t)
    return amp * np.sin(2 * np.pi * np.cumsum(freq) / sr)


def gaps(signal: np.ndarray, spans, sr: int = 16000) -> np.ndarray:
    out = signal.copy()
    for start, end in spans:
        out[int(sr * start):int(sr * end)] = 0.0
    return out


AGITATED = pcm16(tone(hz=230, amp=0.55, vibrato_hz=3.5, vibrato_depth=55))
TIRED = pcm16(gaps(tone(hz=110, amp=0.02),
                   [(0.0, 0.5), (0.7, 1.2), (1.6, 2.0)]))
SAD = pcm16(gaps(tone(hz=130, amp=0.02),
                 [(0.35, 0.44), (0.75, 0.84), (1.15, 1.24)]))
# Ruído não-vocal (teclado/ventoinha): sem F0 estável e sem uma palavra real.
NOISY = pcm16(np.random.default_rng(7).normal(0.0, 0.08, 16000 * 2))


class LexiconTest(unittest.TestCase):
    def test_each_mood_is_recognised_from_words_alone(self):
        cases = {
            "que raiva, nada funciona aqui": "irritado",
            "adorei, ficou ótimo, parabéns": "feliz",
            "estou triste, que saudade": "triste",
            "estou cansado, não dormi": "cansado",
        }
        for text, mood in cases.items():
            with self.subTest(text=text):
                self.assertEqual(emotion.detect(text, None)["mood"], mood)

    def test_neutral_text_and_no_audio_stay_neutral(self):
        result = emotion.detect("que horas são?", None)
        self.assertEqual(result["mood"], "neutro")
        self.assertEqual(result["source"], "neutro")
        self.assertEqual(result["confidence"], 0.0)

    def test_negation_is_not_emotion(self):
        self.assertEqual(
            emotion.detect("não estou irritado, estou bem", None)["mood"],
            "neutro")

    def test_word_boundary_keeps_otimo_out_of_otimista(self):
        result = emotion.detect("o otimista chegou", None)
        self.assertEqual(result["mood"], "neutro")

    def test_hits_are_reported_for_the_turn_meta(self):
        result = emotion.detect("estou cansado", None)
        self.assertEqual(result["text_hits"], {"cansado": ["cansado"]})
        self.assertEqual(result["source"], "texto")


class ProsodyTest(unittest.TestCase):
    def test_agitated_prosody_is_measured_but_words_decide_the_label(self):
        result = emotion.detect("bom dia", AGITATED)
        self.assertEqual(result["mood"], "neutro")
        self.assertGreaterEqual(result["prosody"]["f0_hz"], 170.0)
        self.assertGreaterEqual(result["prosody"]["f0_variation"], 0.10)
        self.assertGreaterEqual(result["prosody"]["vocal_fraction"], 0.10)
        self.assertEqual(result["source"], "neutro")

    def test_quiet_flat_long_pauses_are_measured_not_labelled_alone(self):
        result = emotion.detect("bom dia", TIRED)
        self.assertEqual(result["mood"], "neutro")
        self.assertLessEqual(result["prosody"]["rms_db"], -34.0)
        self.assertGreaterEqual(result["prosody"]["pause_ratio"], 0.35)

    def test_quiet_flat_short_gaps_are_measured_not_labelled_alone(self):
        result = emotion.detect("bom dia", SAD)
        self.assertEqual(result["mood"], "neutro")
        self.assertLessEqual(result["prosody"]["rms_db"], -34.0)

    def test_text_decides_between_irritated_and_happy_activation(self):
        result = emotion.detect("adorei, que ótimo, parabéns", AGITATED)
        self.assertEqual(result["mood"], "feliz")
        self.assertEqual(result["source"], "texto+dsp")

    def test_dsp_accepts_int16_arrays_and_memoryview(self):
        samples = np.frombuffer(AGITATED, dtype="<i2")
        for clip in (samples, memoryview(AGITATED)):
            with self.subTest(clip=type(clip).__name__):
                result = emotion.detect("que raiva", clip)
                self.assertEqual(result["mood"], "irritado")
                self.assertGreater(result["prosody"]["f0_hz"], 0.0)

    def test_real_input_rate_is_used(self):
        # 8 kHz: o mesmo tom tem metade dos períodos por frame. Se a taxa
        # fosse ignorada, a autocorrelação cairia fora da faixa e o F0 seria 0.
        sr = 8000
        clip = pcm16(tone(seconds=2.0, hz=220, amp=0.4, sr=sr))
        result = emotion.detect("bom dia", clip, sample_rate=sr)
        self.assertGreaterEqual(result["prosody"].get("f0_hz", 0.0), 150.0)


class NoSpeechTest(unittest.TestCase):
    """Ruído não-vocal nunca vira humor: sem palavra, sem prosódia."""

    def test_empty_text_with_noisy_pcm_is_neutral_without_a_prompt_line(self):
        for text in ("", "   ", "...", "?!", "123 456"):
            with self.subTest(text=text):
                result = emotion.detect(text, NOISY)
                self.assertEqual(result["mood"], "neutro")
                self.assertEqual(result["confidence"], 0.0)
                self.assertEqual(result["source"], "neutro")
                self.assertEqual(result["prosody"], {})
                self.assertEqual(emotion.prompt_line(result), "")
                self.assertEqual(emotion.tts_tag(result), "")

    def test_calm_words_plus_aggressive_pcm_do_not_invent_anger(self):
        result = emotion.detect("bom dia, tudo bem?", AGITATED)
        self.assertEqual(result["mood"], "neutro")
        self.assertEqual(result["text_hits"], {})
        self.assertGreater(result["prosody"].get("vocal_fraction", 0.0), 0.0)
        self.assertEqual(emotion.prompt_line(result), "")

    def test_irritated_words_win_whatever_the_pcm(self):
        for label, pcm in (("sem áudio", None), ("agitado", AGITATED),
                           ("ruído", NOISY), ("baixo", TIRED)):
            with self.subTest(pcm=label):
                result = emotion.detect("que raiva, nada funciona", pcm)
                self.assertEqual(result["mood"], "irritado")
                self.assertIn("[TOM DO DONO AGORA]",
                              emotion.prompt_line(result))


class FallbackTest(unittest.TestCase):
    def test_bad_audio_never_raises(self):
        for bad in (b"", b"\x01", None, b"\x00" * 4000, "não é áudio"):
            with self.subTest(bad=bad):
                result = emotion.detect("oi", bad)
                self.assertIn(result["mood"], emotion.MOODS + ("neutro",))
                self.assertEqual(result["mood"], "neutro")

    def test_short_clip_is_not_measured(self):
        result = emotion.detect("", b"\x01\x00" * 64)
        self.assertEqual(result["prosody"], {})
        self.assertEqual(result["mood"], "neutro")


class DeliveryTest(unittest.TestCase):
    def test_prompt_line_carries_tone_without_facts(self):
        line = emotion.prompt_line("cansado")
        self.assertIn("[TOM DO DONO AGORA]", line)
        self.assertIn("TOM", line)
        self.assertIn("não mude nenhum fato", line)
        for mood in emotion.MOODS:
            self.assertTrue(emotion.prompt_line(mood), mood)

    def test_prompt_line_accepts_the_detect_dict_and_rejects_neutral(self):
        result = emotion.detect("estou cansado", None)
        self.assertEqual(emotion.prompt_line(result),
                         emotion.prompt_line("cansado"))
        self.assertEqual(emotion.prompt_line("neutro"), "")
        self.assertEqual(emotion.prompt_line(None), "")
        self.assertEqual(emotion.prompt_line("lixo"), "")

    def test_tts_tag_is_the_assistant_delivery_not_a_mirror(self):
        self.assertEqual(emotion.tts_tag("irritado"), "[calm]")
        self.assertEqual(emotion.tts_tag("feliz"), "[excited]")
        self.assertEqual(emotion.tts_tag("triste"), "[soft tone]")
        self.assertEqual(emotion.tts_tag("cansado"), "[soft tone]")
        self.assertEqual(emotion.tts_tag("neutro"), "")

    def test_freshness_window(self):
        self.assertTrue(emotion.is_fresh(100.0, 130.0, 180.0))
        self.assertFalse(emotion.is_fresh(100.0, 400.0, 180.0))
        self.assertFalse(emotion.is_fresh(0.0, 10.0, 180.0))
        self.assertFalse(emotion.is_fresh(None, 10.0, 180.0))
        self.assertFalse(emotion.is_fresh("x", 10.0, 180.0))


if __name__ == "__main__":
    unittest.main()
