"""OTP no WhatsApp e no e-mail: anúncio sem resposta e dedupe persistente."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from core import email_otp_watch, whatsapp_watch

ACCOUNT = "principal"
CHAT = "5511999999999"


def wa_item(text: str = "Seu código de verificação é 123456", **overrides) -> dict:
    item = {
        "ts": int(time.time()),
        "chat": CHAT,
        "name": "Vida",
        "from_me": False,
        "group": False,
        "text": text,
        "id": f"msg-{time.time_ns()}",
        "media": None,
    }
    item.update(overrides)
    return item


class WhatsappOtpTests(unittest.TestCase):
    def setUp(self):
        whatsapp_watch.reset()
        self.replies: list[tuple[str, dict]] = []
        self.otps: list[dict] = []
        self.logs: list[str] = []
        self.addCleanup(whatsapp_watch.reset)
        self.addCleanup(whatsapp_watch.stop)
        whatsapp_watch.start(self._reply, logger=self.logs.append,
                             on_otp=self._otp, assistant_name="Gino")

    def _reply(self, account: str, item: dict) -> bool:
        self.replies.append((account, item))
        return True

    def _otp(self, payload: dict) -> bool:
        self.otps.append(payload)
        return True

    def test_individual_code_is_announced_and_never_answered(self):
        whatsapp_watch._dispatch(
            ACCOUNT, wa_item("Gino, seu código de verificação é 123456"))
        self.assertEqual(len(self.otps), 1)
        self.assertEqual(self.otps[0]["code"], "123456")
        self.assertEqual(self.otps[0]["origin"], "whatsapp")
        self.assertEqual(self.otps[0]["sender"], "Vida")
        self.assertEqual(self.replies, [])

    def test_code_without_the_name_is_still_announced(self):
        whatsapp_watch._dispatch(ACCOUNT, wa_item("Código: 482913"))
        self.assertEqual(len(self.otps), 1)
        self.assertEqual(self.otps[0]["code"], "482913")

    def test_duplicate_message_is_announced_once(self):
        item = wa_item("Seu código é 123456", id="same-id")
        whatsapp_watch._dispatch(ACCOUNT, item)
        whatsapp_watch._dispatch(ACCOUNT, dict(item))
        self.assertEqual(len(self.otps), 1)
        self.assertEqual(self.replies, [])

    def test_group_code_is_ignored(self):
        whatsapp_watch._dispatch(
            ACCOUNT, wa_item("Seu código é 123456", group=True,
                             chat="120363000000000000@g.us"))
        self.assertEqual(self.otps, [])
        self.assertEqual(self.replies, [])

    def test_owner_code_is_ignored(self):
        whatsapp_watch._dispatch(
            ACCOUNT, wa_item("Seu código é 123456", from_me=True))
        self.assertEqual(self.otps, [])

    def test_history_sync_code_is_ignored(self):
        old = int(time.time()) - whatsapp_watch.MAX_AGE_S - 60
        whatsapp_watch._dispatch(ACCOUNT, wa_item("Seu código é 123456", ts=old))
        self.assertEqual(self.otps, [])

    def test_ordinary_message_naming_the_assistant_still_replies(self):
        whatsapp_watch._dispatch(ACCOUNT, wa_item("Gino, tudo bem?"))
        self.assertEqual(self.otps, [])
        self.assertEqual(len(self.replies), 1)

    def test_number_without_the_keyword_is_not_an_otp(self):
        whatsapp_watch._dispatch(ACCOUNT, wa_item("Gino, o pedido 123456 chegou"))
        self.assertEqual(self.otps, [])


class EmailDedupeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "seen.json"
        email_otp_watch.reset(store_path=self.store)
        self.addCleanup(email_otp_watch.reset)
        self.announced: list[dict] = []

    def _handler(self, payload: dict) -> bool:
        self.announced.append(payload)
        return True

    def _row(self, uid: str = "101",
             body: str = "Seu código de verificação é 123456",
             subject: str = "Código de verificação do Google",
             sender: str = '"Google" <no-reply@accounts.google.com>',
             ts: float | None = None) -> dict:
        return {
            "uid": uid,
            "from": sender,
            "subject": subject,
            "ts": int(time.time() if ts is None else ts),
            "body": body,
        }

    def test_same_uid_is_announced_once(self):
        row = self._row()
        email_otp_watch.process_messages([row], account="me@example.com",
                                         handler=self._handler)
        email_otp_watch.process_messages([dict(row)], account="me@example.com",
                                         handler=self._handler)
        self.assertEqual(len(self.announced), 1)
        self.assertEqual(self.announced[0]["origin"], "email")
        self.assertEqual(self.announced[0]["app"], "Google")

    def test_seen_history_survives_a_restart(self):
        row = self._row()
        email_otp_watch.process_messages([row], account="me@example.com",
                                         handler=self._handler)
        self.assertTrue(self.store.exists())
        email_otp_watch.reset(store_path=self.store)   # reinício simulado
        email_otp_watch.process_messages([dict(row)], account="me@example.com",
                                         handler=self._handler)
        self.assertEqual(len(self.announced), 1)

    def test_two_messages_are_two_announcements(self):
        rows = [self._row(uid="101", body="Seu código é 111111"),
                self._row(uid="102", body="Seu código é 222222")]
        email_otp_watch.process_messages(rows, account="me@example.com",
                                         handler=self._handler)
        self.assertEqual([p["code"] for p in self.announced], ["111111", "222222"])

    def test_stale_code_is_remembered_without_announcement(self):
        now = time.time()
        row = self._row(ts=now - email_otp_watch.FRESH_S - 60)
        self.assertEqual(email_otp_watch.process_messages(
            [row], account="me@example.com", handler=self._handler, now=now), [])
        self.assertEqual(email_otp_watch.process_messages(
            [row], account="me@example.com", handler=self._handler, now=now), [])
        self.assertEqual(self.announced, [])

    def test_ordinary_email_is_ignored_and_remembered(self):
        row = self._row(body="A reunião é às 15h.", subject="Aviso")
        email_otp_watch.process_messages([row], account="me@example.com",
                                         handler=self._handler)
        self.assertEqual(self.announced, [])

    def test_handler_false_leaves_the_code_for_the_next_poll(self):
        row = self._row()
        calls: list[dict] = []

        def failing(payload: dict) -> bool:
            calls.append(payload)
            return False

        self.assertEqual(email_otp_watch.process_messages(
            [row], account="me@example.com", handler=failing), [])
        email_otp_watch.process_messages([row], account="me@example.com",
                                         handler=self._handler)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.announced), 1)


if __name__ == "__main__":
    unittest.main()
