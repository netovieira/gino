"""Spoken consent for a refused bridge action (main._try_action_consent + actiongate).

Reproduces the live bug of 31/10/2026: the bridge refused
whatsapp_control/send (403 "was not confirmed"), Gino was still speaking the
refusal when the owner answered "Pode enviar" — the barge-in classifier kept
the words as a "complement" and the actiongate block never saw them, so the
window was never opened and the retry was refused again.

The fix matches the consent BEFORE the barge-in classification, still
voice-only: the window opens only when (a) a request is pending (mark_pending
runs only on a bridge refusal, core/actiongate.py confirm()) and (b) the
owner's short utterance matches rootgate's consent matcher. A model never
reaches this path; an echo of Gino's own voice never reaches it either.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import main
from core import actiongate, rootgate


class FakeUI:
    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, text):
        self.logs.append(text)


def _obj(speaking=False, own_echo=False):
    """A JarvisLive bare enough for the consent matcher — no loop, no mic."""
    obj = object.__new__(main.JarvisLive)
    obj._is_speaking = speaking
    obj._reply_active = speaking
    obj.ui = FakeUI()
    obj.said: list[str] = []
    obj._say_now = lambda text, log=True: obj.said.append(text)
    obj._log_heard = lambda text: obj.said.append(f"heard:{text}")
    obj._looks_like_own_voice = lambda text: own_echo
    return obj


class _Clock:
    """A monotonic clock the test moves by hand, so expiry is exact."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def monotonic(self) -> float:
        return self.now


def _patch_clock(clock):
    return mock.patch.object(actiongate, "time",
                             SimpleNamespace(monotonic=clock.monotonic))


class SpokenConsentWindowTests(unittest.TestCase):
    """Pending refusal -> spoken "pode enviar" -> window -> confirm() True."""

    def setUp(self):
        actiongate.close()
        rootgate.close()
        self.addCleanup(actiongate.close)
        self.addCleanup(rootgate.close)

    def _refuse_once(self):
        """The bridge's refusal channel: confirm() parks the request."""
        self.assertFalse(actiongate.confirm(
            "Confirm 'whatsapp_control'",
            "Risk: medium. Arguments: action=send contato=Vida"))
        self.assertTrue(actiongate.has_pending())

    def test_pode_enviar_spoken_while_gino_is_still_talking_opens_window(self):
        # The exact live scenario: the answer arrives DURING the refusal
        # narration. It must open the window, not be kept as a complement.
        self._refuse_once()
        obj = _obj(speaking=True)
        self.assertTrue(main.JarvisLive._try_action_consent(obj, "Pode enviar"))
        self.assertTrue(actiongate.authorized())
        self.assertFalse(actiongate.has_pending())
        self.assertIn("Pode, autorizado.", obj.said)
        # The retry now passes: confirm() finds the open window. Authorising a
        # call does not spend the window — only the 120 s deadline closes it.
        self.assertTrue(actiongate.confirm("Confirm 'whatsapp_control'", "again"))
        self.assertTrue(actiongate.authorized())

    def test_bare_pode_answers_a_pending_request(self):
        self._refuse_once()
        obj = _obj(speaking=False)
        self.assertTrue(main.JarvisLive._try_action_consent(obj, "pode"))
        self.assertTrue(actiongate.authorized())

    def test_window_lasts_the_deadline_not_a_single_use(self):
        clock = _Clock()
        with _patch_clock(clock):
            self._refuse_once()
            obj = _obj()
            self.assertTrue(main.JarvisLive._try_action_consent(obj, "pode enviar"))
            self.assertTrue(actiongate.confirm("Confirm 'whatsapp_control'", "first"))
            # Still open near the end of the window: a first call does not
            # spend the consent on the handler's behalf.
            clock.now += actiongate.WINDOW_SECONDS - 1.0
            self.assertTrue(actiongate.confirm("Confirm 'send_message'", "second"))
            # Only the deadline closes it.
            clock.now += 2.0
            self.assertFalse(actiongate.confirm("Confirm 'send_message'", "third"))
            self.assertFalse(actiongate.authorized())

    def test_stray_consent_without_pending_opens_nothing(self):
        # No refusal happened: nothing on the table, nothing authorisable.
        obj = _obj()
        self.assertFalse(main.JarvisLive._try_action_consent(obj, "pode"))
        self.assertFalse(actiongate.authorized())

    def test_echo_of_our_own_refusal_does_not_open_the_window(self):
        # Gino's playback leaking into the mic must not self-authorise.
        self._refuse_once()
        obj = _obj(speaking=True, own_echo=True)
        self.assertFalse(main.JarvisLive._try_action_consent(obj, "pode enviar"))
        self.assertFalse(actiongate.authorized())
        self.assertTrue(actiongate.has_pending())  # kept for the real answer

    def test_long_sentence_containing_pode_is_not_an_answer(self):
        self._refuse_once()
        obj = _obj()
        too_long = ("eu acho que voce pode me dizer depois que horas ficam "
                    "quando o sol se poe la no horizonte")
        self.assertFalse(main.JarvisLive._try_action_consent(obj, too_long))
        self.assertFalse(actiongate.authorized())
        self.assertTrue(actiongate.has_pending())


class EconomyQoderTurnConsentTests(unittest.TestCase):
    """FIX 1 (31/10/2026): the voice gates never ran in the mode the assistant
    actually lives in.

    _EconomySession.send_client_content injects the owner's words straight
    into JarvisLive._qoder_turn — NOT through _on_groq_text, where the
    restart/power/consent gates are called. 22 consecutive bridge calls ended
    "approved: false" because the spoken "Pode enviar" never reached
    _try_action_consent. The gates must run on that entry too.
    """

    def setUp(self):
        actiongate.close()
        rootgate.close()
        self.addCleanup(actiongate.close)
        self.addCleanup(rootgate.close)

    def _obj(self):
        obj = _obj(speaking=True)     # the refusal narration is still playing
        obj._restart = None
        obj._power = None
        obj._qoder_lock = None        # words reaching the model => TypeError
        return obj

    def test_consent_words_entering_qoder_turn_open_the_window(self):
        # The exact live scenario: the bridge refused, the words enter
        # through the economy path (_qoder_turn(text), no pcm).
        self.assertFalse(actiongate.confirm(
            "Confirm 'whatsapp_control'",
            "Risk: medium. Arguments: action=send contato=Vida"))
        self.assertTrue(actiongate.has_pending())
        obj = self._obj()
        main.JarvisLive._qoder_turn(obj, "Pode enviar")
        self.assertTrue(actiongate.authorized())
        self.assertFalse(actiongate.has_pending())
        # The following bridge call passes — this is what failed 22 times.
        self.assertTrue(actiongate.confirm("Confirm 'whatsapp_control'", "retry"))
        self.assertTrue(actiongate.authorized())    # still inside the 120 s window
        self.assertIn("Pode, autorizado.", obj.said)

    def test_retry_replay_does_not_run_the_gates(self):
        # retry=True replays the SAME words to the model; re-gating them
        # would let the assistant's own retried prompt authorise itself.
        self.assertFalse(actiongate.confirm("Confirm 'whatsapp_control'", "x"))
        obj = self._obj()
        with self.assertRaises(Exception):
            main.JarvisLive._qoder_turn(obj, "Pode enviar", retry=True)
        self.assertFalse(actiongate.authorized())
        self.assertTrue(actiongate.has_pending())   # still waiting for a voice

    def test_stray_economy_text_opens_nothing_without_pending(self):
        # A reminder/briefing line injected through the same entry: with no
        # refused request on the table, nothing authorises.
        obj = self._obj()
        with self.assertRaises(Exception):   # reaches the model, as it should
            main.JarvisLive._qoder_turn(obj, "pode")
        self.assertFalse(actiongate.authorized())


if __name__ == "__main__":
    unittest.main()
