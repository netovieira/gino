"""Testes do verificador externo de acoes (FASE3-TASK3).

Cobre:
  * FileVerifier: escrita (arquivo existe) e delecao (arquivo nao existe)
  * CommandVerifier: codigo de saida e hash da stdout
  * MessageVerifier: WhatsApp inbox e email pending
  * VerifierThread: consumacao assincrona de recibos HMAC
  * Integracao com guard.py: fatos nao verificados rejeitam assercao
  * Evento VERIFICATION_FAILED gravado no activity log
  * Latencia de verificacao de arquivo < 50 ms
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from core.action_verifier import (
    CommandVerifier,
    FileVerifier,
    MessageVerifier,
    Verifier,
    VerifierThread,
    _log_failure,
    get_verifier,
)
from core import tool_receipts


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture()
def hmac_key() -> bytes:
    return b"test-hmac-key-for-action-verifier"


@pytest.fixture()
def activity_log(tmp_path: Path) -> Path:
    return tmp_path / "activity.log"


@pytest.fixture()
def verifier_thread(hmac_key: bytes, activity_log: Path) -> VerifierThread:
    vt = VerifierThread(key=hmac_key, activity_log_path=activity_log)
    vt.start()
    yield vt
    vt.stop()


# ── FileVerifier ────────────────────────────────────────────────────────────

class TestFileVerifier:

    def test_write_existing_file_passes(self, tmp_path: Path):
        f = tmp_path / "novo.txt"
        f.write_text("conteudo")
        v = FileVerifier()
        assert v.check("cid-1", "file_controller",
                        {"action": "write", "path": str(f)}, "ok") is True

    def test_write_missing_file_fails(self, tmp_path: Path, activity_log: Path):
        missing = tmp_path / "inexistente.txt"
        v = FileVerifier()
        with patch("core.action_verifier._ACTIVITY_LOG_PATH", activity_log):
            result = v.check("cid-2", "file_controller",
                             {"action": "write", "path": str(missing)}, "ok")
        assert result is False
        log_text = activity_log.read_text(encoding="utf-8")
        assert "VERIFICATION_FAILED" in log_text
        assert "cid-2" in log_text

    def test_delete_existing_file_fails(self, tmp_path: Path, activity_log: Path):
        f = tmp_path / "restos.txt"
        f.write_text("ainda aqui")
        v = FileVerifier()
        with patch("core.action_verifier._ACTIVITY_LOG_PATH", activity_log):
            result = v.check("cid-3", "file_controller",
                             {"action": "delete", "path": str(f)}, "ok")
        assert result is False
        log_text = activity_log.read_text(encoding="utf-8")
        assert "VERIFICATION_FAILED" in log_text
        assert "cid-3" in log_text

    def test_delete_missing_file_passes(self, tmp_path: Path):
        gone = tmp_path / "apagado.txt"
        v = FileVerifier()
        assert v.check("cid-4", "file_controller",
                        {"action": "delete", "path": str(gone)}, "ok") is True

    def test_unknown_operation_passes(self, tmp_path: Path):
        v = FileVerifier()
        assert v.check("cid-5", "file_controller",
                        {"action": "read", "path": str(tmp_path)}, "ok") is True

    def test_no_path_passes(self):
        v = FileVerifier()
        assert v.check("cid-6", "file_controller", {"action": "write"}, "ok") is True

    def test_latency_under_50ms(self, tmp_path: Path):
        f = tmp_path / "fast.txt"
        f.write_text("x")
        v = FileVerifier()
        start = time.perf_counter()
        for i in range(100):
            v.check(f"cid-lat-{i}", "file_controller",
                    {"action": "write", "path": str(f)}, "ok")
        elapsed_ms = (time.perf_counter() - start) / 100 * 1000
        assert elapsed_ms < 50, f"latencia media {elapsed_ms:.2f} ms > 50 ms"

    def test_verb_in_args_write(self, tmp_path: Path):
        f = tmp_path / "verbo.txt"
        f.write_text("ok")
        v = FileVerifier()
        assert v.check("cid-7", "generic_tool",
                        {"operation": "criar", "path": str(f)}, "ok") is True

    def test_verb_in_args_delete(self, tmp_path: Path, activity_log: Path):
        f = tmp_path / "verbo_del.txt"
        f.write_text("ok")
        v = FileVerifier()
        with patch("core.action_verifier._ACTIVITY_LOG_PATH", activity_log):
            result = v.check("cid-8", "generic_tool",
                             {"operation": "remover", "path": str(f)}, "ok")
        assert result is False


# ── CommandVerifier ─────────────────────────────────────────────────────────

class TestCommandVerifier:

    def test_exit_code_zero_passes(self):
        v = CommandVerifier()
        assert v.check("cid-c1", "terminal_run", {},
                        {"exit_code": 0, "stdout": "ok"}) is True

    def test_exit_code_nonzero_fails(self, activity_log: Path):
        v = CommandVerifier()
        with patch("core.action_verifier._ACTIVITY_LOG_PATH", activity_log):
            result = v.check("cid-c2", "terminal_run", {},
                             {"exit_code": 1, "stdout": "erro"})
        assert result is False
        log_text = activity_log.read_text(encoding="utf-8")
        assert "VERIFICATION_FAILED" in log_text
        assert "cid-c2" in log_text

    def test_stdout_hash_match_passes(self):
        v = CommandVerifier()
        stdout = "listagem\n"
        h = f"sha256:{hashlib.sha256(stdout.encode()).hexdigest()}"
        assert v.check("cid-c3", "terminal_run", {},
                        {"exit_code": 0, "stdout": stdout,
                         "stdout_hash": h}) is True

    def test_stdout_hash_mismatch_fails(self, activity_log: Path):
        v = CommandVerifier()
        with patch("core.action_verifier._ACTIVITY_LOG_PATH", activity_log):
            result = v.check("cid-c4", "terminal_run", {},
                             {"exit_code": 0, "stdout": "real",
                              "stdout_hash": "sha256:0000"})
        assert result is False

    def test_string_result_passes(self):
        v = CommandVerifier()
        assert v.check("cid-c5", "terminal_run", {}, "saida pura") is True

    def test_invalid_exit_code_fails(self, activity_log: Path):
        v = CommandVerifier()
        with patch("core.action_verifier._ACTIVITY_LOG_PATH", activity_log):
            result = v.check("cid-c6", "terminal_run", {},
                             {"exit_code": "abc"})
        assert result is False


# ── MessageVerifier ─────────────────────────────────────────────────────────

class TestMessageVerifier:

    def test_whatsapp_inbox_match(self, tmp_path: Path):
        inbox = tmp_path / "whatsapp_inbox.jsonl"
        inbox.write_text(json.dumps({
            "ts": int(time.time()),
            "chat": "551199999999",
            "text": "Ola!",
            "from_me": True,
        }) + "\n", encoding="utf-8")
        v = MessageVerifier(inbox_path=inbox)
        assert v.check("cid-m1", "whatsapp_control",
                        {"to": "551199999999", "text": "Ola!"}, "ok") is True

    def test_whatsapp_inbox_no_match(self, tmp_path: Path, activity_log: Path):
        inbox = tmp_path / "whatsapp_inbox.jsonl"
        inbox.write_text(json.dumps({
            "ts": int(time.time()),
            "chat": "551188888888",
            "text": "Outra pessoa",
        }) + "\n", encoding="utf-8")
        v = MessageVerifier(inbox_path=inbox)
        with patch("core.action_verifier._ACTIVITY_LOG_PATH", activity_log):
            result = v.check("cid-m2", "whatsapp_control",
                             {"to": "551199999999", "text": "Ola!"}, "ok")
        assert result is False

    def test_whatsapp_no_inbox_passes(self):
        v = MessageVerifier(inbox_path=Path("/nao/existe"))
        assert v.check("cid-m3", "whatsapp_control",
                        {"to": "551199999999"}, "ok") is True

    def test_email_pending_check_passes(self):
        v = MessageVerifier(pending_check=lambda: {"to": "a@b.com"})
        assert v.check("cid-m4", "email_control",
                        {"to": "a@b.com"}, "ok") is True

    def test_email_no_pending_fallback(self):
        v = MessageVerifier(pending_check=lambda: None)
        # Sem pending e sem _result com queued — aceita (nao ha como verificar).
        assert v.check("cid-m5", "email_control", {}, "ok") is True

    def test_no_recipient_passes(self):
        v = MessageVerifier()
        assert v.check("cid-m6", "whatsapp_control", {}, "ok") is True


# ── VerifierThread ──────────────────────────────────────────────────────────

class TestVerifierThread:

    def test_submit_and_verify_hmac(self, verifier_thread, hmac_key):
        receipt = tool_receipts.make_receipt(
            "file_controller", {"action": "write", "path": "/tmp/x"}, "ok",
            key=hmac_key,
        )
        verifier_thread.submit(receipt)
        # Aguarda processamento.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if verifier_thread.result_for(receipt["call_id"]) is not None:
                break
            time.sleep(0.05)
        assert verifier_thread.result_for(receipt["call_id"]) is True

    def test_invalid_hmac_fails(self, verifier_thread, hmac_key, activity_log):
        receipt = tool_receipts.make_receipt(
            "terminal_run", {"command": "ls"}, {"exit_code": 0},
            key=hmac_key,
        )
        # Corrompe o HMAC.
        receipt["hmac"] = "sha256:0000000000000000"
        verifier_thread.submit(receipt)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if verifier_thread.result_for(receipt["call_id"]) is not None:
                break
            time.sleep(0.05)
        assert verifier_thread.result_for(receipt["call_id"]) is False
        # Verifica que o evento foi gravado.
        log_text = activity_log.read_text(encoding="utf-8")
        assert "VERIFICATION_FAILED" in log_text
        assert "HMAC" in log_text

    def test_verify_sync_file(self, tmp_path: Path, hmac_key, activity_log):
        vt = VerifierThread(key=hmac_key, activity_log_path=activity_log)
        f = tmp_path / "sync.txt"
        f.write_text("ok")
        assert vt.verify_sync(
            "cid-s1", "file_controller",
            {"action": "write", "path": str(f)}, "ok"
        ) is True

    def test_verify_sync_file_fails(self, tmp_path: Path, hmac_key,
                                     activity_log):
        vt = VerifierThread(key=hmac_key, activity_log_path=activity_log)
        missing = tmp_path / "nope.txt"
        assert vt.verify_sync(
            "cid-s2", "file_controller",
            {"action": "write", "path": str(missing)}, "ok"
        ) is False
        assert len(vt.failures) == 1
        assert vt.failures[0]["call_id"] == "cid-s2"

    def test_queue_full_drops_receipt(self, hmac_key, activity_log):
        vt = VerifierThread(key=hmac_key, activity_log_path=activity_log,
                            queue_size=1)
        # Nao inicia a thread — a fila vai encher.
        receipt1 = tool_receipts.make_receipt("t", {}, "r1", key=hmac_key)
        receipt2 = tool_receipts.make_receipt("t", {}, "r2", key=hmac_key)
        vt._queue.put(receipt1)  # enche a fila
        vt.submit(receipt2)      # deve ser descartado sem erro
        # Limpa.
        vt._queue.get_nowait()

    def test_stop_is_idempotent(self, verifier_thread):
        verifier_thread.stop()
        verifier_thread.stop()  # nao deve levantar


# ── guard.py integration ────────────────────────────────────────────────────

class TestGuardVerifierIntegration:

    def test_fact_with_failed_verification_rejected(self, tmp_path: Path):
        from core.flows.guard import guard

        missing = tmp_path / "inexistente.txt"
        facts = [{
            "assertion": True,
            "spoken": "Arquivo criado.",
            "call_id": "cid-g1",
            "tool": "file_controller",
            "tool_args": {"action": "write", "path": str(missing)},
            "tool_result": "ok",
        }]
        ok, reason = guard("Arquivo criado.", facts, "complement")
        assert ok is False
        assert "verificação externa" in reason

    def test_fact_with_passed_verification_accepted(self, tmp_path: Path):
        from core.flows.guard import guard

        f = tmp_path / "existe.txt"
        f.write_text("ok")
        facts = [{
            "assertion": True,
            "spoken": "Arquivo criado.",
            "call_id": "cid-g2",
            "tool": "file_controller",
            "tool_args": {"action": "write", "path": str(f)},
            "tool_result": "ok",
        }]
        ok, reason = guard("Arquivo criado.", facts, "complement")
        assert ok is True

    def test_fact_without_tool_metadata_passes(self):
        from core.flows.guard import guard

        facts = [{
            "assertion": True,
            "spoken": "Queen tocando no Spotify.",
        }]
        ok, reason = guard("Queen tocando no Spotify.", facts, "complement")
        assert ok is True

    def test_brief_phase_unaffected(self):
        from core.flows.guard import guard

        ok, reason = guard("Boa noite.", [], "brief")
        assert ok is True


# ── _log_failure ────────────────────────────────────────────────────────────

class TestLogFailure:

    def test_writes_to_activity_log(self, activity_log: Path):
        with patch("core.action_verifier._ACTIVITY_LOG_PATH", activity_log):
            _log_failure("cid-lf1", "file_controller", "motivo de teste")
        text = activity_log.read_text(encoding="utf-8")
        assert "VERIFICATION_FAILED" in text
        assert "cid-lf1" in text
        assert "motivo de teste" in text

    def test_handles_missing_directory(self, tmp_path: Path):
        bad_path = tmp_path / "no_such_dir" / "activity.log"
        with patch("core.action_verifier._ACTIVITY_LOG_PATH", bad_path):
            # Nao deve levantar.
            _log_failure("cid-lf2", "tool", "razao")


# ── get_verifier singleton ──────────────────────────────────────────────────

class TestGetVerifier:

    def test_returns_same_instance(self):
        import core.action_verifier as mod
        mod._instance = None
        v1 = get_verifier()
        v2 = get_verifier()
        assert v1 is v2
        v1.stop()
        mod._instance = None
