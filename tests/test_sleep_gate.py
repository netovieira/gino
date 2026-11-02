"""FIX 1: the false sleep and the silently dropped owner utterance.

The 01/11 audit found "SYS: Dormindo" firing 11–15 s after Gino's own speech
(19:44:42 spoke → 19:44:53 "Dormindo — nenhuma fala por 2 minutos"): the local
gates that `return True` early in _on_groq_text never refreshed
`_last_user_speech`, which is the ONLY thing _run_sleep_watch looks at. The
same audit counted 19 transcribed owner sentences discarded by the sleep gate
with nothing but `print("[Wake] asleep — not addressed to me ...")`.

These tests pin:
  * every early-return gate that consumed the utterance restarts the clock;
  * a discarded asleep utterance writes SYS to the activity log;
  * an address cue without the name ("para de me ignorar") wakes and answers;
  * the owner speaking 3x inside a minute wakes and answers;
  * a TV/unknown voice doing the same does not.
"""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

import main
from core import rootgate


class FakeUI:
    muted = True

    def __init__(self):
        self.logs: list[str] = []
        self.state = ""

    def write_log(self, text):
        self.logs.append(text)

    def set_state(self, state):
        self.state = state


def _bare(awake=True):
    """A JarvisLive with the loop/session only, so the gate chain can run."""
    obj = object.__new__(main.JarvisLive)
    obj._loop = object()
    obj.session = object()
    obj._groq = None
    obj._current_pcm = None
    obj._restart = None
    obj._power = None
    obj._voice_id_enabled = False
    obj._speaker = None
    obj._series_gate = None
    obj._cinema_mode_enabled = False
    obj._qoder_live_enabled = True          # skips the legacy local-gate table
    obj._qoder_live_ready = lambda: True
    obj._qoder_turn = lambda *a, **k: None
    obj._qoder_pending = None
    obj._awake = awake
    obj._wake_enabled = True
    obj._asst_name = "Gino"
    obj._is_speaking = False
    obj._reply_active = False
    obj._last_user_speech = 1.0
    obj._asleep_owner_speech = []
    obj._session_log = []
    obj._turn_ref = 0
    obj._begin_user_epoch = lambda: {}
    obj._memory_record_user = lambda text: None
    obj._index_event = lambda *a, **k: None
    obj._qoder_hold = lambda *a, **k: None
    obj.ui = FakeUI()
    obj.said: list[str] = []
    obj._say_now = lambda text, log=True: obj.said.append(text)
    return obj


PCM = b"\x01\x00" * 8000


class SleepClockRefreshedByGates(unittest.TestCase):
    """Each gate that answers and returns must move `_last_user_speech`."""

    def tearDown(self):
        rootgate.close()

    def _assert_refreshed(self, obj, text="ok"):
        self.assertGreater(obj._last_user_speech, 1.0,
                           f"o relógio de sono não foi renovado por «{text}»")

    def test_series_mode_gate(self):
        obj = _bare()
        obj._series_mode_gate = lambda *a, **k: True
        main.JarvisLive._on_groq_text(obj, "modo série", PCM)
        self._assert_refreshed(obj, "modo série")

    def test_cinema_mode_gate(self):
        obj = _bare()
        obj._cinema_mode_gate = lambda *a, **k: True
        main.JarvisLive._on_groq_text(obj, "modo cinema", PCM)
        self._assert_refreshed(obj, "modo cinema")

    def test_voice_train_gate(self):
        obj = _bare()
        obj._voice_train_gate = lambda *a, **k: True
        main.JarvisLive._on_groq_text(obj, "cadastra minha voz", PCM)
        self._assert_refreshed(obj, "cadastro de voz")

    def test_restart_confirmation(self):
        obj = _bare()
        obj._restart = SimpleNamespace(pending=lambda: True)
        obj._handle_restart_confirmation = lambda text, pcm: True
        main.JarvisLive._on_groq_text(obj, "confirmo reiniciar", PCM)
        self._assert_refreshed(obj, "reinício")

    def test_power_confirmation(self):
        obj = _bare()
        obj._power = SimpleNamespace(pending=lambda: True)
        obj._handle_power_confirmation = lambda text, pcm: True
        main.JarvisLive._on_groq_text(obj, "confirmo desligar", PCM)
        self._assert_refreshed(obj, "energia")

    def test_action_consent(self):
        obj = _bare()
        obj._try_action_consent = (
            lambda text, pcm=None: main.JarvisLive._note_owner_speech(obj, "x")
            or True)
        main.JarvisLive._on_groq_text(obj, "pode enviar", PCM)
        self._assert_refreshed(obj, "consentimento")

    def test_local_gate_table(self):
        obj = _bare()
        obj._qoder_live_enabled = False
        names = ("_remember_permanently", "_blocklist_local", "_music_gate",
                 "_youtube_local", "_apps_gate", "_screen_gate",
                 "_voice_enroll_gate", "_power_gate", "_restart_gate",
                 "_persona_gate", "_self_code_gate")
        for name in names:
            setattr(obj, name, lambda *a, **k: False)
        obj._remember_permanently = lambda text: True   # the gate that answers
        obj._chat_memory = None
        obj._memory_librarian = None
        main.JarvisLive._on_groq_text(obj, "lembra que eu gosto de café", PCM)
        self._assert_refreshed(obj, "memória")

    def test_blocked_phrase(self):
        obj = _bare()
        obj._is_blocked_phrase = lambda text: "palavra proibida"
        main.JarvisLive._on_groq_text(obj, "palavra proibida", PCM)
        self._assert_refreshed(obj, "frase bloqueada")
        self.assertTrue(any("Frase bloqueada" in line for line in obj.ui.logs))


class RootAuthorizationNeedsTheOwnerVoice(unittest.TestCase):
    """FIX 2: the sudo matcher answered a TV "pode" at 13:17:39."""

    def setUp(self):
        rootgate.close()
        self.addCleanup(rootgate.close)

    def test_owner_voice_opens_the_sudo_window_and_refreshes_the_clock(self):
        obj = _bare()
        obj._owner_speaker_confirmed = lambda text, pcm: (
            True, 0.71, "voz=Anthero")
        self.assertFalse(rootgate.authorized())
        main.JarvisLive._on_groq_text(obj, "pode usar sudo", PCM)
        self.assertTrue(rootgate.authorized())
        self.assertGreater(obj._last_user_speech, 1.0)
        self.assertIn("Autorizado.", obj.said)

    def test_unconfirmed_voice_never_opens_the_sudo_window(self):
        obj = _bare()
        obj._owner_speaker_confirmed = lambda text, pcm: (
            False, 0.11, "voz=desconhecida")
        main.JarvisLive._on_groq_text(obj, "pode usar sudo", PCM)
        self.assertFalse(rootgate.authorized())
        self.assertEqual(obj._last_user_speech, 1.0)
        self.assertTrue(any("consentimento ignorado" in line
                            for line in obj.ui.logs))


class AsleepDiscardsAreNeverSilent(unittest.TestCase):
    """The gate that drops a sentence while asleep must say so, and sometimes
    decide it was the owner calling anyway."""

    def _asleep(self):
        obj = _bare(awake=False)
        obj._owner_speaker_confirmed = lambda text, pcm: (
            False, 0.12, "voz=desconhecida")
        return obj

    def test_discard_writes_sys_line_with_the_transcript(self):
        obj = self._asleep()
        text = "Regina, eu não estou falando nada, é o som da TV."
        main.JarvisLive._on_groq_text(obj, text, PCM)
        self.assertFalse(obj._awake)
        line = [l for l in obj.ui.logs if "descartada" in l]
        self.assertTrue(line, obj.ui.logs)
        self.assertIn("SYS: dormindo — fala não endereçada descartada", line[0])
        self.assertIn("não estou falando nada", line[0])
        self.assertIn("você?", line[0])
        self.assertIn("score=0.12", line[0])
        # A dropped line is not owner speech: the clock does not move.
        self.assertEqual(obj._last_user_speech, 1.0)

    def test_address_cue_without_the_name_wakes_and_answers(self):
        # The exact discarded sentence from the audit.
        obj = self._asleep()
        calls: list[tuple] = []
        obj._qoder_turn = lambda text, pcm=None, **k: calls.append((text, pcm))
        main.JarvisLive._on_groq_text(obj, "Cara, para de me ignorar porra.", PCM)
        self.assertTrue(obj._awake)
        self.assertEqual(calls[0][0], "Cara, para de me ignorar porra.")
        self.assertGreater(obj._last_user_speech, 1.0)

    def test_owner_speaking_three_times_in_a_minute_wakes(self):
        obj = self._asleep()
        obj._owner_speaker_confirmed = lambda text, pcm: (
            True, 0.62, "voz=Anthero")
        self.assertFalse(obj._asleep_utterance_wakes("olha isso aqui", PCM))
        self.assertFalse(obj._asleep_utterance_wakes("e agora", PCM))
        self.assertTrue(obj._asleep_utterance_wakes("mas que coisa", PCM))

    def test_unknown_voice_three_times_does_not_wake(self):
        obj = self._asleep()
        for _ in range(4):
            self.assertFalse(
                obj._asleep_utterance_wakes("diálogo da novela", PCM))
        self.assertFalse(obj._awake)

    def test_three_speakers_in_a_minute_do_not_wake(self):
        # Owner lines older than the window do not count.
        obj = self._asleep()
        obj._owner_speaker_confirmed = lambda text, pcm: (
            True, 0.62, "voz=Anthero")
        old = time.monotonic() - (main.ASLEEP_SPEAK_WINDOW_S + 5.0)
        obj._asleep_owner_speech = [old, old]
        self.assertFalse(obj._asleep_utterance_wakes("bom dia", PCM))


if __name__ == "__main__":
    unittest.main()
