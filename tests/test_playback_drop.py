"""O descarte de áudio no playback precisa ser visível, honesto e recuperável.

Reproduz a sessão de 02/11: o dono interrompeu, o epoch subiu e o loop de
`_play_audio` continuou descartando lotes em silêncio; o write seguinte ficou
preso dentro do PortAudio e o alto-falante nunca mais voltou. Estes testes
provam que (1) um lote cortado não toca, mas fica contado e marcado como
cortado — não como falado, e (2) um write que não retorna reabre a saída.
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


class _UI:
    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, text):
        self.logs.append(text)

    def push_visemes(self, *_a):
        pass

    def set_audio_level(self, _level):
        pass


class _Visemes:
    def frames(self, frames, _hop):
        return []

    def reset(self):
        pass


class _Echo:
    def note_output(self, *_a):
        pass

    def is_user_speech(self, *_a):
        return False


class _Stream:
    def __init__(self, block=None):
        self.active = False
        self.starts = 0
        self.aborts = 0
        self.closes = 0
        self.writes: list[bytes] = []
        self.block = block

    @property
    def latency(self):
        return 0.05

    def start(self):
        self.starts += 1
        self.active = True

    def abort(self):
        self.aborts += 1
        self.active = False

    def stop(self):
        self.active = False

    def close(self, ignore_errors=False):
        self.closes += 1
        self.active = False

    def write(self, data):
        if self.block is not None:
            self.block.wait(5.0)
        self.writes.append(bytes(data))


def _make_obj(chunks=()):
    obj = object.__new__(main.JarvisLive)
    obj._is_speaking = False
    obj._speaking_lock = threading.Lock()
    obj.set_speaking = lambda value: setattr(obj, "_is_speaking", bool(value))
    obj.ui = _UI()
    obj.audio_in_queue = asyncio.Queue()
    for chunk in chunks:
        obj.audio_in_queue.put_nowait(chunk)
    obj._visemes = _Visemes()
    obj._echo = _Echo()
    obj._out_latency = 0.05
    obj._play_cursor = 0.0
    obj._last_play_batch = 0.0
    obj._duck_released_on_drain = True
    obj._turn_done_event = None
    obj._interrupted = False
    obj._owner_voice_until = 0.0
    obj._play_burst_until = 0.0
    obj._speech_epoch = 1
    obj._speech_epoch_turn = {}
    obj._speech_gap_until = 0.0
    obj._turn_ref = 43
    obj._turn_input_chan = "voz"
    obj._out_stream = None
    obj._play_abort = threading.Event()
    obj._play_abort_at = 0.0
    obj._play_io_lock = threading.Lock()
    obj._play_writing = set()
    obj._play_exec = None
    obj._play_stuck = 0
    obj._play_discarded = 0
    obj.notes: list[dict] = []
    obj._note_speech = lambda *a, **kw: obj.notes.append(kw)
    obj._duck_check_drain = lambda: None
    return obj


class _FakeSD:
    class PortAudioError(Exception):
        pass

    def __init__(self, factory):
        self.RawOutputStream = factory


class PlaybackEpochDropTest(unittest.TestCase):
    """Lote de resposta já cortada: nada de som, mas o log diz "cortado"."""

    CHUNK = b"\x00\x00" * 2400

    def _run(self, obj, factory, timeout=1.5):
        async def _main():
            try:
                await main.JarvisLive._play_audio(obj)
            except asyncio.CancelledError:
                pass

        fake = _FakeSD(factory)
        with mock.patch.object(main, "sd", fake), \
                mock.patch.object(main, "get_output_device", return_value=""), \
                mock.patch.object(main.audio_devices, "resolve",
                                  return_value=None):
            async def _driver():
                task = asyncio.create_task(_main())
                try:
                    await asyncio.wait_for(task, timeout=timeout)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            asyncio.run(_driver())

    def test_lote_descartado_nao_toca_e_e_marcado_como_cortado(self):
        obj = _make_obj(chunks=[self.CHUNK])
        obj._speech_epoch_turn = {1: 42}
        obj._owner_voice_until = time.monotonic() + 0.35   # segura o lote
        streams: list[_Stream] = []

        def factory(**_kw):
            st = _Stream()
            streams.append(st)
            return st

        def _bump_epoch():
            time.sleep(0.1)
            obj._speech_epoch = 2

        threading.Thread(target=_bump_epoch, daemon=True).start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self._run(obj, factory)

        self.assertEqual(streams[0].writes, [],
                         "o lote da resposta cortada chegou ao alto-falante")
        self.assertIn("lote descartado no playback", out.getvalue())
        self.assertIn("2 chunk(s)", out.getvalue())
        self.assertTrue(any(n.get("generated") is False and n.get("cut") is True
                            and n.get("turn") == 42 for n in obj.notes),
                        f"a fala cortada não foi registrada: {obj.notes}")
        self.assertTrue(any("fala cortada antes do alto-falante" in line
                            for line in obj.ui.logs), obj.ui.logs)

    def test_epoch_que_sobe_durante_a_montagem_do_lote_tambem_descarta(self):
        obj = _make_obj(chunks=[self.CHUNK])
        obj._speech_epoch_turn = {1: 42}
        streams: list[_Stream] = []

        def factory(**_kw):
            st = _Stream()
            streams.append(st)
            return st

        orig = main._pcm_visemes

        def _bump_on_visemes(samples, sr=24000):
            obj._speech_epoch = 2      # o dono cortou enquanto o lote era montado
            return orig(samples, sr)

        out = io.StringIO()
        with mock.patch.object(main, "_pcm_visemes", _bump_on_visemes):
            with contextlib.redirect_stdout(out):
                self._run(obj, factory)

        self.assertEqual(streams[0].writes, [])
        self.assertIn("lote descartado no playback", out.getvalue())
        self.assertTrue(any(n.get("turn") == 42 and n.get("cut") is True
                            for n in obj.notes), obj.notes)


class PlaybackWriteTimeoutTest(unittest.TestCase):
    """Write que não retorna não pode deixar o alto-falante mudo para sempre."""

    def test_write_preso_reabre_a_saida_e_o_proximo_lote_toca(self):
        block = threading.Event()
        obj = _make_obj(chunks=[b"\x00\x00" * 2400])
        streams: list[_Stream] = []

        def factory(**_kw):
            st = _Stream(block=block if not streams else None)
            streams.append(st)
            return st

        fake = _FakeSD(factory)
        out = io.StringIO()

        async def _driver():
            with mock.patch.object(main, "sd", fake), \
                    mock.patch.object(main, "get_output_device", return_value=""), \
                    mock.patch.object(main.audio_devices, "resolve",
                                      return_value=None), \
                    mock.patch.object(main, "_PLAY_WRITE_TIMEOUT_S", 0.05):
                task = asyncio.create_task(main.JarvisLive._play_audio(obj))
                try:
                    for _ in range(100):        # a saída foi reaberta
                        if len(streams) >= 2:
                            break
                        await asyncio.sleep(0.01)
                    self.assertGreaterEqual(len(streams), 2,
                                            "a saída travada não foi reaberta")
                    await obj.audio_in_queue.put(b"\x00\x00" * 2400)
                    for _ in range(100):        # o novo stream recebeu áudio
                        if streams[1].writes:
                            break
                        await asyncio.sleep(0.01)
                    await asyncio.sleep(0.1)    # deixa o write bom terminar
                finally:
                    block.set()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        with contextlib.redirect_stdout(out):
            asyncio.run(_driver())

        self.assertTrue(streams[1].writes,
                        "o lote seguinte não foi para a saída reaberta")
        self.assertIn("write não retornou", out.getvalue())
        self.assertEqual(obj._play_stuck, 0,
                         "o contador de writes presos não zerou no write bom")


class GapBargeProofTest(unittest.TestCase):
    """Fragmento no vão entre frases não corta a resposta sem prova de voz."""

    def _obj(self, *, speaking: bool, reply_active: bool, proven: bool):
        obj = object.__new__(main.JarvisLive)
        obj._recent_spoken_text = ""
        obj._is_speaking = speaking
        obj._reply_active = reply_active
        obj._speech_gap_until = (time.monotonic() + 5.0
                                 if not speaking else 0.0)
        obj._qoder_ignore = False
        obj._qoder_live = None
        obj._mark_qoder_voice_preempt = lambda: None
        obj._log_heard = lambda _t: None
        obj._barge_fragment_proven = lambda _t, _p: proven
        calls: list[str] = []
        obj.interrupt = lambda: calls.append("interrupt")
        obj._on_groq_text = lambda text, **_kw: calls.append(text)
        obj.ui = _UI()
        return obj, calls

    def test_fragmento_curto_no_vao_sem_prova_nao_derruba_a_resposta(self):
        obj, calls = self._obj(speaking=False, reply_active=True,
                               proven=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main.JarvisLive._handle_barge_in(obj, "aham")
        self.assertEqual(calls, [])
        self.assertIn("fragmento curto sem dono", out.getvalue())

    def test_fragmento_curto_com_prova_de_voz_interrompe(self):
        obj, calls = self._obj(speaking=False, reply_active=True, proven=True)
        with contextlib.redirect_stdout(io.StringIO()):
            main.JarvisLive._handle_barge_in(obj, "aham")
        self.assertEqual(calls, ["interrupt", "aham"])

    def test_palavra_de_parada_no_vao_interrompe_sem_prova(self):
        obj, calls = self._obj(speaking=False, reply_active=True,
                               proven=False)
        with contextlib.redirect_stdout(io.StringIO()):
            main.JarvisLive._handle_barge_in(obj, "espera")
        self.assertEqual(calls, ["interrupt", "espera"])

    def test_frase_longa_no_vao_interrompe_sem_prova(self):
        obj, calls = self._obj(speaking=False, reply_active=True,
                               proven=False)
        phrase = "olha, o relatório de ontem ainda não ficou pronto"
        with contextlib.redirect_stdout(io.StringIO()):
            main.JarvisLive._handle_barge_in(obj, phrase)
        self.assertEqual(calls, ["interrupt", phrase])

    def test_turno_longo_sem_audio_recente_nao_exige_prova(self):
        # Um agente trabalhando por minutos deixa de ser "lacuna de síntese":
        # o comando curto do dono volta a ser um turno normal.
        obj, calls = self._obj(speaking=False, reply_active=True,
                               proven=False)
        obj._speech_gap_until = 0.0
        with contextlib.redirect_stdout(io.StringIO()):
            main.JarvisLive._handle_barge_in(obj, "aham")
        self.assertEqual(calls, ["interrupt", "aham"])

    def test_drain_marca_o_vao_de_sintese(self):
        obj = object.__new__(main.JarvisLive)
        obj._is_speaking = True
        obj._reply_active = True
        obj._last_play_batch = time.monotonic() - 1.2
        obj.audio_in_queue = queue.Queue()
        obj.set_speaking = lambda _v: None
        obj._speech_gap_until = 0.0
        main.JarvisLive._note_speech_gap(obj)
        main.JarvisLive._release_speaking_on_drain(obj)
        self.assertGreater(obj._speech_gap_until, time.monotonic())


if __name__ == "__main__":
    unittest.main()
