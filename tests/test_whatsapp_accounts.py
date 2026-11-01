"""WhatsApp account CRUD used by the new UI (update/delete/default promotion)."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import whatsapp  # noqa: E402


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    original = dict(whatsapp._accounts)
    monkeypatch.setattr(whatsapp, "_ensure_accounts", lambda: None)
    monkeypatch.setattr(whatsapp, "_save_accounts", lambda: None)
    monkeypatch.setattr(whatsapp, "_stop_account", lambda _a: None)
    monkeypatch.setattr(whatsapp.shutil, "rmtree", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "memory.config_manager.save_plugin_config", lambda *_a, **_k: None)
    created = {}
    for name, default in (("principal", True), ("trabalho", False)):
        acct = whatsapp._Account(name, f"uuid-{name}", default=default)
        acct.dir = tmp_path / name
        acct.inbox_file = tmp_path / f"{name}.jsonl"
        acct.media_dir = tmp_path / f"{name}-media"
        created[name] = acct
    whatsapp._accounts.clear()
    whatsapp._accounts.update({a.uuid: a for a in created.values()})
    yield created
    whatsapp._accounts.clear()
    whatsapp._accounts.update(original)


def test_update_name_flags_and_default(accounts):
    assert whatsapp.update_account(
        "principal", new_name="pessoal", auto_start=False, notify=True,
        default=True) is True
    acct = whatsapp._accounts["uuid-principal"]
    assert acct.name == "pessoal"
    assert acct.auto_start is False
    assert acct.notify is True
    assert acct.default is True
    assert accounts["trabalho"].default is False


def test_update_rejects_duplicate_name(accounts):
    assert whatsapp.update_account("principal", new_name="trabalho") is False


def test_remove_default_promotes_another(accounts, monkeypatch):
    monkeypatch.setattr(
        whatsapp, "_save_accounts", lambda: None, raising=False)
    assert whatsapp.remove_account("principal") is True
    assert "uuid-principal" not in whatsapp._accounts
    assert accounts["trabalho"].default is True
    assert whatsapp.default_account_name() in ("trabalho", "principal")
