"""A WhatsApp voice note in the owner's direct chat confirms a parked restart.

Live defect of 03/11/2026 (16:22): the owner answered "confirmo reiniciar" with
a voice note from his phone. The note WAS transcribed and decoded, but the
strict pre-check (``_whatsapp_owner_voice``) scored the clip at 0.37/0.38 —
just under the 0.45 threshold — so the PCM was dropped, the note reached
``_qoder_turn`` as plain text, and the confirmation gate refused it with
"sem áudio — texto nunca confirma uma ação". The parked request stayed parked
and the restart never happened. Even when the note DID pass (16:21:45), the
acknowledgement only went to the room speakers; the phone stayed unanswered.

These tests pin the contract:

  (a) an exact-phrase note in the owner's direct chat keeps its decoded audio
      for the restart confirmation gate and applies the parked request;
  (b) the WhatsApp audio acknowledgement is sent BEFORE the restart fires;
  (c) typed text (anyone), a third-party voice, a group and an approximate
      phrase ("pode reiniciar") apply nothing;
  (d) with no parked request the exact phrase applies nothing.

No network, no real audio, no real speaker model: decoding and speaker-id are
fakes; the gates themselves are the real code.
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

import main
from core import whatsapp_watch

OWNER = "Anthero"
ACCOUNT = "principal"
OWNER_CHAT = "5517999990000"
THIRD_CHAT = "5511888888888"
OWNER_PCM = b"\x01\x00" * 8000
PHRASE = "Confirmo reiniciar."


class FakeUI:
    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, text):
        self.logs.append(text)


class FakeRestart:
    """Records confirmation attempts instead of restarting anything."""

    def __init__(self, pending=None, known=None, outcome="Restarting the assistant now."):
        self.pending_value = dict(pending) if pending else None
        self.known = known
        self.outcome = outcome
        self.confirmed: list[str] = []
        self.cancelled = 0
        self.renewed: list[str] = []
        self.events: list[tuple[str, str]] = []

    def pending(self):
        return self.pending_value

    def last_known(self):
        return self.known

    def cancel(self):
        self.cancelled += 1
        self.pending_value = None
        return "cancelled"

    def renew_expired(self, reason="", kind=""):
        self.renewed.append(reason)
        if self.known is None:
            return {"renewed": False, "detail": "no earlier request on record"}
        self.pending_value = {"kind": kind or "assistant",
                              "reason": self.known.get("reason", ""),
                              "phrase": "confirmo reiniciar"}
        return {"renewed": True, "kind": kind or "assistant",
                "reason": "", "phrase": "confirmo reiniciar", "detail": "ok"}

    def confirm(self, phrase):
        self.confirmed.append(phrase)
        self.events.append(("confirm", phrase))
        self.pending_value = None
        return self.outcome


class FakeSpeaker:
    owner_name = OWNER

    def __init__(self, name=OWNER, known=True, authorized=True, records=None):
        self._name = name
        self._known = known
        self._authorized = authorized
        self._records = records

    def embedding(self, _pcm):
        return [1.0, 0.0]

    def identify(self, _emb, device=None):
        return {"name": self._name, "score": 0.9, "sample_score": 0.9,
                "effective_score": 0.9, "known": self._known,
                "authorized": self._authorized, "device": device}

    def list(self):
        if self._records is not None:
            return self._records
        return [{"name": OWNER, "owner": True, "authorized": True,
                 "devices": ["pc", "celular"]}]


def owner_only_records():
    return [{"name": OWNER, "owner": True, "authorized": True,
             "devices": ["pc", "celular"]}]


def owner_and_guest_records():
    return owner_only_records() + [
        {"name": "Ana", "owner": False, "authorized": True,
         "devices": ["pc"]}]


def _voice_item(chat=OWNER_CHAT, text="", from_me=False, name=OWNER):
    return {"ts": 0, "chat": chat, "name": name, "from_me": from_me,
            "group": False, "text": text,
            "media": {"kind": "mensagem de voz", "ext": ".ogg"},
            "id": f"voice-{chat}"}


def _text_item(chat=OWNER_CHAT, text=PHRASE, name=OWNER):
    return {"ts": 0, "chat": chat, "name": name, "from_me": False,
            "group": False, "text": text, "media": None,
            "id": f"typed-{chat}"}


def _autoreply_obj(speaker=None, restart=None):
    """The real _whatsapp_autoreply shell, with stubbed side channels."""
    obj = object.__new__(main.JarvisLive)
    obj._qoder_lock = threading.RLock()
    obj._qoder_live_enabled = True
    obj._qoder_live_ready = lambda: True
    obj._asst_name = "Gino"
    obj.ui = FakeUI()
    obj._session_log = []
    obj._next_turn_ref = mock.Mock()
    obj._begin_user_epoch = mock.Mock(return_value={})
    obj._memory_record_user = mock.Mock()
    obj._index_event = mock.Mock()
    obj._last_user_speech = 0.0
    obj._qoder_turn = mock.Mock()
    obj._qoder_hold = mock.Mock()
    obj._speaker = speaker if speaker is not None else FakeSpeaker()
    obj._restart = restart
    obj._power = None
    obj._wa_stt_lock = threading.Lock()
    obj._transcribe_whatsapp_audio = mock.Mock(return_value=PHRASE)
    obj._decode_whatsapp_pcm = mock.Mock(return_value=OWNER_PCM)
    obj._restart_confirmation_pcm = (
        main.JarvisLive._restart_confirmation_pcm.__get__(obj, main.JarvisLive))
    obj._device_enroll_note = lambda *a, **k: False
    obj._whatsapp_owner_voice = mock.Mock(return_value=(None, "score baixo"))
    obj._whatsapp_voice_anomaly = mock.Mock()
    obj.said: list[str] = []
    obj._say_now = lambda text, **kw: obj.said.append(text)
    obj._log_heard = lambda text: obj.said.append(f"heard:{text}")
    return obj


def _autoreply(obj, item, direct=True):
    with mock.patch.object(whatsapp_watch, "is_direct", return_value=direct), \
            mock.patch("core.whatsapp.media_path",
                       return_value="/tmp/nota.ogg"):
        return obj._whatsapp_autoreply(ACCOUNT, item)


def _turn_obj(speaker, restart):
    """The real _qoder_turn shells: gates, voiceprint, reply channel."""
    obj = object.__new__(main.JarvisLive)
    obj._restart = restart
    obj._restart_enabled = True
    obj._power = None
    obj._speaker = speaker
    obj._qoder_live = None
    obj._qoder_ignore = False
    obj._qoder_retry_used = False
    obj._last_user_speech = 0.0
    obj._device_enroll_cancel_gate = lambda *a, **k: False
    obj.interrupt = lambda: None
    obj._restart_answer_expected = (
        main.JarvisLive._restart_answer_expected.__get__(obj, main.JarvisLive))
    obj._restart_reply = main.JarvisLive._restart_reply.__get__(
        obj, main.JarvisLive)
    obj._handle_restart_confirmation = (
        main.JarvisLive._handle_restart_confirmation.__get__(
            obj, main.JarvisLive))
    obj._handle_late_restart_confirmation = (
        main.JarvisLive._handle_late_restart_confirmation.__get__(
            obj, main.JarvisLive))
    obj._owner_voice_ok = main.JarvisLive._owner_voice_ok.__get__(
        obj, main.JarvisLive)
    obj._owner_voice = main.JarvisLive._owner_voice.__get__(
        obj, main.JarvisLive)
    obj._only_owner_enrolled = main.JarvisLive._only_owner_enrolled.__get__(
        obj, main.JarvisLive)
    obj._log_heard = lambda text: None
    obj._voice_audit = lambda *a, **k: None
    obj.said: list[str] = []
    obj._say_now = lambda text, **kw: obj.said.append(text)
    obj.wa_replies: list[tuple[str, str, str]] = []
    obj._send_whatsapp_answer = (
        lambda account, chat, text, media_type="text", speaker="":
        (obj.wa_replies.append((account, chat, text)),
         restart.events.append(("reply", text)),
         True)[-1])
    return obj


class WhatsAppRestartPcmWiringTests(unittest.TestCase):
    """_whatsapp_autoreply: the note's audio survives an inconclusive print."""

    def test_parked_restart_keeps_the_decoded_note_for_the_gate(self):
        restart = FakeRestart(pending={"kind": "assistant",
                                       "phrase": "confirmo reiniciar"})
        obj = _autoreply_obj(restart=restart)
        self.assertTrue(_autoreply(obj, _voice_item()))

        _, kwargs = obj._qoder_turn.call_args
        # The strict print did not match (0.38 live), so nothing else is armed
        self.assertIsNone(kwargs["pcm"])
        self.assertFalse(kwargs["owner_voice"])
        self.assertIsNone(kwargs["gate_text"])
        # ...but the note's own audio goes to the restart confirmation gate
        self.assertEqual(kwargs["confirm_pcm"], OWNER_PCM)
        self.assertEqual(kwargs["reply_channel"], "whatsapp")
        self.assertEqual(kwargs["wa_chat"], OWNER_CHAT)
        self.assertTrue(kwargs["confirm_direct"])

    def test_no_parked_request_means_no_audio_is_reused(self):
        obj = _autoreply_obj(restart=FakeRestart(pending=None))
        self.assertTrue(_autoreply(obj, _voice_item()))
        _, kwargs = obj._qoder_turn.call_args
        self.assertIsNone(kwargs["confirm_pcm"])
        obj._decode_whatsapp_pcm.assert_not_called()

    def test_approximate_phrase_does_not_reuse_the_audio(self):
        restart = FakeRestart(pending={"kind": "assistant",
                                       "phrase": "confirmo reiniciar"})
        obj = _autoreply_obj(restart=restart)
        obj._transcribe_whatsapp_audio.return_value = "pode reiniciar"
        self.assertTrue(_autoreply(obj, _voice_item()))
        _, kwargs = obj._qoder_turn.call_args
        self.assertIsNone(kwargs["confirm_pcm"])

    def test_typed_text_never_carries_audio(self):
        restart = FakeRestart(pending={"kind": "assistant",
                                       "phrase": "confirmo reiniciar"})
        obj = _autoreply_obj(restart=restart)
        self.assertTrue(_autoreply(obj, _text_item(text=PHRASE)))
        _, kwargs = obj._qoder_turn.call_args
        self.assertIsNone(kwargs["confirm_pcm"])
        self.assertIsNone(kwargs["pcm"])
        self.assertIsNone(kwargs["gate_text"])
        self.assertFalse(kwargs["owner_voice"])

    def test_group_voice_note_never_becomes_a_turn(self):
        restart = FakeRestart(pending={"kind": "assistant",
                                       "phrase": "confirmo reiniciar"})
        obj = _autoreply_obj(restart=restart)
        item = _voice_item(chat="120363000000000000@g.us")
        item["group"] = True
        self.assertFalse(_autoreply(obj, item))
        obj._qoder_turn.assert_not_called()
        obj._qoder_hold.assert_not_called()


class WhatsAppRestartConfirmGateTests(unittest.TestCase):
    """_qoder_turn: exact phrase + owner print applies the parked request."""

    def test_authenticated_note_applies_and_answers_before_restarting(self):
        restart = FakeRestart(pending={"kind": "assistant",
                                       "phrase": "confirmo reiniciar"})
        obj = _turn_obj(FakeSpeaker(records=owner_only_records()), restart)

        main.JarvisLive._qoder_turn(
            obj, "[WHATSAPP — CHAT DIRETO DO DONO] confirmo reiniciar",
            pcm=None, confirm_pcm=OWNER_PCM, owner_voice=False,
            gate_text=PHRASE, source="whatsapp", reply_channel="whatsapp",
            speak=False, wa_account=ACCOUNT, wa_chat=OWNER_CHAT,
            confirm_direct=True)

        self.assertEqual(restart.confirmed, [PHRASE])
        know_events = [name for name, _ in restart.events]
        self.assertEqual(know_events, ["reply", "confirm"],
                         "o áudio de resposta sai ANTES do reinício")
        self.assertIn("Entendido, reiniciando o assistente agora.",
                      restart.events[0][1])
        self.assertEqual(obj.wa_replies[0][0], ACCOUNT)
        self.assertEqual(obj.wa_replies[0][1], OWNER_CHAT)
        self.assertEqual(obj.said, [], "nada é falado na sala pelo chat")

    def test_single_owner_has_no_indulgence_anymore(self):
        # The genuine note scored 0.37 against the enrolled centroid. The old
        # microphone rule accepted that when the owner was the only person
        # enrolled; that ``allow_single`` escape was removed, so the same note
        # is now refused here too. An unauthenticated speaker cannot restart.
        restart = FakeRestart(pending={"kind": "assistant",
                                       "phrase": "confirmo reiniciar"})
        speaker = FakeSpeaker(name=None, known=False, authorized=False,
                              records=owner_only_records())
        obj = _turn_obj(speaker, restart)

        main.JarvisLive._qoder_turn(
            obj, "wrapper", pcm=None, confirm_pcm=OWNER_PCM,
            gate_text=PHRASE, source="whatsapp", reply_channel="whatsapp",
            speak=False, wa_account=ACCOUNT, wa_chat=OWNER_CHAT,
            confirm_direct=True)

        self.assertEqual(restart.confirmed, [])
        self.assertIsNotNone(restart.pending_value, "pedido continua parado")
        self.assertIn("Não reconheci", obj.wa_replies[-1][2])

    def test_pc_restart_note_still_needs_the_positive_print(self):
        restart = FakeRestart(pending={"kind": "pc",
                                       "phrase": "confirmo reiniciar"})
        speaker = FakeSpeaker(name=None, known=False, authorized=False,
                              records=owner_only_records())
        obj = _turn_obj(speaker, restart)

        main.JarvisLive._qoder_turn(
            obj, "wrapper", pcm=None, confirm_pcm=OWNER_PCM,
            gate_text=PHRASE, source="whatsapp", reply_channel="whatsapp",
            speak=False, wa_account=ACCOUNT, wa_chat=OWNER_CHAT,
            confirm_direct=True)

        self.assertEqual(restart.confirmed, [])
        self.assertEqual(restart.pending_value["kind"], "pc")
        self.assertIn("Não reconheci", obj.wa_replies[-1][2])

    def test_third_party_voice_never_confirms(self):
        restart = FakeRestart(pending={"kind": "assistant",
                                       "phrase": "confirmo reiniciar"})
        speaker = FakeSpeaker(name="Ana", known=True, authorized=True,
                              records=owner_and_guest_records())
        obj = _turn_obj(speaker, restart)

        main.JarvisLive._qoder_turn(
            obj, "wrapper", pcm=None, confirm_pcm=OWNER_PCM,
            gate_text=PHRASE, source="whatsapp", reply_channel="whatsapp",
            speak=False, wa_account=ACCOUNT, wa_chat=OWNER_CHAT,
            confirm_direct=True)

        self.assertEqual(restart.confirmed, [])
        self.assertIsNotNone(restart.pending_value, "pedido continua parado")
        self.assertIn("Não reconheci", obj.wa_replies[-1][2])

    def test_typed_phrase_without_audio_confirms_nothing(self):
        restart = FakeRestart(pending={"kind": "assistant",
                                       "phrase": "confirmo reiniciar"})
        obj = _turn_obj(FakeSpeaker(records=owner_only_records()), restart)

        main.JarvisLive._qoder_turn(
            obj, PHRASE, pcm=None, confirm_pcm=None, gate_text=PHRASE,
            source="whatsapp", reply_channel="whatsapp", speak=False,
            wa_account=ACCOUNT, wa_chat=OWNER_CHAT, confirm_direct=True)

        self.assertEqual(restart.confirmed, [])
        self.assertIsNotNone(restart.pending_value)
        self.assertIn("Não reconheci", obj.wa_replies[-1][2])

    def test_no_parked_request_applies_nothing(self):
        restart = FakeRestart(pending=None, known=None)
        obj = _turn_obj(FakeSpeaker(records=owner_only_records()), restart)

        main.JarvisLive._qoder_turn(
            obj, "wrapper", pcm=None, confirm_pcm=OWNER_PCM,
            gate_text=PHRASE, source="whatsapp", reply_channel="whatsapp",
            speak=False, wa_account=ACCOUNT, wa_chat=OWNER_CHAT,
            confirm_direct=True)

        self.assertEqual(restart.confirmed, [])
        # Sem histórico não há o que renovar; o pedido não é recriado.
        self.assertIsNone(restart.pending())
        self.assertIn("Nada foi feito", obj.wa_replies[-1][2])

    def test_late_phrase_from_history_renews_without_restarting(self):
        restart = FakeRestart(pending=None,
                              known={"kind": "assistant", "reason": "antigo"})
        obj = _turn_obj(FakeSpeaker(records=owner_only_records()), restart)

        main.JarvisLive._qoder_turn(
            obj, "wrapper", pcm=None, confirm_pcm=OWNER_PCM,
            gate_text=PHRASE, source="whatsapp", reply_channel="whatsapp",
            speak=False, wa_account=ACCOUNT, wa_chat=OWNER_CHAT,
            confirm_direct=True)

        self.assertEqual(restart.confirmed, [])
        self.assertEqual(restart.renewed, [""])
        self.assertIsNotNone(restart.pending_value)
        self.assertIn("expirou", obj.wa_replies[-1][2])

    def test_owner_print_in_a_third_party_chat_never_confirms(self):
        restart = FakeRestart(pending={"kind": "assistant",
                                       "phrase": "confirmo reiniciar"})
        obj = _turn_obj(FakeSpeaker(records=owner_only_records()), restart)

        consumed = main.JarvisLive._handle_restart_confirmation(
            obj, PHRASE, OWNER_PCM, reply_channel="whatsapp",
            wa_account=ACCOUNT, wa_chat=THIRD_CHAT, direct=False)

        self.assertFalse(consumed)
        self.assertEqual(restart.confirmed, [])
        self.assertIsNotNone(restart.pending_value)
        self.assertEqual(obj.wa_replies, [])

    def test_third_party_chat_turn_is_marked_not_direct(self):
        obj = _autoreply_obj(restart=FakeRestart(
            pending={"kind": "assistant", "phrase": "confirmo reiniciar"}))
        item = _text_item(chat=THIRD_CHAT, name="Renan",
                          text="Gino, confirmo reiniciar")
        self.assertTrue(_autoreply(obj, item, direct=False))
        _, kwargs = obj._qoder_turn.call_args
        self.assertFalse(kwargs["confirm_direct"])
        self.assertIsNone(kwargs["confirm_pcm"])

    def test_approximate_phrase_is_not_an_answer(self):
        restart = FakeRestart(pending={"kind": "assistant",
                                       "phrase": "confirmo reiniciar"})
        obj = _turn_obj(FakeSpeaker(records=owner_only_records()), restart)

        consumed = main.JarvisLive._handle_restart_confirmation(
            obj, "pode reiniciar", OWNER_PCM, reply_channel="whatsapp",
            wa_account=ACCOUNT, wa_chat=OWNER_CHAT)

        self.assertFalse(consumed, "frase aproximada segue o fluxo normal")
        self.assertEqual(restart.confirmed, [])
        self.assertIsNotNone(restart.pending_value)


if __name__ == "__main__":
    unittest.main()
