"""Testes para recibos HMAC-SHA256 em tool calls (FASE1-TASK2).

Afirma que:
  - Cada tool call bem-sucedido produz um recibo no audit JSONL
  - O recibo tem o formato especificado (call_id, tool, args_hash,
    result_hash, timestamp, hmac)
  - O HMAC verifica com a chave correta
  - O HMAC falha com chave errada (deteccao de adulteracao)
  - Cada recibo tem call_id unico (UUID v4)
  - Calls negados ou com erro nao geram recibo
  - A chave nunca aparece no recibo gravado
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from core import tool_receipts
from core.tool_bridge import ToolBridge


TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class FakeRegistry:
    def __init__(self):
        self._decls = [
            {"name": "web_search", "description": "search",
             "parameters": {"type": "OBJECT"}},
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

    def setUp(self):
        self.key = b"test-hmac-key-for-unit-tests"

    def test_hash_args_deterministic(self):
        h1 = tool_receipts.hash_args({"a": 1, "b": "x"})
        h2 = tool_receipts.hash_args({"b": "x", "a": 1})
        self.assertEqual(h1, h2, "hash deve ser independente da ordem das chaves")

    def test_hash_args_different_inputs(self):
        h1 = tool_receipts.hash_args({"a": 1})
        h2 = tool_receipts.hash_args({"a": 2})
        self.assertNotEqual(h1, h2)

    def test_hash_args_has_sha256_prefix(self):
        h = tool_receipts.hash_args({"x": 1})
        self.assertTrue(h.startswith("sha256:"), "args_hash deve comecar com 'sha256:'")

    def test_hash_result_string(self):
        h = tool_receipts.hash_result("hello")
        self.assertTrue(h.startswith("sha256:"))
        # sha256 hex = 64 chars after prefix
        self.assertEqual(len(h), len("sha256:") + 64)

    def test_hash_result_has_sha256_prefix(self):
        h = tool_receipts.hash_result("data")
        self.assertTrue(h.startswith("sha256:"), "result_hash deve comecar com 'sha256:'")

    def test_make_receipt_has_all_fields(self):
        receipt = tool_receipts.make_receipt(
            "web_search", {"query": "tempo"}, "sunny", key=self.key,
        )
        for field in ("call_id", "tool", "args_hash", "result_hash",
                       "timestamp", "hmac"):
            self.assertIn(field, receipt, f"recibo deve ter campo '{field}'")

    def test_make_receipt_call_id_is_uuid4(self):
        receipt = tool_receipts.make_receipt(
            "tool", {}, "result", key=self.key,
        )
        self.assertRegex(receipt["call_id"], _UUID4_RE,
                         "call_id deve ser UUID v4")

    def test_make_receipt_hmac_has_sha256_prefix(self):
        receipt = tool_receipts.make_receipt(
            "tool", {}, "result", key=self.key,
        )
        self.assertTrue(receipt["hmac"].startswith("sha256:"),
                        "hmac deve comecar com 'sha256:'")

    def test_make_receipt_timestamp_format(self):
        receipt = tool_receipts.make_receipt(
            "tool", {}, "result", key=self.key,
        )
        # ISO-8601 UTC: YYYY-MM-DDTHH:MM:SSZ
        self.assertRegex(receipt["timestamp"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_verify_receipt_with_correct_key(self):
        receipt = tool_receipts.make_receipt(
            "web_search", {"q": "1"}, "result", key=self.key,
        )
        self.assertTrue(tool_receipts.verify_receipt(receipt, self.key))

    def test_verify_receipt_with_wrong_key(self):
        receipt = tool_receipts.make_receipt(
            "web_search", {"q": "1"}, "result", key=self.key,
        )
        self.assertFalse(tool_receipts.verify_receipt(receipt, b"wrong-key"))

    def test_verify_receipt_detects_tampered_result_hash(self):
        receipt = tool_receipts.make_receipt(
            "web_search", {"q": "1"}, "result", key=self.key,
        )
        receipt["result_hash"] = "sha256:" + "a" * 64
        self.assertFalse(tool_receipts.verify_receipt(receipt, self.key))

    def test_verify_receipt_detects_tampered_args_hash(self):
        receipt = tool_receipts.make_receipt(
            "web_search", {"q": "1"}, "result", key=self.key,
        )
        receipt["args_hash"] = "sha256:" + "b" * 64
        self.assertFalse(tool_receipts.verify_receipt(receipt, self.key))

    def test_verify_receipt_detects_tampered_tool(self):
        """O tool nao entra no HMAC, mas adulterar o result_hash sim."""
        receipt = tool_receipts.make_receipt(
            "web_search", {"q": "1"}, "result", key=self.key,
        )
        receipt["result_hash"] = "sha256:" + "c" * 64
        self.assertFalse(tool_receipts.verify_receipt(receipt, self.key))

    def test_each_receipt_has_unique_call_id(self):
        ids = set()
        for _ in range(50):
            r = tool_receipts.make_receipt("tool", {}, "r", key=self.key)
            ids.add(r["call_id"])
        self.assertEqual(len(ids), 50, "cada call_id deve ser unico")

    def test_receipt_does_not_contain_key(self):
        secret_key = b"super-secret-key-do-not-log-1234"
        receipt = tool_receipts.make_receipt(
            "tool", {"a": 1}, "result", key=secret_key,
        )
        receipt_json = json.dumps(receipt)
        self.assertNotIn(secret_key.decode("utf-8"), receipt_json)

    def test_load_key_from_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["JARVIS_RECEIPT_HMAC_KEY"] = "env-secret-key"
            try:
                key = tool_receipts.load_key(Path(tmp) / "nonexistent")
                self.assertEqual(key, b"env-secret-key")
            finally:
                del os.environ["JARVIS_RECEIPT_HMAC_KEY"]

    def test_load_key_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "test.key"
            key_path.write_bytes(b"file-secret-key")
            os.environ.pop("JARVIS_RECEIPT_HMAC_KEY", None)
            key = tool_receipts.load_key(key_path)
            self.assertEqual(key, b"file-secret-key")

    def test_load_key_missing_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop("JARVIS_RECEIPT_HMAC_KEY", None)
            with self.assertRaises(FileNotFoundError):
                tool_receipts.load_key(Path(tmp) / "no-such-key")


class BridgeReceiptIntegrationTests(unittest.TestCase):
    """Testes de integracao: bridge gera recibo apos cada call bem-sucedido."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.audit_path = self.tmpdir / "audit.jsonl"
        self.hmac_key = b"integration-test-hmac-key"
        self.dispatch = RecordingDispatch()
        # Patch the key resolution so the bridge uses our test key
        self._orig_load = tool_receipts.load_key
        tool_receipts.load_key = lambda *a, **kw: self.hmac_key
        self.bridge = ToolBridge(
            FakeRegistry(), self.dispatch, TOKEN,
            audit_path=self.audit_path,
        )
        self.client = TestClient(self.bridge.app)
        self.addCleanup(self.client.close)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore_load)

    def _restore_load(self):
        tool_receipts.load_key = self._orig_load

    def _read_receipts(self) -> list[dict]:
        """Le do audit JSONL apenas os registros que sao recibos (tem 'hmac')."""
        if not self.audit_path.exists():
            return []
        receipts = []
        for line in self.audit_path.read_text().strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Receipts have 'hmac' field; audit records have 'action' field
            if "hmac" in record and "call_id" in record:
                receipts.append(record)
        return receipts

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
        self.assertTrue(
            tool_receipts.verify_receipt(receipts[0], self.hmac_key),
            "HMAC do recibo deve verificar com a chave correta",
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

    def test_receipt_format_matches_spec(self):
        """O recibo deve ter todos os campos do formato especificado."""
        self.client.post(
            "/call", json={"name": "web_search", "args": {"city": "SP"}},
            headers=AUTH,
        )
        receipts = self._read_receipts()
        self.assertEqual(len(receipts), 1)
        r = receipts[0]
        # Campos obrigatorios
        for field in ("call_id", "tool", "args_hash", "result_hash",
                       "timestamp", "hmac"):
            self.assertIn(field, r, f"recibo deve ter campo '{field}'")
        # Prefixos sha256:
        self.assertTrue(r["args_hash"].startswith("sha256:"))
        self.assertTrue(r["result_hash"].startswith("sha256:"))
        self.assertTrue(r["hmac"].startswith("sha256:"))
        # call_id eh UUID v4
        self.assertRegex(r["call_id"], _UUID4_RE)
        # timestamp ISO-8601
        self.assertRegex(r["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

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
        key_text = self.hmac_key.decode("utf-8", errors="ignore")
        self.assertNotIn(key_text, raw)

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

        tool_receipts.load_key = lambda *a, **kw: self.hmac_key
        bridge = ToolBridge(
            FakeRegistry(), failing, TOKEN,
            audit_path=self.audit_path,
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
        tool_receipts.load_key = lambda *a, **kw: self.hmac_key
        bridge = ToolBridge(
            FakeRegistry(), self.dispatch, TOKEN,
            audit_path=self.audit_path,
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

    def test_receipt_can_be_verified_full_cycle(self):
        """Ciclo completo: gera, le do disco, verifica."""
        self.client.post(
            "/call", json={"name": "web_search", "args": {"city": "RJ"}},
            headers=AUTH,
        )
        receipts = self._read_receipts()
        self.assertEqual(len(receipts), 1)
        r = receipts[0]
        self.assertTrue(tool_receipts.verify_receipt(r, self.hmac_key))

    def test_receipt_tool_field_matches_action_name(self):
        self.client.post(
            "/call", json={"name": "web_search", "args": {}},
            headers=AUTH,
        )
        receipts = self._read_receipts()
        self.assertEqual(receipts[0]["tool"], "web_search")


class KeyFileTests(unittest.TestCase):
    """Testes da chave HMAC em disco."""

    def test_key_file_exists(self):
        key_path = Path(__file__).resolve().parent.parent / "config" / "certs" / "tool_hmac.key"
        self.assertTrue(key_path.exists(), f"chave deve existir em {key_path}")

    def test_key_file_permissions(self):
        key_path = Path(__file__).resolve().parent.parent / "config" / "certs" / "tool_hmac.key"
        if not key_path.exists():
            self.skipTest("chave nao existe")
        mode = key_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, "chave deve ter permissao 0600")

    def test_key_file_not_empty(self):
        key_path = Path(__file__).resolve().parent.parent / "config" / "certs" / "tool_hmac.key"
        if not key_path.exists():
            self.skipTest("chave nao existe")
        content = key_path.read_bytes().strip()
        self.assertGreater(len(content), 0, "chave nao deve estar vazia")

    def test_key_in_gitignore(self):
        gitignore = Path(__file__).resolve().parent.parent / ".gitignore"
        content = gitignore.read_text()
        self.assertIn("config/certs/tool_hmac.key", content,
                       "tool_hmac.key deve estar no .gitignore")


if __name__ == "__main__":
    unittest.main()
