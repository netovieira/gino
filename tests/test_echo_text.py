"""Guarda de eco por texto: o turno fantasma morre calado, a fala do dono passa.

Cobre o caso positivo (transcrição que é a nossa própria fala voltando pelo
microfone, inclusive com palavra corrompida pelo transcritor) e o negativo (o
dono falando algo novo logo depois da resposta), além do laço de "não consegui
entender" que o eco provocava em main.py.
"""

from __future__ import annotations

import time
import unittest

import main
from core import echo_text


def _guard(*falas: str) -> echo_text.EchoTextGuard:
    g = echo_text.EchoTextGuard()
    for fala in falas:
        g.note(fala)
    return g


class EchoSimilarityTest(unittest.TestCase):
    def test_same_sentence_is_echo(self):
        fala = "Não consegui entender o que você falou — repete, por favor?"
        self.assertGreaterEqual(echo_text.similarity(fala, fala), 0.9)

    def test_corrupted_word_still_matches(self):
        """O caso real: 'roteador' voltou do transcritor como 'vanteador'."""
        nossa = ("Vou reorganizar o roteador para que a boca fale antes, e o "
                 "raciocínio pensa em paralelo com o orquestrador no meio.")
        voltou = ("vou reorganizar o vanteador para que a boca fale antes e o "
                  "raciocinio pense em paralelo com o orquestrador no meio")
        self.assertGreaterEqual(echo_text.similarity(voltou, nossa),
                                echo_text.EchoTextGuard.LIMIAR)

    def test_fragment_of_a_long_reply_is_echo(self):
        nossa = ("Se a medição depois mostrar que o roteador duvida muito, aí "
                 "sim eu troco pela nuvem gratuita e aceito o dado saindo por "
                 "payload minúsculo de rota.")
        self.assertGreaterEqual(
            echo_text.similarity("aí sim eu troco pela nuvem gratuita", nossa),
            echo_text.EchoTextGuard.LIMIAR)

    def test_owner_sentence_after_reply_is_not_echo(self):
        nossa = "Não consegui entender o que você falou — repete, por favor?"
        do_dono = "Não está recebendo informação nenhuma por microfone."
        self.assertLess(echo_text.similarity(do_dono, nossa),
                        echo_text.EchoTextGuard.LIMIAR)

    def test_owner_reusing_vocabulary_is_not_echo(self):
        nossa = ("Quem monta o argumento é o raciocínio, o Qoder. O gate só "
                 "recebe pronto, verifica se a ferramenta existe e executa.")
        do_dono = ("Quem comanda é o orquestrador, não o raciocínio, cara. "
                   "Cada um separado e o orquestrador só decide o que fazer.")
        self.assertLess(echo_text.similarity(do_dono, nossa),
                        echo_text.EchoTextGuard.LIMIAR)

    def test_short_reply_is_never_echo(self):
        nossa = "Pode enviar a mensagem?"
        for curto in ("pode", "sim", "não", "beleza", "pode enviar"):
            self.assertFalse(echo_text.similarity(curto, nossa)
                             >= echo_text.EchoTextGuard.LIMIAR, curto)


class EchoTextGuardTest(unittest.TestCase):
    def test_matches_recent_speech(self):
        g = _guard("o roteador decide pela regra em cinco milissegundos")
        self.assertTrue(g.matches("o roteador decide pela regra em cinco milissegundos"))

    def test_no_speech_means_no_echo(self):
        self.assertFalse(_guard().matches("qualquer coisa que eu disse agora"))

    def test_expired_speech_does_not_condemn_a_turn(self):
        g = echo_text.EchoTextGuard()
        g.note("o roteador decide pela regra", when=time.monotonic() - 600.0)
        self.assertFalse(g.matches("o roteador decide pela regra"))

    def test_spoke_during_the_capture_window(self):
        g = echo_text.EchoTextGuard()
        agora = time.monotonic()
        g.note("não consegui entender o que você falou", when=agora)
        self.assertTrue(g.spoke_during(agora - 3.0, agora))
        self.assertFalse(g.spoke_during(agora - 120.0, agora - 60.0))

    def test_only_the_last_lines_are_kept(self):
        g = echo_text.EchoTextGuard(guardadas=2)
        for i in range(5):
            g.note(f"linha numero {i} do assistente")
        self.assertEqual(len(g._falas), 2)

    def test_matches_is_cheap(self):
        g = echo_text.EchoTextGuard()
        for i in range(8):
            g.note("linha número %d do assistente, com palavras suficientes "
                   "para serem comparadas de verdade" % i)
        inicio = time.perf_counter()
        for _ in range(50):
            g.matches("linha número sete do assistente com palavras suficientes")
        ms = (time.perf_counter() - inicio) / 50 * 1000
        self.assertLess(ms, 5.0, f"guarda de eco custou {ms:.2f} ms por turno")


class QuotedRepeatTest(unittest.TestCase):
    """Citação de instrução: o dono repetir o que nós citamos não é eco."""

    HINT = ('Diz no microfone "Gino, cadastra minha voz" que eu abro o '
            'roteiro das cinco frases, uma por vez, e fico quieto enquanto '
            'você fala.')

    def test_owner_repeating_our_quoted_trigger(self):
        g = _guard(self.HINT)
        # O guarda de eco continua vendo a semelhança (a fala do dono É um
        # trecho da nossa); quem separa os dois é quoted_repeat.
        self.assertTrue(g.matches("Gino, cadastra minha voz"))
        self.assertTrue(g.quoted_repeat("Gino, cadastra minha voz"))

    def test_transcriber_error_still_counts_as_the_quoted_span(self):
        g = _guard(self.HINT)
        self.assertTrue(g.quoted_repeat("Gino, cadastre minha voz"))

    def test_echo_of_the_whole_line_is_not_a_quoted_repeat(self):
        """O eco devolve a frase inteira, com o "diz" e o resto."""
        g = _guard(self.HINT)
        voltou = ("diz no microfone gino cadastra minha voz que eu abro o "
                  "roteiro das cinco frases")
        self.assertTrue(g.matches(voltou))
        self.assertFalse(g.quoted_repeat(voltou))

    def test_a_long_quoted_passage_is_never_exempt(self):
        longo = ("A regra do roteador local é simples e foi escrita para "
                 "nunca depender da nuvem em decisão de ferramenta")
        g = _guard(f'O dono escreveu: "{longo}".')
        self.assertFalse(g.quoted_repeat(longo))

    def test_expired_quote_does_not_exempt_anything(self):
        g = echo_text.EchoTextGuard()
        g.note(self.HINT, when=time.monotonic() - 600.0)
        self.assertFalse(g.quoted_repeat("Gino, cadastra minha voz"))

    def test_text_without_quotes_is_not_a_quoted_repeat(self):
        g = _guard("Fala comigo cadastra minha voz que eu abro as cinco "
                   "frases, uma por vez, sem pressa.")
        self.assertTrue(g.matches("Gino, cadastra minha voz"))
        self.assertFalse(g.quoted_repeat("Gino, cadastra minha voz"))


class EnrollCommandRecognitionTest(unittest.TestCase):
    """A fala É o pedido de cadastro, não uma frase que apenas o menciona."""

    def test_the_command_alone_is_recognized(self):
        for line in ("cadastra minha voz",
                     "Gino, cadastra minha voz",
                     "quero cadastrar minha voz",
                     "pode cadastrar minha voz do celular"):
            self.assertTrue(main._is_enroll_command(line), line)

    def test_a_sentence_that_contains_the_command_is_not(self):
        for line in (
                'Diz no microfone "Gino, cadastra minha voz" que eu abro o '
                'roteiro das cinco frases, uma por vez, e fico quieto.',
                "Anthero, eu ainda tenho poucas amostras da sua voz. Diga "
                "'cadastra minha voz' quando quiser que eu faça como o Ok "
                "Google e peça cinco frases.",
                "diga cadastra minha voz",
                "implemente uma ferramenta para registrar minha voz",
                "Não consegui entender o que você falou"):
            self.assertFalse(main._is_enroll_command(line), line)


class _FakeUI:
    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, text):
        self.logs.append(str(text))


class GhostTurnIntegrationTest(unittest.TestCase):
    """As duas saídas do fantasma em main.py: turno descartado e boca calada."""

    def test_echo_transcript_never_becomes_the_owner_turn(self):
        obj = object.__new__(main.JarvisLive)
        obj._loop = object()
        obj.session = object()
        falado = "Não consegui entender o que você falou — repete, por favor?"
        obj._echo_text = _guard(falado)
        # Se a guarda falhar, _on_groq_text segue e explode por falta de
        # atributos: o teste vira erro, não falso positivo.
        obj._on_groq_text("Não consegui entender o que você falou, repete por favor?",
                          pcm=b"\x00\x00" * 100)
        self.assertFalse(hasattr(obj, "_current_pcm"),
                         "o eco virou PCM do dono e abriu um turno")

    def test_echo_window_silences_the_repeat_request(self):
        obj = object.__new__(main.JarvisLive)
        obj.ui = _FakeUI()
        obj.said: list[str] = []
        obj._say_now = lambda text, log=True: obj.said.append(text)
        obj._awake = True
        obj._echo_text = _guard("Não consegui entender o que você falou")
        obj._stt_lost_audio("audio", "o STT não reconheceu nada", 3.0)
        self.assertEqual(len(obj.ui.logs), 1, "a perda precisa ficar registrada")
        self.assertEqual(obj.said, [], "o eco virou reclamação falada")

    def test_owner_speech_outside_the_echo_window_still_gets_an_answer(self):
        obj = object.__new__(main.JarvisLive)
        obj.ui = _FakeUI()
        obj.said: list[str] = []
        obj._say_now = lambda text, log=True: obj.said.append(text)
        obj._awake = True
        obj._echo_text = echo_text.EchoTextGuard()   # nada falado por nós
        obj._stt_lost_audio("audio", "o STT não reconheceu nada", 2.0)
        self.assertEqual(len(obj.said), 1,
                         "sem eco, a regra do dono continua: ele é avisado")

    def test_silence_while_the_reply_is_still_playing(self):
        """Mesmo sem guarda preenchida (assistente recém-reiniciado)."""
        obj = object.__new__(main.JarvisLive)
        obj.ui = _FakeUI()
        obj.said: list[str] = []
        obj._say_now = lambda text, log=True: obj.said.append(text)
        obj._awake = True
        obj._echo_text = echo_text.EchoTextGuard()
        obj._is_speaking = True
        obj._stt_lost_audio("audio", "o STT não reconheceu nada", 3.0)
        self.assertEqual(obj.said, [])

    def test_silence_seconds_after_the_reply_ended(self):
        obj = object.__new__(main.JarvisLive)
        obj.ui = _FakeUI()
        obj.said: list[str] = []
        obj._say_now = lambda text, log=True: obj.said.append(text)
        obj._awake = True
        obj._echo_text = echo_text.EchoTextGuard()
        obj._is_speaking = False
        obj._reply_ended_at = time.monotonic() - 1.0
        obj._stt_lost_audio("audio", "o STT não reconheceu nada", 4.0)
        self.assertEqual(obj.said, [])


class EnrollFlowTest(unittest.TestCase):
    """04/11/2026, 12:43: o assistente cita o gatilho e o dono repete.

    O guarda de eco descartou o pedido (0.90 contra limiar 0.62) e o cadastro
    nunca abriu. O turno precisa atravessar o guarda e chegar ao portão real
    do cadastro de voz.
    """

    HINT = ('Diz no microfone "Gino, cadastra minha voz" que eu abro o '
            'roteiro das cinco frases, uma por vez, e fico quieto enquanto '
            'você fala.')
    PCM = b"\x01\x00" * 16000

    def _obj(self, *falas: str):
        obj = object.__new__(main.JarvisLive)
        obj._loop = object()
        obj.session = object()
        obj._echo_text = _guard(*falas)
        obj.ui = _FakeUI()
        obj._voice_train = None
        obj._voice_train_expected = lambda text: False
        # Tudo entre o guarda de eco e o portão do cadastro é stub; o portão
        # (main.JarvisLive._voice_train_gate) roda de verdade.
        obj._restart_answer_expected = lambda text: False
        obj._mail_send_answer_expected = lambda text: False
        obj._owner_direct_action_gate = lambda text, pcm: False
        obj._power = None
        obj._device_enroll_cancel_gate = lambda text, pcm: False
        obj._device_attest_gate = lambda text, pcm: False
        obj._device_enroll_gate = lambda text, pcm: False
        obj._note_owner_speech = lambda why="": None
        obj._voice_id_enabled = True
        obj._speaker = object()
        obj._cinema_active_now = lambda: False
        obj._voice_train_owner_ok = lambda text, pcm: (
            True, 0.88, "voz=Anthero")
        obj._log_heard = lambda text: None
        obj.opened: list[str] = []
        obj._start_voice_training = lambda why="": obj.opened.append(why)
        return obj

    def test_owner_repeating_the_quoted_trigger_opens_enrollment(self):
        obj = self._obj(self.HINT)
        self.assertTrue(obj._echo_text.matches("Gino, cadastra minha voz"),
                        "o teste precisa do caso real: semelhança acima do limiar")
        obj._on_groq_text("Gino, cadastra minha voz", pcm=self.PCM)
        self.assertEqual(obj.opened, ["pedido do dono"],
                         "o comando que nós mesmos citamos virou eco")

    def test_unquoted_command_is_not_swallowed_either(self):
        obj = self._obj("Fala comigo cadastra minha voz que eu abro as cinco "
                        "frases, uma por vez, sem pressa.")
        obj._on_groq_text("Gino, cadastra minha voz", pcm=self.PCM)
        self.assertEqual(obj.opened, ["pedido do dono"])

    def test_echo_of_the_whole_hint_still_never_opens_enrollment(self):
        obj = self._obj(self.HINT)
        obj._on_groq_text(
            "Diz no microfone Gino cadastra minha voz que eu abro o roteiro "
            "das cinco frases uma por vez e fico quieto enquanto você fala",
            pcm=self.PCM)
        self.assertEqual(obj.opened, [], "eco real abriu o cadastro")
        self.assertFalse(hasattr(obj, "_current_pcm"),
                         "o eco virou PCM do dono e abriu um turno")


if __name__ == "__main__":
    unittest.main()
