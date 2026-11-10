"""Synthetic-source classification (core/source_class.py).

Every signal is generated with a fixed seed: no microphone, no model, no GPU.
The labels are the contract; the feature values may move a little with numpy,
but a stable harmonic tone must stay ``musica`` and a white burst ``ruido``.
"""
from __future__ import annotations

import math
import unittest

import numpy as np

from core import source_class


def _tone(sample_rate: int) -> np.ndarray:
    t = np.arange(sample_rate) / float(sample_rate)
    signal = sum(amplitude * np.sin(2 * np.pi * 220 * harmonic * t)
                 for harmonic, amplitude in
                 ((1, 1.0), (2, 0.5), (3, 0.33), (4, 0.25), (5, 0.2)))
    return (0.2 * signal).astype(np.float32)


def _white_burst(sample_rate: int) -> np.ndarray:
    rng = np.random.default_rng(7)
    signal = np.zeros(sample_rate, dtype=np.float32)
    length = int(0.12 * sample_rate)
    signal[:length] = 0.4 * rng.standard_normal(length)
    return signal


def _speech_like(sample_rate: int) -> np.ndarray:
    rng = np.random.default_rng(42)
    t = np.arange(sample_rate) / float(sample_rate)
    white = rng.standard_normal(sample_rate)
    spectrum = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(sample_rate, 1.0 / sample_rate)
    spectrum[(freqs < 300) | (freqs > 3400)] = 0.0
    voiced = np.fft.irfft(spectrum, sample_rate)
    voiced /= np.max(np.abs(voiced))
    syllables = (0.5 + 0.5 * np.sin(2 * np.pi * 4 * t)) ** 2
    syllables *= np.sin(2 * np.pi * 0.8 * t) > -0.3
    return (0.35 * voiced * syllables).astype(np.float32)


def _tv_like(sample_rate: int) -> np.ndarray:
    rng = np.random.default_rng(42)
    mixed = _speech_like(sample_rate) + 0.25 * _tone(sample_rate)
    mixed += 0.08 * rng.standard_normal(sample_rate)
    compressed = np.tanh(2.0 * mixed) / np.tanh(2.0) * 0.4
    return compressed.astype(np.float32)


def _silence(sample_rate: int) -> np.ndarray:
    return np.zeros(sample_rate, dtype=np.float32)


def _steady_white(sample_rate: int) -> np.ndarray:
    return (0.1 * np.random.default_rng(9).standard_normal(sample_rate)
            ).astype(np.float32)


CASES = {
    "musica": _tone,
    "ruido": _white_burst,
    "pessoa": _speech_like,
    "tv": _tv_like,
    "silencio": _silence,
}


class SourceClassTest(unittest.TestCase):
    def test_each_synthetic_source_at_16k(self):
        for expected, make in CASES.items():
            with self.subTest(expected):
                reading = source_class.classify(make(16000), 16000)
                self.assertEqual(reading.label, expected)
                self.assertGreater(reading.confidence, 0.5)

    def test_each_synthetic_source_at_48k(self):
        for expected, make in CASES.items():
            with self.subTest(expected):
                reading = source_class.classify(make(48000), 48000)
                self.assertEqual(reading.label, expected)
                self.assertGreater(reading.confidence, 0.5)

    def test_steady_white_noise_is_noise_not_tv(self):
        for rate in (16000, 48000):
            with self.subTest(rate):
                self.assertEqual(
                    source_class.classify(_steady_white(rate), rate).label,
                    source_class.NOISE)

    def test_short_transient_is_noise(self):
        rate = 16000
        reading = source_class.classify(_white_burst(rate), rate)
        self.assertEqual(reading.label, source_class.NOISE)
        self.assertGreater(reading.features["crest"], 5.0)
        self.assertLess(reading.features["active_ratio"], 0.4)

    def test_features_are_finite_and_serialisable(self):
        reading = source_class.classify(_tone(16000), 16000)
        for key, value in reading.features.items():
            self.assertIsInstance(key, str)
            self.assertTrue(np.isfinite(value), key)
        self.assertIn("rms_db", reading.features)
        self.assertIn("voice_ratio", reading.features)
        self.assertIn("flatness", reading.features)
        self.assertIn("harmonicity", reading.features)
        for name in source_class.CLASSES:
            self.assertIn(f"score_{name}", reading.features)

    def test_int16_and_2d_blocks_are_accepted(self):
        signal = _tone(16000)
        pcm = np.clip(signal * 32767, -32768, 32767).astype(np.int16)
        reading = source_class.classify(pcm.reshape(-1, 1), 16000)
        self.assertEqual(reading.label, source_class.MUSIC)
        raw = source_class.classify(pcm.tobytes(), 16000)
        self.assertEqual(raw.label, source_class.MUSIC)

    def test_silence_and_empty_block(self):
        self.assertEqual(
            source_class.classify(np.zeros(0, np.float32), 16000).label,
            source_class.SILENCE)
        very_quiet = (1e-5 * np.random.default_rng(1).standard_normal(16000)
                      ).astype(np.float32)
        self.assertEqual(
            source_class.classify(very_quiet, 16000).label,
            source_class.SILENCE)

    def test_invalid_sample_rate_is_rejected(self):
        with self.assertRaises(ValueError):
            source_class.classify(_tone(16000), 10)

    def test_rolling_classifier_tracks_the_last_half_second(self):
        for expected in ("musica", "pessoa", "tv"):
            with self.subTest(expected):
                rate = 16000
                signal = CASES[expected](rate)
                classifier = source_class.SourceClassifier(window_ms=500)
                reading = None
                for start in range(0, rate, rate // 50):
                    reading = classifier.push(signal[start:start + rate // 50],
                                              rate)
                self.assertIsNotNone(reading)
                self.assertEqual(reading.label, expected)
                self.assertLessEqual(reading.features["duration_s"], 0.55)

    def test_rolling_classifier_returns_silence_before_any_audio(self):
        classifier = source_class.SourceClassifier()
        reading = classifier.push(np.zeros(0, np.float32), 16000)
        self.assertEqual(reading.label, source_class.SILENCE)
        self.assertEqual(reading.confidence, 0.5)


class HighPassAnalysisTest(unittest.TestCase):
    """O passa-altas de análise: só a forma espectral, nunca o nível.

    Ordem do dono (10/11/2026): limpar os picos fixos de 31/120 Hz que
    desenhavam "música" sobre a voz dele, sem tocar no áudio do STT e sem
    deixar ruído de fundo parecer fala.
    """

    @staticmethod
    def _hum_and_voice(sample_rate=16000, seconds=2.0):
        t = np.arange(int(sample_rate * seconds)) / float(sample_rate)
        rng = np.random.default_rng(3)
        hum = 0.30 * np.sin(2 * np.pi * 31 * t) + 0.20 * np.sin(2 * np.pi * 120 * t)
        voice = (0.06 * np.sin(2 * np.pi * 110 * t) * (1 + np.sin(2 * np.pi * 3.1 * t))
                 + 0.10 * np.sin(2 * np.pi * 220 * t)
                 + 0.12 * np.sin(2 * np.pi * 640 * t)
                 + 0.05 * rng.standard_normal(t.size))
        return (hum + voice).astype(np.float32)

    def test_corte_e_inclinacao_de_2a_ordem(self):
        """-3 dB no corte, ~-12 dB numa oitava abaixo, ~-24 dB em duas."""
        hz = 120.0
        hp = source_class.HighPass(hz, 16000)
        b, a = hp._coeffs[:3], (1.0,) + hp._coeffs[3:]

        def gain(freq):
            z = np.exp(-2j * np.pi * freq / 16000.0)
            return abs((b[0] + b[1] * z + b[2] * z * z)
                       / (a[0] + a[1] * z + a[2] * z * z))

        self.assertAlmostEqual(20 * math.log10(gain(hz)), -3.0, delta=0.6)
        self.assertAlmostEqual(20 * math.log10(gain(hz / 2)), -12.0, delta=1.5)
        self.assertAlmostEqual(20 * math.log10(gain(hz / 4)), -24.0, delta=2.5)
        self.assertGreater(gain(800.0), 0.99)   # a banda de voz inteira passa

    def test_estado_continuo_nao_clipping_na_borda_do_bloco(self):
        x = self._hum_and_voice()
        whole = source_class.HighPass(120.0, 16000).process(x)
        hp = source_class.HighPass(120.0, 16000)
        blocks = [hp.process(x[i:i + 1024]) for i in range(0, x.size, 1024)]
        streamed = np.concatenate(blocks)
        self.assertTrue(np.allclose(whole, streamed, atol=1e-6),
                        "filtrar por blocos mudou o sinal: estado perdido")
        step = np.abs(np.diff(streamed))
        edges = step[np.arange(1023, x.size, 1024)]
        inside = np.delete(step, np.arange(1023, x.size, 1024))
        self.assertLess(float(edges.max()), float(inside.max()) * 1.5,
                        "salto na borda do bloco: o transitório voltou")

    def test_filtra_a_forma_mas_preserva_o_nivel(self):
        x = self._hum_and_voice()
        cru = source_class.classify(x, 16000)
        lido = source_class.classify(x, 16000,
                                     highpass=source_class.HighPass(120.0, 16000))
        self.assertEqual(cru.features["rms_db"], lido.features["rms_db"])
        self.assertEqual(cru.features["peak"], lido.features["peak"])
        self.assertGreater(lido.features["voice_ratio"],
                           cru.features["voice_ratio"] + 0.15)
        self.assertLess(lido.features["tonal"], cru.features["tonal"])
        self.assertAlmostEqual(lido.features["hp_cutoff_hz"], 120.0)
        self.assertEqual(cru.features["hp_cutoff_hz"], 0.0)

    def test_o_caller_nunca_e_mutado(self):
        x = self._hum_and_voice(seconds=1.0)
        antes = x.copy()
        source_class.classify(x, 16000,
                              highpass=source_class.HighPass(120.0, 16000))
        np.testing.assert_array_equal(x, antes)

    def test_corte_zero_desliga_e_config_saneado(self):
        self.assertFalse(source_class.HighPass(0.0, 16000).enabled)
        x = self._hum_and_voice(seconds=1.0)
        cru = source_class.classify(x, 16000)
        desligado = source_class.classify(
            x, 16000, highpass=source_class.HighPass(0.0, 16000))
        self.assertEqual(cru.label, desligado.label)
        self.assertIsNone(source_class.make_highpass(0))
        self.assertEqual(source_class.highpass_hz_from_config({}),
                         source_class.HIGHPASS_CUTOFF_HZ)
        self.assertEqual(source_class.highpass_hz_from_config(
            {"hp_cutoff_hz": "95"}), 95.0)
        self.assertEqual(source_class.highpass_hz_from_config(
            {"hp_cutoff_hz": "texto"}), source_class.HIGHPASS_CUTOFF_HZ)
        self.assertEqual(source_class.highpass_hz_from_config(
            {"hp_cutoff_hz": 9000}), source_class.HIGHPASS_MAX_HZ)

    def test_rota_scipy_e_rota_numpy_dao_o_mesmo_sinal(self):
        try:
            import scipy.signal  # noqa: F401
        except Exception:
            self.skipTest("scipy ausente: só a rota numpy existe aqui")
        x = self._hum_and_voice()

        def run_in_numpy(stream):
            saved, saved_tried = source_class._LFILTER, source_class._LFILTER_TRIED
            source_class._LFILTER, source_class._LFILTER_TRIED = None, True
            try:
                hp = source_class.HighPass(120.0, 16000)
                return np.concatenate([hp.process(part) for part in stream])
            finally:
                source_class._LFILTER, source_class._LFILTER_TRIED = (
                    saved, saved_tried)

        parts = [x[i:i + 1024] for i in range(0, x.size, 1024)]
        sem_scipy = run_in_numpy(parts)
        source_class._resolve_lfilter()           # deixa o scipy resolver de fato
        hp = source_class.HighPass(120.0, 16000)
        com_scipy = np.concatenate([hp.process(part) for part in parts])
        if source_class._LFILTER is not None:
            np.testing.assert_allclose(com_scipy, sem_scipy, atol=1e-5)
        np.testing.assert_allclose(com_scipy,
                                   source_class.HighPass(120.0, 16000).process(x),
                                   atol=1e-5)

    def test_stream_classifier_filtra_na_entrada_e_mantem_o_nivel(self):
        rate = 16000
        x = self._hum_and_voice(seconds=4.0)
        readings = {}
        for hz in (0, 120):
            classifier = source_class.SourceClassifier(window_ms=500,
                                                       highpass_hz=hz)
            last = None
            for i in range(0, x.size, 1024):
                last = classifier.push(x[i:i + 1024], rate)
            readings[hz] = last.features
        self.assertGreater(readings[120]["voice_ratio"],
                           readings[0]["voice_ratio"])
        self.assertAlmostEqual(readings[120]["rms_db"], readings[0]["rms_db"])
        self.assertEqual(readings[0]["hp_cutoff_hz"], 0.0)
        self.assertEqual(readings[120]["hp_cutoff_hz"], 120.0)

    def test_reset_zera_o_estado_do_filtro(self):
        rate = 16000
        x = self._hum_and_voice(seconds=2.0)
        classifier = source_class.SourceClassifier(window_ms=500, highpass_hz=120)
        for i in range(0, x.size, 1024):
            classifier.push(x[i:i + 1024], rate)
        classifier.reset()
        fresh = source_class.SourceClassifier(window_ms=500, highpass_hz=120)
        for i in range(0, x.size, 1024):
            a = classifier.push(x[i:i + 1024], rate)
            b = fresh.push(x[i:i + 1024], rate)
        self.assertEqual(a.label, b.label)
        self.assertEqual(a.features["voice_ratio"], b.features["voice_ratio"])


if __name__ == "__main__":
    unittest.main()
