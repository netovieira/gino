"""UV-7 (fila SKILL.md, item 3): RESPOSTA 1 — busca de memória com rastro.

O contrato (``skills/fluxos/FLUXOS.md``, FLUXO RESPOSTA passo 1) exige que o
cérebro consulte a memória de verdade, e a medição de conformidade cobra
``entrada no tool_bridge.log vivo``. Até aqui só ``/call`` escrevia no ledger:
``/memory/read``, ``/memory/search`` e ``/memory/note`` respondiam e não
deixavam linha — a busca acontecia e não tinha prova (evidência real: o
activity.log registra ``QODER: ferramenta — mcp__jarvis__memory_search``
06/11 15:03 e 23:27 com zero entradas correspondentes no ledger da ponte).

Esta suíte prova o caminho fechado:

  (a) cada chamada de memória deixa um registro JSONL no ``tool_bridge.log``
      com action/args/tier/approved/result/status;
  (b) a query vai truncada (o ledger registra o rastro, não o conteúdo);
  (c) erro do provedor de memória também deixa linha, com status ``error``;
  (d) memória desconfigurada deixa linha ``unavailable`` — ausência com
      motivo, nunca silêncio;
  (e) sem ``audit_path`` (testes hermeticamente isolados) nada quebram.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from core.tool_bridge import ToolBridge

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _memory_provider() -> dict:
    return {
        "read": lambda path: f"mem:{path}",
        "search": lambda query, limit=None: [
            {"path": "memory/chat/2026-11-06.md", "query": query,
             "limit": limit}],
        "note": lambda text, kind=None: {"saved": text, "kind": kind},
    }


def _bridge(audit_path, memory=None, **overrides) -> ToolBridge:
    provider = memory if memory is not None else _memory_provider()
    overrides.setdefault("registry", type("R", (), {
        "get_tool_declarations": lambda self: [],
        "has": lambda self, name: False,
        "run": lambda self, name, parameters, ctx=None: "x",
    })())
    return ToolBridge(overrides.pop("registry"), lambda name, args: "ran",
                      TOKEN, logger=lambda _m: None,
                      audit_path=audit_path,
                      memory=provider,
                      **overrides)


class MemoryLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.audit_path = Path(self._tmp.name) / "tool_bridge.log"
        self.bridge = _bridge(self.audit_path)
        self.client = TestClient(self.bridge.app)
        self.addCleanup(self.client.close)

    def _records(self) -> list[dict]:
        if not self.audit_path.exists():
            return []
        rows = []
        for line in self.audit_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows

    # ── (a) cada endpoint de memória deixa sua linha ──────────────────────

    def test_memory_search_escreve_no_ledger(self):
        """A busca real do cérebro: action memory_search, tier safe, status ok."""
        response = self.client.post("/memory/search", headers=AUTH,
                                    json={"query": "latencia boca local",
                                          "limit": 5})
        self.assertEqual(response.status_code, 200, response.text)

        records = self._records()
        self.assertEqual(len(records), 1, records)
        row = records[0]
        self.assertEqual(row["action"], "memory_search")
        self.assertEqual(row["args"]["query"], "latencia boca local")
        self.assertEqual(row["args"]["limit"], 5)
        self.assertEqual(row["tier"], "safe")
        self.assertTrue(row["approved"])
        self.assertEqual(row["status"], "ok")
        self.assertIn("memory/chat/2026-11-06.md", row["result"])
        self.assertIn("timestamp", row)

    def test_memory_read_escreve_no_ledger(self):
        response = self.client.post("/memory/read", headers=AUTH,
                                    json={"path": "memory/chat/hot.md"})
        self.assertEqual(response.status_code, 200, response.text)

        records = self._records()
        self.assertEqual(len(records), 1, records)
        self.assertEqual(records[0]["action"], "memory_read")
        self.assertEqual(records[0]["args"], {"path": "memory/chat/hot.md"})
        self.assertEqual(records[0]["tier"], "safe")
        self.assertEqual(records[0]["status"], "ok")
        self.assertIn("mem:memory/chat/hot.md", records[0]["result"])

    def test_memory_note_escreve_no_ledger_com_tier_low(self):
        """Anotar escreve no índice: tier low, mas sempre com rastro."""
        response = self.client.post("/memory/note", headers=AUTH,
                                    json={"text": "decidiu usar FAISS",
                                          "kind": "decision"})
        self.assertEqual(response.status_code, 200, response.text)

        records = self._records()
        self.assertEqual(len(records), 1, records)
        self.assertEqual(records[0]["action"], "memory_note")
        self.assertEqual(records[0]["tier"], "low")
        self.assertEqual(records[0]["args"], {"text": "decidiu usar FAISS",
                                              "kind": "decision"})
        self.assertEqual(records[0]["status"], "ok")

    def test_tres_chamadas_tres_linhas(self):
        """O ledger é append-only: uma linha por chamada, na ordem."""
        self.client.post("/memory/search", headers=AUTH,
                         json={"query": "um"})
        self.client.post("/memory/read", headers=AUTH, json={"path": "dois"})
        self.client.post("/memory/note", headers=AUTH, json={"text": "tres"})

        rows = self._records()
        self.assertEqual([r["action"] for r in rows],
                         ["memory_search", "memory_read", "memory_note"])
        self.assertTrue(all(r["status"] == "ok" for r in rows))

    # ── (b) conteúdo truncado, não copiado inteiro ────────────────────────

    def test_query_longa_vai_truncada(self):
        """Prova de chamada, não despejo: a query é cortada no limiar."""
        longa = "conversa " * 200          # 1400 caracteres
        response = self.client.post("/memory/search", headers=AUTH,
                                    json={"query": longa})
        self.assertEqual(response.status_code, 200, response.text)

        row = self._records()[0]
        self.assertLessEqual(len(row["args"]["query"]), 161)
        self.assertTrue(row["args"]["query"].endswith("…"))

    def test_sem_limit_nao_aparece_no_registro(self):
        """Chaves ausentes não viram 'None' no log."""
        self.client.post("/memory/search", headers=AUTH,
                         json={"query": "sem limite"})
        row = self._records()[0]
        self.assertEqual(row["args"], {"query": "sem limite"})

    # ── (c) erro do provedor também deixa linha ───────────────────────────

    def test_busca_quebrada_registra_status_error(self):
        def explode(query, limit=None):
            raise RuntimeError("FAISS indisponível")

        bridge = _bridge(self.audit_path,
                         memory={"search": explode,
                                 "read": lambda path: "x",
                                 "note": lambda text, kind=None: "x"})
        client = TestClient(bridge.app)
        self.addCleanup(client.close)

        response = client.post("/memory/search", headers=AUTH,
                               json={"query": "o que quebrou"})
        self.assertEqual(response.status_code, 500, response.text)

        row = self._records()[-1]
        self.assertEqual(row["action"], "memory_search")
        self.assertEqual(row["status"], "error")
        self.assertIn("FAISS indisponível", row["result"])

    # ── (d) memória desconfigurada: ausência com motivo, não silêncio ─────

    def test_memoria_desligada_registra_unavailable(self):
        bridge = _bridge(self.audit_path, memory={})
        client = TestClient(bridge.app)
        self.addCleanup(client.close)

        response = client.post("/memory/search", headers=AUTH,
                               json={"query": "existe?"})
        self.assertEqual(response.status_code, 501, response.text)

        row = self._records()[-1]
        self.assertEqual(row["action"], "memory_search")
        self.assertEqual(row["status"], "unavailable")
        self.assertFalse(row["approved"])
        self.assertIn("not configured", row["result"])

    # ── (e) sem caminho de auditoria, nada quebra ─────────────────────────

    def test_sem_audit_path_e_inofensivo(self):
        bridge = _bridge(None)
        client = TestClient(bridge.app)
        self.addCleanup(client.close)

        response = client.post("/memory/search", headers=AUTH,
                               json={"query": "rodando sem ledger"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["result"])

    # ── auth continua obrigatória no caminho novo ─────────────────────────

    def test_ledger_so_escreve_chamada_autenticada(self):
        response = self.client.post("/memory/search",
                                    json={"query": "sem token"})
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self._records(), [])


if __name__ == "__main__":
    unittest.main()
