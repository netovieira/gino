"""Cadastro guiado de voz: amostras contam, perfil ausente não rejeita a voz
do dono e um cancelamento com zero amostras loga o MOTIVO legível (04/11: duas
matrículas fecharam "60s sem resposta; nenhuma amostra foi gravada" sem dizer
o que cortou o áudio)."""
from __future__ import annotations

import time
import unittest

import main


class FakeSpeaker:
    owner_name = "Anthero"
    sample_rate = 16000

    def __init__(self):
        self.replaced = []
        self.records = [{"name": "Anthero", "owner": True,
                         "authorized": True, "samples": 3}]

    def list(self):
        return [dict(rec) for rec in self.records]

    def embedding(self, _pcm):
        return [1.0, 0.0]

    def replace_samples(self, name, sample, authorized=False, device="pc"):
        self.replaced.append((name, list(sample), device))
        self.records[0]["samples"] = len(sample)
        return dict(self.records[0])


class FakeSTT:
    """Double do ouvinte: devolve o mesmo formato de voice_gate_stats()."""

    def __init__(self, blocks=40, dropped=12):
        self.blocks = blocks
        self.dropped = dropped

    def set_enrollment(self, _enabled):
        pass

    def voice_gate_stats(self):
        return {"blocks": self.blocks, "clips_dropped": self.dropped,
                "by_reason": {"silencio": self.dropped, "banda": 0}}


class FakeUI:
    def __init__(self):
        self.lines = []

    def write_log(self, text):
        self.lines.append(text)


def _obj():
    obj = object.__new__(main.JarvisLive)
    obj._voice_id_enabled = True
    obj._speaker = FakeSpeaker()
    obj._voice_train = None
    obj._voice_train_offered = False
    obj._cinema_mode_enabled = False
    obj._cinema_active = False
    obj._cinema_loaded = True
    obj.ui = FakeUI()
    obj.said = []
    obj.heard = []
    obj._say_now = lambda text, log=True: obj.said.append(text)
    obj._log_heard = lambda text: obj.heard.append(text)
    obj._owner_speaker_confirmed = lambda _t, _p: (True, 0.88, "voz=Anthero")
    obj._active_mic_key = "c920"
    obj._groq = FakeSTT()
    return obj


def _sample(seconds=1.0):
    return b"\x01\x00" * int(16000 * seconds)


class VoiceEnrollSamplesTest(unittest.TestCase):
    def test_phrase_above_gate_becomes_counted_sample(self):
        """(a) Fala do dono acima do portão vira amostra contada, com o
        diagnóstico do que o microfone viu na linha de aceite."""
        obj = _obj()
        self.assertTrue(main.JarvisLive._voice_train_gate(
            obj, "cadastra minha voz", _sample()))
        self.assertIsNotNone(obj._voice_train)
        self.assertTrue(main.JarvisLive._voice_train_gate(
            obj, main._VOICE_TRAIN_PROMPTS[0], _sample()))
        self.assertEqual(len(obj._voice_train["samples"]), 1)
        self.assertEqual(int(obj._voice_train["done"]), 1)
        aceita = [ln for ln in obj.ui.lines if "aceita" in ln]
        self.assertTrue(aceita)
        # blocos vistos e cortes por motivo legível acompanham o aceite
        self.assertIn("blocos=40", aceita[0])
        self.assertIn("cortados=12", aceita[0])
        self.assertIn("silencio", aceita[0])
        self.assertIn("microfone=c920", aceita[0])

    def test_mic_without_saved_profile_still_accumulates(self):
        """(b) Microfone sem perfil salvo não rejeita a voz: o pedido do dono
        abre o fluxo e as frases acumulam amostras."""
        obj = _obj()
        obj._owner_speaker_confirmed = lambda _t, _p: (
            False, 0.0, "sem cadastro de voz neste microfone (perfil=edifier)")
        self.assertTrue(main.JarvisLive._voice_train_gate(
            obj, "cadastra minha voz", _sample()))
        self.assertIsNotNone(obj._voice_train)
        self.assertTrue(any("iniciando sem perfil" in ln for ln in obj.ui.lines))
        self.assertTrue(main.JarvisLive._voice_train_gate(
            obj, main._VOICE_TRAIN_PROMPTS[0], _sample()))
        self.assertEqual(len(obj._voice_train["samples"]), 1)

    def test_other_voice_rejection_still_blocks_the_trigger(self):
        """Perfil ausente é a única exceção: uma voz de TV continua fora."""
        obj = _obj()
        obj._owner_speaker_confirmed = lambda _t, _p: (False, 0.19, "voz=TV")
        self.assertFalse(main.JarvisLive._voice_train_gate(
            obj, "cadastra minha voz", _sample()))
        self.assertIsNone(obj._voice_train)

    def test_zero_sample_cancel_logs_the_reason(self):
        """(c) Cancelar com zero amostras loga amostras, blocos e motivo do
        portão em vez de sumir em silêncio."""
        obj = _obj()
        self.assertTrue(main.JarvisLive._voice_train_gate(
            obj, "cadastra minha voz", _sample()))
        obj._voice_train["last_activity"] = time.monotonic() - 120.0
        self.assertTrue(main.JarvisLive._voice_train_check_timeout(obj))
        self.assertIsNone(obj._voice_train)
        cancel = [ln for ln in obj.ui.lines if "cancelado por inatividade" in ln]
        self.assertTrue(cancel)
        self.assertIn("amostras=0/5", cancel[0])
        self.assertIn("blocos=40", cancel[0])
        self.assertIn("silencio", cancel[0])
        self.assertIn("microfone=c920", cancel[0])

    def test_rejected_attempt_logs_reason_and_blocks(self):
        """Tentativa rejeitada tem linha com o motivo e os blocos vistos."""
        obj = _obj()
        self.assertTrue(main.JarvisLive._voice_train_gate(
            obj, "cadastra minha voz", _sample()))
        self.assertTrue(main.JarvisLive._voice_train_gate(
            obj, "uma frase qualquer totalmente diferente", _sample()))
        recusada = [ln for ln in obj.ui.lines if "tentativa recusada" in ln]
        self.assertTrue(recusada)
        self.assertIn("blocos=40", recusada[0])
        self.assertIn("microfone=c920", recusada[0])


if __name__ == "__main__":
    unittest.main()
