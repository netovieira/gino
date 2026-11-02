"""With Qoder Live on, every injected turn belongs to the one Qoder session."""
from __future__ import annotations

import threading
import unittest
from unittest import mock

import main
from core import brain
from core.brain import LOCAL_CHAT_DISABLED


class FakeQueue:
    def __init__(self):
        self.items = []
    def put_nowait(self, item):
        self.items.append(item)


class FakeOwner:
    def __init__(self, enabled=True, ready=True):
        self._qoder_live_enabled = enabled
        self._ready = ready
        self._brain_q = FakeQueue()
        self._qoder_turn_backlog = []
        self.turns = []
        self.recorded = []
        self._qoder_lock = threading.RLock()

    def _qoder_live_ready(self):
        return self._ready

    def _qoder_turn(self, text, pcm=None, retry=False):
        self.turns.append(text)

    def _memory_record_user(self, text):
        self.recorded.append(text)

    def _qoder_hold(self, text, pcm=None, reason=""):
        self._qoder_turn_backlog.append({"text": text, "pcm": pcm})


async def _send(owner, text):
    session = main._EconomySession(owner)
    await session.send_client_content(
        turns={"role": "user", "parts": [{"text": text}]},
        turn_complete=True)


class SingleBrainTest(unittest.IsolatedAsyncioTestCase):
    async def test_ready_turn_goes_to_qoder_not_local(self):
        owner = FakeOwner()
        await _send(owner, "bom dia")
        self.assertEqual(owner.turns, ["bom dia"])
        self.assertEqual(owner.recorded, ["bom dia"])
        self.assertEqual(owner._brain_q.items, [])

    async def test_warming_turn_is_held_for_qoder(self):
        owner = FakeOwner(ready=False)
        await _send(owner, "bom dia")
        self.assertEqual(owner.turns, [])
        self.assertEqual([i["text"] for i in owner._qoder_turn_backlog],
                         ["bom dia"])
        self.assertEqual(owner._brain_q.items, [])

    async def test_flag_off_keeps_the_old_economy_path(self):
        owner = FakeOwner(enabled=False, ready=False)
        await _send(owner, "bom dia")
        self.assertEqual(owner._brain_q.items, ["bom dia"])
        self.assertEqual(owner.turns, [])


class LocalModelRetiredTest(unittest.TestCase):
    def test_local_ask_is_explicitly_disabled(self):
        answer = main.JarvisLive._local_ask(None, "resuma isto")
        self.assertEqual(answer, LOCAL_CHAT_DISABLED)
        self.assertIn("desligado", answer.lower())

    def test_custom_provider_is_no_longer_a_rung(self):
        self.assertNotIn("custom", brain.PRESETS)
        self.assertNotIn("custom", brain.FALLBACK_PROVIDERS)
        self.assertIsNone(brain._entry("custom", base_url="http://127.0.0.1:11434/v1"))
        self.assertIsNone(brain._entry("deepseek",
                                       base_url="http://127.0.0.1:11434/v1"))

    def test_stale_custom_config_becomes_a_cloud_rung(self):
        cfg = {"provider": "custom", "model": "qwen2.5:7b-instruct",
               "base_url": "http://127.0.0.1:11434/v1"}

        def plugin_config(namespace):
            return cfg if namespace == "brain" else {"api_key": ""}

        st = brain.settings(plugin_config)
        self.assertEqual(st["provider"], "deepseek")
        self.assertEqual(st["chain"], [{"provider": "deepseek",
                                        "base_url": "https://api.deepseek.com/v1",
                                        "model": "deepseek-chat",
                                        "api_key": ""}])
        self.assertFalse(any("11434" in r.get("base_url", "") for r in st["chain"]))
        # Vision is independent and, since phase 2, served by Qoder by default.
        self.assertEqual(st["vision_provider"], "qoder")
        self.assertTrue(st["vision_enabled"])
        self.assertFalse(st["vision_local"])
        # The local backend is still configured; phase 3 decides its removal.
        self.assertIn("11434", st["vision_base_url"])

    def test_groq_fallback_reuses_the_key_saved_for_the_ears(self):
        cfg = {"provider": "qodercli", "fallback_provider": "groq",
               "fallback_model": "", "fallback_api_key": ""}

        def plugin_config(namespace):
            if namespace == "brain":
                return cfg
            return {"api_key": "gsk_test_key"} if namespace == "groq" else {}

        st = brain.settings(plugin_config)
        groq = [r for r in st["chain"] if r["provider"] == "groq"]
        self.assertEqual(len(groq), 1)
        self.assertEqual(groq[0]["api_key"], "gsk_test_key")

    def test_tool_turn_with_only_qodercli_fails_with_an_honest_message(self):
        cfg = {"provider": "qodercli"}

        def plugin_config(namespace):
            return cfg if namespace == "brain" else {}

        st = brain.settings(plugin_config)
        self.assertEqual([r["provider"] for r in st["chain"]], ["qodercli"])
        tools = [{"type": "function",
                  "function": {"name": "noop", "parameters": {"type": "object"}}}]
        with self.assertRaises(RuntimeError) as ctx:
            brain.chat_stream([{"role": "user", "content": "abre o navegador"}],
                              tools, st, lambda _s: None)
        message = str(ctx.exception).lower()
        self.assertIn("ferramentas", message)
        self.assertIn("fallback", message)

    def test_memory_summarize_goes_to_qoder_not_local(self):
        seen = {}

        def fake_ask(prompt, st=None, model=None, system=None, max_tokens=0,
                     temperature=0.3, timeout=None):
            seen.update({"prompt": prompt, "model": model, "system": system})
            return '{"summary": "ok"}'

        with mock.patch("core.qoder.ask", fake_ask):
            text = main.JarvisLive._memory_summarize(None, "resuma as falas")
        self.assertEqual(text, '{"summary": "ok"}')
        self.assertEqual(seen["prompt"], "resuma as falas")
        self.assertEqual(seen["model"], "Qwen3.8-Flash")
        self.assertIn("JSON", seen["system"])


if __name__ == "__main__":
    unittest.main()
