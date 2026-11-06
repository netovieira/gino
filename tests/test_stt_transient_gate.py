"""Portão de transitório: tecla, clique e impacto não abrem turno.

O microfone fica aberto o dia todo e o VAD de energia disparava com teclado,
impacto e ruído transitório — turnos fantasma. O discriminante medido e
aceito pelo dono: um transitório tem a energia FORA da banda de voz e um
envelope estreito. Um trecho só é "possível voz" quando as três provas passam
(`core.groq_stt.voice_proof`):

  * >= 35% da energia em 300–3400 Hz;
  * envelope ativo com >= 25 ms de largura contínua;
  * entropia espectral (normalizada, dentro da banda) >= 0.45.

Fora disso não abre turno, não vai ao STT e — o segundo defeito — não chega ao
voiceprint de main.py: `_emit()` recusa clipe sem prova e registra o motivo.

O teste mais importante deste arquivo é o da fala BAIXA: o dono já sofreu com
falso positivo e ficaria furioso com falso negativo. Depois do boost menor
(3x em vez de 10x) uma fala fraca de verdade continua abrindo turno.
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


def _click(amplitude: float = 0.45) -> np.ndarray:
    """Tecla/mesa: impacto de ressonância baixa (~150 Hz) com decaimento.

    A energia está toda fora da banda de voz — é o clique de teclado real,
    que antes cruzava o limiar por ~1–2 quadros e abria um turno.
    """
    t = np.arange(_FRAME) / _SR
    x = amplitude * np.exp(-t / 0.012) * np.sin(2 * np.pi * 150.0 * t)
    return (x * 32767.0).astype(np.int16)


def _white_pulse(amplitude: float = 0.35, seed: int = 10) -> np.ndarray:
    """Pulso branco transitório: ruído de 10 ms e silêncio no resto do quadro.

    A banda e a entropia passariam (é ruído), mas o envelope é estreito: um
    pulso não é voz.
    """
    rng = np.random.default_rng(seed)
    x = np.zeros(_FRAME)
    x[100:260] = amplitude * rng.normal(size=160)
    return (x * 32767.0).astype(np.int16)


def _tone(hz: float = 150.0, amplitude: float = 0.3) -> np.ndarray:
    t = np.arange(_FRAME) / _SR
    return (amplitude * np.sin(2 * np.pi * hz * t) * 32767.0).astype(np.int16)


class _Harness(unittest.TestCase):
    def setUp(self):
        self._orig_up = groq_stt._duck.up
        self._orig_down = groq_stt._duck.down
        self._orig_heartbeat = groq_stt._duck.heartbeat
        groq_stt._duck.up = lambda: None
        groq_stt._duck.down = lambda: None
        groq_stt._duck.heartbeat = lambda: None
        self._lock = threading.Lock()
        self.got: list[bytes] = []
        self.dropped: list[tuple[str, float]] = []
        self._listeners = []

    def tearDown(self):
        for listener in self._listeners:
            listener.stop()
        groq_stt._duck.up = self._orig_up
        groq_stt._duck.down = self._orig_down
        groq_stt._duck.heartbeat = self._orig_heartbeat

    def _listener(self, **kwargs):
        def _transcribe(pcm: bytes) -> str:
            with self._lock:
                self.got.append(pcm)
            return "fala transcrita"

        def _gate_drop(reason: str, seconds: float) -> None:
            with self._lock:
                self.dropped.append((reason, seconds))

        listener = groq_stt.GroqListener(
            "", "test", "pt",
            on_text=lambda _t: None,
            on_utterance=lambda _t, _p: None,
            on_status=lambda _m: None,
            on_gate_drop=_gate_drop,
            sample_rate=_SR,
            transcribe_fn=_transcribe,
            use_vad=False,  # tests use synthetic audio, not real speech
            **kwargs,
        )
        # Endpointing é um modelo à parte: o corte fixo de silêncio basta.
        listener._endpoint_complete = lambda _pcm: True
        self._listeners.append(listener)
        return listener

    @staticmethod
    def _feed(listener, count: int, block: np.ndarray) -> None:
        for _ in range(count):
            listener.feed(block)

    def _settle_floor(self, listener) -> None:
        """Deixa o piso da sala assentar antes do estímulo (limiar ~0.006)."""
        self._feed(listener, 40, _SILENCE)

    def _wait_for(self, count: int, timeout: float = 5.0) -> list[bytes]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.got) >= count:
                    return list(self.got)
            time.sleep(0.02)
        with self._lock:
            return list(self.got)

    def _chunks(self) -> list[bytes]:
        with self._lock:
            return list(self.got)


class TransientDoesNotOpenTest(_Harness):
    def test_clique_de_teclado_nao_abre_turno(self):
        listener = self._listener()
        self._settle_floor(listener)
        # Clique real: um quadro de impacto e o resto silêncio.
        for _ in range(8):
            listener.feed(_click())
            self._feed(listener, 4, _SILENCE)
        self.assertFalse(listener.speaking,
                         "um clique de teclado abriu turno")
        self._feed(listener, groq_stt._END_BLOCKS, _SILENCE)
        self.assertEqual(self._wait_for(1, timeout=0.4), [],
                         "o clique chegou ao STT")
        stats = listener.voice_gate_stats()
        self.assertGreater(stats["blocks"], 0, "o portão não mediu nada")
        self.assertGreater(stats["by_reason"]["banda"], 0,
                           "o clique devia falhar na banda de voz")

    def test_pulso_branco_transitorio_nao_abre_turno(self):
        listener = self._listener()
        self._settle_floor(listener)
        for _ in range(8):
            listener.feed(_white_pulse())
            self._feed(listener, 4, _SILENCE)
        self.assertFalse(listener.speaking,
                         "um pulso branco transitório abriu turno")
        self._feed(listener, groq_stt._END_BLOCKS, _SILENCE)
        self.assertEqual(self._wait_for(1, timeout=0.4), [])
        self.assertGreater(listener.voice_gate_stats()["by_reason"]["envelope"],
                           0, "o pulso devia falhar pelo envelope estreito")

    def test_tom_puro_nao_e_voz(self):
        # Um apito/beep: energia e envelope não bastam, a entropia entrega.
        proof = groq_stt.voice_proof(_tone(440.0, 0.3))
        self.assertFalse(proof.ok)
        self.assertEqual(proof.reason, "entropia")
        self.assertLess(proof.entropy, groq_stt._VOICE_MIN_ENTROPY)

    def test_clique_antes_da_fala_nao_envenena_o_portao(self):
        # O clique não pode deixar espectro na janela: a fala que vem logo
        # depois abre o turno normalmente.
        listener = self._listener()
        self._settle_floor(listener)
        for _ in range(3):
            listener.feed(_click())
            self._feed(listener, 4, _SILENCE)
        self._feed(listener, 20, voice_block(6000))
        self.assertTrue(listener.speaking, "o clique impediu a fala de abrir")
        self._feed(listener, groq_stt._END_BLOCKS, _SILENCE)
        self.assertEqual(len(self._wait_for(1)), 1)


class VoiceStillOpensTest(_Harness):
    def test_fala_na_banda_com_envelope_largo_abre_turno(self):
        listener = self._listener()
        self._settle_floor(listener)
        self._feed(listener, 20, voice_block(6000, phase=0))
        self._feed(listener, 20, voice_block(6000, phase=1024))
        self.assertTrue(listener.speaking, "a fala não abriu turno")
        self._feed(listener, groq_stt._END_BLOCKS, _SILENCE)
        chunks = self._wait_for(1)
        self.assertEqual(len(chunks), 1, "a fala não chegou ao STT")
        self.assertGreater(len(chunks[0]) / (2 * _SR), 1.0)
        stats = listener.voice_gate_stats()
        self.assertEqual(stats["turns_opened"], 1)
        self.assertEqual(stats["turns_opened_by_bypass"], 0,
                         "fala legítima não pode depender da válvula")
        self.assertEqual(stats["clips_dropped"], 0)

    def test_fala_baixa_ainda_abre_depois_do_boost_menor(self):
        """O teste que importa: falso NEGATIVO é o que o dono não aceita.

        Fala de verdade fraca (RMS ~0.0097, bem abaixo do nível de voz cheia)
        tem de abrir turno sozinha, passar no portão e chegar ao STT ampliada
        — agora no máximo 3x, e não mais em 10x.
        """
        quiet = voice_block(450)          # RMS ≈ 0.0097
        quiet_rms = float(np.sqrt(np.mean(
            (quiet.astype(np.float32) / 32768.0) ** 2)))
        self.assertLess(quiet_rms, 0.012)
        listener = self._listener()
        self._settle_floor(listener)
        self._feed(listener, 20, quiet)
        self.assertTrue(listener.speaking,
                        "uma fala baixa real deixou de abrir turno")
        self.assertEqual(listener.voice_gate_stats()["turns_opened_by_bypass"],
                         0, "a fala baixa só abriu pela válvula de energia")
        self._feed(listener, groq_stt._END_BLOCKS, _SILENCE)
        chunks = self._wait_for(1)
        self.assertEqual(len(chunks), 1, "a fala baixa não chegou ao STT")
        clip = chunks[0]
        self.assertEqual(clip.boost_gain, groq_stt._MAX_GAIN)
        self.assertLess(clip.pre_rms, 0.03)
        boosted_rms = float(np.sqrt(np.mean(
            (np.frombuffer(bytes(clip), dtype=np.int16).astype(np.float32)
             / 32768.0) ** 2)))
        # O RMS do clipe inclui pré-roll e cauda de silêncio: a comparação
        # justa é com o RMS original do MESMO clipe (`pre_rms`), e o ganho de
        # 3x tem de aparecer nele.
        self.assertGreater(boosted_rms, clip.pre_rms * 2.5,
                           "o clipe não foi ampliado o suficiente para o Whisper")
        self.assertLessEqual(groq_stt._MAX_GAIN, 3.0)
        self.assertLessEqual(groq_stt._TARGET_RMS, 0.04)


class EmitNeedsVoiceProofTest(_Harness):
    def test_emit_nao_dispara_sem_prova_de_voz(self):
        # Um segmento montado à força, sem nunca ter passado pelo portão:
        # é exatamente o clipe que chegava ao voiceprint.
        listener = self._listener()
        listener._seg = [voice_block(6000).tobytes()] * 8
        listener._voiced = 8
        listener._active = True
        listener._voice_proven = False
        listener._bypass_open = False
        listener._close()
        time.sleep(0.2)
        self.assertEqual(self._chunks(), [], "o clipe sem prova foi transcrito")
        with self._lock:
            dropped = list(self.dropped)
        self.assertEqual(len(dropped), 1, "o descarte não foi anunciado")
        self.assertIn(dropped[0][0], groq_stt.VOICE_GATE_REASONS)
        stats = listener.voice_gate_stats()
        self.assertEqual(stats["clips_dropped"], 1)
        self.assertIsNotNone(stats["last_drop"])
        self.assertEqual(stats["last_drop"]["reason"], dropped[0][0])

    def test_prova_de_voz_deixa_o_clipe_passar(self):
        listener = self._listener()
        listener._seg = [voice_block(6000).tobytes()] * 8
        listener._voiced = 8
        listener._active = True
        listener._voice_proven = True
        listener._close()
        self.assertEqual(len(self._wait_for(1)), 1,
                         "clipe com prova de voz foi descartado")
        stats = listener.voice_gate_stats()
        self.assertEqual(stats["clips_dropped"], 0)
        self.assertEqual(stats["turns_opened_by_bypass"], 0)


class GateCriterionTest(unittest.TestCase):
    """Os números do critério, presos no código e no teste."""

    def test_numeros_do_criterio(self):
        self.assertEqual(groq_stt._VOICE_BAND_LO_HZ, 300.0)
        self.assertEqual(groq_stt._VOICE_BAND_HI_HZ, 3400.0)
        self.assertEqual(groq_stt._VOICE_MIN_BAND_FRAC, 0.35)
        self.assertEqual(groq_stt._VOICE_MIN_ENVELOPE_S, 0.025)
        self.assertEqual(groq_stt._VOICE_MIN_ENTROPY, 0.45)
        self.assertEqual(groq_stt._VOICE_WINDOW_BLOCKS, 3)

    def test_voice_proof_mede_as_tres_provas(self):
        voice = groq_stt.voice_proof(voice_block(6000))
        self.assertTrue(voice.ok)
        self.assertGreaterEqual(voice.band_frac, groq_stt._VOICE_MIN_BAND_FRAC)
        self.assertGreaterEqual(voice.envelope_s, groq_stt._VOICE_MIN_ENVELOPE_S)
        self.assertGreaterEqual(voice.entropy, groq_stt._VOICE_MIN_ENTROPY)
        self.assertEqual(voice.reason, "")

    def test_voice_proof_recusa_cada_tipo_de_transitorio(self):
        click = groq_stt.voice_proof(_click())
        self.assertFalse(click.ok)
        self.assertEqual(click.reason, "banda")
        pulse = groq_stt.voice_proof(_white_pulse())
        self.assertFalse(pulse.ok)
        self.assertEqual(pulse.reason, "envelope")
        silence = groq_stt.voice_proof(np.zeros(_FRAME, dtype=np.int16))
        self.assertFalse(silence.ok)
        self.assertEqual(silence.reason, "silencio")

    def test_janela_de_tres_quadros_aceita_fala_e_recusa_pulso(self):
        voice = [voice_block(6000, phase=i * _FRAME) for i in range(3)]
        self.assertTrue(groq_stt.voice_proof(voice).ok)
        pulses = [_white_pulse(seed=10 + i) for i in range(3)]
        self.assertEqual(groq_stt.voice_proof(pulses).reason, "envelope")


class SustainedEnergyValveTest(_Harness):
    def test_energia_sustentada_sem_prova_abre_pela_valvula(self):
        """Válvula contra falso negativo: um transitório é CURTO.

        Um tom contínuo de 150 Hz não tem prova de voz (banda e entropia
        falham), mas fica acima do limiar por mais de 1,2 s — isso não é um
        clique. A válvula abre o turno como antes do portão, e o evento é
        contado separado para main.py poder logar.
        """
        listener = self._listener()
        self._settle_floor(listener)
        self._feed(listener, groq_stt._VOICE_BYPASS_BLOCKS - 1, _tone())
        self.assertFalse(listener.speaking,
                         "abriu antes de a energia provar que é sustentada")
        self._feed(listener, 6, _tone())
        self.assertTrue(listener.speaking, "a válvula não abriu o turno")
        stats = listener.voice_gate_stats()
        self.assertEqual(stats["turns_opened_by_bypass"], 1)
        self._feed(listener, groq_stt._END_BLOCKS, _SILENCE)
        chunks = self._wait_for(1)
        self.assertEqual(len(chunks), 1,
                         "o turno da válvula não chegou ao STT")
        # O clipe da válvula viaja MARCADO: main.py pode logar no voiceprint
        # que aquela fala não passou no portão.
        self.assertFalse(chunks[0].voice_proven,
                         "clipe sem prova chegou marcado como provado")


if __name__ == "__main__":
    unittest.main()
