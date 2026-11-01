"""O microfone reabre quando os alto-falantes silenciam.

O turno do Qoder Live fica aberto por minutos enquanto um agente trabalha.
`_reply_active` continua True nesse tempo todo, e a liberação de
`_is_speaking` dependia dele: depois da primeira frase falada a flag ficava
presa, o microfone fechava e o dono só era ouvido se apertasse INTERRUPT.
"""
from __future__ import annotations

import queue
import threading
import time
import unittest

import main


class SpeakingReleaseTest(unittest.TestCase):

    def _obj(self, *, speaking: bool = True, queued: bool = False,
             batch_age: float = 0.0):
        obj = object.__new__(main.JarvisLive)
        obj._is_speaking = speaking
        obj.audio_in_queue = queue.Queue()
        if queued:
            obj.audio_in_queue.put_nowait(b"chunk")
        obj._last_play_batch = time.monotonic() - batch_age
        calls: list[bool] = []
        obj.set_speaking = lambda value: calls.append(value)
        return obj, calls

    def test_silencio_prolongado_reabre_o_microfone_com_turno_aberto(self):
        obj, calls = self._obj(batch_age=main._DUCK_DRAIN_RELEASE_S + 0.2)
        main.JarvisLive._release_speaking_on_drain(obj)
        self.assertEqual(calls, [False])

    def test_fila_com_audio_nao_libera(self):
        obj, calls = self._obj(queued=True,
                               batch_age=main._DUCK_DRAIN_RELEASE_S + 0.2)
        main.JarvisLive._release_speaking_on_drain(obj)
        self.assertEqual(calls, [])

    def test_pausa_entre_frases_nao_libera(self):
        obj, calls = self._obj(batch_age=0.05)
        main.JarvisLive._release_speaking_on_drain(obj)
        self.assertEqual(calls, [])

    def test_sem_fala_nao_toca_no_estado(self):
        obj, calls = self._obj(speaking=False,
                               batch_age=main._DUCK_DRAIN_RELEASE_S + 0.2)
        main.JarvisLive._release_speaking_on_drain(obj)
        self.assertEqual(calls, [])


class MicGateTest(unittest.TestCase):
    """O portão do microfone não pode ficar ensurdecido por um turno aberto."""

    def _obj(self, *, queued: bool = False, batch_age: float = 0.0,
             reply_active: bool = True):
        obj = object.__new__(main.JarvisLive)
        obj._speaking_lock = threading.Lock()
        obj._is_speaking = True
        obj._reply_active = reply_active
        obj.audio_in_queue = queue.Queue()
        if queued:
            obj.audio_in_queue.put_nowait(b"chunk")
        obj._last_play_batch = time.monotonic() - batch_age
        return obj

    def test_turno_aberto_com_fila_vazia_devolve_o_bloco(self):
        # `_reply_active` True (agente trabalhando), nada tocando: o bloco é do
        # dono e o portão abaixo do callback o entrega ao segmenter.
        obj = self._obj(batch_age=main._DUCK_DRAIN_RELEASE_S + 0.2)
        self.assertFalse(main.JarvisLive._mic_gate_speaking(obj))

    def test_fala_tocando_ainda_fecha_o_portao(self):
        obj = self._obj(queued=True, batch_age=main._DUCK_DRAIN_RELEASE_S + 0.2)
        self.assertTrue(main.JarvisLive._mic_gate_speaking(obj))

    def test_pausa_entre_frases_nao_abre_o_portao(self):
        obj = self._obj(batch_age=0.05)
        self.assertTrue(main.JarvisLive._mic_gate_speaking(obj))


class TranscriptBargeTest(unittest.TestCase):
    """Fala transcrita do dono sempre vira turno, nunca vai para a gaveta."""

    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._recent_spoken_text = ""
        obj._qoder_ignore = False
        obj._qoder_live = None
        obj._qoder_live_ready = lambda: True
        obj._mark_qoder_voice_preempt = lambda: None
        obj._log_heard = lambda text: None
        calls: list[str] = []
        obj.interrupt = lambda: calls.append("interrupt")
        obj._on_groq_text = lambda text, **kw: calls.append(text)
        logs: list[str] = []

        class _UI:
            muted = False

            def write_log(self, msg):
                logs.append(msg)

        obj.ui = _UI()
        return obj, calls, logs

    def test_interjeicao_transcrita_derruba_o_turno_e_vira_fala(self):
        obj, calls, _logs = self._obj()
        main.JarvisLive._handle_barge_in(obj, "espera um pouco")
        self.assertEqual(calls, ["interrupt", "espera um pouco"])

    def test_frase_longa_tambem_vira_turno(self):
        obj, calls, _logs = self._obj()
        main.JarvisLive._handle_barge_in(obj, "para tudo agora e me responde")
        self.assertEqual(calls, ["interrupt", "para tudo agora e me responde"])

    def test_eco_do_proprio_audio_nao_interrompe(self):
        obj, calls, _logs = self._obj()
        obj._recent_spoken_text = "estou verificando o microfone agora mesmo"
        main.JarvisLive._handle_barge_in(
            obj, "estou verificando o microfone agora mesmo")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
