"""Fila de fala: geração nova mata a antiga, teto e idade explícitos.

O defeito de 23,4 s foi uma fala de um turno esperando atrás da fila de outro.
Aqui a fila é exercitada sem Fish Audio e sem worker real de síntese: o que se
prova é que (1) um turno novo descarta os itens antigos e libera quem espera,
(2) o teto da fila tem número e motivo, (3) item velho acima da idade máxima
não é falado, e (4) um chunk já enfileirado de geração antiga não chega ao
alto-falante.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import queue
import threading
import time
import unittest
from unittest import mock

import main
from tests.test_playback_drop import _FakeSD, _Stream
from tests.test_playback_drop import _make_obj as _playback_obj


class _UI:
    def __init__(self):
        self.logs: list[str] = []
        self.muted = False

    def write_log(self, text):
        self.logs.append(str(text))

    def push_visemes(self, *_a):
        pass

    def set_audio_level(self, _level):
        pass


def _obj(turn: int = 1) -> main.JarvisLive:
    obj = object.__new__(main.JarvisLive)
    obj.ui = _UI()
    obj._speech_epoch = 0
    obj._say_gen = 0
    obj._say_gen_turn = 0
    obj._say_preempt = 0
    obj._say_enqueued = {}
    obj._say_q = queue.Queue()
    obj._say_hi_q = queue.Queue()
    obj._say_thread = object()      # não sobe worker real nesta suíte
    obj._turn_ref = turn
    obj._speech_tail_note_new_speech = lambda *a, **k: None
    obj._flush_speech_cut_note = lambda **k: None
    return obj


class GenerationQueueTest(unittest.TestCase):
    def test_turno_novo_descarta_fila_antiga_e_libera_quem_espera(self):
        obj = _obj(turn=1)
        old_done = obj._say_now("frase do turno antigo", origin="qoder")
        self.assertIsNotNone(old_done)
        self.assertEqual(obj._say_q.qsize(), 1)

        obj._turn_ref = 2
        new_done = obj._say_now("frase do turno novo", origin="qoder")

        self.assertEqual(obj._say_gen, 1, "a geração não subiu no turno novo")
        self.assertTrue(old_done.is_set(),
                        "quem esperava a fala antiga ficou preso")
        self.assertFalse(new_done.is_set())
        self.assertEqual(obj._say_q.qsize(), 1)
        self.assertEqual(obj._say_q.queue[0][0], "frase do turno novo")
        self.assertTrue(any("novo turno 2 substituiu o turno 1" in line
                            for line in obj.ui.logs), obj.ui.logs)

    def test_teto_da_fila_e_explicito_e_tem_numero(self):
        obj = _obj()
        old_done = obj._say_now("primeira", origin="aviso")
        for i in range(1, main._SAY_QUEUE_CAP):
            obj._say_now(f"fala {i}", origin="aviso")
        self.assertEqual(obj._say_q.qsize(), main._SAY_QUEUE_CAP)

        newest = obj._say_now("a mais nova", origin="aviso")

        self.assertEqual(obj._say_q.qsize(), main._SAY_QUEUE_CAP,
                         "o teto não manteve o tamanho")
        self.assertTrue(old_done.is_set())
        self.assertFalse(newest.is_set())
        self.assertTrue(any(f"teto da fila ({main._SAY_QUEUE_CAP})" in line
                            for line in obj.ui.logs), obj.ui.logs)

    def test_item_acima_da_idade_nao_e_falado(self):
        obj = _obj()
        obj._say_thread = None
        called: list[str] = []
        obj._say_blocking = lambda text, **kw: called.append(str(text))
        obj._ensure_say_worker()

        old_done = threading.Event()
        old = ("velha demais", True, 0, "", "", "", "aviso", "", 1, "",
               old_done)
        obj._remember_say_item(old, 0, 1, "aviso")
        obj._say_enqueued[id(old)] = (
            time.monotonic() - main._SAY_ITEM_MAX_AGE_S - 1.0, 0, 1, "aviso")
        obj._say_q.put_nowait(old)

        fresh_done = threading.Event()
        fresh = ("nova", True, 0, "", "", "", "aviso", "", 1, "", fresh_done)
        obj._remember_say_item(fresh, 0, 1, "aviso")
        obj._say_q.put_nowait(fresh)

        self.assertTrue(old_done.wait(timeout=2.0),
                        "o item velho não foi liberado ao ser descartado")
        self.assertTrue(fresh_done.wait(timeout=2.0))
        self.assertEqual(called, ["nova"])
        self.assertTrue(any("acima do teto de" in line for line in obj.ui.logs),
                        obj.ui.logs)


class InterruptGenerationTest(unittest.TestCase):
    def test_interrupt_sobe_epoch_geracao_e_preempcao(self):
        obj = object.__new__(main.JarvisLive)
        obj.ui = _UI()
        obj._speech_epoch = 0
        obj._say_gen = 0
        obj._say_preempt = 0
        obj._turn_ref = 7
        obj._interrupted = False
        obj._interrupt_note = ""
        obj._mark_qoder_voice_preempt = lambda: None
        obj._interrupt_kind = lambda reason: "explicito"
        obj._note_speech_abort = lambda *a: None
        obj._speech_audit_lock = threading.RLock()
        obj._drain_audio_into_tail = lambda *a: None
        obj._silence_output_now = lambda: None
        obj._qoder_live = None
        obj._persona_ops = lambda: None
        obj._qoder_lock = threading.RLock()
        obj._persona_text_buf = {}
        obj._persona_seen = {}
        obj._qoder_current_id = None
        obj._qoder_meta = {}
        obj._flows_cancel = lambda *a: None
        obj.set_speaking = lambda _v: None
        obj._visemes = type("V", (), {"reset": lambda self: None})()
        obj._play_cursor = 0.0
        obj._turn_done_event = None

        main.JarvisLive.interrupt(obj)

        self.assertEqual(obj._speech_epoch, 1)
        self.assertEqual(obj._say_gen, 1)
        self.assertEqual(obj._say_preempt, 1)


class PlaybackGenerationDropTest(unittest.TestCase):
    def test_chunk_de_geracao_antiga_nao_chega_ao_alto_falante(self):
        obj = _playback_obj(chunks=[
            main._SpeechChunk(b"\x00\x02" * 2400, gen=1, turn=1)])
        obj._say_gen = 2
        streams: list[_Stream] = []

        def factory(**_kw):
            st = _Stream()
            streams.append(st)
            return st

        async def _main():
            try:
                await main.JarvisLive._play_audio(obj)
            except asyncio.CancelledError:
                pass

        fake = _FakeSD(factory)
        out = io.StringIO()
        with mock.patch.object(main, "sd", fake), \
                mock.patch.object(main, "get_output_device", return_value=""), \
                mock.patch.object(main.audio_devices, "resolve",
                                  return_value=None), \
                contextlib.redirect_stdout(out):
            async def _driver():
                task = asyncio.create_task(_main())
                try:
                    await asyncio.wait_for(task, timeout=0.8)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            asyncio.run(_driver())

        self.assertEqual(streams[0].writes, [],
                         "o chunk da geração antiga tocou")
        self.assertIn("fala de geração 1 descartada", out.getvalue())
        self.assertTrue(any("fala antiga descartada" in line
                            for line in obj.ui.logs), obj.ui.logs)


if __name__ == "__main__":
    unittest.main()
