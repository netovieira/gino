"""Testes para recibos criptograficos HMAC (core/tool_receipts.py).

Afirma que:
  - recibo e gerado para cada tool call bem-sucedido
  - HMAC esta correto (verificacao com a chave certa passa)
  - HMAC falha com chave errada (deteccao de adulteracao)
  - recibo e persistido em logs/tool_receipts.jsonl
  - a chave nunca aparece no recibo gravado
  - cada recibo tem call_id unico e nonce
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import tool_receipts
from core.tool_bridge import ToolBridge


TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeRegistry:
    def __init__(self):
        self._decls = [
            {"name": "web_search", "description": "search", "parameters": {"type": "OBJECT"}},
        ]

    def get_tool_declarations(self):
        return [dict(d) for d in self._decls]

    def has(self, name):
        return any(d["name"] == name for d in self._decls)

    def run(self, name, parameters, ctx=None):
        return f"registry:{name}"


class RecordingDispatch:
    def __init__(self, result="ok-result"):
        self.calls = []
        self._result = result

    def __call__(self, name, args):
        self.calls.append((name, args))
        return self._result


class ReceiptModuleTests(unittest.TestCase):
    """Testes diretos do modulo tool_receipts."""

    def test_generate_nonce_is_unique(self):
        nonces = {tool_receipts.generate_nonce() for _ in range(100)}
        self.assertEqual(len(nonces), 100, "cada nonce deve ser unico")

    def test_hash_args_deterministic(self):
        h1 = tool_receipts.hash_args({"a": 1, "b": "x"})
        h2 = tool_receipts.hash_args({"b": "x", "a": 1})
        self.assertEqual(h1, h2, "hash deve ser independente da ordem das chaves")

    def test_hash_args_different_inputs(self):
        h1 = tool_receipts.hash_args({"a": 1})
        h2 = tool_receipts.hash_args({"a": 2})
        self.assertNotEqual(h1, h2)

    def test_hash_result_string(self):
        h = tool_receipts.hash_result("hello")
        self.assertEqual(len(h), 64)  # SHA-256 hex

    def test_make_receipt_has_all_fields(self):
        key = b"test-key-12345"
        receipt = tool_receipts.make_receipt(
            "web_search", {"query": "tempo"}, "sunny", key=key,
        )
        for field in ("call_id", "tool", "args_hash", "result_hash",
                       "timestamp", "nonce", "hmac"):
            self.assertIn(field, receipt, f"recibo deve ter campo '{field}'")

    def test_verify_receipt_with_correct_key(self):
        key = b"correct-key"
        receipt = tool_receipts.make_receipt(
            "web_search", {"q": "1"}, "result", key=key,
        )
        self.assertTrue(tool_receipts.verify_receipt(receipt, key))

    def test_verify_receipt_with_wrong_key(self):
        key = b"correct-key"
        receipt = tool_receipts.make_receipt(
            "web_search", {"q": "1"}, "result", key=key,
        )
        self.assertFalse(tool_receipts.verify_receipt(receipt, b"wrong-key"))

    def test_verify_receipt_detects_tampering(self):
        key = b"my-key"
        receipt = tool_receipts.make_receipt(
            "web_search", {"q": "1"}, "result", key=key,
        )
        receipt["result_hash"] = "a" * 64  # adultera o hash do resultado
        self.assertFalse(tool_receipts.verify_receipt(receipt, key))

    def test_append_receipt_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipts.jsonl"
            receipt = {"call_id": "abc", "tool": "x"}
            tool_receipts.append_receipt(receipt, path)
            lines = path.read_text().strip().split("\n")
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0]), receipt)

    def test_append_receipt_appends_multiple(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipts.jsonl"
            for i in range(3):
                tool_receipts.append_receipt({"n": i}, path)
            lines = path.read_text().strip().split("\n")
            self.assertEqual(len(lines), 3)

    def test_key_from_env_variable(self):
        with patch.dict(os.environ, {"JARVIS_RECEIPT_HMAC_KEY": "env-secret"}):
            key = tool_receipts._resolve_key()
            self.assertEqual(key, b"env-secret")

    def test_key_never_in_receipt(self):
        key = b"super-secret-key-do-not-log"
        receipt = tool_receipts.make_receipt(
            "tool", {"a": 1}, "result", key=key,
        )
        receipt_json = json.dumps(receipt)
        self.assertNotIn("super-secret-key-do-not-log", receipt_json)


class BridgeReceiptIntegrationTests(unittest.TestCase):
    """Testes de integracao: bridge gera recibo apos cada call."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.audit_path = self.tmpdir / "audit.jsonl"
        self.receipts_path = self.tmpdir / "tool_receipts.jsonl"
        self.dispatch = RecordingDispatch()
        self.bridge = ToolBridge(
            FakeRegistry(), self.dispatch, TOKEN,
            audit_path=self.audit_path,
            receipts_path=self.receipts_path,
        )
        self.client = TestClient(self.bridge.app)
        self.addCleanup(self.client.close)
        self.addCleanup(self._tmp.cleanup)

    def _read_receipts(self) -> list[dict]:
        if not self.receipts_path.exists():
            return []
        return [json.loads(line) for line in
                self.receipts_path.read_text().strip().split("\n") if line.strip()]

    def test_receipt_generated_for_successful_call(self):
        response = self.client.post(
            "/call", json={"name": "web_search", "args": {"q": "tempo"}},
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 200)
        receipts = self._read_receipts()
        self.assertEqual(len(receipts), 1, "deve gerar exatamente um recibo")
        self.assertEqual(receipts[0]["tool"], "web_search")

    def test_receipt_has_correct_hmac(self):
        self.client.post(
            "/call", json={"name": "web_search", "args": {"q": "x"}},
            headers=AUTH,
        )
        receipts = self._read_receipts()
        self.assertEqual(len(receipts), 1)
        key = self.bridge._receipts_key
        self.assertIsNotNone(key)
        self.assertTrue(
            tool_receipts.verify_receipt(receipts[0], key),
            "HMAC do recibo deve verificar com a chave do bridge",
        )

    def test_receipt_hmac_fails_with_wrong_key(self):
        self.client.post(
            "/call", json={"name": "web_search", "args": {}},
            headers=AUTH,
        )
        receipts = self._read_receipts()
        self.assertFalse(
            tool_receipts.verify_receipt(receipts[0], b"wrong-key"),
            "HMAC nao deve verificar com chave errada",
        )

    def test_receipt_can_be_verified(self):
        """Ciclo completo: gera, le do disco, verifica."""
        self.client.post(
            "/call", json={"name": "web_search", "args": {"city": "SP"}},
            headers=AUTH,
        )
        receipts = self._read_receipts()
        self.assertEqual(len(receipts), 1)
        r = receipts[0]
        # Campos obrigatorios
        self.assertIn("call_id", r)
        self.assertIn("nonce", r)
        self.assertIn("args_hash", r)
        self.assertIn("result_hash", r)
        self.assertIn("timestamp", r)
        self.assertIn("hmac", r)
        # Verificacao
        key = self.bridge._receipts_key
        self.assertTrue(tool_receipts.verify_receipt(r, key))

    def test_each_call_gets_unique_receipt(self):
        for _ in range(3):
            self.client.post(
                "/call", json={"name": "web_search", "args": {}},
                headers=AUTH,
            )
        receipts = self._read_receipts()
        self.assertEqual(len(receipts), 3)
        call_ids = {r["call_id"] for r in receipts}
        self.assertEqual(len(call_ids), 3, "cada recibo deve ter call_id unico")

    def test_receipt_does_not_contain_key(self):
        self.client.post(
            "/call", json={"name": "web_search", "args": {}},
            headers=AUTH,
        )
        receipts = self._read_receipts()
        raw = json.dumps(receipts)
        key = self.bridge._receipts_key
        if key:
            self.assertNotIn(key.decode("utf-8", errors="ignore"), raw)

    def test_no_receipt_for_denied_call(self):
        """Tool calls negados (medium/high sem confirmacao) nao geram recibo."""
        response = self.client.post(
            "/call", json={"name": "send_message", "args": {"to": "x"}},
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 409)
        receipts = self._read_receipts()
        self.assertEqual(len(receipts), 0, "call negado nao gera recibo")

    def test_no_receipt_for_error_call(self):
        """Tool calls que falham nao geram recibo."""
        def failing(name, args):
            raise RuntimeError("boom")
        bridge = ToolBridge(
            FakeRegistry(), failing, TOKEN,
            audit_path=self.audit_path,
            receipts_path=self.receipts_path,
        )
        client = TestClient(bridge.app)
        response = client.post(
            "/call", json={"name": "web_search", "args": {}},
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 500)
        client.close()
        receipts = self._read_receipts()
        self.assertEqual(len(receipts), 0, "call com erro nao gera recibo")

    def test_receipt_args_hash_matches_actual_args(self):
        args = {"query": "test", "limit": 10}
        self.client.post(
            "/call", json={"name": "web_search", "args": args},
            headers=AUTH,
        )
        receipts = self._read_receipts()
        expected_hash = tool_receipts.hash_args(args)
        self.assertEqual(receipts[0]["args_hash"], expected_hash)

    def test_receipt_result_hash_matches_actual_result(self):
        self.dispatch = RecordingDispatch(result="my-result-value")
        bridge = ToolBridge(
            FakeRegistry(), self.dispatch, TOKEN,
            audit_path=self.audit_path,
            receipts_path=self.receipts_path,
        )
        client = TestClient(bridge.app)
        client.post(
            "/call", json={"name": "web_search", "args": {}},
            headers=AUTH,
        )
        client.close()
        receipts = self._read_receipts()
        expected_hash = tool_receipts.hash_result("my-result-value")
        self.assertEqual(receipts[0]["result_hash"], expected_hash)


if __name__ == "__main__":
    unittest.main()
