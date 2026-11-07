"""Mute da interface = SOMENTE o microfone (ordem do dono, 07/11/2026).

O botão/F4 de mute fechava tudo: captura E voz. O dono quer o mute só como
"feche o meu microfone"; a fala do assistente continua saindo. Os elos que
calavam a voz (`ui.muted` em `_say_blocking`, o descarte de chunks no
`_play_audio`, e os portões de aviso/e-mail/Qoder/OTP/sono/narração) foram
derrubados. O único caminho que cala a fala é o modo silêncio
(`_speech_route`) e o controle de volume — intocados aqui.

Invariantes fixadas:
  * mute ligado → captura/STT inibida (o portão do microfone continua lá);
  * mute ligado → TTS/voz NÃO inibida (sintetiza; sem descarte mudo no
    playback; sem os antigos avisos "microfone mutado — ... não falou");
  * mute desligado → ambos liberados;
  * modo silêncio continua retendo fala (não é o mute da UI);
  * a boca animada segue a fala mesmo com o microfone mutado.
"""
from __future__ import annotations

import inspect
import unittest
from unittest import mock

import main


def _obj(muted: bool, route: dict | None = None):
    lines: list[str] = []

    class UI:
        def __init__(self):
            self.muted = muted
            self.state = ""

        def write_log(self, text):
            lines.append(str(text))

        def set_state(self, state):
            self.state = state

    obj = object.__new__(main.JarvisLive)
    obj.ui = UI()
    obj._asst_name = "Gino"
    obj._awake = True
    obj._phone_active = False
    obj._session_log: list[str] = []
    obj._turn_ref = 3
    obj._resolve_say_style = lambda origin, ref: ""
    obj._note_speech = lambda *a, **k: 0
    obj._speech_route = lambda *a, **k: dict(
        route or {"muted": False, "subject": ""})
    obj._voice_train_note_speech = lambda *a, **k: 0
    obj._speaker_names = lambda labels: set()
    obj.retained: list[str] = []
    obj._retain_spoken = lambda text, **k: obj.retained.append(text)
    return obj, lines


class SayWhileMutedTests(unittest.TestCase):
    """mute ligado → a boca continua trabalhando."""

    def _run_say(self, obj, text, **kw):
        calls: list[str] = []

        def _fake_synth(unit_text, _st, **_kw):
            calls.append(str(unit_text))
            return b""

        with mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.brain.synth_pcm24k", side_effect=_fake_synth):
            main.JarvisLive._say_blocking(obj, text, **kw)
        return calls

    def test_muted_say_still_synthesises(self):
        obj, lines = _obj(muted=True)
        calls = self._run_say(obj, "Bom dia, chefe.", log=True, origin="aviso")
        self.assertTrue(calls, "mute da UI não pode inibir a síntese")
        self.assertEqual(obj.retained, [])
        self.assertFalse(any("não falou" in l for l in lines))
        self.assertFalse(any("microfone mutado" in l for l in lines))

    def test_unmuted_say_synthesises_too(self):
        obj, lines = _obj(muted=False)
        calls = self._run_say(obj, "Boa noite.", log=True, origin="aviso")
        self.assertTrue(calls)

    def test_silence_gate_still_retains(self):
        # O portão de fala é o silêncio — não o mute da interface.
        obj, lines = _obj(muted=False, route={"muted": True,
                                              "subject": "TheroTV"})
        calls = self._run_say(obj, "Fala cortada pelo silêncio.", log=True,
                              origin="aviso")
        self.assertEqual(calls, [], "silêncio não pode sintetizar")
        self.assertEqual(obj.retained, ["Fala cortada pelo silêncio."])


class PlaybackDoesNotDropOnMuteTests(unittest.TestCase):
    """O worker de reprodução não conhece mais o mute da UI."""

    def test_play_audio_has_no_ui_mute_gate(self):
        src = inspect.getsource(main.JarvisLive._play_audio)
        self.assertNotIn("ui.muted", src,
                         "o playback voltou a descartar fala no mute")

    def test_say_blocking_has_no_ui_mute_gate(self):
        src = inspect.getsource(main.JarvisLive._say_blocking)
        self.assertNotIn("ui.muted", src,
                         "o portão de mute voltou a viver na boca")


class MicStaysClosedWhileMutedTests(unittest.TestCase):
    """mute ligado → captura/STT inibidos: o portão continua no microfone."""

    def test_listen_audio_still_gates_the_mic_on_mute(self):
        src = inspect.getsource(main.JarvisLive._listen_audio)
        self.assertIn("if self.ui.muted:", src,
                      "o mute deixou de fechar o microfone")
        self.assertIn("mic_open = (not self.ui.muted", src,
                      "o portão de abertura do microfone sumiu")

    def test_audio_audit_closed_follows_the_mic_mute(self):
        obj, _lines = _obj(muted=True)
        self.assertTrue(main.JarvisLive._audio_audit_closed(obj))
        obj, _lines = _obj(muted=False)
        self.assertFalse(main.JarvisLive._audio_audit_closed(obj))

    def test_outbound_lines_ignore_the_ui_mute(self):
        # Nenhum caminho de fala consulta mais `ui.muted`: nem a retenção do
        # aviso de e-mail (`mic_muted` sumiu), nem OTP, nem sono, nem o
        # "pensando" do sidecar, nem a retenção do Qoder, nem o gancho do
        # WhatsApp do dono.
        src = inspect.getsource(main)
        self.assertNotIn("mic_muted", src,
                         "a retenção de e-mail voltou a ler o mute da UI")
        for name in ("_announce_otp", "_sleep_announce", "_qoder_on_thinking",
                     "_deliver_qoder_event", "_whatsapp_owner_wake",
                     "_drain_whatsapp_owner_wake"):
            fn_src = inspect.getsource(getattr(main.JarvisLive, name))
            self.assertNotIn("ui.muted", fn_src,
                             f"{name} voltou a suprimir fala pelo mute da UI")


class MouthAnimatesWhileMutedTests(unittest.TestCase):
    """A boca animada é a FALA, não o microfone."""

    def test_avatar_mouth_moves_while_muted_and_speaking(self):
        from core.avatar import HoloAvatar
        av = HoloAvatar()
        av.step(0.05, 0.0, speaking=False, muted=True)     # assenta o resto
        opened = 0.0
        for _ in range(8):
            av.step(0.05, 0.8, speaking=True, muted=True,
                    v_open=1.0, v_level=0.9)
            opened = max(opened, av._mouth)
        self.assertGreater(opened, 0.05,
                           "com o mute da UI a boca parou de acompanhar a voz")

    def test_avatar_mouth_rests_while_muted_and_silent(self):
        from core.avatar import HoloAvatar
        av = HoloAvatar()
        for _ in range(30):
            av.step(0.05, 0.0, speaking=False, muted=True, v_open=0.0,
                    v_level=0.0)
        self.assertLess(av._mouth, 0.02,
                        "sem fala o descanso da boca não depende do mute")


if __name__ == "__main__":
    unittest.main()
