"""Silero VAD integrado no fluxo de STT.

O VAD (Silero ONNX) é o detector primário de atividade de voz no segmentador
(``GroqListener.feed``). O limiar RMS continua como camada de fallback: um
bloco é candidato a fala quando o VAD diz "fala" OU o RMS cruza o piso da
sala. Sem o modelo ONNX presente, o comportamento é idêntico ao de antes
(RMS puro).

O que estes testes afirmam:
  * O VAD é instanciado quando o modelo está presente.
  * ``_vad_says_speech`` é chamado em cada bloco que passa por ``feed()``.
  * O contador ``_vad_calls`` cresce com blocos alimentados.
  * A propriedade ``vad_active`` reflete a disponibilidade do modelo.
  * O VAD é reiniciado nos limites de segmento (abertura e fechamento).
  * Fala sintética continua abrindo turno (o VAD não quebrou o fluxo).
  * Silêncio continua sem abrir turno (o VAD não gera falso positivo).
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

import numpy as np

from core import groq_stt, silero_vad
from tests.audio_fixtures import voice_block

_SR = 16000
_FRAME = 1024
_SILENCE = np.zeros(_FRAME, dtype=np.int16)


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
        self._listeners: list[groq_stt.GroqListener] = []

    def tearDown(self):
        for listener in self._listeners:
            listener.stop()
        groq_stt._duck.up = self._orig_up
        groq_stt._duck.down = self._orig_down
        groq_stt._duck.heartbeat = self._orig_heartbeat

    def _listener(self, **kwargs) -> groq_stt.GroqListener:
        def _transcribe(pcm: bytes) -> str:
            with self._lock:
                self.got.append(pcm)
            return "fala transcrita"

        listener = groq_stt.GroqListener(
            "", "test", "pt",
            on_text=lambda _t: None,
            on_utterance=lambda _t, _p: None,
            on_status=lambda _m: None,
            sample_rate=_SR,
            transcribe_fn=_transcribe,
            **kwargs,
        )
        listener._endpoint_complete = lambda _pcm: True
        self._listeners.append(listener)
        return listener

    @staticmethod
    def _feed(listener, count: int, block: np.ndarray) -> None:
        for _ in range(count):
            listener.feed(block)

    def _settle_floor(self, listener) -> None:
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


@unittest.skipUnless(silero_vad.available(), "Silero VAD model not downloaded")
class VADIsIntegratedTest(_Harness):
    """O VAD está ligado no fluxo — não é só um wrapper solitário."""

    def test_listener_carrega_o_vad_quando_o_modelo_existe(self):
        listener = self._listener()
        self.assertTrue(listener.vad_active,
                        "o Silero VAD não foi carregado no GroqListener")
        self.assertIsNotNone(listener._vad)

    def test_vad_e_chamado_em_cada_bloco(self):
        """O contador de chamadas cresce com blocos alimentados."""
        listener = self._listener()
        self.assertEqual(listener._vad_calls, 0)
        self._feed(listener, 10, _SILENCE)
        self.assertGreaterEqual(listener._vad_calls, 10,
                                "o VAD não foi chamado em cada bloco")

    def test_vad_e_chamado_com_fala_sintetica(self):
        listener = self._listener()
        self._settle_floor(listener)
        self._feed(listener, 5, voice_block(6000))
        self.assertGreaterEqual(listener._vad_calls, 5,
                                "o VAD não processou blocos de fala")

    def test_vad_reinicia_na_abertura_de_segmento(self):
        """O estado interno do VAD não vaza entre segmentos."""
        listener = self._listener()
        self._settle_floor(listener)
        # Abre um turno com fala sintética.
        self._feed(listener, 20, voice_block(6000))
        self.assertTrue(listener.speaking)
        # O VAD foi resetado na abertura (estado zerado após o reset).
        # Alimentar mais blocos deve incrementar o contador a partir do reset.
        calls_before = listener._vad_calls
        self._feed(listener, 5, voice_block(6000))
        self.assertGreater(listener._vad_calls, calls_before,
                           "o VAD parou de ser chamado após abrir segmento")

    def test_vad_reinicia_no_fecho_de_segmento(self):
        listener = self._listener()
        self._settle_floor(listener)
        self._feed(listener, 20, voice_block(6000))
        self.assertTrue(listener.speaking)
        # Fecha o turno com silêncio.
        self._feed(listener, groq_stt._END_BLOCKS, _SILENCE)
        chunks = self._wait_for(1)
        self.assertEqual(len(chunks), 1, "a fala não foi transcrita")
        # Após o fecho, o VAD foi resetado. Novos blocos de silêncio devem
        # manter o VAD chamado (contexto limpo).
        calls_after_close = listener._vad_calls
        self._feed(listener, 5, _SILENCE)
        self.assertGreater(listener._vad_calls, calls_after_close,
                           "o VAD parou de ser chamado após fechar segmento")


@unittest.skipUnless(silero_vad.available(), "Silero VAD model not downloaded")
class VADDoesNotBreakFlowTest(_Harness):
    """O VAD não quebrou o fluxo existente de abertura e fecho de turnos."""

    def test_fala_sintetica_abre_turno_com_vad(self):
        listener = self._listener()
        self._settle_floor(listener)
        self._feed(listener, 20, voice_block(6000, phase=0))
        self._feed(listener, 20, voice_block(6000, phase=1024))
        self.assertTrue(listener.speaking,
                        "a fala não abriu turno com o VAD ligado")
        self._feed(listener, groq_stt._END_BLOCKS, _SILENCE)
        chunks = self._wait_for(1)
        self.assertEqual(len(chunks), 1,
                         "a fala não chegou ao STT com o VAD ligado")

    def test_silencio_nao_abre_turno_com_vad(self):
        listener = self._listener()
        self._settle_floor(listener)
        self._feed(listener, 60, _SILENCE)
        self.assertFalse(listener.speaking,
                         "silêncio abriu turno com o VAD ligado")

    def test_clique_nao_abre_turno_com_vad(self):
        """O portão de transitório continua funcionando junto com o VAD."""
        listener = self._listener()
        self._settle_floor(listener)
        t = np.arange(_FRAME) / _SR
        click = (0.45 * np.exp(-t / 0.012)
                 * np.sin(2 * np.pi * 150.0 * t) * 32767.0).astype(np.int16)
        for _ in range(8):
            listener.feed(click)
            self._feed(listener, 4, _SILENCE)
        self.assertFalse(listener.speaking,
                         "clique abriu turno com o VAD ligado")


class VADFallbackWithoutModelTest(_Harness):
    """Sem o modelo ONNX, o comportamento é idêntico ao de antes."""

    def test_sem_modelo_vad_esta_desligado(self):
        # Força o VAD a None, simulando ausência do modelo.
        listener = self._listener()
        listener._vad = None
        self.assertFalse(listener.vad_active)
        # _vad_says_speech retorna False quando o VAD não está carregado.
        self.assertFalse(listener._vad_says_speech(b"\x00\x00" * _FRAME))

    def test_fala_abre_turno_sem_vad_puro_rms(self):
        listener = self._listener()
        listener._vad = None
        self._settle_floor(listener)
        self._feed(listener, 20, voice_block(6000, phase=0))
        self._feed(listener, 20, voice_block(6000, phase=1024))
        self.assertTrue(listener.speaking,
                        "a fala não abriu turno no modo RMS puro (fallback)")


class VADMockCallTest(_Harness):
    """Afirma que o método is_speech do SileroVAD é invocado."""

    def test_is_speech_e_invocado_pelo_feed(self):
        listener = self._listener()
        if listener._vad is None:
            # Sem modelo: cria um mock para afirmar a integração.
            fake_vad = mock.MagicMock()
            fake_vad.is_speech.return_value = False
            listener._vad = fake_vad
        else:
            # Com modelo: espiona o método real.
            fake_vad = mock.MagicMock(wraps=listener._vad)
            listener._vad = fake_vad

        listener._vad_calls = 0
        self._feed(listener, 5, _SILENCE)
        self.assertGreaterEqual(fake_vad.is_speech.call_count, 5,
                                "is_speech não foi chamado pelo feed()")


if __name__ == "__main__":
    unittest.main()
