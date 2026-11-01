"""Autonomous WhatsApp replies: trigger matcher, anti-loop, dedupe, rate limit
and the narrow voice-gate bypass for whatsapp_control action=send.

The feature: a NEW individual message that names the assistant ("Gino") may be
answered without the owner's spoken consent. The bypass is deliberately tiny —
same account, same chat, action=send only — and the watcher must never answer
the owner, its own replies, a group, a duplicated event, or the same contact
more than five times an hour.
"""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from core import actiongate, whatsapp_watch
from core.tool_bridge import ToolBridge

TOKEN = "test-token-whatsapp"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ACCOUNT = "principal"
CHAT = "5511999999999"


def wa_item(text: str = "Gino, tudo bem?", **overrides) -> dict:
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


class WatcherHarness(unittest.TestCase):
    def setUp(self):
        actiongate.close()
        whatsapp_watch.reset()
        self.calls: list[tuple[str, dict]] = []
        self.logs: list[str] = []
        self.addCleanup(actiongate.close)
        self.addCleanup(whatsapp_watch.reset)
        self.addCleanup(whatsapp_watch.stop)
        whatsapp_watch.start(self._handler, logger=self.logs.append,
                             assistant_name="Gino")

    def _handler(self, account: str, item: dict) -> bool:
        self.calls.append((account, item))
        return True


class TriggerMatcherTests(unittest.TestCase):
    def setUp(self):
        whatsapp_watch.set_assistant_name("Gino")

    def test_name_follows_a_rename(self):
        whatsapp_watch.set_assistant_name("Pedro")
        self.assertTrue(whatsapp_watch.matches("oi Pedro, tudo bem?"))
        self.assertFalse(whatsapp_watch.matches("oi Gino, tudo bem?"))
        self.assertTrue(whatsapp_watch.matches("PEDRO!"))
        self.assertFalse(whatsapp_watch.matches("pedronildo"))

    def test_name_is_case_insensitive_and_word_bounded(self):
        for text in ("Gino", "gino", "oi GINO, tudo bem?", "fala, Gino.",
                     "GINÓ"):
            self.assertTrue(whatsapp_watch.matches(text), text)

    def test_words_that_only_contain_the_name_do_not_match(self):
        for text in ("ginormous", "imagino", "o ginozinho", ""):
            self.assertFalse(whatsapp_watch.matches(text), text)

    def test_non_string_input_is_safe(self):
        self.assertFalse(whatsapp_watch.matches(None))
        self.assertFalse(whatsapp_watch.matches(123))


class TriggerGateTests(WatcherHarness):
    def test_new_individual_message_naming_the_assistant_triggers(self):
        self.assertTrue(whatsapp_watch.handle(ACCOUNT, wa_item()))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], ACCOUNT)

    def test_group_messages_never_trigger(self):
        self.assertFalse(whatsapp_watch.handle(
            ACCOUNT, wa_item(group=True, chat="120363000000000000")))
        self.assertEqual(self.calls, [])

    def test_group_chat_id_never_triggers(self):
        self.assertFalse(whatsapp_watch.handle(
            ACCOUNT, wa_item(chat="120363000000000000@g.us")))
        self.assertEqual(self.calls, [])

    def test_message_without_the_name_does_not_trigger(self):
        self.assertFalse(whatsapp_watch.handle(
            ACCOUNT, wa_item("tudo bem? saudades")))
        self.assertEqual(self.calls, [])

    def test_owner_message_never_triggers(self):
        self.assertFalse(whatsapp_watch.handle(
            ACCOUNT, wa_item("Gino, manda o boleto", from_me=True)))
        self.assertEqual(self.calls, [])

    def test_assistant_reply_never_triggers_itself(self):
        self.assertFalse(whatsapp_watch.handle(
            ACCOUNT, wa_item("Gino: já mandei, qualquer coisa me chama",
                             from_me=True)))
        self.assertEqual(self.calls, [])

    def test_history_sync_older_than_the_freshness_window_is_ignored(self):
        old = int(time.time()) - whatsapp_watch.MAX_AGE_S - 60
        self.assertFalse(whatsapp_watch.handle(
            ACCOUNT, wa_item(ts=old)))
        self.assertEqual(self.calls, [])


class DirectChatTests(WatcherHarness):
    def setUp(self):
        super().setUp()
        whatsapp_watch.set_direct_chat(ACCOUNT, CHAT)
        self.addCleanup(whatsapp_watch.set_direct_chat, "", "")
        # The owner's real config may select another outbound account; the
        # test pins the account it configured.
        acct = mock.patch(
            "core.whatsapp_watch.whatsapp.default_account_name",
            return_value=ACCOUNT)
        acct.start()
        self.addCleanup(acct.stop)

    def test_direct_chat_does_not_need_the_name(self):
        self.assertTrue(whatsapp_watch.handle(
            ACCOUNT, wa_item("tudo bem? chegou o pedido")))
        self.assertEqual(len(self.calls), 1)

    def test_group_is_never_direct(self):
        self.assertFalse(whatsapp_watch.handle(
            ACCOUNT, wa_item("tudo bem?", group=True,
                             chat="120363000000000000")))
        self.assertEqual(self.calls, [])

    def test_direct_chat_has_no_hourly_limit(self):
        for n in range(whatsapp_watch.RATE_LIMIT + 2):
            self.assertTrue(whatsapp_watch.handle(
                ACCOUNT, wa_item("mensagem direta", id=f"direct-{n}")))
        self.assertEqual(len(self.calls), whatsapp_watch.RATE_LIMIT + 2)

    def test_reply_from_another_paired_account_never_triggers(self):
        other = "5511888888888"
        with mock.patch("core.whatsapp_watch.whatsapp.list_accounts",
                        return_value=[{"name": "trabalho", "number": other}]):
            self.assertFalse(whatsapp_watch.handle(
                ACCOUNT, wa_item("Gino, resposta antiga", chat=other)))
        self.assertEqual(self.calls, [])


class DedupeTests(WatcherHarness):
    def test_same_message_id_is_processed_once(self):
        item = wa_item(id="same-id")
        self.assertTrue(whatsapp_watch.handle(ACCOUNT, item))
        self.assertFalse(whatsapp_watch.handle(ACCOUNT, dict(item)))
        self.assertEqual(len(self.calls), 1)

    def test_same_text_with_a_new_id_is_a_new_message(self):
        self.assertTrue(whatsapp_watch.handle(ACCOUNT, wa_item(id="m1")))
        self.assertTrue(whatsapp_watch.handle(ACCOUNT, wa_item(id="m2")))
        self.assertEqual(len(self.calls), 2)

    def test_same_id_on_another_account_is_a_separate_message(self):
        item = wa_item(id="shared-id")
        self.assertTrue(whatsapp_watch.handle(ACCOUNT, item))
        self.assertTrue(whatsapp_watch.handle("trabalho", dict(item)))
        self.assertEqual(len(self.calls), 2)


class RateLimitTests(WatcherHarness):
    def test_five_replies_per_contact_per_hour_then_silence(self):
        for n in range(whatsapp_watch.RATE_LIMIT):
            self.assertTrue(whatsapp_watch.handle(ACCOUNT, wa_item(
                id=f"m{n}")), f"reply {n + 1} should be allowed")
        self.assertFalse(whatsapp_watch.handle(ACCOUNT, wa_item(id="m-over")))
        self.assertEqual(len(self.calls), whatsapp_watch.RATE_LIMIT)
        self.assertTrue(any("limite" in line.lower() for line in self.logs))

    def test_each_contact_has_its_own_hourly_budget(self):
        for n in range(whatsapp_watch.RATE_LIMIT):
            self.assertTrue(whatsapp_watch.handle(
                ACCOUNT, wa_item(id=f"vida-{n}")))
        self.assertFalse(whatsapp_watch.handle(ACCOUNT, wa_item(id="vida-over")))
        self.assertTrue(whatsapp_watch.handle(
            ACCOUNT, wa_item(id="mae-1", chat="5511888888888", name="Mãe")))
        self.assertEqual(len(self.calls), whatsapp_watch.RATE_LIMIT + 1)

    def test_failed_dispatch_does_not_spend_the_budget(self):
        whatsapp_watch.stop()
        whatsapp_watch.start(lambda account, item: False, logger=self.logs.append)
        for n in range(whatsapp_watch.RATE_LIMIT + 2):
            self.assertFalse(whatsapp_watch.handle(ACCOUNT, wa_item(
                id=f"fail-{n}")))
        self.assertFalse(any("limite" in line.lower() for line in self.logs))


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def monotonic(self) -> float:
        return self.now


class AutonomousSendBypassTests(unittest.TestCase):
    def setUp(self):
        actiongate.close()
        self.addCleanup(actiongate.close)

    def _grant(self):
        actiongate.allow_whatsapp_reply(ACCOUNT, CHAT, "Vida")

    @staticmethod
    def _send(**overrides) -> dict:
        args = {"action": "send", "account": ACCOUNT, "to": "Vida", "text": "oi"}
        args.update(overrides)
        return args

    def test_send_to_the_triggering_contact_is_allowed(self):
        self._grant()
        self.assertTrue(actiongate.whatsapp_reply_allowed(
            "whatsapp_control", self._send()))
        self.assertTrue(actiongate.whatsapp_reply_allowed(
            "whatsapp_control", self._send(to="VIDA")))
        self.assertTrue(actiongate.whatsapp_reply_allowed(
            "whatsapp_control", self._send(to="5511999999999")))
        self.assertTrue(actiongate.whatsapp_reply_allowed(
            "whatsapp_control", self._send(to="+55 11 99999-9999")))
        self.assertTrue(actiongate.whatsapp_reply_allowed(
            "whatsapp_control", self._send(to="11 99999-9999")))
        self.assertTrue(actiongate.whatsapp_reply_allowed(
            "whatsapp_control", self._send(account="")))

    def test_other_action_is_not_covered(self):
        self._grant()
        for action in ("send_file", "recent", "search", "status", ""):
            self.assertFalse(actiongate.whatsapp_reply_allowed(
                "whatsapp_control", self._send(action=action)), action)

    def test_other_contact_is_not_covered(self):
        self._grant()
        self.assertFalse(actiongate.whatsapp_reply_allowed(
            "whatsapp_control", self._send(to="Mãe")))
        self.assertFalse(actiongate.whatsapp_reply_allowed(
            "whatsapp_control", self._send(to="5511888888888")))

    def test_other_account_is_not_covered(self):
        self._grant()
        self.assertFalse(actiongate.whatsapp_reply_allowed(
            "whatsapp_control", self._send(account="trabalho")))

    def test_other_tool_is_not_covered(self):
        self._grant()
        self.assertFalse(actiongate.whatsapp_reply_allowed(
            "send_message", self._send()))
        self.assertFalse(actiongate.whatsapp_reply_allowed(
            "root_exec", {"action": "send", "to": "Vida"}))

    def test_no_grant_allows_nothing(self):
        self.assertFalse(actiongate.whatsapp_reply_allowed(
            "whatsapp_control", self._send()))

    def test_grant_expires_on_its_own(self):
        clock = _Clock()
        with mock.patch.object(actiongate, "time",
                               SimpleNamespace(monotonic=clock.monotonic)):
            self._grant()
            self.assertTrue(actiongate.whatsapp_reply_allowed(
                "whatsapp_control", self._send()))
            clock.now += actiongate.AUTO_REPLY_SECONDS + 1.0
            self.assertFalse(actiongate.whatsapp_reply_allowed(
                "whatsapp_control", self._send()))

    def test_grant_does_not_open_the_voice_window(self):
        self._grant()
        self.assertFalse(actiongate.confirm(
            "Confirm 'whatsapp_control'", "Risk: medium. Arguments: action=send"))
        self.assertFalse(actiongate.authorized())
        self.assertTrue(actiongate.whatsapp_reply_allowed(
            "whatsapp_control", self._send()))


class _Registry:
    def get_tool_declarations(self):
        return []


class _Dispatch:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, name, args):
        self.calls.append((name, dict(args)))
        return f"ran:{name}"


class BridgeBypassTests(unittest.TestCase):
    """The bridge must bypass the voice gate only through the narrow grant."""

    def setUp(self):
        actiongate.close()
        self.addCleanup(actiongate.close)
        self.confirmations: list[tuple[str, str]] = []
        self.dispatch = _Dispatch()
        self.bridge = ToolBridge(
            _Registry(), self.dispatch, TOKEN,
            confirm=lambda title, message: (
                self.confirmations.append((title, message)) or False),
            preauthorize=actiongate.whatsapp_reply_allowed,
            logger=lambda _message: None,
        )
        self.client = TestClient(self.bridge.app)
        self.addCleanup(self.client.close)

    def _call(self, **args):
        return self.client.post("/call", headers=AUTH,
                                json={"name": "whatsapp_control", "args": args})

    def test_granted_reply_skips_the_voice_confirmation(self):
        actiongate.allow_whatsapp_reply(ACCOUNT, CHAT, "Vida")
        response = self._call(action="send", account=ACCOUNT, to="Vida", text="oi")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["approved"])
        self.assertEqual(self.confirmations, [])
        self.assertEqual(len(self.dispatch.calls), 1)

    def test_other_action_still_asks_the_owner(self):
        actiongate.allow_whatsapp_reply(ACCOUNT, CHAT, "Vida")
        response = self._call(action="send_file", account=ACCOUNT,
                              to="Vida", path="/tmp/x.jpg")
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(len(self.confirmations), 1)
        self.assertEqual(self.dispatch.calls, [])

    def test_other_contact_still_asks_the_owner(self):
        actiongate.allow_whatsapp_reply(ACCOUNT, CHAT, "Vida")
        response = self._call(action="send", account=ACCOUNT, to="Mãe", text="oi")
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(len(self.confirmations), 1)
        self.assertEqual(self.dispatch.calls, [])


if __name__ == "__main__":
    unittest.main()
