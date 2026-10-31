"""O duck não pode ficar preso: aberto pela fala, tem de voltar ao normal.

Reproduz o bug observado em 31/10/2026 — apps travados a 20% enquanto o
assistente trabalhava em segundo plano (turno guardado / handler que morreu /
troca que nunca falou). A regra do dono é exata: volume a 20% só DURANTE fala
do usuário ou do assistente. O watchdog de core/duck.py é a rede de segurança
que devolve os volumes quando nenhum dos dois sinais de vida existe.

Estilo: os testes do repo mockam o duck (ver test_mic_capture.py); aqui o
próprio pactl é falso, então nada toca no áudio real da máquina.
"""
from __future__ import annotations

import threading
import time
import unittest

from core import duck, groq_stt


def _others() -> list[tuple[str, str, int, str, str]]:
    # Um sink-input de outro app (Firefox a 60%), nunca o nosso.
    return [("12", "999999", 60, "Mozilla Firefox",
             "Sample Specification: float32le 2ch 48000Hz")]


def _with_own() -> list[tuple[str, str, int, str, str]]:
    # A stream própria do assistente (python, mono, 24 kHz): está falando.
    return _others() + [("7", "", 100, "python3.14",
                         "Sample Specification: s16le 1ch 24000Hz")]


def _wait(cond, timeout: float = 8.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class WatchdogBase(unittest.TestCase):
    def setUp(self):
        self._orig_inputs = duck._inputs
        self._orig_pactl = duck._pactl
        self._orig_check = duck.DUCK_CHECK_S
        self._orig_stale = duck.DUCK_STALE_S
        self._orig_saved = dict(duck._saved)
        self._orig_probe = duck._probe
        duck._saved.clear()
        duck._probe = None
        duck.DUCK_CHECK_S = 0.05
        duck.DUCK_STALE_S = 0.3
        self.comandos: list[tuple] = []
        self.own_stream = False
        duck._pactl = lambda *a: self.comandos.append(a) or ""
        duck._inputs = lambda: _with_own() if self.own_stream else _others()

    def tearDown(self):
        duck._inputs = self._orig_inputs
        duck._pactl = self._orig_pactl
        duck.DUCK_CHECK_S = self._orig_check
        duck.DUCK_STALE_S = self._orig_stale
        duck._probe = self._orig_probe
        duck._saved.clear()


class DuckWatchdogTest(WatchdogBase):
    def _ducked(self) -> bool:
        with duck._SAVED_LOCK:
            return bool(duck._saved)

    def test_duck_aberto_salva_volume_original(self):
        duck.down()
        self.assertTrue(_wait(self._ducked), "down() não salvou o sink-input")
        # 60% × 0.20 = 12%, salvo para restauração
        self.assertIn(("set-sink-input-volume", "12", "12%"), self.comandos)
        with duck._SAVED_LOCK:
            self.assertEqual(duck._saved.get("12"), 60)
        duck.up()
        self.assertTrue(_wait(lambda: not self._ducked()))
        self.assertIn(("set-sink-input-volume", "12", "60%"), self.comandos)

    def test_troca_que_esquece_up_e_solta_pelo_watchdog(self):
        """Bug: down() abre o duck e o caminho de erro nunca chama up() —
        o watchdog TEM de devolver o volume em segundos, não para sempre."""
        duck.down()
        self.assertTrue(_wait(self._ducked))
        # Nenhuma fala ativa: sem heartbeat, sem stream própria.
        self.assertTrue(_wait(lambda: not self._ducked()),
                        "watchdog não libertou um duck preso")
        self.assertIn(("set-sink-input-volume", "12", "60%"), self.comandos)

    def test_fala_do_usuario_mantem_o_duck(self):
        """Heartbeat do segmentador (blocks de mic) = fala ativa: o watchdog
        não pode cortar a música durante a frase do usuário."""
        duck.down()
        self.assertTrue(_wait(self._ducked))
        fim = time.monotonic() + self._orig_stale_grace()
        while time.monotonic() < fim:
            duck.heartbeat()          # feed() chama a cada ~64 ms
            time.sleep(0.05)
        self.assertTrue(self._ducked(),
                        "watchdog soltou o duck durante fala ativa do usuário")
        # A fala parou de verdade: agora ele solta.
        self.assertTrue(_wait(lambda: not self._ducked()),
                        "watchdog não libertou depois do fim da fala")

    def _orig_stale_grace(self) -> float:
        return duck.DUCK_STALE_S * 3

    def test_fala_do_assistente_mantem_o_duck(self):
        """A stream própria tocando (24 kHz mono) é o sinal de fala do
        assistente; o watchdog não pode duckar/unduckar no meio da reply."""
        duck.down()
        self.assertTrue(_wait(self._ducked))
        self.own_stream = True        # playback ativo
        fim = time.monotonic() + duck.DUCK_STALE_S * 3
        while time.monotonic() < fim:
            time.sleep(0.05)
        self.assertTrue(self._ducked(),
                        "watchdog cortou o duck durante a fala do assistente")
        self.own_stream = False       # playback acabou, ninguém fechou a troca
        self.assertTrue(_wait(lambda: not self._ducked()))

    def test_down_idempotente_reafirma_sem_empilhar(self):
        duck.down()
        self.assertTrue(_wait(self._ducked))
        first = dict(duck._saved)
        duck.down()                   # gatilho sobreposto (fala + reply)
        time.sleep(0.2)
        self.assertEqual(dict(duck._saved), first,
                         "segundo down() empilhou o duck ou perdeu o original")
        self.assertNotIn(("set-sink-input-volume", "12", "2%"), self.comandos)


class SttErrorPathTest(unittest.TestCase):
    """O elo quebrado específico: o handler da fala morre no meio do turno —
    nada em main.py vai fechar a troca — e up() TEM de ser chamado."""

    def setUp(self):
        self.calls: list[str] = []
        self._orig = (groq_stt._duck.up, groq_stt._duck.down,
                      groq_stt._duck.heartbeat)
        groq_stt._duck.up = lambda: self.calls.append("up")
        groq_stt._duck.down = lambda: self.calls.append("down")
        groq_stt._duck.heartbeat = lambda: self.calls.append("beat")

    def tearDown(self):
        (groq_stt._duck.up, groq_stt._duck.down,
         groq_stt._duck.heartbeat) = self._orig

    def _listener(self, on_utterance):
        return groq_stt.GroqListener(
            "", "test", "pt",
            on_text=lambda _t: None,
            on_utterance=on_utterance,
            on_status=lambda _m: None,
            sample_rate=16000,
            transcribe_fn=lambda _pcm: "abre o editor de texto agora",
        )

    def test_handler_exception_libera_o_duck(self):
        def explode(_t, _p):
            raise RuntimeError("turno morreu no gate")

        listener = self._listener(explode)
        try:
            pcm = bytes(2 * 16000)          # 1 s de "fala" transcrita
            listener._process_utterance(pcm)
            self.assertEqual(self.calls.count("up"), 1,
                             "troca terminou por erro e up() não foi chamado")
        finally:
            listener.stop()

    def test_handler_exception_durante_reply_nao_corta_a_fala(self):
        """Se a reply JÁ está soando, soltar o duck agora desduckaria a fala
        do assistente no meio — quem fecha é o probe/watchdog."""
        def explode(_t, _p):
            raise RuntimeError("barge-in morreu")

        listener = self._listener(explode)
        listener.set_assistant_speaking(True)
        try:
            listener._process_utterance(bytes(2 * 16000))
            self.assertNotIn("up", self.calls,
                             "up() cortou a fala do assistente em andamento")
        finally:
            listener.stop()


if __name__ == "__main__":
    unittest.main()
