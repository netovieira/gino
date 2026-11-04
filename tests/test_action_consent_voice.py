"""Spoken consent for a refused bridge action (main._try_action_consent + actiongate).

Reproduces the live bug of 31/10/2026: the bridge refused
whatsapp_control/send (403 "was not confirmed"), Gino was still speaking the
refusal when the owner answered "Pode enviar" — the barge-in classifier kept
the words as a "complement" and the actiongate block never saw them, so the
window was never opened and the retry was refused again.

The fix matches the consent BEFORE the barge-in classification. With a request
pending, the 120 s window opens only when (a) a request is pending
(mark_pending runs only on a bridge refusal, core/actiongate.py confirm()),
(b) the owner's short utterance matches the consent matcher and (c) the
VOICEPRINT confirms the owner. (c) is the 01/11 security fix: at 13:17:39 a TV
line ("Você pode olhar.") matched (b) and opened 120 s of whatsapp_control, and
Gino's own transcribed reply ("pode ser algo em 6 em 6 horas") matched too.

When the owner answers BEFORE the bridge parks the request (02/11/2026
16:04:16 and 16:12:11; refusals at 16:04:33 and 16:12:27), the same matcher
arms a 15-minute window for the next WhatsApp send. The consent is VOICE-ONLY:
the owner's voiceprint (or, for a remote voice note, the same check on the
decoded audio) is the only thing that opens it. Typed text from ANY source —
PC keyboard, WhatsApp, another device — never does. A model never reaches this
path; an echo of Gino's own voice never reaches it either.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import main
from core import actiongate, rootgate

OWNER = "Anthero"
OWNER_PCM = b"\x01\x00" * 8000


class FakeUI:
    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, text):
        self.logs.append(text)


class FakeSpeaker:
    """A voice index whose answer the test decides: owner, guest, or TV."""

    def __init__(self, name=OWNER, known=True, authorized=True,
                 score=0.82, verdict="known", records=None):
        self.owner_name = OWNER
        self._name = name
        self._known = known
        self._authorized = authorized
        self._score = score
        self._verdict = verdict
        self._records = (records if records is not None else
                         [{"name": OWNER, "owner": True, "authorized": True}])

    def embedding(self, pcm):
        return [1.0, 0.0]

    def identify(self, embedding, include_auto=False, voiced_seconds=None):
        return {
            "name": self._name,
            "score": self._score,
            "sample_score": self._score,
            "effective_score": self._score,
            "known": self._known,
            "authorized": self._authorized,
            "verdict": self._verdict,
        }

    def list(self):
        return list(self._records)


def tv_speaker(score: float = 0.12) -> FakeSpeaker:
    """A TV voice far below the gray floor: never the owner, even alone."""
    return FakeSpeaker(name=None, known=False, authorized=False,
                       score=score, verdict="unknown")


def _obj(speaking=False, own_echo=False, speaker=None, name="Gino"):
    """A JarvisLive bare enough for the consent matcher — no loop, no mic."""
    obj = object.__new__(main.JarvisLive)
    obj._is_speaking = speaking
    obj._reply_active = speaking
    obj._asst_name = name
    obj._speaker = speaker if speaker is not None else FakeSpeaker()
    obj._series_gate = None
    obj._series_decision_for = lambda pcm: None
    obj._last_user_speech = 0.0
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
        self.assertTrue(main.JarvisLive._try_action_consent(
            obj, "Pode enviar", OWNER_PCM))
        self.assertTrue(actiongate.authorized())
        self.assertFalse(actiongate.has_pending())
        self.assertIn("Pode, autorizado.", obj.said)
        # The retry now passes: confirm() finds the open window. Authorising a
        # call does not spend the window — only the 120 s deadline closes it.
        self.assertTrue(actiongate.confirm("Confirm 'whatsapp_control'", "again"))
        self.assertTrue(actiongate.authorized())
        # FIX 1: the gate that consumed the owner's words restarts the clock.
        self.assertGreater(obj._last_user_speech, 0.0)

    def test_bare_pode_answers_a_pending_request(self):
        self._refuse_once()
        obj = _obj(speaking=False)
        self.assertTrue(main.JarvisLive._try_action_consent(
            obj, "pode", OWNER_PCM))
        self.assertTrue(actiongate.authorized())

    def test_window_lasts_the_deadline_not_a_single_use(self):
        clock = _Clock()
        with _patch_clock(clock):
            self._refuse_once()
            obj = _obj()
            self.assertTrue(main.JarvisLive._try_action_consent(
                obj, "pode enviar", OWNER_PCM))
            self.assertTrue(actiongate.confirm("Confirm 'whatsapp_control'", "first"))
            # Still open near the end of the window: a first call does not
            # spend the consent on the handler's behalf.
            clock.now += actiongate.WINDOW_SECONDS - 1.0
            self.assertTrue(actiongate.confirm("Confirm 'send_message'", "second"))
            # Only the deadline closes it.
            clock.now += 2.0
            self.assertFalse(actiongate.confirm("Confirm 'send_message'", "third"))
            self.assertFalse(actiongate.authorized())

    def test_consent_without_pending_arms_the_next_whatsapp_send(self):
        # The 02/11/2026 race: the owner answered "pode enviar" BEFORE the
        # bridge parked the request (16:04:16 and 16:12:11; the refusals came
        # at 16:04:33 and 16:12:27). The consent must survive until the call
        # arrives, scoped to WhatsApp and bounded to 15 minutes.
        obj = _obj()
        self.assertTrue(main.JarvisLive._try_action_consent(
            obj, "Pode enviar", OWNER_PCM))
        self.assertTrue(actiongate.authorized())
        self.assertFalse(actiongate.has_pending())
        self.assertIn("Pode, autorizado.", obj.said)
        self.assertTrue(actiongate.confirm(
            "Confirm 'whatsapp_control'",
            "Risk: medium. Arguments: action=send contato=Vida"))
        # The armed window never authorises an unrelated risky action.
        self.assertFalse(actiongate.confirm(
            "Confirm 'personas'", "Risk: high. Arguments: action=create"))
        self.assertTrue(actiongate.authorized())

    def test_echo_of_our_own_refusal_does_not_open_the_window(self):
        # Gino's playback leaking into the mic must not self-authorise.
        self._refuse_once()
        obj = _obj(speaking=True, own_echo=True)
        self.assertFalse(main.JarvisLive._try_action_consent(
            obj, "pode enviar", OWNER_PCM))
        self.assertFalse(actiongate.authorized())
        self.assertTrue(actiongate.has_pending())  # kept for the real answer

    def test_long_sentence_containing_pode_is_not_an_answer(self):
        self._refuse_once()
        obj = _obj()
        too_long = ("eu acho que voce pode me dizer depois que horas ficam "
                    "quando o sol se poe la no horizonte")
        self.assertFalse(main.JarvisLive._try_action_consent(
            obj, too_long, OWNER_PCM))
        self.assertFalse(actiongate.authorized())
        self.assertTrue(actiongate.has_pending())

    # ── FIX 2: words alone never authorise; the voiceprint must be the owner ─
    def test_tv_voice_saying_pode_opens_nothing(self):
        # 13:17:39: "Você pode olhar." from the TV opened 120 s of
        # whatsapp_control. Same words, unknown voice: no window.
        self._refuse_once()
        obj = _obj(speaker=tv_speaker())
        self.assertTrue(main.JarvisLive._try_action_consent(
            obj, "Você pode olhar.", OWNER_PCM))
        self.assertFalse(actiongate.authorized())
        self.assertTrue(actiongate.has_pending())
        self.assertTrue(any("consentimento ignorado" in line
                            for line in obj.ui.logs))
        self.assertTrue(any("falante não confirmado como dono" in line
                            for line in obj.ui.logs))
        self.assertNotIn("Pode, autorizado.", obj.said)

    def test_gray_band_owner_indulgence_still_opens_the_window(self):
        # One enrolled voice (the owner) and a score inside the gray band:
        # the same indulgence that answers his sentence as his authorises it.
        self._refuse_once()
        obj = _obj(speaker=FakeSpeaker(name=None, known=False,
                                       authorized=False, score=0.45,
                                       verdict="unknown"))
        self.assertTrue(main.JarvisLive._try_action_consent(
            obj, "pode enviar", OWNER_PCM))
        self.assertTrue(actiongate.authorized())

    def test_known_guest_voice_cannot_authorise(self):
        # An authorised guest is not the owner for a risky action.
        self._refuse_once()
        records = [{"name": OWNER, "owner": True, "authorized": True},
                   {"name": "Ana", "owner": False, "authorized": True}]
        obj = _obj(speaker=FakeSpeaker(name="Ana", known=True,
                                       authorized=True, score=0.9,
                                       records=records))
        self.assertTrue(main.JarvisLive._try_action_consent(
            obj, "pode enviar", OWNER_PCM))
        self.assertFalse(actiongate.authorized())
        self.assertTrue(actiongate.has_pending())

    def test_consent_without_audio_on_the_voice_channel_opens_nothing(self):
        # A voice-channel line with no PCM cannot be compared with the owner's
        # voiceprint, so it is not trusted — same rule as typed text.
        self._refuse_once()
        obj = _obj()
        self.assertFalse(main.JarvisLive._try_action_consent(
            obj, "pode enviar", None, channel="voice"))
        self.assertFalse(actiongate.authorized())
        self.assertTrue(actiongate.has_pending())
        self.assertTrue(any("sem áudio" in line for line in obj.ui.logs))

    def test_typed_text_channel_cannot_arm_the_send_window(self):
        # Owner rule: TEXT NEVER AUTHORISES. A typed "pode enviar" may come
        # from another device holding his session; only the voiceprint counts.
        obj = _obj()
        self.assertFalse(main.JarvisLive._try_action_consent(
            obj, "Pode enviar", None, channel="text"))
        self.assertFalse(actiongate.authorized())
        self.assertTrue(any("sem áudio" in line for line in obj.ui.logs))

    def test_whatsapp_text_channel_cannot_arm_the_send_window(self):
        obj = _obj()
        self.assertFalse(main.JarvisLive._try_action_consent(
            obj, "Pode enviar", None, channel="whatsapp"))
        self.assertFalse(actiongate.authorized())
        self.assertTrue(any("sem áudio" in line for line in obj.ui.logs))

    def test_task_channel_cannot_arm_the_send_window(self):
        obj = _obj()
        self.assertFalse(main.JarvisLive._try_action_consent(
            obj, "Pode enviar", None, channel="task"))
        self.assertFalse(actiongate.authorized())

    def test_armed_send_window_expires_after_fifteen_minutes(self):
        clock = _Clock()
        with _patch_clock(clock):
            obj = _obj()
            self.assertTrue(main.JarvisLive._try_action_consent(
                obj, "Pode enviar", OWNER_PCM))
            clock.now += actiongate.CONSENT_TTL_SECONDS - 1.0
            self.assertTrue(actiongate.confirm(
                "Confirm 'whatsapp_control'", "inside"))
            clock.now += 2.0
            self.assertFalse(actiongate.confirm(
                "Confirm 'whatsapp_control'", "expired"))
            self.assertFalse(actiongate.authorized())

    def test_short_consent_matcher_rejects_normal_sentences(self):
        # "pode" inside a normal request is not an answer.
        for text in ("você pode me dizer as horas",
                     "pode ser algo em 8 em 8 horas",
                     "pode falar mais alto",
                     "eu acho que voce pode me dizer depois"):
            self.assertFalse(actiongate.looks_like_short_consent(text), text)
        for text in ("pode", "Pode enviar", "Gino, pode enviar.",
                     "Eu disse, pode enviar.", "Pode enviar para Carol.",
                     "pode mandar sim"):
            self.assertTrue(actiongate.looks_like_short_consent(text), text)


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
        # The exact live scenario: the bridge refused, the owner's answer
        # enters through the economy path with its PCM.
        self.assertFalse(actiongate.confirm(
            "Confirm 'whatsapp_control'",
            "Risk: medium. Arguments: action=send contato=Vida"))
        self.assertTrue(actiongate.has_pending())
        obj = self._obj()
        main.JarvisLive._qoder_turn(obj, "Pode enviar", OWNER_PCM)
        self.assertTrue(actiongate.authorized())
        self.assertFalse(actiongate.has_pending())
        # The following bridge call passes — this is what failed 22 times.
        self.assertTrue(actiongate.confirm("Confirm 'whatsapp_control'", "retry"))
        self.assertTrue(actiongate.authorized())    # still inside the 120 s window
        self.assertIn("Pode, autorizado.", obj.said)

    def test_consent_words_without_audio_never_authorise(self):
        # Owner rule (02/11/2026): TEXT NEVER AUTHORISES, not even on the
        # entry the economy path uses. The words alone cannot open the window;
        # the turn then continues to the model (here: the missing lock raises).
        obj = self._obj()
        with self.assertRaises(TypeError):
            main.JarvisLive._qoder_turn(obj, "Pode enviar")
        self.assertFalse(actiongate.authorized())
        self.assertNotIn("Pode, autorizado.", obj.said)
        self.assertTrue(any("sem áudio" in line for line in obj.ui.logs))

    def test_retry_replay_does_not_run_the_gates(self):
        # retry=True replays the SAME words to the model; re-gating them
        # would let the assistant's own retried prompt authorise itself.
        self.assertFalse(actiongate.confirm("Confirm 'whatsapp_control'", "x"))
        obj = self._obj()
        with self.assertRaises(Exception):
            main.JarvisLive._qoder_turn(obj, "Pode enviar", OWNER_PCM, retry=True)
        self.assertFalse(actiongate.authorized())
        self.assertTrue(actiongate.has_pending())   # still waiting for a voice

    def test_economy_voice_consent_without_pending_arms_the_send_window(self):
        # The owner answered before the refusal existed: the words must not be
        # thrown away just because there is nothing pending yet.
        obj = self._obj()
        main.JarvisLive._qoder_turn(obj, "pode", OWNER_PCM)
        self.assertTrue(actiongate.authorized())
        self.assertFalse(actiongate.has_pending())
        self.assertTrue(actiongate.confirm(
            "Confirm 'whatsapp_control'", "Risk: medium. Arguments: action=send"))


if __name__ == "__main__":
    unittest.main()
