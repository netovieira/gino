"""Gatilho de cadastro pela faixa cinzenta + eco que exige sobreposição real.

Mundo real (activity.log 09:30/09:31): o dono foi rejeitado no gatilho do
cadastro (score 0.46, AS-norm -1.17 contra piso 0.00 — perfil contaminado por
eco) e a frase seguinte foi descartada como "eco do meu TTS" sem nenhuma fala
minha correspondente. Fix A: o gatilho do CADASTRO (só ele) aceita a faixa
cinzenta do microfone já conhecido como do dono, e as 5 frases precisam bater
entre si antes de substituir o perfil. Fix B: eco exige sobreposição de texto
com fala minha ativa/recentíssima.
"""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

import main
from core import echo_text


PCM = b"\x01\x00" * 13000   # ~0.81 s @16 kHz: suficiente p/ o passo do cadastro


def _phrase_pcm(index: int) -> bytes:
    """Clip whose first byte selects the embedding the fake speaker returns."""
    return bytes([index]) + b"\x01\x00" * 13000


class GraySpeaker:
    """SpeakerId double: known/authorized owner match with a controllable score."""

    owner_name = "Anthero"
    sample_rate = 16000

    def __init__(self, score=0.46, known=True, name="Anthero", authorized=True,
                 vectors=None):
        self.score = score
        self.known = known
        self.name = name
        self.authorized = authorized
        self.vectors = vectors or {}
        self.replaced: list = []
        self.enrolled: list = []
        self.records = [{"name": "Anthero", "owner": True, "authorized": True,
                         "samples": 3}]

    def list(self):
        return [dict(rec) for rec in self.records]

    def has_profile_for(self, key):
        return self.known

    def embedding(self, pcm):
        if pcm and pcm[0] in self.vectors:
            return list(self.vectors[pcm[0]])
        return [1.0, 0.0, 0.0]

    def identify(self, embedding, **kwargs):
        return {
            "name": self.name if self.known else None,
            "score": self.score,
            "sample_score": self.score,
            "effective_score": self.score,
            "known": self.known,
            "authorized": self.authorized and self.known,
            "verdict": "known" if self.known else "unknown",
            "device": "c920",
        }

    def replace_samples(self, name, samples, authorized=False, device="pc"):
        self.replaced.append((name, list(samples), authorized))
        self.records[0]["samples"] = len(samples)
        return {"samples": len(samples), "backup": ""}

    def enroll(self, name, samples, authorized=False, device="pc"):
        self.enrolled.append((name, list(samples), authorized))
        self.records[0]["samples"] = len(samples)
        return {"samples": len(samples)}


class FakeUI:
    muted = False

    def __init__(self):
        self.lines: list[str] = []

    def write_log(self, text):
        self.lines.append(text)


def _obj(speaker, confirmed=(False, 0.46,
                             "voz não corresponde ao dono: AS-norm -1.17 vs 0.00"),
         decision=None):
    """JarvisLive mínima: o portão real roda; o voiceprint é o caso podre."""
    obj = object.__new__(main.JarvisLive)
    obj._voice_id_enabled = True
    obj._speaker = speaker
    obj._voice_train = None
    obj._voice_train_offered = False
    obj._voice_train_gray_open = False
    obj._cinema_mode_enabled = False
    obj._cinema_active = False
    obj._cinema_loaded = True
    obj.ui = FakeUI()
    obj.said: list[str] = []
    obj.heard: list[str] = []
    obj._say_now = lambda text, **kw: obj.said.append(text)
    obj._log_heard = lambda text: obj.heard.append(text)
    obj._owner_speaker_confirmed = lambda text, pcm, **kw: tuple(confirmed)
    obj._owner_decision_for_match = lambda match, **kw: decision
    obj._voice_indulgence_gray_floor = main.DEFAULT_VOICE_INDULGENCE_GRAY_FLOOR
    obj._groq = None
    obj._awake = True
    obj._last_user_speech = 1.0
    obj._voice_bootstrap_until = 0.0
    obj._voice_bootstrap_by = ""
    # NOTA: sem _is_speaking/_tail_until aqui — sem esses atributos o
    # prompt-guard do cadastro trata o objeto como teste-duplo e as frases
    # avançam (ver _voice_train_prompt_active). Os testes de eco abaixo
    # colocam o relógio de fala quando precisam dele.
    return obj


def _ok_decision(**over):
    data = dict(profile_state="ok", can_answer=True, acoustic="",
                score=0.46, device="c920")
    data.update(over)
    return SimpleNamespace(**data)


COHERENT_VECTORS = {
    0: [1.0, 0.0, 0.0],
    1: [0.98, 0.18, 0.0],
    2: [0.97, 0.05, 0.2],
    3: [0.99, 0.1, 0.1],
    4: [0.95, 0.2, 0.15],
}
DISCREPANT_VECTORS = {
    0: [1.0, 0.0, 0.0],
    1: [0.99, 0.1, 0.0],
    2: [0.98, 0.0, 0.15],
    3: [0.0, 1.0, 0.0],   # outra voz no meio do trecho
    4: [0.0, 0.0, 1.0],
}


class GrayTriggerTest(unittest.TestCase):
    """(a) abre | (b) fora da cinza/desconhecido/terceiro recusa | (c) nada mais."""

    def test_reproved_score_inside_the_gray_band_on_known_mic_opens(self):
        obj = _obj(GraySpeaker(score=0.46))
        self.assertTrue(main.JarvisLive._voice_train_gate(
            obj, "cadastra minha voz", PCM))
        self.assertIsNotNone(obj._voice_train)
        self.assertTrue(obj._voice_train.get("gray_trigger"))
        line = [l for l in obj.ui.lines if "entrou por faixa cinzenta" in l]
        self.assertTrue(line, obj.ui.lines)
        self.assertIn("score=0.46", line[0])

    def test_score_below_the_gray_band_is_still_refused(self):
        obj = _obj(GraySpeaker(score=0.20))
        self.assertFalse(main.JarvisLive._voice_train_gate(
            obj, "cadastra minha voz", PCM))
        self.assertIsNone(obj._voice_train)
        self.assertTrue(any("não confirmada como dono" in l for l in obj.ui.lines))

    def test_unknown_mic_is_still_refused(self):
        obj = _obj(GraySpeaker(score=0.46, known=False, name=None))
        self.assertFalse(main.JarvisLive._voice_train_gate(
            obj, "cadastra minha voz", PCM))
        self.assertIsNone(obj._voice_train)

    def test_known_third_voice_is_still_refused(self):
        obj = _obj(GraySpeaker(score=0.90, name="Alice"))
        self.assertFalse(main.JarvisLive._voice_train_gate(
            obj, "cadastra minha voz", PCM))
        self.assertIsNone(obj._voice_train)

    def test_diffuse_room_acoustic_is_still_refused(self):
        obj = _obj(GraySpeaker(score=0.46),
                   decision=_ok_decision(acoustic="diffuse"))
        self.assertFalse(main.JarvisLive._voice_train_gate(
            obj, "cadastra minha voz", PCM))
        self.assertIsNone(obj._voice_train)

    def test_gray_acceptance_grants_no_other_privileged_action(self):
        obj = _obj(GraySpeaker(score=0.46))
        self.assertTrue(main.JarvisLive._voice_train_gate(
            obj, "cadastra minha voz", PCM))
        # Nenhuma janela de bootstrap foi armada pela cinza.
        self.assertFalse(main.JarvisLive._voice_bootstrap_active(obj))
        self.assertEqual(obj._voice_bootstrap_until, 0.0)
        # A flag do gatilho foi consumida pela sessão.
        self.assertFalse(obj._voice_train_gray_open)
        # Toda ação privilegiada continua passando por _owner_speaker_confirmed,
        # que segue recusando o MESMO clipe (sudo, envio, energia, leitura).
        confirmed, _score, _detail = obj._owner_speaker_confirmed(
            "pode usar sudo", PCM)
        self.assertFalse(confirmed)
        # Estrutural: o caminho cinza só existe dentro de _voice_train_owner_ok,
        # chamado apenas pelo gatilho de cadastro.
        src = main.__dict__["JarvisLive"].__dict__
        self.assertNotIn("_voice_train_gray_ok", src["_try_action_consent"].__code__.co_names)
        self.assertNotIn("_voice_train_gray_ok",
                         src["_owner_direct_action_gate"].__code__.co_names)

    def test_trigger_regex_still_gates_the_gray_path(self):
        # "registre minha voz" é cadastro; "fecha isso" não — a cinza só vale
        # para a regex explícita de cadastro.
        obj = _obj(GraySpeaker(score=0.46))
        self.assertFalse(main.JarvisLive._voice_train_gate(
            obj, "fecha isso", PCM))
        self.assertIsNone(obj._voice_train)


class SessionCoherenceTest(unittest.TestCase):
    """(d) 5 amostras autoconsistentes substituem; discrepantes são recusadas."""

    def _run_five(self, vectors, gray=False):
        if gray:
            obj = _obj(GraySpeaker(score=0.46, vectors=vectors))
            self.assertTrue(main.JarvisLive._voice_train_gate(
                obj, "cadastra minha voz", PCM))
        else:
            obj = _obj(GraySpeaker(vectors=vectors),
                       confirmed=(True, 0.88, "voz=Anthero"))
            self.assertTrue(main.JarvisLive._voice_train_gate(
                obj, "cadastra minha voz", PCM))
        for i, prompt in enumerate(main._VOICE_TRAIN_PROMPTS):
            self.assertTrue(main.JarvisLive._voice_train_gate(
                obj, prompt, _phrase_pcm(i)), prompt)
        self.assertIsNone(obj._voice_train)
        return obj

    def test_five_consistent_samples_replace_the_old_profile(self):
        obj = self._run_five(COHERENT_VECTORS)
        self.assertEqual(len(obj._speaker.replaced), 1)
        self.assertEqual(len(obj._speaker.replaced[0][1]), 5)
        self.assertTrue(obj._speaker.replaced[0][2])
        self.assertTrue(any("Substituí" in s for s in obj.said), obj.said)

    def test_five_consistent_samples_via_gray_announce_the_swap(self):
        obj = self._run_five(COHERENT_VECTORS, gray=True)
        self.assertEqual(len(obj._speaker.replaced), 1)
        self.assertTrue(any("perfil antigo estava contaminado" in s
                            for s in obj.said), obj.said)
        self.assertTrue(any("concluído por faixa cinzenta" in l
                            for l in obj.ui.lines), obj.ui.lines)

    def test_discrepant_samples_are_refused_with_a_reason(self):
        obj = self._run_five(DISCREPANT_VECTORS, gray=True)
        self.assertEqual(obj._speaker.replaced, [])
        self.assertEqual(obj._speaker.records[0]["samples"], 3)
        self.assertTrue(any("não batem entre si" in l for l in obj.ui.lines),
                        obj.ui.lines)
        self.assertTrue(any("não bateram entre si" in s for s in obj.said),
                        obj.said)


class EchoNeedsRealOverlapTest(unittest.TestCase):
    """B: eco só quando o texto bate com uma fala minha ativa/recentíssima."""

    def _asleep(self, guard):
        obj = _obj(GraySpeaker(score=0.46, name=None, known=False))
        obj._awake = False
        obj._echo_text = guard
        obj._asleep_owner_speech = []
        return obj

    def _tail_of_the_assistant(self):
        g = echo_text.EchoTextGuard()
        g.note("Vou cadastrar a sua voz em cinco frases curtas, uma por vez.")
        return g

    def test_identical_to_my_last_line_is_still_echo(self):
        g = self._tail_of_the_assistant()
        obj = self._asleep(g)
        obj._is_speaking = True
        phrase = "Vou cadastrar a sua voz em cinco frases curtas uma por vez"
        self.assertFalse(obj._asleep_utterance_wakes(phrase, PCM))
        echo_lines = [l for l in obj.ui.lines if "eco do meu TTS" in l]
        self.assertTrue(echo_lines, obj.ui.lines)
        self.assertIn("eco por texto sobreposto", echo_lines[0])

    def test_enroll_phrase_without_any_of_my_speech_is_not_echo(self):
        g = echo_text.EchoTextGuard()
        g.note("Já te digo, criador")           # 09:31:08: nada sobre "cadastrai"
        obj = self._asleep(g)
        obj._tail_until = time.monotonic() + 5.0   # janela de cauda aberta
        obj._owner_speaker_confirmed = lambda text, pcm, **kw: (
            True, 0.41, "indulgência (faixa cinzenta, perfil=c920)")
        self.assertFalse(obj._asleep_utterance_wakes("Cadastrai minha voz.", PCM))
        self.assertFalse([l for l in obj.ui.lines if "eco do meu TTS" in l],
                         obj.ui.lines)
        # Seguiu para o portão normal (não endereçada, dormindo) em vez de
        # ser rotulada de eco:
        self.assertTrue(any("não endereçada" in l for l in obj.ui.lines),
                        obj.ui.lines)

    def test_enroll_command_reaches_the_gate_while_echo_window_open(self):
        """Fluxo real: com a janela aberta e nenhuma fala minha correspondente,
        o pedido de cadastro chega ao portão e abre o roteiro."""
        obj = _obj(GraySpeaker(score=0.46))
        obj._loop = object()
        obj.session = object()
        obj._awake = False
        obj._wake_enabled = True
        obj._asst_name = "Gino"
        obj._tail_until = time.monotonic() + 5.0
        obj._echo_text = self._tail_of_the_assistant()
        obj._voice_bootstrap_gate = lambda *a, **k: False
        obj._restart_answer_expected = lambda text: False
        obj._mail_send_answer_expected = lambda text: False
        obj._owner_direct_action_gate = lambda text, pcm: False
        obj._power = None
        obj._device_enroll_cancel_gate = lambda text, pcm: False
        obj._device_attest_gate = lambda text, pcm: False
        obj._device_enroll_gate = lambda text, pcm: False
        obj._note_owner_speech = lambda why="": None
        obj._is_speaking = False
        obj._stt_lost_audio = lambda *a, **k: False
        obj._next_turn_ref = lambda chan: None
        obj._note_turn_owner_text = lambda text: None
        obj._mic_status_gate = lambda text, pcm: False
        obj._cinema_mode_gate = lambda text, pcm: False
        obj._silence_mode_gate = lambda text, pcm: False
        obj._is_blocked_phrase = lambda text: None
        obj._reply_active = False
        main.JarvisLive._on_groq_text(obj, "cadastra minha voz", PCM)
        self.assertIsNotNone(obj._voice_train, obj.ui.lines)
        self.assertTrue(obj._voice_train.get("gray_trigger"))
        self.assertFalse([l for l in obj.ui.lines if "eco do meu TTS" in l])


if __name__ == "__main__":
    unittest.main()
