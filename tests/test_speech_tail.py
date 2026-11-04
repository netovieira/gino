"""Sobra de fala interrompida: não se perde quando o abort não tem dono.

O dono reclamou (03/11/2026): o log registrava o texto inteiro da resposta mas
o alto-falante parava nos primeiros segundos — o barge-in derrubava o áudio e
o resto da frase era descartado. A política de cauda separa os dois casos:

  (a) abort legítimo (turno novo, fala transcrita, INTERRUPT, ordem local):
      a sobra morre de propósito, como sempre;
  (b) abort sem turno nem fala identificada (barge-in provisório que nunca
      virou transcrição, falha de playback): o resto volta quando o canal
      libera, do ponto exato onde parou;
  (c) fala nova de outro turno substitui a pendente: a sobra antiga morre em
      silêncio, com rastro debug no terminal;
  (d) nada que já saiu é repetido: a cauda guarda só o PCM ainda não
      enfileirado.
"""

from __future__ import annotations

import contextlib
import io
import queue
import threading
import time
import unittest
from unittest import mock

import main

TURN = 7
CHUNK = 2400


class _UI:
    def __init__(self):
        self.logs = []

    def write_log(self, line):
        self.logs.append(str(line))

    def holo_transcript(self, _text):
        pass


class _Queue:
    def __init__(self, obj=None, abort_after=0):
        self.items = []
        self.obj = obj
        self.abort_after = abort_after
        self.puts = 0

    def put_nowait(self, item):
        self.items.append(item)
        self.puts += 1
        if self.obj is not None and self.puts == self.abort_after:
            self.obj._speech_epoch += 1

    def get_nowait(self):
        if not self.items:
            raise queue.Empty
        return self.items.pop(0)

    def empty(self):
        return not self.items


class _Loop:
    def __init__(self, queue):
        self.queue = queue

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


def _make_obj(*, epoch=0, abort_after=0):
    obj = object.__new__(main.JarvisLive)
    obj.ui = _UI()
    obj._speech_epoch = epoch
    obj._speech_epoch_turn = {}
    obj._speech_aborts = {}
    obj._speech_tails = {}
    obj._speech_tail_pending = None
    obj._speech_tail_blocked = {}
    obj._speech_tail_seq = 0
    obj._turn_ref = TURN
    obj._turn_seq = TURN
    obj._turn_input_chan = "voz"
    obj._turn_owner_text = {}
    obj._turn_first_audio_ms = {}
    obj._turn_t0 = 0.0
    obj._session_log = []
    obj._recent_spoken_text = ""
    obj._asst_name = "Gino"
    obj._speech_audit = {}
    obj._speech_audit_lock = threading.RLock()
    obj._speech_cut_notes = []
    obj._qoder_lock = threading.RLock()
    obj._qoder_current_id = None
    obj._qoder_turn_backlog = []
    obj._qoder_meta = {}
    obj._qoder_text_buf = {}
    obj._qoder_spoken = {}
    obj._qoder_turns = {}
    obj._qoder_voice_preempts = {}
    obj._qoder_ignore = False
    obj._is_speaking = False
    obj._speaking_lock = threading.Lock()
    obj._brain_busy = False
    obj._turn_done_event = threading.Event()
    obj._last_user_speech = 0.0
    obj._awake = True
    obj._wait_for_user_quiet = lambda: True
    obj._user_is_speaking = lambda: False
    # Sentinela: impede que a cauda abra o vigia real durante o teste.
    obj._speech_cut_watch = threading.current_thread()
    obj.audio_in_queue = _Queue(obj, abort_after=abort_after)
    obj._loop = _Loop(obj.audio_in_queue)
    obj._played = []
    return obj


def _abort(obj, kind, epoch=0):
    obj._speech_aborts[epoch] = {"kind": kind, "turn": TURN, "at": time.monotonic()}


def _stub_interrupt(obj):
    """Peças do interrupt real que não têm nada a ver com a cauda."""
    obj._visemes = type("V", (), {"reset": lambda self: None})()
    obj._play_cursor = 0.0
    obj._out_stream = None
    obj._play_abort = threading.Event()
    obj._mark_qoder_voice_preempt = lambda: None
    obj._qoder_live = None
    obj.set_speaking = lambda value: setattr(obj, "_is_speaking", value)


def _speak_cut(obj, pcm, text="frase longa da resposta", epoch=0):
    """Fala uma frase cujo epoch é derrubado no meio do enfileiramento."""
    with (
        mock.patch("core.brain.settings", return_value={}),
        mock.patch("core.brain.synth_pcm24k", return_value=pcm),
    ):
        obj._say_blocking(text, log=False, epoch=epoch, origin="qoder", chan="voz")


class TailRescheduledTest(unittest.TestCase):
    """(b) e (d): abort sem dono devolve a sobra do ponto exato."""

    def test_barge_in_provisorio_retoma_o_resto_do_pcm(self):
        pcm = b"\x00\x00" * (CHUNK // 2) * 4  # 4 blocos de 50 ms
        obj = _make_obj(abort_after=2)
        _abort(obj, "barge-in")
        _speak_cut(obj, pcm)
        tail = obj._speech_tail_pending
        self.assertIsInstance(tail, dict, "a sobra não foi guardada")
        # (d) só o que ainda não foi para a fila entra na cauda.
        self.assertEqual(tail["audio_bytes"], len(pcm) - 2 * CHUNK)
        self.assertEqual(b"".join(tail["audio"]), pcm[2 * CHUNK :])

        # O interrupt real esvazia a fila; aqui simulamos o mesmo.
        obj.audio_in_queue.items.clear()
        tail["at"] -= main._SPEECH_TAIL_GRACE_S + 1.0
        self.assertTrue(obj._speech_tail_step())
        self.assertIsNone(obj._speech_tail_pending)
        self.assertEqual(
            obj.audio_in_queue.items,
            [pcm[2 * CHUNK :]],
            "a retomada repetiu o trecho que já tinha saído",
        )

    def test_frases_do_epoch_caido_entram_na_cauda_e_voltam(self):
        pcm = b"\x00\x00" * (CHUNK // 2) * 2
        obj = _make_obj(abort_after=2)
        _abort(obj, "barge-in")
        _speak_cut(obj, pcm)
        obj._speech_tail_retain_text(
            "A resposta continua aqui.", turn=TURN, epoch=0, log=False, chan="voz", origin="qoder"
        )
        said = []
        obj._say_now = lambda text, **kw: said.append((text, kw))
        obj.audio_in_queue.items.clear()
        obj._speech_tail_pending["at"] -= main._SPEECH_TAIL_GRACE_S + 1.0
        self.assertTrue(obj._speech_tail_step())
        self.assertEqual(len(said), 1, said)
        self.assertEqual(said[0][0], "A resposta continua aqui.")
        self.assertEqual(said[0][1].get("origin"), "cauda")

    def test_falha_tecnica_retoma_sem_esperar_janela(self):
        pcm = b"\x00\x00" * (CHUNK // 2) * 4
        obj = _make_obj(abort_after=1)
        _abort(obj, "tecnico")
        _speak_cut(obj, pcm)
        obj.audio_in_queue.items.clear()
        # Sem a janela do barge-in: falha de playback não espera transcrição.
        self.assertTrue(obj._speech_tail_step())
        self.assertEqual(obj.audio_in_queue.items, [pcm[CHUNK:]])


class InterruptClassificationTest(unittest.TestCase):
    """O motivo do abort decide o destino da sobra — e fica registrado."""

    def test_interrupt_do_barge_in_e_provisorio(self):
        obj = _make_obj()
        obj._interrupt_note = "SYS: barge-in: TTS interrompido pelo dono"
        self.assertEqual(obj._interrupt_kind(""), "barge-in")

    def test_interrupt_explicito_e_tecnico(self):
        obj = _make_obj()
        obj._interrupt_note = ""
        self.assertEqual(obj._interrupt_kind(""), "explicito")
        self.assertEqual(obj._interrupt_kind("tecnico"), "tecnico")
        self.assertEqual(obj._interrupt_kind("fala"), "explicito")

    def test_interrupt_grava_o_motivo_do_epoch_antigo(self):
        obj = _make_obj()
        _stub_interrupt(obj)
        obj._interrupt_note = "SYS: barge-in: TTS interrompido pelo dono"
        main.JarvisLive.interrupt(obj)
        self.assertEqual(obj._speech_epoch, 1)
        self.assertEqual(obj._speech_aborts[0]["kind"], "barge-in")
        self.assertEqual(obj._speech_aborts[0]["turn"], TURN)


class InterruptQueueTailTest(unittest.TestCase):
    """O esvaziamento da fila não pode ser a perda que o dono ouviu."""

    def test_barge_in_guarda_os_blocos_da_fila(self):
        obj = _make_obj()
        _stub_interrupt(obj)
        seed = [b"A" * CHUNK, b"B" * CHUNK]
        obj.audio_in_queue.items.extend(seed)
        obj._interrupt_note = "SYS: barge-in: TTS interrompido pelo dono"
        main.JarvisLive.interrupt(obj)
        self.assertEqual(obj.audio_in_queue.items, [])
        tail = obj._speech_tail_pending
        self.assertIsInstance(tail, dict, "os blocos da fila foram perdidos")
        self.assertEqual(b"".join(tail["audio"]), b"".join(seed))

    def test_interrupt_explicito_nao_guarda_a_fila(self):
        obj = _make_obj()
        _stub_interrupt(obj)
        obj.audio_in_queue.items.extend([b"A" * CHUNK, b"B" * CHUNK])
        obj._interrupt_note = ""
        main.JarvisLive.interrupt(obj)
        self.assertEqual(obj.audio_in_queue.items, [])
        self.assertIsNone(obj._speech_tail_pending)

    def test_fila_e_resto_da_frase_retomam_na_ordem_sem_repetir(self):
        pcm = b"\x00\x00" * (CHUNK // 2) * 4
        obj = _make_obj()
        _stub_interrupt(obj)
        seed = [b"A" * CHUNK, b"B" * CHUNK]
        obj.audio_in_queue.items.extend(seed)
        obj._interrupt_note = "SYS: barge-in: TTS interrompido pelo dono"
        main.JarvisLive.interrupt(obj)
        # O say-worker da frase em curso encontra o epoch antigo e guarda o
        # resto DEPOIS dos blocos que já estavam na fila.
        _speak_cut(obj, pcm)
        tail = obj._speech_tail_pending
        self.assertEqual(
            b"".join(tail["audio"]),
            b"".join(seed) + pcm,
            "a ordem fila→resto da frase foi quebrada",
        )
        said = []
        obj._say_now = lambda text, **kw: said.append((text, kw))
        tail["at"] -= main._SPEECH_TAIL_GRACE_S + 1.0
        self.assertTrue(obj._speech_tail_step())
        self.assertEqual(
            b"".join(obj.audio_in_queue.items),
            b"".join(seed) + pcm,
            "a retomada repetiu ou reordenou o que já tinha saído",
        )


class TailDiscardedTest(unittest.TestCase):
    """(a) e (c): abort com dono não retoma; fala nova mata a pendente."""

    def test_abort_legitimo_nao_cria_cauda(self):
        pcm = b"\x00\x00" * (CHUNK // 2) * 4
        obj = _make_obj(abort_after=2)
        _abort(obj, "explicito")
        _speak_cut(obj, pcm)
        self.assertIsNone(obj._speech_tail_pending)
        # O laço parou no ponto do abort: nada depois dele foi enfileirado.
        self.assertEqual(len(obj.audio_in_queue.items), 2)

    def test_turno_novo_mata_a_cauda_pendente(self):
        pcm = b"\x00\x00" * (CHUNK // 2) * 4
        obj = _make_obj(abort_after=2)
        _abort(obj, "barge-in")
        _speak_cut(obj, pcm)
        self.assertIsNotNone(obj._speech_tail_pending)
        obj.audio_in_queue.items.clear()
        obj._turn_ref = TURN + 1  # o dono abriu outro assunto
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(obj._speech_tail_step())
        self.assertIsNone(obj._speech_tail_pending)
        self.assertEqual(
            obj.audio_in_queue.items, [], "a sobra antiga voltou por cima do assunto novo"
        )
        self.assertIn("[Say][debug]", out.getvalue())
        self.assertIn("novo turno do dono", out.getvalue())

    def test_fala_nova_substitui_e_loga_em_debug(self):
        pcm = b"\x00\x00" * (CHUNK // 2) * 4
        obj = _make_obj(abort_after=2)
        _abort(obj, "barge-in")
        _speak_cut(obj, pcm)
        self.assertIsNotNone(obj._speech_tail_pending)
        obj._say_thread = threading.current_thread()  # não abre worker real
        obj._say_q = queue.Queue()
        obj._turn_ref = TURN + 1
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            obj._say_now("nova fala do dono", log=False, origin="qoder")
        self.assertIsNone(obj._speech_tail_pending)
        log = out.getvalue()
        self.assertIn("[Say][debug]", log)
        self.assertIn("descartada em silêncio", log)
        self.assertIn("nova fala do turno", log)

    def test_novo_turno_pelo_ref_mata_a_cauda(self):
        pcm = b"\x00\x00" * (CHUNK // 2) * 4
        obj = _make_obj(abort_after=2)
        _abort(obj, "barge-in")
        _speak_cut(obj, pcm)
        self.assertIsNotNone(obj._speech_tail_pending)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            obj._next_turn_ref("voz")
        self.assertIsNone(obj._speech_tail_pending)
        self.assertIn("descartada em silêncio", out.getvalue())

    def test_cauda_descartada_nao_ressuscita_com_item_velho(self):
        obj = _make_obj()
        _abort(obj, "barge-in")
        obj._speech_tail_retain_text("parte 1", turn=TURN, epoch=0, log=False, origin="qoder")
        obj._turn_ref = TURN + 1
        self.assertTrue(obj._speech_tail_step())  # descarta pelo novo turno
        obj._speech_tail_retain_text("parte 2", turn=TURN, epoch=0, log=False, origin="qoder")
        self.assertIsNone(obj._speech_tail_pending)

    def test_fala_do_dono_depois_do_corte_impede_nova_cauda(self):
        obj = _make_obj()
        _abort(obj, "barge-in")
        obj._speech_aborts[0]["at"] -= 1.0
        obj._last_user_speech = time.monotonic()
        obj._speech_tail_retain_text(
            "resto da resposta", turn=TURN, epoch=0, log=False, origin="qoder"
        )
        self.assertIsNone(obj._speech_tail_pending)


if __name__ == "__main__":
    unittest.main()
