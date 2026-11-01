"""Tests for core/tool_bridge.py — policy, auth, delegation, audit, lifecycle."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from core import rootgate
from core import actiongate
from core.tool_bridge import ACTION_TIERS, ToolBridge

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeRegistry:
    """Minimal stand-in for ActionRegistry (declarations + run fallback)."""

    def __init__(self):
        self._decls = [
            {"name": "web_search", "description": "search", "parameters": {"type": "OBJECT"}},
            {"name": "root_exec", "description": "shell", "parameters": {"type": "OBJECT"}},
            {"name": "send_message", "description": "send", "parameters": {"type": "OBJECT"}},
        ]
        self.calls: list[tuple[str, dict]] = []

    def get_tool_declarations(self):
        return [dict(d) for d in self._decls]

    def has(self, name):
        return any(d["name"] == name for d in self._decls)

    def run(self, name, parameters, ctx=None):
        self.calls.append((name, parameters, threading.current_thread().name))
        return f"registry:{name}"


class RecordingDispatch:
    def __init__(self, result=None):
        self.calls: list[tuple[str, dict, str]] = []
        self._result = result

    def __call__(self, name, args):
        self.calls.append((name, args, threading.current_thread().name))
        if self._result is not None:
            return self._result
        return f"ran:{name}"


def make_bridge(**overrides):
    registry = overrides.pop("registry", None) or FakeRegistry()
    dispatch = overrides.pop("dispatch", None)
    if dispatch is None:
        dispatch = RecordingDispatch()
    logs = overrides.pop("logs", None)
    logger = logs.append if logs is not None else (lambda _m: None)
    return ToolBridge(registry, dispatch, TOKEN, logger=logger, **overrides)


class HealthAndAuthTests(unittest.TestCase):
    def setUp(self):
        self.bridge = make_bridge()
        self.client = TestClient(self.bridge.app)
        self.addCleanup(self.client.close)

    def test_health_needs_no_auth(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["service"], "jarvis-tool-bridge")

    def test_tools_rejects_missing_or_wrong_token(self):
        self.assertEqual(self.client.get("/tools").status_code, 401)
        self.assertEqual(
            self.client.get("/tools", headers={"Authorization": "Bearer nope"}).status_code,
            401,
        )
        self.assertEqual(
            self.client.get("/tools", headers={"Authorization": TOKEN}).status_code,
            401,
        )
        self.assertEqual(self.client.get("/tools", headers=AUTH).status_code, 200)

    def test_call_requires_auth(self):
        response = self.client.post("/call", json={"name": "web_search", "args": {}})
        self.assertEqual(response.status_code, 401)

    def test_empty_token_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            ToolBridge(FakeRegistry(), RecordingDispatch(), "")


class CatalogAndTierTests(unittest.TestCase):
    def setUp(self):
        self.bridge = make_bridge()
        self.client = TestClient(self.bridge.app)
        self.addCleanup(self.client.close)

    def test_tool_catalog_includes_tier(self):
        response = self.client.get("/tools", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        tools = {t["name"]: t for t in response.json()["tools"]}
        self.assertEqual(tools["web_search"]["tier"], "safe")
        self.assertEqual(tools["root_exec"]["tier"], "high")
        self.assertEqual(tools["send_message"]["tier"], "medium")

    def test_tier_mapping(self):
        bridge = self.bridge
        self.assertEqual(bridge.risk_tier("web_search"), "safe")
        self.assertEqual(bridge.risk_tier("weather_report"), "safe")
        self.assertEqual(bridge.risk_tier("music_control"), "low")
        self.assertEqual(bridge.risk_tier("open_app"), "low")
        self.assertEqual(bridge.risk_tier("file_controller"), "low")
        self.assertEqual(bridge.risk_tier("send_message"), "medium")
        self.assertEqual(bridge.risk_tier("email_control"), "medium")
        self.assertEqual(bridge.risk_tier("browser_control"), "medium")
        self.assertEqual(bridge.risk_tier("runtime_status"), "safe")
        self.assertEqual(bridge.risk_tier("restart_assistant"), "low")
        self.assertEqual(bridge.risk_tier("root_exec"), "high")
        self.assertEqual(bridge.risk_tier("phrase_blocklist"), "high")
        self.assertEqual(bridge.risk_tier("totally_unknown_action"), "high")

    def test_arguments_can_escalate_known_action(self):
        self.assertEqual(self.bridge.risk_tier("computer_settings", {"action": "volume_up"}), "low")
        self.assertEqual(self.bridge.risk_tier("computer_settings", {"action": "shutdown"}), "high")
        self.assertEqual(self.bridge.risk_tier("file_controller", {"action": "list"}), "low")
        self.assertEqual(self.bridge.risk_tier("file_controller", {"action": "delete"}), "high")

    def test_all_discovered_action_names_are_covered(self):
        # Names currently exposed by actions/ and plugins/ (see their TOOL dicts).
        declared = {
            "browser_control", "code_helper", "computer_control", "computer_settings",
            "continuity", "desktop_control", "dev_agent", "file_controller",
            "file_processor", "flight_finder", "game_updater", "open_app",
            "qoder_agent", "qoder_job", "reminder", "root_exec", "send_message",
            "video_player", "weather_report", "web_search", "youtube_video",
            "android_control", "morning_briefing", "brain_status", "cakto_sales",
            "email_control", "focus_mode", "gemini_accounts_status",
            "groq_listen_status", "humor_mode", "iphone_control", "mac_control",
            "music_control", "organizer", "phrase_blocklist", "qoder_status",
            "rastracking", "trello_control", "whatsapp_control",
        }
        missing = declared - set(ACTION_TIERS)
        self.assertEqual(missing, set(), f"unclassified actions: {sorted(missing)}")


class CallTests(unittest.TestCase):
    def setUp(self):
        self.registry = FakeRegistry()
        self.dispatch = RecordingDispatch()
        self.confirmations = []
        self.bridge = make_bridge(registry=self.registry, dispatch=self.dispatch,
                                  confirm=self._confirm)
        self.client = TestClient(self.bridge.app)
        self.addCleanup(self.client.close)

    def _confirm(self, title, message):
        self.confirmations.append((title, message))
        return True

    def test_safe_call_uses_dispatch_on_worker_thread(self):
        response = self.client.post(
            "/call", headers=AUTH,
            json={"name": "web_search", "args": {"query": "clima"}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["result"], "ran:web_search")
        self.assertEqual(body["tier"], "safe")
        self.assertEqual(len(self.dispatch.calls), 1)
        name, args, thread_name = self.dispatch.calls[0]
        self.assertEqual(name, "web_search")
        self.assertEqual(args, {"query": "clima"})
        self.assertNotEqual(thread_name, "MainThread")
        self.assertEqual(self.registry.calls, [])

    def test_registry_is_used_when_no_dispatch_is_injected(self):
        bridge = ToolBridge(self.registry, None, TOKEN, logger=lambda _m: None)
        client = TestClient(bridge.app)
        self.addCleanup(client.close)
        response = client.post("/call", headers=AUTH,
                               json={"name": "web_search", "args": {}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"], "registry:web_search")
        self.assertEqual(len(self.registry.calls), 1)

    def test_high_tier_without_confirm_returns_409(self):
        bridge = make_bridge(registry=self.registry, dispatch=self.dispatch,
                             confirm=None)
        client = TestClient(bridge.app)
        self.addCleanup(client.close)
        response = client.post("/call", headers=AUTH,
                               json={"name": "root_exec", "args": {"command": "whoami"}})
        self.assertEqual(response.status_code, 409, response.text)
        body = response.json()
        self.assertEqual(body["status"], "confirmation_required")
        self.assertEqual(body["tier"], "high")
        self.assertEqual(self.dispatch.calls, [])

    def test_high_tier_with_confirm_runs(self):
        response = self.client.post(
            "/call", headers=AUTH,
            json={"name": "root_exec", "args": {"command": "whoami"}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"], "ran:root_exec")
        self.assertEqual(len(self.confirmations), 1)
        self.assertIn("root_exec", self.confirmations[0][0])

    def test_high_tier_denied_does_not_run(self):
        self.confirmations.clear()
        bridge = make_bridge(registry=self.registry, dispatch=self.dispatch,
                             confirm=lambda title, message: False)
        client = TestClient(bridge.app)
        self.addCleanup(client.close)
        response = client.post("/call", headers=AUTH,
                               json={"name": "computer_settings",
                                     "args": {"action": "shutdown"}})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["status"], "denied")
        self.assertEqual(self.dispatch.calls, [])

    def test_medium_tier_requires_confirmation(self):
        bridge = make_bridge(registry=self.registry, dispatch=self.dispatch,
                             confirm=None)
        client = TestClient(bridge.app)
        self.addCleanup(client.close)
        response = client.post("/call", headers=AUTH,
                               json={"name": "send_message", "args": {"text": "oi"}})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["tier"], "medium")

        response = self.client.post(
            "/call", headers=AUTH,
            json={"name": "send_message", "args": {"text": "oi"}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.confirmations)

    def test_low_tier_runs_without_confirmation(self):
        response = self.client.post("/call", headers=AUTH,
                                    json={"name": "music_control", "args": {"action": "play"}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tier"], "low")
        self.assertEqual(self.confirmations, [])

    def test_unknown_action_is_high_and_requires_confirmation(self):
        bridge = make_bridge(registry=self.registry, dispatch=self.dispatch,
                             confirm=None)
        client = TestClient(bridge.app)
        self.addCleanup(client.close)
        response = client.post("/call", headers=AUTH,
                               json={"name": "mystery_tool", "args": {}})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["tier"], "high")

    def test_dispatch_exception_returns_500(self):
        def boom(name, args):
            raise RuntimeError("kaboom")

        bridge = make_bridge(registry=self.registry, dispatch=boom)
        client = TestClient(bridge.app)
        self.addCleanup(client.close)
        response = client.post("/call", headers=AUTH,
                               json={"name": "web_search", "args": {}})
        self.assertEqual(response.status_code, 500, response.text)
        self.assertIn("kaboom", response.json()["error"])

    def test_dispatch_timeout_returns_504(self):
        def slow(name, args):
            time.sleep(0.6)
            return "too late"

        bridge = make_bridge(registry=self.registry, dispatch=slow, timeout=0.1)
        client = TestClient(bridge.app)
        self.addCleanup(client.close)
        response = client.post("/call", headers=AUTH,
                               json={"name": "web_search", "args": {}})
        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(response.json()["status"], "timeout")

    def test_confirm_endpoint_delegates(self):
        response = self.client.post("/confirm", headers=AUTH,
                                    json={"reason": "delete the files",
                                          "title": "Are you sure?"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["approved"])
        self.assertEqual(self.confirmations[0][0], "Are you sure?")
        self.assertEqual(self.confirmations[0][1], "delete the files")

    def test_confirm_endpoint_without_channel_is_501(self):
        bridge = make_bridge(registry=self.registry, dispatch=self.dispatch,
                             confirm=None)
        client = TestClient(bridge.app)
        self.addCleanup(client.close)
        response = client.post("/confirm", headers=AUTH, json={"reason": "x"})
        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.json()["status"], "unavailable")


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit_path = Path(self.tmp.name) / "logs" / "tool_bridge.jsonl"

    def _records(self):
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def test_audit_redacts_secret_arguments_and_records_result(self):
        dispatch = RecordingDispatch(result="done")
        bridge = make_bridge(dispatch=dispatch, audit_path=self.audit_path)
        client = TestClient(bridge.app)
        self.addCleanup(client.close)

        secret_args = {
            "query": "visible",
            "api_key": "sk-live-123",
            "token": "tok-abc",
            "password": "hunter2",
            "nested": {
                "secret": "s3cr3t",
                "authorization": "Bearer abc",
                "note": "keep-me",
            },
            "items": [{"apiKey": "camel-secret"}],
        }
        response = client.post("/call", headers=AUTH,
                               json={"name": "web_search", "args": secret_args})
        self.assertEqual(response.status_code, 200, response.text)

        records = self._records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["action"], "web_search")
        self.assertEqual(record["tier"], "safe")
        self.assertTrue(record["approved"])
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["result"], "done")
        self.assertIn("timestamp", record)

        blob = self.audit_path.read_text(encoding="utf-8")
        for leaked in ("sk-live-123", "tok-abc", "hunter2", "s3cr3t", "Bearer abc",
                       "camel-secret"):
            self.assertNotIn(leaked, blob)
        self.assertIn("[REDACTED]", blob)
        self.assertEqual(record["args"]["query"], "visible")
        self.assertEqual(record["args"]["nested"]["note"], "keep-me")

    def test_audit_records_denied_confirmation(self):
        bridge = make_bridge(audit_path=self.audit_path, confirm=None)
        client = TestClient(bridge.app)
        self.addCleanup(client.close)
        response = client.post("/call", headers=AUTH,
                               json={"name": "root_exec", "args": {"command": "reboot"}})
        self.assertEqual(response.status_code, 409)
        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "confirmation_required")
        self.assertFalse(records[0]["approved"])
        self.assertEqual(records[0]["tier"], "high")

    def test_no_audit_path_is_harmless(self):
        bridge = make_bridge(audit_path=None)
        client = TestClient(bridge.app)
        self.addCleanup(client.close)
        response = client.post("/call", headers=AUTH,
                               json={"name": "web_search", "args": {}})
        self.assertEqual(response.status_code, 200)


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.memory = {
            "read": lambda path: f"mem:{path}",
            "search": lambda query, limit=None: [{"path": "a.md", "query": query,
                                                  "limit": limit}],
            "note": lambda text, kind=None: {"saved": text, "kind": kind},
        }
        self.voice = {
            "list": lambda: ["Anthero", "Heloisa"],
            "enroll": lambda name, pcm16_or_embeddings, authorized=False: {
                "name": name, "authorized": authorized,
                "vector": list(pcm16_or_embeddings)},
            "authorize": lambda name: {"authorized": name},
        }
        self.restart = {
            "assistant": lambda reason: f"assistant:{reason}",
            "pc": lambda reason: f"pc:{reason}",
        }
        self.bridge = make_bridge(memory=self.memory, voice=self.voice,
                                  restart=self.restart,
                                  local_ask=lambda prompt: f"local:{prompt}",
                                  start_task=lambda task, mode="answer", approval=None:
                                  f"task:{task}:{mode}:{approval}")
        self.client = TestClient(self.bridge.app)
        self.addCleanup(self.client.close)

    def test_memory_endpoints(self):
        response = self.client.post("/memory/read", headers=AUTH,
                                    json={"path": "memory/chat/hot.md"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"], "mem:memory/chat/hot.md")

        response = self.client.post("/memory/search", headers=AUTH,
                                    json={"query": "gino", "limit": 5})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"][0]["query"], "gino")
        self.assertEqual(response.json()["result"][0]["limit"], 5)

        response = self.client.post("/memory/note", headers=AUTH,
                                    json={"text": "comprou pão", "kind": "fact"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"],
                         {"saved": "comprou pão", "kind": "fact"})

    def test_voice_endpoints(self):
        response = self.client.post("/voice/list", headers=AUTH)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"], ["Anthero", "Heloisa"])

        response = self.client.post("/voice/enroll", headers=AUTH,
                                    json={"name": "Heloisa", "authorized": True})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("microphone", response.json()["detail"])

        response = self.client.post("/voice/enroll", headers=AUTH,
                                    json={"name": "Heloisa", "authorized": True,
                                          "embedding": [0.1, 0.2, 0.3]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"],
                         {"name": "Heloisa", "authorized": True,
                          "vector": [0.1, 0.2, 0.3]})

        response = self.client.post("/voice/authorize", headers=AUTH,
                                    json={"name": "Heloisa"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"], {"authorized": "Heloisa"})

    def test_restart_endpoints(self):
        response = self.client.post("/restart/assistant", headers=AUTH,
                                    json={"reason": "atualização"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"], "assistant:atualização")

        response = self.client.post("/restart/pc", headers=AUTH,
                                    json={"reason": "pedido do dono"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"], "pc:pedido do dono")

    def test_local_ask(self):
        response = self.client.post("/local_ask", headers=AUTH,
                                    json={"prompt": "resuma isto"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"], "local:resuma isto")

    def test_task_start(self):
        response = self.client.post("/task/start", headers=AUTH,
                                    json={"task": "subir", "mode": "write"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"], "task:subir:write:None")

    def test_missing_components_return_501(self):
        bridge = make_bridge()
        client = TestClient(bridge.app)
        self.addCleanup(client.close)
        cases = [
            ("/memory/read", {"path": "x"}),
            ("/memory/search", {"query": "x"}),
            ("/memory/note", {"text": "x"}),
            ("/voice/list", {}),
            ("/voice/enroll", {"name": "x"}),
            ("/voice/authorize", {"name": "x"}),
            ("/restart/assistant", {"reason": "x"}),
            ("/restart/pc", {"reason": "x"}),
            ("/local_ask", {"prompt": "x"}),
        ]
        for path, body in cases:
            with self.subTest(path=path):
                response = client.post(path, headers=AUTH, json=body)
                self.assertEqual(response.status_code, 501, response.text)
                self.assertEqual(response.json()["status"], "unavailable")
                self.assertTrue(response.json()["error"])


class LifecycleTests(unittest.TestCase):
    def test_start_stop_on_os_assigned_port(self):
        bridge = make_bridge(port=0)
        try:
            port = bridge.start()
            self.assertIsInstance(port, int)
            self.assertGreater(port, 0)
            self.assertEqual(bridge.url(), f"http://127.0.0.1:{port}")
            self.assertTrue(bridge.is_running())

            with httpx.Client(trust_env=False, timeout=5.0) as client:
                health = client.get(f"{bridge.url()}/health")
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.json()["status"], "ok")

                self.assertEqual(client.get(f"{bridge.url()}/tools").status_code, 401)
                tools = client.get(f"{bridge.url()}/tools", headers=AUTH)
                self.assertEqual(tools.status_code, 200)
                self.assertEqual(len(tools.json()["tools"]), 3)
        finally:
            bridge.stop()

        self.assertFalse(bridge.is_running())

    def test_start_is_idempotent(self):
        bridge = make_bridge(port=0)
        try:
            first = bridge.start()
            second = bridge.start()
            self.assertEqual(first, second)
        finally:
            bridge.stop()

    def test_stop_without_start_is_safe(self):
        make_bridge().stop()


class VoiceConfirmTests(unittest.TestCase):
    """The bridge confirm channel is the voice window in core/actiongate.py:
    only a spoken consent opens it, the 120 s deadline (not a use count) closes
    it, and the root (sudo) window never bleeds into it."""

    def setUp(self):
        actiongate.close()
        rootgate.close()
        self.addCleanup(actiongate.close)
        self.addCleanup(rootgate.close)
        self.registry = FakeRegistry()
        self.dispatch = RecordingDispatch()
        self.bridge = make_bridge(registry=self.registry, dispatch=self.dispatch,
                                  confirm=actiongate.confirm)
        self.client = TestClient(self.bridge.app)
        self.addCleanup(self.client.close)

    def _send(self):
        return self.client.post("/call", headers=AUTH,
                                json={"name": "send_message",
                                      "args": {"text": "oi"}})

    def test_medium_denied_without_window_and_marks_pending(self):
        response = self._send()
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["status"], "denied")
        self.assertEqual(self.dispatch.calls, [])
        # The refused request waits so the next short consent answers it.
        self.assertTrue(actiongate.has_pending())

    def test_short_consent_with_pending_opens_window_and_action_passes(self):
        self._send()  # refused, now pending
        self.assertTrue(actiongate.looks_like_consent("pode enviar"))
        actiongate.open_window()
        actiongate.clear_pending()
        response = self._send()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.dispatch.calls[0][0], "send_message")

    def test_window_stays_open_for_the_whole_deadline(self):
        actiongate.open_window()
        response = self._send()
        self.assertEqual(response.status_code, 200, response.text)
        # Closing the window is the deadline's job, not the first action's.
        response = self._send()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.dispatch.calls), 2)

    def test_handler_asking_for_a_missing_argument_does_not_spend_the_window(self):
        # The live failure of 31/10: the window was consumed by a call the
        # handler aborted asking "para quem?", so the retry with the contact
        # was refused with 403.
        calls = []

        def dispatch(name, args):
            calls.append((name, dict(args)))
            if not args.get("to"):
                return "para quem? diga o nome do contato ou o número"
            return f"enviado para {args['to']}"

        bridge = make_bridge(registry=self.registry, dispatch=dispatch,
                             confirm=actiongate.confirm)
        client = TestClient(bridge.app)
        self.addCleanup(client.close)

        actiongate.open_window()
        first = client.post(
            "/call", headers=AUTH,
            json={"name": "whatsapp_control",
                  "args": {"action": "send", "text": "oi"}},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertIn("para quem?", first.json()["result"])
        self.assertTrue(actiongate.authorized())   # the abort did not spend it

        second = client.post(
            "/call", headers=AUTH,
            json={"name": "whatsapp_control",
                  "args": {"action": "send", "to": "Vida", "text": "oi"}},
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["result"], "enviado para Vida")
        self.assertEqual(calls[-1][1]["to"], "Vida")

    def test_consent_without_pending_matches_nothing_and_window_stays_closed(self):
        self.assertFalse(actiongate.has_pending())
        response = self._send()
        self.assertEqual(response.status_code, 403, response.text)
        self.assertFalse(actiongate.authorized())

    def test_root_window_does_not_authorise_common_actions(self):
        rootgate.open_window()
        response = self._send()
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.dispatch.calls, [])


if __name__ == "__main__":
    unittest.main()
