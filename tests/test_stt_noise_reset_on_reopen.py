"""O microfone reabre e o ouvido continua surdo: reset da estimativa de ruido.

Diagnóstico (07/11/2026): 491 linhas `[Groq] mic peak ... quiet` e 0 SPEECH
desde o boot, com o microfone entregando blocos. O gate espectral de
`core.noise_filter` guarda uma média móvel do espectro do RUÍDO que pertence ao
stream que a mediu. Quando o PulseAudio troca de dispositivo (ou a stream é
reaberta depois de um stall) a estimativa antiga continua lá: multiplicada por
`_OVER_SUB` ela derruba o ganho de cada bloco até o piso espectral (0.02), o
nível filtrado desaba, o `floor` do segmentador decai até `_FLOOR_MIN`, o
`gate` vira `_THRESH_MIN` e nada cruza mais — inclusive a fala do dono, que
nem chega a ser medida.

Este arquivo cobre o conserto (reset em todo reopen/start) e a medição nova
(peak cru x peak filtrado). O portão em si NÃO foi mudado: a decisão de medir
o gate no cru é da próxima rodada, e é exatamente o que as duas colunas do log
vão permitir decidir.
"""
from __future__ import annotations

import contextlib
import io
import re
import threading
import time
import unittest

import numpy as np

from core import groq_stt, noise_filter
from tests.audio_fixtures import voice_block

_SR = 16000
_FRAME = 1024                      # 64 ms a 16 kHz (CHUNK_SIZE de main.py)
_VOICE = 12000                     # nível de fala do dono no microfone do PC
_BINS = _FRAME // 2 + 1
_END_SILENCE = groq_stt._END_BLOCKS + 2
_PEAK_LINE_RE = re.compile(
    r"mic peak raw=(?P<raw>[\d.]+) filtrado=(?P<filtrado>[\d.]+) "
    r"floor=(?P<floor>[\d.]+) gate=(?P<gate>[\d.]+)")


def _poison(multiplo: int = 100) -> float:
    """Estimativa de ruido de OUTRO microfone: a escala do q25 atual x N.

    É o estado que o log mostrava: a magnitude aprendida do dispositivo
    anterior muito acima do que o atual entrega. Sem reset, o ganho de Wiener
    vira o piso espectral em todos os bins.
    """
    noise_filter.reset_noise_estimate()
    noise_filter.filter_block(voice_block(amplitude=_VOICE, phase=0).tobytes(),
                              _SR)
    base = float(noise_filter.noise_estimate_mean() or 0.0)
    noise_filter._noise_estimate = np.full(_BINS, base * multiplo,
                                           dtype=np.float32)
    return base


class _Harness(unittest.TestCase):
    """Microfone sintético + registro de resets, sem tocar em alto-falante.

    `use_vad=False` de propósito: o que se mede aqui é o ESTADO do filtro e do
    gate de nível, não o modelo ONNX. Com o caminho de RMS a surdez aparece
    exatamente como no log (pico filtrado abaixo do gate) e a recuperação
    também.
    """

    def setUp(self):
        self._orig = (groq_stt._duck.up, groq_stt._duck.down,
                      groq_stt._duck.heartbeat)
        groq_stt._duck.up = lambda: None
        groq_stt._duck.down = lambda: None
        groq_stt._duck.heartbeat = lambda: None
        self._estimate_backup = noise_filter._noise_estimate
        self._stats_backup = noise_filter.reset_stats()
        # Contador de resets com o motivo de cada um.
        self.resets: list[str] = []
        self._orig_reset = noise_filter.reset_noise_estimate

        def _counting_reset(reason: str = "") -> bool:
            self.resets.append(str(reason))
            return self._orig_reset(reason)

        noise_filter.reset_noise_estimate = _counting_reset
        self._clips: list[bytes] = []
        self._listeners: list[groq_stt.GroqListener] = []

    def tearDown(self):
        for listener in self._listeners:
            listener.stop()
        noise_filter.reset_noise_estimate = self._orig_reset
        noise_filter._noise_estimate = self._estimate_backup
        noise_filter._reset_count = self._stats_backup["count"]
        noise_filter._last_reset_reason = self._stats_backup["last_reason"]
        groq_stt._duck.up, groq_stt._duck.down, groq_stt._duck.heartbeat = \
            self._orig

    def _listener(self) -> groq_stt.GroqListener:
        listener = groq_stt.GroqListener(
            api_key="", model="m", language="pt",
            on_text=lambda _t: None,
            use_vad=False,
            transcribe_fn=lambda pcm: self._clips.append(pcm) or "texto",
            engine_label="Teste")
        self._listeners.append(listener)
        return listener

    def _feed_voice(self, listener, blocks: int = 10, start: int = 0) -> None:
        for i in range(blocks):
            listener.feed(voice_block(amplitude=_VOICE, phase=start + i))

    def _feed_silence(self, listener) -> None:
        silence = np.zeros(_FRAME, dtype=np.int16)
        for _ in range(_END_SILENCE):
            listener.feed(silence)

    def _simulate_reopen(self, listener, seconds: float = 2.0) -> None:
        """Um buraco de blocos: a stream fechou e reabriu.

        É a única evidência que o `feed()` tem de uma reabertura — ele não vê
        PulseAudio, só a ausência de áudio.
        """
        listener._last_feed_at = time.monotonic() - seconds

    def _poison_gate(self) -> None:
        """Envenena a estimativa sem contar o reset que o envenenamento faz."""
        _poison()
        self.resets.clear()


class TestResetOnReopen(_Harness):

    def test_boot_resets_the_estimate(self):
        """Um ouvinte novo é um par de ouvidos novo: começa sem estimativa."""
        self.resets.clear()
        self._listener()
        self.assertEqual(self.resets, ["start"])

    def test_three_reopens_reset_three_times(self):
        """Todo reopen chama `reset_noise_estimate()` — três reopens, três."""
        listener = self._listener()
        self._poison_gate()
        before = noise_filter.reset_stats()["count"]
        for _ in range(3):
            self._simulate_reopen(listener)
            self._feed_voice(listener, blocks=10, start=0)
            self._feed_silence(listener)
        self.assertEqual(self.resets, ["reopen", "reopen", "reopen"])
        self.assertEqual(noise_filter.reset_stats()["count"] - before, 3)

    def test_short_hiccup_does_not_reset(self):
        """Um atraso de um bloco não é reabertura: a estimativa continua útil."""
        listener = self._listener()
        self.resets.clear()
        for _ in range(40):
            listener._last_feed_at = time.monotonic() - 0.2
            self._feed_voice(listener, blocks=1)
        self.assertEqual(self.resets, [])
        self.assertTrue(noise_filter.has_noise_estimate())

    def test_notify_noise_reset_carries_the_reason(self):
        """Quem é dono do grafo de áudio (main.py) anuncia a troca de dispositivo."""
        listener = self._listener()
        self.resets.clear()
        listener.notify_noise_reset("device_change")
        self._feed_voice(listener, blocks=1)
        self.assertEqual(self.resets, ["device_change"])
        # Consumido uma única vez: o próximo bloco da mesma stream não repete.
        self._feed_voice(listener, blocks=1, start=1)
        self.assertEqual(self.resets, ["device_change"])

    def test_hold_open_marks_the_next_block_as_reopen(self):
        """Janela de mute fecha o microfone no meio da frase → reopen depois."""
        listener = self._listener()
        self._feed_voice(listener, blocks=6)     # abre o segmento
        self.assertTrue(listener.speaking)
        info = listener.hold_open()
        self.assertTrue(info["held"])
        self.resets.clear()
        self._simulate_reopen(listener, seconds=0.1)   # sem buraco perceptível
        self._feed_voice(listener, blocks=1)
        self.assertEqual(self.resets, ["reopen"])


class TestGateUnsticksAfterReopen(_Harness):
    """O sintoma do log: pico cru de fala, pico filtrado de silêncio.

    Aqui o gate NÃO é re-medido no cru (decisão da próxima rodada); o que se
    prova é que com a estimativa certa o mesmo áudio volta a abrir turno.
    """

    def test_poisoned_gate_is_deaf(self):
        listener = self._listener()
        self._poison_gate()
        listener._floor = 0.02
        self._feed_voice(listener, blocks=10)
        self.assertEqual(listener._gate_stats["turns_opened"], 0)
        self.assertFalse(listener.speaking)
        # O áudio chegou e morreu no filtro: é a linha do log novo.
        self.assertGreater(listener._peak_raw, 0.1)
        self.assertLess(listener._peak, listener._peak_raw * 0.1)

    def test_reopen_hears_the_same_voice_again(self):
        listener = self._listener()
        self._poison_gate()
        listener._floor = 0.02
        self._feed_voice(listener, blocks=10)
        self.assertEqual(listener._gate_stats["turns_opened"], 0)

        self._simulate_reopen(listener)
        self._feed_voice(listener, blocks=10, start=10)
        self.assertEqual(listener._gate_stats["turns_opened"], 1)
        self.assertTrue(listener.speaking)

    def test_floor_is_not_pinned_at_the_minimum_after_reopens(self):
        """`floor` preso em `_FLOOR_MIN` era a assinatura da surdez.

        Depois de cada reopen o nível filtrado volta à escala do cru, então o
        floor sobe do zero aprendido com o áudio esmagado e o gate deixa de
        ser o piso artificial `_THRESH_MIN`.
        """
        listener = self._listener()
        self._poison_gate()
        listener._floor = groq_stt._FLOOR_MIN
        for _ in range(3):
            self._simulate_reopen(listener)
            self._feed_voice(listener, blocks=10)
            self._feed_silence(listener)
        self.assertGreater(listener._floor, groq_stt._FLOOR_MIN)
        thresh = max(groq_stt._THRESH_MIN, listener._floor * groq_stt._FLOOR_MULT)
        self.assertGreater(thresh, groq_stt._THRESH_MIN)
        # Três reopens, três clips transcritos: nada ficou travado.
        self.assertEqual(self.resets.count("reopen"), 3)
        self.assertEqual(listener._gate_stats["turns_opened"], 3)

    def test_estimate_reseeds_from_the_new_stream(self):
        """Reset não é "sem filtro": o próximo bloco re-me o próprio piso."""
        listener = self._listener()
        self._poison_gate()
        self.assertGreater(float(noise_filter.noise_estimate_mean()), 10.0)
        self._simulate_reopen(listener)
        self._feed_voice(listener, blocks=1)
        self.assertTrue(noise_filter.has_noise_estimate())
        self.assertLess(float(noise_filter.noise_estimate_mean()), 5.0)


class TestPeakLogRawVsFiltered(_Harness):

    def test_log_line_reports_raw_and_filtered(self):
        """`[Groq] mic peak raw=X filtrado=Y floor=Z gate=W` — onde o áudio morre."""
        listener = self._listener()
        self._poison_gate()
        listener._floor = 0.02
        listener._peak_t = time.monotonic() - groq_stt._LEVEL_LOG_S - 1.0
        listener._peak_log_t = 0.0
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self._feed_voice(listener, blocks=5)
        lines = [ln for ln in out.getvalue().splitlines() if "mic peak" in ln]
        self.assertTrue(lines, "a linha de nível não foi impressa")
        text = lines[-1]
        match = _PEAK_LINE_RE.search(text)
        self.assertIsNotNone(match, text)
        numbers = {key: float(value)
                   for key, value in match.groupdict().items() if value}
        self.assertGreater(numbers["raw"], 0.1)
        # A promessa do log novo: dizer ONDE o áudio quebrou, na mesma linha.
        self.assertLess(numbers["filtrado"], numbers["raw"] * 0.1)
        # O gate impresso é o piso x multiplicador, arredondado na linha.
        self.assertAlmostEqual(
            numbers["gate"],
            max(groq_stt._THRESH_MIN, numbers["floor"] * groq_stt._FLOOR_MULT),
            delta=0.001)
        self.assertTrue(text.endswith("SPEECH") or text.endswith("quiet"), text)

    def test_log_is_throttled_to_one_per_second(self):
        """O medidor roda no callback de áudio: no máximo uma linha por segundo."""
        listener = self._listener()
        listener._peak_t = 0.0
        listener._peak_log_t = time.monotonic()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for _ in range(200):
                listener._last_feed_at = time.monotonic() - 0.4
                self._feed_voice(listener, blocks=1)
        self.assertEqual(out.getvalue().count("mic peak"), 0)


class TestResetNoiseEstimateApi(unittest.TestCase):
    """Contrato de `core.noise_filter.reset_noise_estimate`: idempotente e
    thread-safe — o reopen roda em uma thread e o `filter_block` no callback
    de áudio da outra."""

    def setUp(self):
        self._estimate_backup = noise_filter._noise_estimate
        noise_filter.reset_noise_estimate()

    def tearDown(self):
        noise_filter._noise_estimate = self._estimate_backup

    def test_second_reset_is_a_no_op(self):
        noise_filter.filter_block(
            (np.ones(_FRAME, dtype=np.int16) * 8000).tobytes(), _SR)
        self.assertTrue(noise_filter.has_noise_estimate())
        self.assertTrue(noise_filter.reset_noise_estimate("reopen"))
        self.assertFalse(noise_filter.reset_noise_estimate("reopen"))
        self.assertFalse(noise_filter.has_noise_estimate())

    def test_reset_without_argument_keeps_working(self):
        """Chamada sem motivo (tests/, scripts/) continua válida."""
        noise_filter.filter_block(
            (np.ones(_FRAME, dtype=np.int16) * 8000).tobytes(), _SR)
        noise_filter.reset_noise_estimate()
        self.assertIsNone(noise_filter._noise_estimate)

    def test_concurrent_reset_never_breaks_the_callback(self):
        """O bloco em curso usa a própria cópia da estimativa.

        Antes, um reset caindo entre a leitura e o uso deixava
        `_noise_estimate` em None dentro do `filter_block` — TypeError no
        thread do microfone, que é exatamente como um ouvido morre sem ninguém
        ver.
        """
        errors: list[BaseException] = []
        stop = threading.Event()
        raw = (np.random.default_rng(3).integers(-9000, 9000,
                                                 size=_FRAME)).astype(
            np.int16).tobytes()

        def _audio() -> None:
            try:
                while not stop.is_set():
                    noise_filter.filter_block(raw, _SR)
            except BaseException as e:      # noqa: BLE001 - é a asserção
                errors.append(e)

        def _reopens() -> None:
            while not stop.is_set():
                noise_filter.reset_noise_estimate("reopen")
                time.sleep(0.0005)

        threads = [threading.Thread(target=_audio, daemon=True) for _ in range(3)]
        threads.append(threading.Thread(target=_reopens, daemon=True))
        for t in threads:
            t.start()
        time.sleep(0.5)
        stop.set()
        for t in threads:
            t.join(timeout=2.0)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
