"""WhatsApp direct chat: text→text, audio→audio, no room TTS, exact chat."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import main
from core import whatsapp_watch


class _UI:
    def __init__(self):
        self.logs = []

    def write_log(self, text):
        self.logs.append(text)


class _Memory:
    def __init__(self):
        self.turns = []

    def append_turn(self, role, text):
        self.turns.append((role, text))


class _Loop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class DirectChatTest(unittest.TestCase):
    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._qoder_lock = threading.RLock()
        obj._qoder_live_enabled = True
        obj._qoder_live_ready = lambda: True
        obj._asst_name = "Gino"
        obj.ui = _UI()
        obj._chat_memory = _Memory()
        obj._last_user_speech = 0.0
        obj._qoder_turn = mock.Mock()
        obj._qoder_hold = mock.Mock()
        obj._begin_user_epoch = mock.Mock(return_value={"new_subject": False})
        obj._memory_record_user = mock.Mock()
        obj._transcribe_whatsapp_audio = mock.Mock(return_value="transcrição do áudio")
        obj._wa_stt_lock = threading.Lock()
        return obj

    def test_direct_text_goes_to_qoder_as_whatsapp_and_text_reply(self):
        obj = self._obj()
        item = {"chat": "5511999999999", "name": "Anthero", "text": "oi Gino"}
        with mock.patch.object(whatsapp_watch, "is_direct", return_value=True):
            self.assertTrue(obj._whatsapp_autoreply("trabalho", item))
        obj._qoder_turn.assert_called_once()
        args, kwargs = obj._qoder_turn.call_args
        self.assertIn("CHAT DIRETO DO DONO", args[0])
        self.assertEqual(kwargs["source"], "whatsapp")
        self.assertFalse(kwargs["speak"])
        self.assertEqual(kwargs["media_type"], "text")
        self.assertEqual(kwargs["wa_account"], "trabalho")
        self.assertEqual(kwargs["wa_chat"], "5511999999999")
        obj._memory_record_user.assert_called_once_with("oi Gino")
        self.assertTrue(any(line.startswith("WHATSAPP YOU:")
                            for line in obj.ui.logs))

    def test_direct_audio_is_transcribed_and_replies_audio(self):
        obj = self._obj()
        item = {"chat": "5511999999999", "name": "Anthero", "text": "",
                "media": {"kind": "áudio", "ext": ".ogg"}}
        with mock.patch.object(whatsapp_watch, "is_direct", return_value=True), \
                mock.patch("core.whatsapp.media_path", return_value="/tmp/x.ogg"):
            self.assertTrue(obj._whatsapp_autoreply("trabalho", item))
        obj._transcribe_whatsapp_audio.assert_called_once_with("/tmp/x.ogg")
        _, kwargs = obj._qoder_turn.call_args
        self.assertEqual(kwargs["media_type"], "audio")
        self.assertIn("transcrição do áudio", obj._qoder_turn.call_args[0][0])

    def test_third_party_is_external_not_owner(self):
        obj = self._obj()
        item = {"chat": "5511888888888", "name": "Vida",
                "text": "Gino, tudo bem?"}
        with mock.patch.object(whatsapp_watch, "is_direct", return_value=False):
            self.assertTrue(obj._whatsapp_autoreply("principal", item))
        obj._memory_record_user.assert_not_called()
        self.assertIn(("system", "[WhatsApp de Vida] Gino, tudo bem?"),
                      obj._chat_memory.turns)
        _, kwargs = obj._qoder_turn.call_args
        self.assertEqual(kwargs["media_type"], "text")
        self.assertIn("MENSAGEM DE TERCEIRO", obj._qoder_turn.call_args[0][0])


class SendAnswerTest(unittest.TestCase):
    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._synthesize_whatsapp_audio = mock.Mock(return_value="")
        return obj

    def test_text_answer_uses_send(self):
        obj = self._obj()
        with mock.patch("core.whatsapp.send", return_value=(True, "ok")) as send:
            self.assertTrue(obj._send_whatsapp_answer(
                "trabalho", "5511999999999", "resposta", "text"))
        send.assert_called_once_with("5511999999999", "resposta",
                                     account="trabalho")

    def test_audio_answer_uses_send_media_and_falls_back_to_text(self):
        obj = self._obj()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as handle:
            path = handle.name
        obj._synthesize_whatsapp_audio = mock.Mock(return_value=path)
        with mock.patch("core.whatsapp.send_media",
                        return_value=(True, "ok")) as send_media, \
                mock.patch("core.whatsapp.send") as send:
            self.assertTrue(obj._send_whatsapp_answer(
                "trabalho", "5511999999999", "resposta", "audio"))
        send_media.assert_called_once()
        send.assert_not_called()
        self.assertFalse(Path(path).exists())

        obj._synthesize_whatsapp_audio = mock.Mock(return_value="")
        with mock.patch("core.whatsapp.send", return_value=(True, "ok")) as send:
            self.assertTrue(obj._send_whatsapp_answer(
                "trabalho", "5511999999999", "resposta", "audio"))
        send.assert_called_once()


class NoRoomSpeechTest(unittest.TestCase):
    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._qoder_lock = threading.RLock()
        obj._qoder_ignore = False
        obj._qoder_current_id = "t1"
        obj._qoder_turns = {"t1": "pedido"}
        obj._qoder_text_buf = {"t1": ""}
        obj._qoder_spoken = {"t1": []}
        obj._qoder_meta = {"t1": {"source": "whatsapp", "speak": False}}
        obj._qoder_say = mock.Mock()
        obj._qoder_finish = mock.Mock()
        obj._send_whatsapp_answer = mock.Mock()
        obj._memory_librarian = None
        obj._chat_memory = _Memory()
        obj._session_log = []
        obj._asst_name = "Gino"
        obj.ui = _UI()
        return obj

    def test_whatsapp_text_is_buffered_not_spoken(self):
        obj = self._obj()
        main.JarvisLive._qoder_on_text(obj, "t1", "resposta no zap", False)
        obj._qoder_say.assert_not_called()
        self.assertIn("resposta no zap", obj._qoder_text_buf["t1"])

    def test_whatsapp_done_sends_and_does_not_speak(self):
        obj = self._obj()
        obj._qoder_text_buf["t1"] = "resposta no zap"
        main.JarvisLive._qoder_on_done(obj, "t1", "resposta no zap")
        obj._qoder_say.assert_not_called()
        obj._send_whatsapp_answer.assert_called_once()
        self.assertTrue(any(line.startswith("WHATSAPP Gino:")
                            for line in obj.ui.logs))


class OutboundAccountTest(unittest.TestCase):
    def test_selected_outbound_account_removes_the_question(self):
        from plugins import whatsapp_control as wc
        accounts = [{"name": "principal"}, {"name": "trabalho"}]
        with mock.patch.object(wc, "_accounts", return_value=accounts), \
                mock.patch("plugins.whatsapp_control.whatsapp.default_account_name",
                           return_value="trabalho"):
            self.assertIsNone(wc._ambiguous(""))

    def test_without_a_selection_it_still_asks(self):
        from plugins import whatsapp_control as wc
        accounts = [{"name": "principal"}, {"name": "trabalho"}]
        with mock.patch.object(wc, "_accounts", return_value=accounts), \
                mock.patch("plugins.whatsapp_control.whatsapp.default_account_name",
                           return_value=""):
            self.assertTrue(wc._ambiguous(""))


if __name__ == "__main__":
    unittest.main()
