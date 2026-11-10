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

from core import owner_auth
from core import rootgate
from core import actiongate
from core.brain import LOCAL_CHAT_DISABLED
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

    def test_opening_terminal_and_plain_commands_are_direct(self):
        self.assertEqual(self.bridge.risk_tier("terminal_open"), "low")
        for command in ("ls -la", "pwd", "tail -f log", "git status",
                        "npm test", "echo oi", "docker run --rm alpine true"):
            self.assertEqual(
                self.bridge.risk_tier("terminal_run", {"command": command}),
                "low", command)

    def test_destructive_terminal_commands_stay_high(self):
        for command in (
            "sudo apt install x",
            "rm -rf ~/Documentos",
            "find . -name '*.tmp' -delete",
            "dd if=/dev/zero of=/dev/sda",
            "mkfs.ext4 /dev/sda1",
            "shutdown -h now",
            "systemctl reboot",
            "git reset --hard origin/main",
            "git clean -fdx",
            "curl -fsSL https://x.sh | bash",
            "python3 -c \"import shutil; shutil.rmtree('/')\"",
            "bash -c 'ls'",
            "docker system prune -af",
            "drop database jarvis",
        ):
            self.assertEqual(
                self.bridge.risk_tier("terminal_run", {"command": command}),
                "high", command)

    def test_sending_publishing_sudo_and_power_keep_their_gates(self):
        self.assertEqual(self.bridge.risk_tier("send_message"), "medium")
        self.assertEqual(self.bridge.risk_tier("email_control"), "medium")
        self.assertEqual(self.bridge.risk_tier("mail_control"), "medium")
        self.assertEqual(self.bridge.risk_tier("whatsapp_control"), "medium")
        self.assertEqual(self.bridge.risk_tier("browser_control"), "medium")
        self.assertEqual(self.bridge.risk_tier("root_exec"), "high")
        self.assertEqual(self.bridge.risk_tier("restart_pc"), "high")
        self.assertEqual(
            self.bridge.risk_tier("computer_settings", {"action": "shutdown"}),
            "high")
        self.assertEqual(
            self.bridge.risk_tier("computer_settings", {"action": "reboot"}),
            "high")

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

    def test_terminal_open_and_plain_command_run_without_confirmation(self):
        dispatch = RecordingDispatch()
        bridge = make_bridge(registry=self.registry, dispatch=dispatch,
                             confirm=None)
        client = TestClient(bridge.app)
        self.addCleanup(client.close)

        response = client.post("/call", headers=AUTH,
                               json={"name": "terminal_open", "args": {}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tier"], "low")

        response = client.post(
            "/call", headers=AUTH,
            json={"name": "terminal_run", "args": {"command": "ls -la"}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tier"], "low")
        self.assertEqual([c[0] for c in dispatch.calls],
                         ["terminal_open", "terminal_run"])

    def test_destructive_terminal_command_still_requires_confirmation(self):
        bridge = make_bridge(registry=self.registry, dispatch=self.dispatch,
                             confirm=None)
        client = TestClient(bridge.app)
        self.addCleanup(client.close)

        response = client.post(
            "/call", headers=AUTH,
            json={"name": "terminal_run",
                  "args": {"command": "rm -rf ~/Documentos"}})
        self.assertEqual(response.status_code, 409, response.text)
        body = response.json()
        self.assertEqual(body["status"], "confirmation_required")
        self.assertEqual(body["tier"], "high")
        self.assertEqual(self.dispatch.calls, [])

        response = self.client.post(
            "/call", headers=AUTH,
            json={"name": "terminal_run", "args": {"command": "sudo reboot"}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.confirmations), 1)
        self.assertIn("terminal_run", self.confirmations[0][0])
        self.assertEqual(self.dispatch.calls[0][0], "terminal_run")

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
        parsed = [json.loads(line) for line in lines if line.strip()]
        # Filter out HMAC receipts (they have 'call_id'+'hmac' instead of 'action')
        return [r for r in parsed if "action" in r]

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
                                  local_ask=lambda prompt: LOCAL_CHAT_DISABLED,
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

    def test_local_ask_reports_the_model_is_disabled(self):
        response = self.client.post("/local_ask", headers=AUTH,
                                    json={"prompt": "resuma isto"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertIn("desligado", result.lower())
        self.assertIn("Qoder", result)
        self.assertEqual(result, LOCAL_CHAT_DISABLED)

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

    def test_consent_spoken_before_the_bridge_parks_the_send_is_not_lost(self):
        # 02/11/2026: the owner said "Pode enviar" at 16:04:16 and 16:12:11,
        # but the whatsapp_control call only parked the request at 16:04:33
        # and 16:12:27 — after the words. The armed send window must let the
        # call that lands later through, scoped to WhatsApp.
        actiongate.arm_send_window()
        self.assertFalse(actiongate.has_pending())
        response = self.client.post(
            "/call", headers=AUTH,
            json={"name": "whatsapp_control",
                  "args": {"action": "send", "to": "Vida", "text": "oi"}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.dispatch.calls[0][0], "whatsapp_control")

    def test_armed_send_window_does_not_authorise_another_tool(self):
        actiongate.arm_send_window()
        response = self._send()   # send_message, not whatsapp_control
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.dispatch.calls, [])

    def test_root_window_does_not_authorise_common_actions(self):
        rootgate.open_window()
        response = self._send()
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.dispatch.calls, [])


class DenialMemoryTests(unittest.TestCase):
    """A refused call stays refused without a new prompt; only a fresh spoken
    consent (core/actiongate.open_window) lets the identical retry through."""

    def setUp(self):
        actiongate.close()
        self.addCleanup(actiongate.close)
        self.dispatch = RecordingDispatch()
        self.bridge = make_bridge(dispatch=self.dispatch,
                                  confirm=actiongate.confirm,
                                  authorized=actiongate.authorized)
        self.client = TestClient(self.bridge.app)
        self.addCleanup(self.client.close)

    def _send_whatsapp(self, to="Vida", text="oi"):
        return self.client.post(
            "/call", headers=AUTH,
            json={"name": "whatsapp_control",
                  "args": {"action": "send", "to": to, "text": text}},
        )

    def test_first_attempt_is_a_normal_refusal(self):
        first = self._send_whatsapp()
        self.assertEqual(first.status_code, 403, first.text)
        self.assertEqual(first.json()["status"], "denied")
        self.assertFalse(first.json().get("repeated"))
        self.assertEqual(self.dispatch.calls, [])
        self.assertTrue(actiongate.has_pending())

    def test_identical_repeat_is_suppressed_without_reopening_the_request(self):
        first = self._send_whatsapp()
        self.assertEqual(first.status_code, 403, first.text)
        parked = actiongate.pending()

        second = self._send_whatsapp()
        self.assertEqual(second.status_code, 403, second.text)
        body = second.json()
        self.assertTrue(body["repeated"])
        self.assertTrue(body["awaiting_voice"])
        self.assertEqual(body["status"], "denied")
        # Redacao da decisao 14 (dono, 10/11/2026): toda recusa nomeia as DUAS
        # portas, no mesmo pe -- a voz dele ou a senha digitada. O que este
        # teste prova nao mudou: a repeticao identica e suprimida sem reabrir
        # a janela (assercoes abaixo); a frase so diz COMO o dono responde.
        self.assertIn("still awaiting the owner's authorisation",
                      body["message"])
        self.assertIn(owner_auth.FACTOR_VOICE, body["message"])
        self.assertIn(owner_auth.FACTOR_TEXT, body["message"])
        self.assertEqual(self.dispatch.calls, [])
        # No new prompt: the parked request is exactly the first one.
        self.assertEqual(actiongate.pending(), parked)
        # The message body is never kept, only a short digest.
        self.assertNotIn("oi", json.dumps(self.bridge._denials))

    def test_new_spoken_consent_allows_the_same_call(self):
        self.assertEqual(self._send_whatsapp().status_code, 403)
        actiongate.open_window()          # the owner said "pode"
        again = self._send_whatsapp()
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(self.dispatch.calls[0][0], "whatsapp_control")
        self.assertEqual(self.bridge._denials, {})

    def test_suppression_is_scoped_to_action_and_recipient(self):
        self.assertEqual(self._send_whatsapp(to="Vida").status_code, 403)
        other_contact = self._send_whatsapp(to="Ana")
        self.assertFalse(other_contact.json().get("repeated"))
        other_action = self.client.post(
            "/call", headers=AUTH,
            json={"name": "send_message", "args": {"text": "oi"}},
        )
        self.assertEqual(other_action.status_code, 403, other_action.text)
        self.assertFalse(other_action.json().get("repeated"))

    def test_a_lapsed_voice_park_is_rearmed_without_a_new_prompt(self):
        # The bridge does not re-ask the model, but the owner's "pode" is still
        # the answer to the original question; actiongate drops a park after
        # five minutes, so a suppressed retry re-parks it (question, not prompt).
        self.assertEqual(self._send_whatsapp().status_code, 403)
        self.assertTrue(actiongate.has_pending())
        self._lapse_the_park()
        self.assertFalse(actiongate.has_pending())

        repeat = self._send_whatsapp()
        self.assertEqual(repeat.status_code, 403, repeat.text)
        self.assertTrue(repeat.json()["repeated"])
        self.assertTrue(actiongate.has_pending())
        self.assertTrue(actiongate.looks_like_consent("pode"))

    def test_the_cancelling_refusal_does_not_rearm_the_voice_park(self):
        self.assertEqual(self._send_whatsapp().status_code, 403)   # 1
        self._lapse_the_park()
        self.assertEqual(self._send_whatsapp().status_code, 403)   # 2, re-armed
        self.assertTrue(actiongate.has_pending())
        self._lapse_the_park()
        third = self._send_whatsapp()                              # 3, cancelled
        self.assertEqual(third.status_code, 403, third.text)
        self.assertTrue(third.json()["cancelled"])
        self.assertFalse(actiongate.has_pending())

    @staticmethod
    def _lapse_the_park():
        """Pretend the five-minute voice answer window elapsed."""
        with actiongate._lock:
            actiongate._pending["until"] = time.monotonic() - 1.0


class ManualClock:
    """Wall clock double: the pending ledger must use real time, so tests move
    the clock instead of sleeping through a six-hour TTL."""

    def __init__(self, now=1_000_000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class PendingLedgerTests(unittest.TestCase):
    """A refused request is pending, not a retry loop.

    The 2026-10-31 incident: a voice-note joke was refused all night and still
    came back ~10 times in 30 h with a reworded caption. The bridge now keeps
    the refused request on disk, keyed by payload (not by caption), with a TTL,
    a refusal limit, and a dead mark that survives a restart.
    """

    JOKE = "/tmp/piada-gino-20261031-235422.ogg"
    PHONE = "5517981792464"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pending = Path(self.tmp.name) / "logs" / "tool_bridge_pending.json"
        self.clock = ManualClock()

    # ── helpers ────────────────────────────────────────────────────────────

    def _bridge(self, confirmed=False, authorized=lambda: False, logs=None,
                clock=None, **kwargs):
        logs = logs if logs is not None else []
        prompts = []
        dispatch = RecordingDispatch()

        def confirm(title, message):
            prompts.append((title, message))
            return confirmed() if callable(confirmed) else confirmed

        bridge = ToolBridge(FakeRegistry(), dispatch, TOKEN,
                            logger=logs.append, confirm=confirm,
                            authorized=authorized, pending_path=self.pending,
                            clock=clock or self.clock, **kwargs)
        client = TestClient(bridge.app)
        self.addCleanup(client.close)
        return bridge, client, prompts, dispatch, logs

    def _send_joke(self, client, caption="Piada, em voz.", message=None):
        args = {"action": "send_file", "to": self.PHONE, "path": self.JOKE,
                "caption": caption}
        if message is not None:
            args["message"] = message
        return client.post(
            "/call", headers=AUTH,
            json={"name": "whatsapp_control", "args": args},
        )

    def _entries(self):
        return json.loads(self.pending.read_text(encoding="utf-8"))["entries"]

    # ── TTL ────────────────────────────────────────────────────────────────

    def test_pending_action_expires_after_the_ttl_and_is_marked_dead(self):
        bridge, client, prompts, dispatch, logs = self._bridge(pending_ttl=3600)

        first = self._send_joke(client)
        self.assertEqual(first.status_code, 403, first.text)
        self.assertEqual(len(prompts), 1)
        entry = next(iter(self._entries().values()))
        self.assertEqual(entry["status"], "denied")
        self.assertEqual(entry["until"], self.clock.now + 3600)

        # A retry does not renew the deadline: only the owner does.
        self.clock.advance(1800)
        repeat = self._send_joke(client, caption="Agora sim, vindo do zap do Gino")
        self.assertEqual(repeat.status_code, 403, repeat.text)
        self.assertTrue(repeat.json()["repeated"])
        self.assertEqual(len(prompts), 1)
        self.assertEqual(next(iter(self._entries().values()))["until"],
                         self.clock.now - 1800 + 3600)

        # One second past the TTL the request is dead, in the store.
        self.clock.advance(1801)
        expired = self._send_joke(client, caption="outra legenda qualquer")
        self.assertEqual(expired.status_code, 403, expired.text)
        body = expired.json()
        self.assertTrue(body["expired"])
        self.assertFalse(body["cancelled"])
        self.assertFalse(body["awaiting_voice"])
        self.assertEqual(len(prompts), 1)
        self.assertEqual(dispatch.calls, [])
        stored = next(iter(self._entries().values()))
        self.assertEqual(stored["status"], "expired")
        lines = [line for line in logs
                 if line.startswith("SYS: ação pendente expirada: ")]
        self.assertEqual(len(lines), 1, logs)
        self.assertIn("whatsapp_control", lines[0])
        # The log line appears once: a dead mark is never logged again.
        self._send_joke(client, caption="mais uma")
        self.assertEqual([line for line in logs
                          if line.startswith("SYS: ação pendente expirada: ")],
                         lines)

    # ── refusal limit ──────────────────────────────────────────────────────

    def test_three_refusals_cancel_the_pending_action_for_good(self):
        bridge, client, prompts, dispatch, logs = self._bridge()

        first = self._send_joke(client)
        self.assertEqual(first.status_code, 403, first.text)
        self.assertFalse(first.json().get("repeated"))

        second = self._send_joke(client)
        self.assertEqual(second.status_code, 403, second.text)
        self.assertTrue(second.json()["repeated"])
        self.assertFalse(second.json().get("cancelled"))
        self.assertEqual(second.json()["attempts"], 2)

        third = self._send_joke(client)
        self.assertEqual(third.status_code, 403, third.text)
        self.assertTrue(third.json()["cancelled"])
        self.assertFalse(third.json()["awaiting_voice"])

        cancelled = [line for line in logs
                     if line.startswith("SYS: ação pendente cancelada por negativas (3x): ")]
        self.assertEqual(len(cancelled), 1, logs)
        self.assertIn("whatsapp_control", cancelled[0])
        self.assertEqual(next(iter(self._entries().values()))["status"], "cancelled")

        # One prompt total; a fourth attempt with another caption never prompts.
        self.assertEqual(len(prompts), 1)
        fourth = self._send_joke(client, caption="Agora sim, vindo do zap do Gino")
        self.assertEqual(fourth.status_code, 403, fourth.text)
        self.assertTrue(fourth.json()["cancelled"])
        self.assertEqual(len(prompts), 1)
        self.assertEqual(dispatch.calls, [])

    def test_unanswered_confirmation_timeouts_count_too(self):
        prompts = []
        dispatch = RecordingDispatch()

        def silent(title, message):
            prompts.append((title, message))
            time.sleep(0.3)
            return False

        bridge = ToolBridge(FakeRegistry(), dispatch, TOKEN,
                            logger=lambda _m: None, confirm=silent,
                            authorized=lambda: False, pending_path=self.pending,
                            clock=self.clock, timeout=0.05)
        client = TestClient(bridge.app)
        self.addCleanup(client.close)

        first = self._send_joke(client)
        self.assertEqual(first.status_code, 504, first.text)   # unanswered
        second = self._send_joke(client)
        self.assertEqual(second.status_code, 403, second.text)
        self.assertTrue(second.json()["repeated"])
        third = self._send_joke(client)
        self.assertEqual(third.status_code, 403, third.text)
        self.assertTrue(third.json()["cancelled"])
        self.assertEqual(len(prompts), 1)

    # ── payload dedupe ────────────────────────────────────────────────────

    def test_payload_dedupe_ignores_reworded_captions(self):
        base = {"action": "send_file", "to": self.PHONE, "path": self.JOKE}
        key_a = ToolBridge._denial_key(
            "whatsapp_control", dict(base, caption="Piada, em voz."))
        key_b = ToolBridge._denial_key(
            "whatsapp_control", dict(base, caption="Agora sim, vindo do zap do Gino"))
        key_c = ToolBridge._denial_key(
            "whatsapp_control", dict(base, caption="x", message="outra legenda"))
        self.assertEqual(key_a, key_b)
        self.assertEqual(key_a, key_c)
        other_file = ToolBridge._denial_key(
            "whatsapp_control", dict(base, path="/tmp/outra.ogg"))
        self.assertNotEqual(key_a, other_file)
        # The message text is the payload for a text send: not cosmetic.
        text_a = ToolBridge._denial_key(
            "send_message", {"platform": "WhatsApp", "receiver": "Vida", "text": "oi"})
        text_b = ToolBridge._denial_key(
            "send_message", {"platform": "WhatsApp", "receiver": "Vida", "text": "outra"})
        self.assertNotEqual(text_a, text_b)
        # Nor are labels that actually select what runs: a window title and a
        # code request are payload, not captions.
        window_a = ToolBridge._denial_key(
            "computer_control", {"action": "focus_window", "title": "Thiago York"})
        window_b = ToolBridge._denial_key(
            "computer_control", {"action": "focus_window", "title": "Terminal"})
        self.assertNotEqual(window_a, window_b)
        code_a = ToolBridge._denial_key(
            "code_helper", {"action": "write", "description": "criar login"})
        code_b = ToolBridge._denial_key(
            "code_helper", {"action": "write", "description": "apagar login"})
        self.assertNotEqual(code_a, code_b)

    def test_reworded_caption_does_not_reopen_the_prompt(self):
        bridge, client, prompts, dispatch, _logs = self._bridge()
        self.assertEqual(self._send_joke(client).status_code, 403)
        retry = self._send_joke(client, caption="Agora sim, vindo do zap do Gino",
                                message="legenda nova")
        self.assertEqual(retry.status_code, 403, retry.text)
        self.assertTrue(retry.json()["repeated"])
        self.assertEqual(len(prompts), 1)
        self.assertEqual(len(self._entries()), 1)

    def test_the_ledger_never_stores_the_message_body(self):
        bridge, client, _prompts, _dispatch, _logs = self._bridge()
        self.assertEqual(self._send_joke(client, caption="Piada, em voz.").status_code, 403)
        blob = self.pending.read_text(encoding="utf-8") + json.dumps(bridge._denials)
        self.assertNotIn("Piada", blob)
        self.assertNotIn(self.JOKE, blob)

    # ── persistence across restarts ───────────────────────────────────────

    def test_cancelled_state_survives_a_restart(self):
        _bridge, client, _prompts, _dispatch, _logs = self._bridge()
        for _ in range(3):
            self.assertEqual(self._send_joke(client).status_code, 403)
        self.assertEqual(next(iter(self._entries().values()))["status"], "cancelled")

        # A fresh process, same store: the dead mark is loaded, not forgotten.
        restarted, client2, prompts2, dispatch2, _logs2 = self._bridge(confirmed=True)
        response = self._send_joke(client2, caption="legenda totalmente nova")
        self.assertEqual(response.status_code, 403, response.text)
        self.assertTrue(response.json()["cancelled"])
        self.assertEqual(prompts2, [])
        self.assertEqual(dispatch2.calls, [])
        self.assertEqual(restarted.pending_actions()[0]["status"], "cancelled")

    def test_restart_applies_the_ttl_before_anything_can_replay(self):
        _bridge, client, prompts, _dispatch, _logs = self._bridge(pending_ttl=3600)
        self.assertEqual(self._send_joke(client).status_code, 403)
        self.assertEqual(len(prompts), 1)

        later = ManualClock(self.clock.now + 3601)
        logs2 = []
        restarted, client2, prompts2, dispatch2, _logs2 = self._bridge(
            confirmed=True, clock=later, logs=logs2, pending_ttl=3600)
        self.assertTrue(any(line.startswith("SYS: ação pendente expirada: ")
                            for line in logs2), logs2)
        self.assertEqual(next(iter(self._entries().values()))["status"], "expired")

        response = self._send_joke(client2, caption="legenda nova")
        self.assertEqual(response.status_code, 403, response.text)
        self.assertTrue(response.json()["expired"])
        self.assertEqual(prompts2, [])
        self.assertEqual(dispatch2.calls, [])

    def test_a_fresh_owner_consent_reopens_the_request(self):
        consent = {"open": False}
        bridge, client, prompts, dispatch, _logs = self._bridge(
            confirmed=lambda: consent["open"],
            authorized=lambda: consent["open"])
        for _ in range(3):
            self.assertEqual(self._send_joke(client).status_code, 403)
        self.assertTrue(next(iter(self._entries().values()))["status"] == "cancelled")

        consent["open"] = True          # the owner said "pode"
        response = self._send_joke(client, caption="Agora sim, vindo do zap do Gino")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(dispatch.calls[0][0], "whatsapp_control")
        self.assertEqual(bridge._denials, {})
        self.assertEqual(self._entries(), {})
        self.assertGreaterEqual(len(prompts), 2)   # the consent reopened it


if __name__ == "__main__":
    unittest.main()
