"""Resposta antiga não volta por cima de assunto novo (dono, 02/11/2026).

O dono reclamou: "eu acho que você está atrasado... está respondendo
perguntas que eu fiz lá atrás, depois do áudio que eu mandei agora". A perícia
achou o corte de barge-in reemitido como "Só para fechar o assunto anterior"
(651 s depois, sobre assunto novo) e a fala cortada no meio reemitida inteira.

Aqui se fixa o contrato:
  (a) turno preemptado com a pergunta já substituída não produz fala retida;
  (b) sobra de corte técnico só sai com fila VAZIA e citando o pedido original;
  (c) nenhuma fala do dono fica sem resposta nem sem item na fila.
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

import main


class _EmptyQueue:
    def empty(self):
        return True

    def put_nowait(self, _item):
        return None


class _BusyQueue:
    def empty(self):
        return False

    def put_nowait(self, _item):
        return None


class _Loop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class _UI:
    def __init__(self):
        self.logs = []

    def write_log(self, line):
        self.logs.append(str(line))

    def set_state(self, _state):
        return None


class _Jarvis:
    """Superfície mínima de JarvisLive usada pelos caminhos sob teste."""

    def __init__(self, queue=None):
        obj = object.__new__(main.JarvisLive)
        obj._asst_name = "Gino"
        obj._session_log = []
        obj._speech_epoch = 0
        obj._speech_audit = {}
        obj._speech_audit_lock = threading.RLock()
        obj._turn_ref = 7
        obj._turn_input_chan = "voz"
        obj._turn_owner_text = {}
        obj._turn_first_audio_ms = {}
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
        obj._brain_busy = False
        obj._turn_done_event = None
        obj.audio_in_queue = queue if queue is not None else _EmptyQueue()
        obj._loop = _Loop()
        obj.ui = _UI()
        obj._wait_for_user_quiet = lambda: True
        obj._user_is_speaking = lambda: False
        # Sentinela: impede que `_start_cut_note_watch_locked` abra a thread
        # real, que correria com as asserções. O vigia é chamado à mão.
        obj._speech_cut_watch = threading.current_thread()
        obj.spoken = []
        obj._say_now = lambda text, **kw: obj.spoken.append(text)
        self.obj = obj
        self.spoken = obj.spoken


def _age(obj, seconds=10.0):
    for note in obj._speech_cut_notes:
        note["at"] -= seconds


def _open_turn(obj, ref, question):
    obj._turn_ref = ref
    obj._note_turn_owner_text(question)


class ReplacedTurnProducesNoRetainedSpeechTest(unittest.TestCase):
    """(a) a resposta abandonada pela fala nova não é falada depois."""

    def test_barge_in_tail_is_discarded_not_reemitted(self):
        j = _Jarvis()
        obj = j.obj
        _open_turn(obj, 7, "não precisa auto-play")
        # A resposta estava no ar e o dono falou por cima: corte "meio".
        obj._queue_speech_cut_note("O autoplay da TV fica desligado.",
                                   turn=7, reason="meio")
        self.assertEqual(len(obj._speech_cut_notes), 1)

        # O dono abriu outro assunto: nova pergunta, novo turno.
        _open_turn(obj, 8, "e a tela, fica sempre acesa?")
        obj._flush_speech_cut_note()
        obj._flush_speech_cut_note_quiet()

        self.assertEqual(j.spoken, [],
                         "resto de resposta substituída nunca vira fala")
        self.assertEqual(getattr(obj, "_speech_cut_notes"), [])

    def test_cut_by_turn_substitution_is_dropped_even_without_new_question(self):
        # Botão INTERRUPT: o dono parou a resposta, sem fala nova do dono.
        j = _Jarvis()
        obj = j.obj
        _open_turn(obj, 7, "não precisa auto-play")
        obj._queue_speech_cut_note("O autoplay da TV fica desligado.",
                                   turn=7, reason="turno")
        obj._flush_speech_cut_note()
        obj._flush_speech_cut_note_quiet()
        self.assertEqual(j.spoken, [])
        self.assertEqual(getattr(obj, "_speech_cut_notes"), [])

    def test_half_sentence_is_not_reissued_whole(self):
        # Caso 20:52:17: a fala cortada no meio ("...a matrí") era reemitida
        # inteira depois de o dono já ter falado coisas novas.
        j = _Jarvis()
        obj = j.obj
        _open_turn(obj, 7, "como fica a matrícula por dispositivo?")
        obj._queue_speech_cut_note("A matrícula por dispositivo fica na",
                                   turn=7, reason="meio")
        _open_turn(obj, 8, "deixa, e o karaokê?")
        _age(obj)
        obj._flush_speech_cut_note()
        obj._flush_speech_cut_note_quiet()
        self.assertEqual(j.spoken, [])
        self.assertFalse(any("matrícula por dispositivo fica na" in line
                             for line in j.spoken))


class UndeliveredContentNeedsQuietAndCitationTest(unittest.TestCase):
    """(b) sobra técnica só sai com a fila vazia e citando o pedido."""

    def _prepared(self, queue=None):
        j = _Jarvis(queue=queue)
        obj = j.obj
        _open_turn(obj, 7, "a tela fica sempre acesa?")
        obj._queue_speech_cut_note("A tela fica sempre acesa.", turn=7,
                                   reason="tts")
        _age(obj)
        return j, obj

    def test_release_waits_for_an_empty_queue_and_the_owner_quiet(self):
        j, obj = self._prepared(queue=_BusyQueue())
        self.assertFalse(obj._cut_note_watch_step())
        self.assertEqual(j.spoken, [])
        self.assertEqual(len(obj._speech_cut_notes), 1)

        obj.audio_in_queue = _EmptyQueue()
        obj._brain_busy = True
        self.assertFalse(obj._cut_note_watch_step())
        self.assertEqual(j.spoken, [])

        obj._brain_busy = False
        self.assertFalse(obj._cut_note_watch_step())
        self.assertEqual(len(j.spoken), 1)
        self.assertIn("Sobre o que você me perguntou", j.spoken[0])
        self.assertIn("«a tela fica sempre acesa?", j.spoken[0])
        self.assertIn("A tela fica sempre acesa.", j.spoken[0])
        self.assertEqual(getattr(obj, "_speech_cut_notes"), [])
        # Liberada uma vez, não volta na próxima passada.
        self.assertTrue(obj._cut_note_watch_step())
        self.assertEqual(len(j.spoken), 1)

    def test_release_does_not_happen_when_a_newer_subject_exists(self):
        j, obj = self._prepared()
        _open_turn(obj, 8, "e o áudio do karaokê?")
        # A nota deixa de ser liberável: o vigia só informa que não sobra nada.
        self.assertTrue(obj._cut_note_watch_step())
        self.assertEqual(j.spoken, [])
        # A nota da pergunta substituída é encerrada, não liberada.
        added = []
        fake_queue = mock.Mock()
        fake_queue.add.side_effect = lambda *a, **k: added.append((a, k))
        with mock.patch.object(main, "_task_queue", fake_queue):
            obj._flush_speech_cut_note()
        self.assertEqual(getattr(obj, "_speech_cut_notes"), [])
        self.assertEqual(j.spoken, [])
        # Nada da resposta tinha tocado: a pergunta vai para a fila, com o
        # pedido original citado, em vez de virar fala sobre o assunto novo.
        self.assertEqual(len(added), 1)
        self.assertIn("a tela fica sempre acesa?", added[0][0][0])


class NoOwnerSpeechGoesUnansweredTest(unittest.TestCase):
    """(c) descartar fala sem responder a pergunta seria pior que o atraso."""

    def test_preempt_before_any_answer_enqueues_the_owner_question(self):
        j = _Jarvis()
        obj = j.obj
        _open_turn(obj, 7, "não precisa auto-play")
        obj._qoder_current_id = "t1"
        obj._qoder_turns["t1"] = "não precisa auto-play"
        obj._qoder_meta["t1"] = {"source": "owner", "turn_ref": 7,
                                 "task_id": ""}
        obj._speech_audit[7] = [{"generated": False, "origin": "qoder",
                                 "cut": True}]
        obj._qoder_finish = lambda *a, **k: None
        added = []
        fake_queue = mock.Mock()
        fake_queue.add.side_effect = lambda *a, **k: added.append((a, k))
        with mock.patch.object(main, "_task_queue", fake_queue):
            main.JarvisLive._qoder_preempt(obj, "t1")

        self.assertEqual(j.spoken, [], "nada de fala atrasada")
        self.assertEqual(len(added), 1, "a pergunta não pode morrer em silêncio")
        title = added[0][0][0]
        self.assertIn("não precisa auto-play", title)
        self.assertEqual(added[0][1].get("payload", {}).get("origin"),
                         "preempt-carryover")
        # O item é o mesmo: um segundo preempt não duplica a fila.
        with mock.patch.object(main, "_task_queue", fake_queue):
            main.JarvisLive._qoder_preempt(obj, "t1")
        self.assertEqual(len(added), 1)

    def test_discarding_a_cut_note_enqueues_when_nothing_was_heard(self):
        j = _Jarvis()
        obj = j.obj
        _open_turn(obj, 7, "a tela fica sempre acesa?")
        obj._speech_audit[7] = [{"generated": False, "origin": "qoder",
                                 "cut": True}]
        obj._queue_speech_cut_note("A tela fica sempre acesa.", turn=7,
                                   reason="meio")
        _open_turn(obj, 8, "deixa isso, e o karaokê?")
        added = []
        fake_queue = mock.Mock()
        fake_queue.add.side_effect = lambda *a, **k: added.append((a, k))
        with mock.patch.object(main, "_task_queue", fake_queue):
            obj._flush_speech_cut_note()

        self.assertEqual(j.spoken, [])
        self.assertEqual(len(added), 1)
        self.assertIn("a tela fica sempre acesa?", added[0][0][0])

    def test_discarding_after_a_partial_answer_adds_nothing_to_the_queue(self):
        j = _Jarvis()
        obj = j.obj
        _open_turn(obj, 7, "a tela fica sempre acesa?")
        obj._speech_audit[7] = [{"generated": True, "origin": "qoder"}]
        obj._queue_speech_cut_note("O brilho também sobe sozinho.", turn=7,
                                   reason="meio")
        _open_turn(obj, 8, "deixa isso, e o karaokê?")
        added = []
        fake_queue = mock.Mock()
        fake_queue.add.side_effect = lambda *a, **k: added.append((a, k))
        with mock.patch.object(main, "_task_queue", fake_queue):
            obj._flush_speech_cut_note()

        self.assertEqual(j.spoken, [])
        self.assertEqual(added, [],
                         "a pergunta já teve resposta ouvida; nada a reenfileirar")

    def test_owner_question_is_what_gets_cited(self):
        j = _Jarvis()
        obj = j.obj
        _open_turn(obj, 7, "não precisa auto-play")
        obj._queue_speech_cut_note("O autoplay fica desligado.", turn=7,
                                   reason="tts")
        _age(obj)
        obj._cut_note_watch_step()
        self.assertEqual(len(j.spoken), 1)
        self.assertIn("não precisa auto-play", j.spoken[0])
        self.assertNotIn("Só para fechar o assunto anterior", j.spoken[0])

    def test_repeating_the_same_question_does_not_duplicate_the_queue(self):
        j = _Jarvis()
        obj = j.obj
        _open_turn(obj, 7, "não precisa auto-play")
        obj._qoder_current_id = "t1"
        obj._qoder_turns["t1"] = "não precisa auto-play"
        obj._qoder_meta["t1"] = {"source": "owner", "turn_ref": 7,
                                 "task_id": ""}
        obj._speech_audit[7] = [{"generated": False, "origin": "qoder"}]
        obj._qoder_finish = lambda *a, **k: None
        # O dono repetiu a mesma pergunta: o turno novo é quem responde.
        _open_turn(obj, 8, "não precisa auto-play")
        added = []
        fake_queue = mock.Mock()
        fake_queue.add.side_effect = lambda *a, **k: added.append((a, k))
        with mock.patch.object(main, "_task_queue", fake_queue):
            main.JarvisLive._qoder_preempt(obj, "t1")
        self.assertEqual(added, [])
        self.assertEqual(j.spoken, [])


class LateDoneOfReplacedTurnTest(unittest.TestCase):
    """O `done` que chega depois não fala a resposta do turno substituído."""

    def test_qoder_say_drops_a_turn_that_is_no_longer_registered(self):
        j = _Jarvis()
        obj = j.obj
        obj._qoder_turns = {"t2": "assunto novo"}
        obj._qoder_current_id = "t2"
        main.JarvisLive._qoder_say(obj, "t1", "Resposta antiga completa.")
        self.assertEqual(j.spoken, [])
        self.assertNotIn("t1", obj._qoder_spoken)

    def test_on_done_of_a_replaced_room_turn_never_speaks(self):
        j = _Jarvis()
        obj = j.obj
        obj._qoder_turns = {}
        obj._qoder_meta = {}
        obj._qoder_current_id = "t2"
        main.JarvisLive._qoder_on_done(obj, "t1", "Resposta antiga completa.")
        self.assertEqual(j.spoken, [])


if __name__ == "__main__":
    unittest.main()
