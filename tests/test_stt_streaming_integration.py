"""UP-3 ao vivo: o StreamingSession plugado no caminho real do GroqListener.

O que `tests/test_stt_streaming.py` nunca provou: que o segmentador de
verdade — VAD, portão de transitório, worker, endpointing — ABRE uma sessão
por frase, alimenta os MESMOS blocos (pré-roll de 1,5 s incluso) e entrega
`on_partial` enquanto a boca do dono ainda está aberta. Aqui a frase é
sintética (64 ms por quadro, como no callback do microfone), o transcritor é
um fake determinístico que devolve um texto NOVO a cada chamada, e o que se
conta é a ordem dos eventos:

    parcial 1, 2, 3 ... ANTES do fim da frase  ->  final (uma única vez)

Mais a retrocompatibilidade prometida: sem callback, com a chave de ambiente
desligada, ou com o caminho parcial quebrado de propósito, o turno sai
exatamente como saía antes — o parcial é acréscimo, nunca dependência.
"""
from __future__ import annotations

import os
import threading
import time
import unittest
from unittest import mock

import numpy as np

import main
from core import groq_stt
from tests.audio_fixtures import voice_block

_SR = 16000
_FRAME = 1024                     # CHUNK_SIZE de main.py: 64 ms a 16 kHz
_SILENCE = np.zeros(_FRAME, dtype=np.int16)
_VOICE = voice_block(8000)        # passa no portão de transitório
_PREROLL_FRAMES = groq_stt.ring_blocks(groq_stt._PREROLL_S)   # 24 + 3


def _engine(calls: list, lock: threading.Lock):
    """Transcritor fake: um texto novo por chamada com voz, "" no silêncio.

    Texto novo a cada janela é o que impede a deduplicação do
    StreamingSession de esconder parciais — assim "pelo menos 3" vira uma
    contagem honesta de eventos, não de tentativas. E silêncio não vira
    texto, como no Whisper de verdade: a primeira janela do pré-roll, que é
    só o quarto antes da primeira sílaba, não produz evento.
    """
    def fn(pcm: bytes) -> str:
        x = np.frombuffer(pcm, dtype=np.int16)
        if x.size == 0 or not np.any(x):
            return ""
        secs = len(pcm) / (2 * _SR)
        with lock:
            n = len(calls) + 1
            calls.append((len(pcm), secs, bytes(pcm)))
        return f"trecho {n} de {secs:.1f}s"
    return fn


class StreamingListenerTest(unittest.TestCase):
    """O caminho incremental plugado no segmentador real, ponta a ponta."""

    def setUp(self):
        # O duck fala com o PipeWire da máquina; aqui só interessa a
        # segmentação e as janelas progressivas.
        for name in ("up", "down", "heartbeat"):
            patcher = mock.patch.object(getattr(groq_stt, "_duck"), name,
                                        lambda *a, **k: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("core.usage.note", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lock = threading.Lock()
        self.calls: list[tuple[int, float, bytes]] = []
        # Fila única de eventos na ordem em que NASCERAM: é essa ordem que
        # prova "parcial antes do fim", não timestamps soltos.
        self.events: list[tuple[str, str, float]] = []
        # O texto que o caminho de sempre (worker + handler) entregou ao dono:
        # o evento stt_final tem de ser EXATAMENTE ele, nada de texto próprio.
        self.pipeline: list[str] = []
        self.final_ready = threading.Event()

    def tearDown(self):
        for listener in getattr(self, "_listeners", []):
            listener.stop()

    def _listener(self, **kwargs):
        self._listeners = getattr(self, "_listeners", [])

        def on_partial(text: str, ms: float) -> None:
            self.events.append(("partial", text, ms))

        def on_final(text: str, ms: float) -> None:
            self.events.append(("final", text, ms))
            self.final_ready.set()

        opts = dict(
            sample_rate=_SR,
            transcribe_fn=_engine(self.calls, self.lock),
            use_vad=False,                      # áudio sintético, sem modelo
            on_partial=on_partial,
            on_final=on_final,
        )
        opts.update(kwargs)
        listener = groq_stt.GroqListener(
            "", "test", "pt",
            on_text=lambda _t: None,
            on_utterance=lambda t, _p: self.pipeline.append(t),
            on_status=lambda _m: None,
            **opts)
        # O endpointing é um modelo à parte: aqui o corte fixo de silêncio
        # basta para fechar a frase.
        listener._endpoint_complete = lambda _pcm: True
        self._listeners.append(listener)
        return listener

    def _feed(self, listener: groq_stt.GroqListener, count: int,
              block: np.ndarray) -> None:
        for _ in range(count):
            listener.feed(block)

    def _speak(self, listener: groq_stt.GroqListener,
               voiced_frames: int = 110) -> None:
        """Uma frase longa de ~7 s: pré-roll + fala, como no microfone."""
        self._feed(listener, _PREROLL_FRAMES + 3, _SILENCE)   # enche o anel
        self._feed(listener, voiced_frames, _VOICE)

    def _close(self, listener: groq_stt.GroqListener) -> bool:
        self._feed(listener, groq_stt._END_BLOCKS + 1, _SILENCE)
        return self.final_ready.wait(5.0)

    def _wait_partials(self, n: int, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if len(self.events) and \
                        sum(1 for e in self.events if e[0] == "partial") >= n:
                    return True
            time.sleep(0.02)
        return False

    # -- a prova principal ----------------------------------------------------

    def test_partials_arrive_before_the_phrase_ends(self):
        listener = self._listener()
        self._speak(listener)
        # Três janelas progressivas ENQUANTO o segmentador ainda tem o
        # microfone aberto: é o "antes do fim da frase" do critério UP-3.
        self.assertTrue(self._wait_partials(3),
                        f"parciais demais não vieram: {self.events}")
        self.assertTrue(listener.speaking,
                        "a frase já fechou — o parcial não veio 'ao vivo'")
        self.assertTrue(self._close(listener), "o turno final nunca saiu")
        events = list(self.events)
        partials = [e for e in events if e[0] == "partial"]
        finals = [e for e in events if e[0] == "final"]
        self.assertGreaterEqual(len(partials), 3)
        # UM único evento final: a sessão parcial é descartada no fim, não
        # transcrita de novo (nada de caminho duplo no engine).
        self.assertEqual(len(finals), 1)
        first_final = events.index(finals[0])
        # Os três parciais que chegaram antes do fim estão ANTES do final.
        self.assertLessEqual(events.index(partials[2]), first_final)
        # Relógio ms_since_open monotônico entre parciais (mesma frase).
        mss = [e[2] for e in partials]
        self.assertEqual(mss, sorted(mss))
        self.assertGreater(finals[0][2], 0.0)
        # ...e o texto final é o do caminho de sempre (worker + endpointing),
        # entregue ao cérebro exatamente como antes da plugagem.
        self.assertEqual(self.pipeline, [finals[0][1]])

    def test_first_partial_carries_the_preroll_head(self):
        """O pré-roll de 1,5 s abre o caminho parcial: a primeira janela com
        voz contém o silêncio ANTES do primeiro fonema — a frase não é
        picotada no começo."""
        listener = self._listener()
        self._speak(listener)
        self.assertTrue(self._wait_partials(1), "nenhum parcial chegou")
        _, secs, pcm = self.calls[0]
        self.assertGreaterEqual(secs, 1.4, "a 1ª janela ignorou o pré-roll")
        x = np.frombuffer(pcm, dtype=np.int16)
        nz = np.flatnonzero(x)
        self.assertGreater(nz.size, 0)
        leading = int(nz[0]) / _SR
        self.assertGreaterEqual(leading, 1.0,
                                "o começo da frase foi cortado do parcial")

    def test_second_utterance_opens_a_fresh_session(self):
        listener = self._listener()
        self._speak(listener)
        self.assertTrue(self._wait_partials(3))
        self.assertTrue(self._close(listener))
        self.final_ready.clear()
        n_partials = sum(1 for e in self.events if e[0] == "partial")
        # Frase nova: sessão nova, parciais novos, um final novo.
        self._speak(listener)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if sum(1 for e in self.events if e[0] == "partial") >= n_partials + 3:
                break
            time.sleep(0.02)
        self.assertGreaterEqual(
            sum(1 for e in self.events if e[0] == "partial"), n_partials + 3,
            "a segunda frase não abriu sessão parcial nova")
        self.assertTrue(self._close(listener))
        self.assertEqual(sum(1 for e in self.events if e[0] == "final"), 2)

    # -- retrocompatibilidade --------------------------------------------------

    def test_without_callback_the_pipeline_is_the_old_one(self):
        # Nenhum on_partial = nada montado: sem flag, sem thread, sem fila.
        got: list[str] = []
        listener = groq_stt.GroqListener(
            "", "test", "pt",
            on_text=lambda t: got.append(t),
            on_status=lambda _m: None,
            sample_rate=_SR,
            transcribe_fn=_engine(self.calls, self.lock),
            use_vad=False)
        listener._endpoint_complete = lambda _pcm: True
        self._listeners = [listener]
        self.assertFalse(listener._partial_enabled)
        self.assertIsNone(listener._partial_thread)
        self._speak(listener)
        self._feed(listener, groq_stt._END_BLOCKS + 1, _SILENCE)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not got:
            time.sleep(0.02)
        self.assertTrue(got, "o caminho antigo deixou de entregar o turno")
        self.assertIsNone(listener._partial_thread)

    def test_kill_switch_disables_the_partial_path(self):
        with mock.patch.dict(os.environ, {"JARVIS_STT_PARTIAL": "0"}):
            listener = self._listener()
            self.assertFalse(listener._partial_enabled)
            self._speak(listener)
            self.assertTrue(self._close(listener), "o turno final quebrou")
        self.assertEqual([e for e in self.events if e[0] == "partial"], [])
        self.assertEqual(len([e for e in self.events if e[0] == "final"]), 1)

    def test_broken_partial_never_eats_the_turn(self):
        """Com o caminho parcial arrebentado de propósito, o turno sai inteiro."""
        listener = self._listener()

        def raiser(_pcm: bytes) -> str:
            raise RuntimeError("parcial quebrado")

        listener._partial_transcribe = raiser
        self._speak(listener)
        time.sleep(0.3)          # dá à thread tempo de quebrar à vontade
        self.assertTrue(self._close(listener), "o final desapareceu com o erro")
        self.assertEqual([e for e in self.events if e[0] == "partial"], [])
        self.assertEqual(len([e for e in self.events if e[0] == "final"]), 1)
        self.assertTrue(listener._partial_thread.is_alive(),
                        "a thread parcial morreu com um erro de transcrição")


class ActivityLogLineFormatTest(unittest.TestCase):
    """As linhas que main.py escreve: [Fluxos] stt_partial / stt_final."""

    class _UI:
        def __init__(self):
            self.lines: list[str] = []

        def write_log(self, text: str):
            self.lines.append(text)

    def _app(self):
        app = object.__new__(main.JarvisLive)
        app.ui = self._UI()
        return app

    def test_partial_line_format(self):
        app = self._app()
        main.JarvisLive._stt_partial_event(app, "abre o navegador", 1536.4)
        line = app.ui.lines[0]
        self.assertRegex(line,
                         r"^\[Fluxos\] stt_partial texto=.* ms_since_open=\d+$")
        self.assertEqual(
            line, "[Fluxos] stt_partial texto='abre o navegador' "
                  "ms_since_open=1536")

    def test_final_line_format(self):
        app = self._app()
        main.JarvisLive._stt_final_event(app, "abre o navegador agora", 4200.0)
        self.assertEqual(
            app.ui.lines[0],
            "[Fluxos] stt_final texto='abre o navegador agora' "
            "ms_since_open=4200")

    def test_the_lines_are_never_attributed_to_the_owner(self):
        # core/activity trata "[Fluxos]" como sistema: a linha do parcial não
        # pode virar "YOU:" no painel nem voz cadastrável no voiceprint.
        from core import activity
        self.assertTrue(activity.is_system_text(
            "[Fluxos] stt_partial texto='oi' ms_since_open=1500"))


if __name__ == "__main__":
    unittest.main()
