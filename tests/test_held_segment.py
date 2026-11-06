"""Regra do dono (02/11/2026): fala registrada nunca é jogada fora.

O caso real: o microfone fecha na janela de mute (barulho do vídeo, chamada,
mute manual) enquanto ele FALA, e o segmento aberto era "descartado" sem
transcrição — foi isso que virou a reclamação de 21:00 ("não registrou nada").

Agora o áudio do segmento é RETIDO (`hold_open`) e volta ao STT na primeira
reabertura do microfone, juntado à continuação da mesma frase pelo agregador de
endpointing que já existe; se não houver como transcrever (curto demais,
silêncio, o STT não reconheceu), main.py registra "fala não aproveitada" no
activity.log e pede a repetição em uma frase curta. Silêncio é falha.

Estes testes fixam:
  (a) segmento aberto no instante do mute é retido e transcrito na reabertura,
      uma única vez (sem resposta dupla e sem fala duplicada);
  (b) segmento irrecuperável vira item (activity.log) + notificação curta;
  (c) a voz do próprio assistente não vira fala do dono.
"""
from __future__ import annotations

import threading
import time
import unittest

import numpy as np

import main
from core import groq_stt
from tests.audio_fixtures import voice_block

_SR = 16000
_FRAME = 1024          # CHUNK_SIZE de main.py: 64 ms a 16 kHz
_SILENCE = np.zeros(_FRAME, dtype=np.int16)


def _tone(amplitude: int = 4800) -> np.ndarray:
    """Fala sintética: passa no portão de voz (um senoide puro não passa)."""
    return voice_block(amplitude)


class _Harness(unittest.TestCase):
    def setUp(self):
        self._orig_up = groq_stt._duck.up
        self._orig_down = groq_stt._duck.down
        groq_stt._duck.up = lambda: None
        groq_stt._duck.down = lambda: None
        self._lock = threading.Lock()
        self.got: list[bytes] = []
        self.lost: list[tuple[str, str, float]] = []
        self.texts: list[str] = []
        self._listeners = []

    def tearDown(self):
        for listener in self._listeners:
            listener.stop()
        groq_stt._duck.up = self._orig_up
        groq_stt._duck.down = self._orig_down

    def _listener(self, transcript: str = "texto"):
        def _transcribe(pcm: bytes) -> str:
            with self._lock:
                self.got.append(pcm)
            return transcript

        def _lost(kind, reason, seconds):
            with self._lock:
                self.lost.append((kind, reason, seconds))

        listener = groq_stt.GroqListener(
            "", "test", "pt",
            on_text=self.texts.append,
            on_utterance=lambda t, _p: self.texts.append(t),
            on_status=lambda _m: None,
            on_lost=_lost,
            sample_rate=_SR,
            transcribe_fn=_transcribe,
            use_vad=False,  # tests use synthetic audio, not real speech
        )
        # Endpointing é um modelo à parte: aqui o corte fixo de silêncio basta.
        listener._endpoint_complete = lambda _pcm: True
        self._listeners.append(listener)
        return listener

    @staticmethod
    def _feed(listener, count: int, block: np.ndarray) -> None:
        for _ in range(count):
            listener.feed(block)

    def _wait_for(self, count: int, timeout: float = 5.0) -> list[bytes]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.got) >= count:
                    return list(self.got)
            time.sleep(0.02)
        with self._lock:
            return list(self.got)

    def _lost_items(self):
        with self._lock:
            return list(self.lost)


class HeldSegmentIsRetainedTest(_Harness):
    def test_open_segment_at_the_mute_is_transcribed_on_the_reopen(self):
        listener = self._listener(transcript="abre o navegador")
        # Ele está FALANDO quando o microfone fecha: um segmento aberto.
        self._feed(listener, 3, _SILENCE)
        self._feed(listener, 20, _tone())          # ~1,3 s de voz
        self.assertTrue(listener.speaking)
        held = listener.hold_open()
        self.assertTrue(held["held"], "o áudio do segmento aberto foi perdido")
        self.assertFalse(listener.speaking)
        self.assertTrue(listener.has_held_speech())
        self.assertAlmostEqual(listener.held_seconds, held["seconds"], places=3)
        self.assertEqual(self._wait_for(1, timeout=0.4), [],
                         "transcreveu antes da reabertura do microfone")

        # O microfone reabre: o primeiro bloco devolve o retido ao STT.
        self._feed(listener, 20, _tone())          # continuação da frase
        self._feed(listener, groq_stt._END_BLOCKS, _SILENCE)
        chunks = self._wait_for(1)
        self.assertEqual(len(chunks), 1,
                         f"esperava um único turno, chegaram {len(chunks)}")
        self.assertFalse(listener.has_held_speech(), "o retido ficou para trás")

        # Uma transcrição só, com o começo da frase DENTRO dela e sem duplicar.
        self.assertEqual(len(self.texts), 1)
        clip = chunks[0]
        head = held["seconds"]
        self.assertGreaterEqual(len(clip) / (2 * _SR), head,
                                "o áudio retido não chegou ao STT")
        # Nada foi reportado como perdido: a fala foi aproveitada.
        self.assertEqual(self._lost_items(), [])

    def test_every_held_frame_reaches_the_stt_exactly_once(self):
        listener = self._listener(transcript="continua")
        self._feed(listener, 3, _SILENCE)
        speech = [_tone(4000 + 8 * i) for i in range(12)]
        for block in speech:
            listener.feed(block)
        held = listener.hold_open()
        self.assertTrue(held["held"])
        held_pcm = b"".join(b.tobytes() for b in speech)

        tail = [_tone(6000 + 8 * i) for i in range(12)]
        for block in tail:
            listener.feed(block)
        self._feed(listener, groq_stt._END_BLOCKS, _SILENCE)
        chunks = self._wait_for(1)
        self.assertEqual(len(chunks), 1)
        clip = chunks[0]
        self.assertEqual(clip.count(held_pcm), 1,
                         "o retido foi duplicado ou perdido no caminho")
        self.assertEqual(clip.count(b"".join(b.tobytes() for b in tail)), 1)

    def test_held_fragment_is_transcribed_when_no_continuation_comes(self):
        # Ele fechou o mic e não voltou a falar: o retido não pode morrer
        # esperando para sempre — vira turno depois da janela do agregador.
        original = groq_stt._HELD_WAIT_S
        groq_stt._HELD_WAIT_S = 0.3
        try:
            listener = self._listener(transcript="eu queria que você")
            self._feed(listener, 3, _SILENCE)
            self._feed(listener, 20, _tone())
            self.assertTrue(listener.hold_open()["held"])
            listener.feed(_SILENCE)                 # reabertura, sem fala
            chunks = self._wait_for(1, timeout=3.0)
            self.assertEqual(len(chunks), 1)
            self.assertGreaterEqual(len(chunks[0]) / (2 * _SR), 1.0)
        finally:
            groq_stt._HELD_WAIT_S = original


class UnrecoverableSegmentTest(_Harness):
    def test_too_short_open_segment_becomes_an_explicit_item(self):
        listener = self._listener()
        # 3 quadros abrem o VAD; um quarto e nada mais: curto demais para
        # qualquer transcrição.
        self._feed(listener, 3, _SILENCE)
        self._feed(listener, 4, _tone())
        held = listener.hold_open()
        self.assertFalse(held["held"])
        self.assertTrue(held["lost"], "o segmento curto foi descartado calado")
        items = self._lost_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][0], "inicio")
        self.assertGreater(items[0][2], 0.0)
        self.assertFalse(listener.has_held_speech())
        self.assertEqual(self._wait_for(1, timeout=0.3), [],
                         "áudio curto demais virou turno")

    def test_stt_that_does_not_recognise_the_clip_reports_the_loss(self):
        listener = self._listener(transcript="")   # Whisper devolve vazio
        self._feed(listener, 3, _SILENCE)
        self._feed(listener, 30, _tone())          # ~2 s de voz, nível audível
        self._feed(listener, groq_stt._END_BLOCKS, _SILENCE)
        chunks = self._wait_for(1)
        self.assertEqual(len(chunks), 1)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not self._lost_items():
            time.sleep(0.02)
        items = self._lost_items()
        self.assertEqual(len(items), 1,
                         "a captura sumiu sem rastro (regra do dono)")
        self.assertEqual(items[0][0], "audio")
        self.assertIn("reconheceu", items[0][1])
        self.assertEqual(self.texts, [])


class OwnVoiceNeverBecomesOwnerSpeechTest(_Harness):
    def test_echo_blocks_never_retain_speech(self):
        listener = self._listener(transcript="fala do assistente")
        # Enquanto o assistente fala, a própria voz vai só para o anel
        # (main.py chama feed(..., preroll_only=True) para ela).
        for _ in range(30):
            listener.feed(_tone(), preroll_only=True)
        self.assertFalse(listener.speaking)
        # O microfone fecha durante a resposta: não há o que reter.
        held = listener.hold_open()
        self.assertFalse(held["held"])
        self.assertFalse(held["lost"])
        self.assertEqual(self._lost_items(), [])
        # E a reabertura não inventa turno nenhum.
        listener.feed(_SILENCE)
        self.assertEqual(self._wait_for(1, timeout=0.3), [])
        self.assertEqual(self.texts, [])

    def test_echo_does_not_wake_the_retained_head(self):
        listener = self._listener(transcript="pergunta do dono")
        self._feed(listener, 3, _SILENCE)
        self._feed(listener, 20, _tone())
        self.assertTrue(listener.hold_open()["held"])
        # Blocos da própria voz (preroll_only) não são a reabertura: o retido
        # continua guardado em vez de virar turno com o eco do alto-falante.
        for _ in range(20):
            listener.feed(_tone(), preroll_only=True)
        self.assertTrue(listener.has_held_speech())
        self.assertEqual(self._wait_for(1, timeout=0.3), [])
        self.assertEqual(self.texts, [])

    def test_reply_start_drops_the_mixed_segment_before_it_is_held(self):
        listener = self._listener(transcript="eco do assistente")
        self._feed(listener, 3, _SILENCE)
        self._feed(listener, 10, _tone())
        self.assertTrue(listener.speaking)
        listener.set_assistant_speaking(True)      # a resposta começou a tocar
        listener.feed(_tone())                     # o drop acontece no feed
        held = listener.hold_open()
        self.assertFalse(held["held"],
                         "áudio misturado (TTS + dono) virou fala do dono")
        self.assertEqual(self._lost_items(), [])
        self.assertEqual(self._wait_for(1, timeout=0.3), [])


class _FakeUI:
    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, text):
        self.logs.append(str(text))


class LostSpeechItemTest(unittest.TestCase):
    """Maior regra do dono: nunca ser ignorado — o item existe no log e na voz."""

    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj.ui = _FakeUI()
        obj.said: list[str] = []
        obj._say_now = lambda text, log=True: obj.said.append(text)
        obj._awake = True
        return obj

    def test_lost_speech_writes_activity_item_and_asks_to_repeat(self):
        obj = self._obj()
        obj._stt_lost_audio("inicio", "curto demais para transcrever", 1.7)
        self.assertEqual(len(obj.ui.logs), 1)
        self.assertIn("fala não aproveitada", obj.ui.logs[0])
        self.assertIn("perdi o começo", obj.ui.logs[0])
        self.assertEqual(len(obj.said), 1)
        self.assertIn("perdi o começo", obj.said[0].lower())
        self.assertIn("repete", obj.said[0].lower())

    def test_every_loss_is_logged_but_the_voice_does_not_loop(self):
        obj = self._obj()
        for _ in range(3):
            obj._stt_lost_audio("audio", "o STT não reconheceu nada", 2.0)
        self.assertEqual(len(obj.ui.logs), 3, "alguma perda ficou sem registro")
        self.assertEqual(len(obj.said), 1, "o pedido de repetição virou metralhadora")

    def test_asleep_never_speaks_but_still_logs(self):
        obj = self._obj()
        obj._awake = False
        obj._stt_lost_audio("audio", "o STT não reconheceu nada", 2.0)
        self.assertEqual(len(obj.ui.logs), 1)
        self.assertEqual(obj.said, [])


class _FakeListener:
    """Superfície mínima do listener para o caminho do callback do microfone."""

    speaking = True

    def __init__(self):
        self.held = 0
        self.discarded = 0

    def mic_idle_seconds(self):
        return 0.2

    def speaking_is_stale(self, _max_idle_s):
        return False

    def hold_open(self):
        self.held += 1
        return {"held": True, "seconds": 1.2, "voiced": 20, "lost": False}

    def discard_open(self):
        self.discarded += 1
        return True


class OrphanCloseRetainsTest(unittest.TestCase):
    def test_mute_window_retains_instead_of_discarding(self):
        obj = object.__new__(main.JarvisLive)
        obj._groq = _FakeListener()
        obj._owner_voice_until = 5.0
        obj._owner_voice_since = 1.0
        self.assertTrue(obj._close_orphan_stt("microfone mutado"))
        self.assertEqual(obj._groq.held, 1)
        self.assertEqual(obj._groq.discarded, 0)
        self.assertEqual(obj._owner_voice_until, 0.0)

    def test_deliberate_mute_while_asleep_does_not_retain(self):
        obj = object.__new__(main.JarvisLive)
        obj._groq = _FakeListener()
        obj._owner_voice_until = 5.0
        obj._owner_voice_since = 1.0
        self.assertTrue(obj._close_orphan_stt("microfone mutado (dormindo)",
                                             retain=False))
        self.assertEqual(obj._groq.held, 0)
        self.assertEqual(obj._groq.discarded, 1)


if __name__ == "__main__":
    unittest.main()
