"""O dono nunca é cortado: a fala dele para o assistente e vira turno.

Reproduz a cadeia que engolia a fala do dono enquanto o TTS tocava:

1. `_play_audio` chama `set_speaking(True)` a cada lote (~200 ms) e o
   listener re-armava `_drop_segment` em TODA chamada — o segmento aberto
   era apagado antes de virar utterance, então o barge-in por voz nunca
   disparava.
2. Mesmo quando o texto chegava, `_handle_barge_in` guardava frases fora da
   lista de "parar/perguntar" como complemento: a fala dele era registrada
   e descartada enquanto a resposta continuava por cima.

Agora o VAD + echo guard derrubam a resposta na hora e qualquer utterance
transcrita vira um turno normal.
"""
from __future__ import annotations

import threading
import time
import unittest

import numpy as np

import main
from core import groq_stt
from tests.audio_fixtures import voice_block


class _FakeEcho:
    def __init__(self, user: bool):
        self.user = user

    def is_user_speech(self, _pcm, _sr, _level):
        return self.user


class _FakeVad:
    def __init__(self, speech: bool):
        self.speech = speech

    def is_speech(self, _pcm):
        return self.speech


class _FakeUI:
    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, text):
        self.logs.append(text)


class _FakeQoder:
    def __init__(self):
        self.interrupted = False

    def interrupt(self):
        self.interrupted = True


class RepeatedPlaybackBatchesTest(unittest.TestCase):
    """O loop de playback chama set_assistant_speaking(True) por lote."""

    def setUp(self):
        self._orig_up = groq_stt._duck.up
        self._orig_down = groq_stt._duck.down
        groq_stt._duck.up = lambda: None
        groq_stt._duck.down = lambda: None
        self.listener = groq_stt.GroqListener(
            "", "test", "pt",
            on_text=lambda _t: None,
            on_utterance=lambda _t, _p: None,
            on_status=lambda _m: None,
            sample_rate=16000,
            transcribe_fn=lambda _pcm: "",
        )
        self.block = voice_block(9000)

    def tearDown(self):
        self.listener.stop()
        groq_stt._duck.up = self._orig_up
        groq_stt._duck.down = self._orig_down

    def _open(self):
        for _ in range(groq_stt._START_BLOCKS):
            self.listener.feed(self.block)
        self.assertTrue(self.listener.speaking, "o segmento não abriu")

    def test_repeated_reply_batches_do_not_wipe_the_owner_segment(self):
        self._open()
        self.listener.set_assistant_speaking(True)   # a resposta começou
        self.listener.feed(self.block)               # descarta o segmento velho
        self._open()                                 # o dono começa a falar
        for _ in range(10):                          # lotes do playback
            self.listener.set_assistant_speaking(True)
            self.listener.feed(self.block)
            self.assertTrue(
                self.listener.speaking,
                "lote de playback apagou a fala do dono em andamento")

    def test_leaving_the_reply_state_allows_a_new_utterance(self):
        self.listener.set_assistant_speaking(True)
        self.listener.set_assistant_speaking(False)
        self._open()


class OwnerFloorTest(unittest.TestCase):
    """O piso do dono: VAD de voz humana, nunca o retorno do nosso TTS."""

    def _obj(self, *, echo_user: bool, vad_speech: bool,
             barge_in: bool = True, cancel_gate: bool = False):
        obj = object.__new__(main.JarvisLive)
        obj._voice_barge_in = barge_in
        obj._echo = _FakeEcho(echo_user)
        obj._barge_vad = _FakeVad(vad_speech)
        obj._cancel = (type("C", (), {"last_gate": cancel_gate})()
                       if cancel_gate else None)
        obj._tail_until = 0.0
        obj._owner_voice_until = 0.0
        return obj

    def test_echo_alone_is_not_the_owner(self):
        # Enquanto a resposta toca, o VAD ouve o próprio TTS — o echo guard
        # é quem diz que aquilo é nosso, e o piso não sobe.
        obj = self._obj(echo_user=False, vad_speech=True)
        block = np.full(1024, 9000, dtype=np.int16)
        self.assertFalse(main.JarvisLive._owner_voice_block(obj, block, True))

    def test_human_voice_over_the_reply_takes_the_floor(self):
        obj = self._obj(echo_user=True, vad_speech=True)
        block = np.full(1024, 9000, dtype=np.int16)
        self.assertTrue(main.JarvisLive._owner_voice_block(obj, block, True))

    def test_machine_only_block_does_not_take_the_floor_while_listening(self):
        obj = self._obj(echo_user=False, vad_speech=True, cancel_gate=True)
        block = np.full(1024, 9000, dtype=np.int16)
        self.assertFalse(main.JarvisLive._owner_voice_block(obj, block, False))

    def test_barge_in_disabled_never_takes_the_floor(self):
        obj = self._obj(echo_user=True, vad_speech=True, barge_in=False)
        block = np.full(1024, 9000, dtype=np.int16)
        self.assertFalse(main.JarvisLive._owner_voice_block(obj, block, True))

    def test_floor_holds_playback_and_marks_the_owner_speaking(self):
        obj = self._obj(echo_user=True, vad_speech=True)
        obj._last_user_speech = 0.0
        obj._groq = None
        self.assertFalse(main.JarvisLive._owner_has_floor(obj))
        obj._owner_voice_until = time.monotonic() + 5.0
        self.assertTrue(main.JarvisLive._owner_has_floor(obj))
        self.assertTrue(main.JarvisLive._user_is_speaking(obj))

    def test_room_energy_alone_does_not_hold_the_assistant(self):
        # Chuva abre o segmento por energia; com o VAD carregado e sem voz
        # humana no piso, a linha do assistente não fica muda por 30 s.
        obj = self._obj(echo_user=True, vad_speech=True)
        obj._last_user_speech = 0.0
        obj._stt_has_open_segment = lambda: True
        self.assertFalse(main.JarvisLive._user_is_speaking(obj))

    def test_without_vad_the_open_segment_still_holds(self):
        # Sem o modelo de voz, o segmento de energia é a única prova: manter
        # a espera antiga é melhor do que falar por cima do dono.
        obj = self._obj(echo_user=True, vad_speech=True)
        obj._barge_vad = None
        obj._last_user_speech = 0.0
        obj._stt_has_open_segment = lambda: True
        self.assertTrue(main.JarvisLive._user_is_speaking(obj))

    def test_just_accepted_transcript_still_holds(self):
        obj = self._obj(echo_user=True, vad_speech=True)
        obj._last_user_speech = time.monotonic()
        obj._stt_has_open_segment = lambda: True
        self.assertTrue(main.JarvisLive._user_is_speaking(obj))


class OwnerTakesFloorTest(unittest.TestCase):
    def test_owner_floor_stops_the_reply(self):
        calls: list[str] = []
        obj = object.__new__(main.JarvisLive)
        obj._speaking_lock = threading.Lock()
        obj._is_speaking = True
        obj._qoder_ignore = False
        obj._qoder_live = _FakeQoder()
        obj.ui = _FakeUI()
        obj.interrupt = lambda: calls.append("interrupt")
        obj._owner_voice_until = time.monotonic() + 5.0
        main.JarvisLive._owner_takes_floor(obj)
        self.assertTrue(obj._qoder_ignore)
        self.assertTrue(obj._qoder_live.interrupted)
        self.assertEqual(calls, ["interrupt"])

    def test_silent_assistant_is_left_alone(self):
        calls: list[str] = []
        obj = object.__new__(main.JarvisLive)
        obj._speaking_lock = threading.Lock()
        obj._is_speaking = False
        obj._qoder_ignore = False
        obj._qoder_live = _FakeQoder()
        obj.ui = _FakeUI()
        obj.interrupt = lambda: calls.append("interrupt")
        main.JarvisLive._owner_takes_floor(obj)
        self.assertFalse(obj._qoder_ignore)
        self.assertFalse(obj._qoder_live.interrupted)
        self.assertEqual(calls, [])


class OwnerInterruptClassificationTest(unittest.TestCase):
    """Toda utterance do dono vira turno — nada de "complemento" engolido."""

    def _obj(self, own_voice: bool = False):
        calls: list = []
        obj = object.__new__(main.JarvisLive)
        obj.ui = _FakeUI()
        obj._looks_like_own_voice = lambda _t: own_voice
        obj._log_heard = lambda t: calls.append(("heard", t))
        obj._qoder_ignore = False
        obj._qoder_live = _FakeQoder()
        obj.interrupt = lambda: calls.append("interrupt")
        obj._on_groq_text = lambda t, _heard=False: calls.append(("turn", t))
        return obj, calls

    def test_complement_phrase_now_stops_and_runs_as_a_turn(self):
        obj, calls = self._obj()
        phrase = "e também aquele negócio do relatório de ontem"
        main.JarvisLive._handle_barge_in(obj, phrase)
        self.assertIn("interrupt", calls)
        self.assertIn(("turn", phrase), calls)
        self.assertTrue(obj._qoder_ignore)
        self.assertTrue(obj._qoder_live.interrupted)

    def test_own_voice_echo_does_not_interrupt(self):
        obj, calls = self._obj(own_voice=True)
        main.JarvisLive._handle_barge_in(obj, "e também aquele negócio")
        self.assertEqual(calls, [])


class TrueBargeInTest(unittest.TestCase):
    """O onset do dono mata o áudio que já está no dispositivo e a fila.

    A fila vazia não basta: `stream.write()` só prova que o buffer aceitou o
    lote, e o dispositivo ainda pode ter dezenas/centenas de ms para tocar.
    `interrupt()` agora aborta o stream (`abort()`), e `_play_audio` reinicia
    o dispositivo para a próxima resposta.
    """

    class _Stream:
        def __init__(self):
            self.aborts = 0
            self.starts = 0
            self.active = True

        def abort(self):
            self.aborts += 1
            self.active = False

        def start(self):
            self.starts += 1
            self.active = True

    class _Queue:
        def __init__(self, items):
            self.items = list(items)

        def empty(self):
            return not self.items

        def get_nowait(self):
            if not self.items:
                raise IndexError("empty")
            return self.items.pop(0)

    def _obj(self, *, speaking: bool = True, chunks: int = 3):
        obj = object.__new__(main.JarvisLive)
        obj.ui = _FakeUI()
        obj._speaking_lock = threading.Lock()
        obj._is_speaking = speaking
        obj._speech_epoch = 7
        obj._reply_active = True
        obj._interrupted = False
        obj._qoder_live = None
        obj._qoder_lock = threading.RLock()
        obj._persona_text_buf = {}
        obj._persona_seen = set()
        obj._qoder_current_id = None
        obj._qoder_meta = {}
        obj._visemes = type("V", (), {"reset": lambda self: None})()
        obj._play_cursor = 1.0
        obj._turn_done_event = None
        obj._interrupt_note = ""
        obj._mark_qoder_voice_preempt = lambda: None
        obj.audio_in_queue = self._Queue([b"chunk"] * chunks)
        obj._out_stream = self._Stream()
        obj._play_abort = threading.Event()
        obj.set_speaking = lambda value: setattr(obj, "_is_speaking", value)
        obj.interrupt = lambda: main.JarvisLive.interrupt(obj)
        return obj

    def test_interrupt_aborts_the_device_and_drops_pending_chunks(self):
        obj = self._obj(chunks=4)
        stream = obj._out_stream
        main.JarvisLive.interrupt(obj)
        self.assertEqual(stream.aborts, 1, "o buffer do dispositivo não foi abortado")
        self.assertEqual(obj.audio_in_queue.items, [],
                         "sobraram chunks da resposta interrompida na fila")
        self.assertEqual(obj._speech_epoch, 8)
        self.assertFalse(obj._is_speaking)
        self.assertTrue(obj._play_abort.is_set(),
                        "o loop de playback não foi avisado para reiniciar")

    def test_owner_onset_during_playback_stops_and_logs_barge_in(self):
        obj = self._obj(chunks=2)
        obj._owner_voice_until = time.monotonic() + 5.0
        obj._owner_voice_since = time.monotonic() - 0.8
        main.JarvisLive._owner_takes_floor(obj)
        self.assertEqual(obj._out_stream.aborts, 1)
        self.assertEqual(obj.audio_in_queue.items, [])
        barge = [line for line in obj.ui.logs if line.startswith("SYS: barge-in:")]
        self.assertEqual(len(barge), 1, obj.ui.logs)
        self.assertIn("falou 0.8s no áudio", barge[0])
        self.assertNotIn("SYS: Interrompido — ouvindo...", obj.ui.logs,
                         "o barge-in deve gravar uma linha só")

    def test_no_false_trigger_when_nothing_is_playing(self):
        obj = self._obj(speaking=False, chunks=2)
        main.JarvisLive._owner_takes_floor(obj)
        self.assertEqual(obj._out_stream.aborts, 0)
        self.assertEqual(obj.audio_in_queue.items, [b"chunk", b"chunk"])
        self.assertEqual(obj.ui.logs, [])
        self.assertFalse(obj._play_abort.is_set())

    def test_a_fresh_burst_does_not_confirm_a_barge_in_by_itself(self):
        obj = self._obj()
        obj._play_burst_until = time.monotonic() + 0.25
        self.assertFalse(main.JarvisLive._barge_onset_counts(obj, True),
                         "o ataque do nosso próprio TTS abriu um barge-in")
        # Sem playback não há eco nosso para confundir: o bloco conta.
        self.assertTrue(main.JarvisLive._barge_onset_counts(obj, False))
        obj._play_burst_until = time.monotonic() - 1.0
        self.assertTrue(main.JarvisLive._barge_onset_counts(obj, True))

    def test_playback_loop_restarts_the_device_after_the_abort(self):
        obj = self._obj()
        stream = obj._out_stream
        obj._play_abort.set()
        stream.active = False
        main.JarvisLive._restart_output_after_abort(obj, stream)
        self.assertEqual(stream.starts, 1)
        self.assertTrue(stream.active)
        self.assertFalse(obj._play_abort.is_set())

    def test_silence_without_playback_is_safe(self):
        obj = self._obj()
        obj._out_stream = None
        obj._play_abort.clear()
        main.JarvisLive._silence_output_now(obj)
        self.assertTrue(obj._play_abort.is_set())


if __name__ == "__main__":
    unittest.main()
