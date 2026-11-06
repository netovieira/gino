"""Emenda contínua do áudio do WhatsApp: nenhum buraco interno > 120 ms.

Defeito medido (05/11/2026, reclamação do dono): nota de voz de 28,7 s
enviada às 22:34 com 24 buracos internos acima de 120 ms (soma 5,9 s,
maior 340 ms) — o `_FISH_GAP` de 220 ms de zeros somava ao padding que o
modelo cola em cada peça, e nada cortava esse silêncio antes de virar ogg.
Estes testes medem o caminho real: síntese → ogg PTT → entrega.
"""
from __future__ import annotations

import array
import math
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import main
from core import brain

SR = 24000
SR_PLAY = 48000


def _tone(ms: int, amp: float = 0.3, freq: float = 300.0) -> bytes:
    n = SR * ms // 1000
    return array.array(
        "h",
        [int(amp * 32767 * math.sin(2 * math.pi * freq * i / SR))
         for i in range(n)]).tobytes()


def _zeros(ms: int, rate: int = SR) -> bytes:
    return b"\x00\x00" * (rate * ms // 1000)


def _defective_pcm() -> bytes:
    """O que o caminho antigo produzia: fala + 80 ms de cauda do modelo +
    220 ms de gap + 60 ms de cabeça da peça seguinte, em cada junção."""
    buf = bytearray(_zeros(100))                       # silêncio inicial
    for i in range(5):
        buf += _tone(400)
        if i < 4:
            buf += _zeros(80) + _zeros(220) + _zeros(60)   # 360 ms de buraco
    buf += _zeros(180)                                 # silêncio final
    return bytes(buf)


def _holes(pcm: bytes, rate: int, thr: float = 0.01,
           encoded: str = "int16") -> dict:
    """Molduras de 10 ms abaixo de `thr` de RMS → runs de silêncio.
    Devolve a métrica exigida: buracos internos, soma e maior, em ms.
    `encoded`: "int16" para PCM cru do motor, "f32" para decoded do ffmpeg."""
    import numpy as np
    if encoded == "int16":
        a = np.frombuffer(pcm[: (len(pcm) // 2) * 2],
                          dtype=np.int16).astype(np.float32) / 32768.0
    else:
        a = np.frombuffer(pcm, dtype=np.float32)
    frame = rate // 100
    n = len(a) // frame
    rms = np.sqrt((a[:n * frame].reshape(n, frame) ** 2).mean(axis=1))
    sil = rms < thr
    runs, i = [], 0
    while i < n:
        if sil[i]:
            j = i
            while j < n and sil[j]:
                j += 1
            runs.append((i * 10, j * 10))
            i = j
        else:
            i += 1
    internal = [(s, e) for (s, e) in runs if s > 0 and e < n * 10]
    return dict(dur_ms=len(a) / rate * 1000.0,
                internal=internal,
                total_internal_ms=sum(e - s for s, e in internal),
                max_internal_ms=max((e - s for s, e in internal), default=0),
                big=[h for h in internal if h[1] - h[0] > 120])


class SmoothPcmSilenceTest(unittest.TestCase):
    def test_internal_holes_are_capped_and_edges_trimmed(self):
        pcm = _defective_pcm()
        before = _holes(pcm, SR)
        self.assertEqual(len(before["big"]), 4)          # 360 ms por junção
        self.assertEqual(before["max_internal_ms"], 360)
        out = brain.smooth_pcm_silence(pcm, SR, max_internal_ms=100)
        after = _holes(out, SR)
        self.assertEqual(after["big"], [])
        self.assertLessEqual(after["max_internal_ms"], 120)
        self.assertEqual(len(after["internal"]), 4)
        # a fala em si não é tocada: 5 blocos de 400 ms continuam lá
        # (+ as bordas mantidas de 20 ms e 60 ms, que não são "internas")
        voiced_ms = after["dur_ms"] - after["total_internal_ms"]
        self.assertAlmostEqual(voiced_ms, 2000.0 + 20.0 + 60.0, delta=30.0)

    def test_short_fixture_passes_through_untouched(self):
        self.assertEqual(brain.smooth_pcm_silence(b"\x01\x02", SR), b"\x01\x02")

    def test_returns_int16_bytes_of_the_same_width(self):
        out = brain.smooth_pcm_silence(_tone(500) + _zeros(300) + _tone(500), SR)
        self.assertEqual(len(out) % 2, 0)


class FishPiecesJoinWithoutGapTest(unittest.TestCase):
    def test_pieces_with_model_padding_join_below_the_hole_gate(self):
        st = {"tts_engine": "fish"}

        def fake(text, settings, style=None):
            return _zeros(60) + _tone(300) + _zeros(80)

        with mock.patch("core.brain.refresh_fish_voice", return_value=st), \
                mock.patch("core.brain._fish_pieces",
                           return_value=["parte um", "parte dois",
                                         "parte três"]), \
                mock.patch("core.brain.tts_fish_pcm24k", side_effect=fake):
            pcm = brain.synth_pcm24k("texto longo com virgulas", st)
        holes = _holes(pcm, SR)
        self.assertEqual(holes["big"], [],
                         f"buracos {holes['internal']}")


class WhatsappAudioFlowTest(unittest.TestCase):
    """Fluxo real de entrega: síntese stubada com PCM conhecido → ogg → send."""

    def _obj(self):
        return object.__new__(main.JarvisLive)

    def test_reply_ogg_is_continuous_canonical_ptt_and_one_file(self):
        if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            self.skipTest("ffmpeg/ffprobe indisponíveis")
        obj = self._obj()
        defective = _defective_pcm()
        before = _holes(defective, SR)
        self.assertEqual(len(before["big"]), 4)   # o defeito de referência
        with mock.patch("core.whatsapp.ffmpeg_ready"), \
                mock.patch("core.brain.settings",
                           return_value={"tts_engine": "fish"}), \
                mock.patch("core.numwords.speakable",
                           side_effect=lambda text, style="words": text), \
                mock.patch("core.brain.synth_pcm24k",
                           return_value=defective):
            path = obj._synthesize_whatsapp_audio("cinco frases faladas")
        self.assertTrue(path)
        try:
            # (c) formato canônico de PTT: opus, 48 kHz, mono — um arquivo só.
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a:0",
                 "-show_entries",
                 "stream=codec_name,sample_rate,channels:format=duration",
                 "-of", "default=noprint_wrappers=1", path],
                capture_output=True, text=True, timeout=30)
            info = dict(line.split("=", 1) for line in
                        probe.stdout.splitlines() if "=" in line)
            self.assertEqual(info.get("codec_name"), "opus")
            self.assertEqual(info.get("sample_rate"), "48000")
            self.assertEqual(info.get("channels"), "1")

            raw = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", path, "-f", "f32le",
                 "-ac", "1", "-ar", str(SR_PLAY), "-"],
                capture_output=True, timeout=60).stdout
            holes = _holes(raw, SR_PLAY, encoded="f32")
            # (b) nenhum buraco interno acima de 120 ms.
            self.assertEqual(holes["big"], [],
                             f"buracos >120ms: {holes['internal']}")
            # (a) duração bate com o texto: 2,0 s de fala + 4 junções de
            # no máximo 100 ms + bordas aparadas, com tolerância do Opus.
            self.assertAlmostEqual(holes["dur_ms"], 2480.0, delta=150.0)
            # a quebra medida antes era 360 ms em cada uma das 4 junções.
            self.assertLessEqual(holes["max_internal_ms"], 120)

            # (d) entrega: um arquivo só via send_media, texto nunca junto.
            with mock.patch("core.whatsapp.send_media",
                            return_value=(True, "ok")) as send_media, \
                    mock.patch("core.whatsapp.send") as send_text:
                obj2 = self._obj()
                obj2._synthesize_whatsapp_audio = mock.Mock(return_value=path)
                self.assertTrue(obj2._send_whatsapp_answer(
                    "", "5517999999999", "cinco frases faladas", "audio"))
            send_media.assert_called_once()
            self.assertEqual(send_media.call_args.args[1], str(path))
            send_text.assert_not_called()
            self.assertTrue(obj2._wa_audio_sent)
            self.assertFalse(Path(path).exists())   # enviado e apagado
        finally:
            if path and Path(path).exists():
                os.unlink(path)


if __name__ == "__main__":
    unittest.main()
