"""OTP no WhatsApp e no e-mail: anúncio sem resposta e dedupe persistente."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from email.utils import formatdate
from pathlib import Path

import pytest

from core import email as mail
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


# ── o registro novo de e-mail: a única fonte de conta e de senha ────────────
# Estes testes não tocam rede nem configuração de verdade: o plugin_config é um
# dicionário em memória e o IMAP é falso.

@pytest.fixture()
def registry(monkeypatch, tmp_path):
    """plugin_config em memória + diretórios temporários do registro."""
    data: dict[str, dict] = {}

    def get_cfg(namespace):
        return dict(data.get(namespace, {}))

    def save_cfg(namespace, values):
        data.setdefault(namespace, {}).update(values)

    import memory.config_manager as cm
    monkeypatch.setattr(cm, "get_plugin_config", get_cfg)
    monkeypatch.setattr(cm, "save_plugin_config", save_cfg)
    monkeypatch.setattr(cm, "get_plugin_setting",
                        lambda ns, key, default=None: get_cfg(ns).get(key, default))
    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(mail, "_STATE_ROOT", tmp_path / "config" / "mail")
    monkeypatch.setattr(mail, "_MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(mail, "PENDING_PATH",
                        tmp_path / "memory" / "mail_send_pending.json")
    mail.reset()
    email_otp_watch.reset(store_path=tmp_path / "seen.json")
    yield data
    mail.reset()
    email_otp_watch.reset()


class _FakeInbox:
    """O mínimo de IMAP que core/email.py usa, registrando como foi aberta."""

    def __init__(self, messages: dict[bytes, bytes]):
        self.messages = messages
        self.specs: list[str] = []
        self.readonly: list[bool] = []
        self.logged_out = False

    def select(self, folder, readonly=False):
        self.readonly.append(bool(readonly))
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b" ".join(self.messages.keys())]
        if command == "FETCH":
            self.specs.append(str(args[1]))
            requested = args[0]
            if isinstance(requested, bytes):
                keys = [k.strip() for k in requested.split(b",") if k.strip()]
            else:
                keys = [k.strip().encode()
                        for k in str(requested).split(",") if k.strip()]
            out = []
            for key in keys:
                raw = self.messages.get(key, b"")
                if raw:
                    out.append((b"1 (UID " + key + b" BODY[] {%d}" % len(raw),
                                raw))
            return "OK", out or [None]
        return "OK", [None]

    def logout(self):
        self.logged_out = True
        return "BYE", []


def _message(subject: str, body: str,
             sender: str = "Google <no-reply@accounts.google.com>",
             message_id: str = "<code@google.com>") -> bytes:
    """Cabeçalho + corpo, com data de AGORA (o vigia descarta o que é velho)."""
    return (f"From: {sender}\r\nSubject: {subject}\r\n"
            f"Date: {formatdate(time.time(), localtime=True)}\r\n"
            f"Message-ID: {message_id}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}").encode()


def test_watcher_resolves_the_account_from_the_new_registry(registry,
                                                            monkeypatch):
    """Conta e senha vêm de plugin_config.mail + cofre; o namespace antigo não
    existe. A leitura é somente leitura e com BODY.PEEK."""
    mail.add_account("principal", "eu@gmail.com", provider="gmail",
                     otp=True, notify=True)
    mail.set_app_password("principal", "abcd efgh ijkl mnop")
    assert "email" not in registry, "o nome de e-mail não mora mais aqui"

    fake = _FakeInbox({b"7": _message(
        "Seu codigo de verificacao", "Seu código de verificação é 731902")})
    opened: list[object] = []

    def fake_open(acct):
        opened.append(acct)
        return fake

    monkeypatch.setattr(mail, "_imap_open", fake_open)
    codes: list[dict] = []
    out = email_otp_watch.poll_once(codes.append)

    assert [p["code"] for p in out] == ["731902"]
    assert out[0]["origin"] == "email"
    assert out[0]["account"] == "eu@gmail.com"
    assert opened and opened[0].address == "eu@gmail.com"
    # A senha veio do cofre (sem os espaços do Gmail) e não vazou para metadado.
    assert mail.app_password("principal") == "abcdefghijklmnop"
    meta = json.dumps(mail.list_accounts(), ensure_ascii=False)
    assert "abcdefghijklmnop" not in meta
    assert "app_password" not in meta and '"password"' not in meta
    # Somente leitura, corpo por PEEK, conexão fechada no fim.
    assert fake.readonly == [True]
    assert fake.specs and all("BODY.PEEK" in spec for spec in fake.specs)
    assert fake.logged_out is True


def test_otp_account_metadata_never_carries_the_password(registry):
    mail.add_account("principal", "eu@gmail.com", otp=True)
    mail.set_app_password("principal", "abcdefghijklmnop")
    rec = mail.otp_accounts()[0]
    assert rec["has_password"] is True
    blob = json.dumps(rec, ensure_ascii=False)
    assert "abcdefghijklmnop" not in blob
    assert '"password"' not in blob


def test_start_registers_the_pass_on_the_single_mail_loop(registry,
                                                          monkeypatch):
    """`start` não abre laço próprio: registra o passo e a cadência de 60 s."""
    mail.add_account("principal", "eu@gmail.com", otp=True)
    mail.set_app_password("principal", "x")
    started: dict = {}
    monkeypatch.setattr(mail, "start", lambda **kw: started.update(kw))
    email_otp_watch.start(lambda payload: True)
    assert started.get("interval") == email_otp_watch.POLL_S
    assert callable(mail._otp_processor)
    email_otp_watch.stop()
    assert mail._otp_processor is None


def test_purge_legacy_removes_only_the_old_keys(tmp_path, monkeypatch):
    """A limpeza apaga as chaves antigas, guarda backup 600 e poupa o cofre."""
    import memory.config_manager as cm
    path = tmp_path / "api_keys.json"
    path.write_text(json.dumps({"plugin_config": {
        "email": {"address": "eu@gmail.com",
                  "app_password": "abcd efgh ijkl mnop",
                  "name": "Anthero", "account_name": "principal",
                  "imap_host": "", "smtp_host": ""},
        "mail": {"accounts": [{"name": "principal", "uuid": "mail-principal",
                               "address": "eu@gmail.com", "otp": True}]},
        "mail_secrets": {"mail-principal": "abcdefghijklmnop"},
    }}), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(cm, "CONFIG_FILE", path)

    removed = mail.purge_legacy()
    assert set(removed) == set(mail.LEGACY_KEYS)

    data = json.loads(path.read_text(encoding="utf-8"))["plugin_config"]
    assert "email" not in data, "o namespace antigo inteiro foi embora"
    assert data["mail_secrets"]["mail-principal"] == "abcdefghijklmnop"
    stored = json.dumps(data, ensure_ascii=False)
    assert "abcd efgh" not in stored and "app_password" not in stored
    assert data["mail"]["accounts"][0]["address"] == "eu@gmail.com"

    backups = list(tmp_path.glob("api_keys.json.backup-*"))
    assert len(backups) == 1
    assert (backups[0].stat().st_mode & 0o777) == 0o600
    assert (path.stat().st_mode & 0o777) == 0o600

    assert mail.purge_legacy() == [], "idempotente: nada para remover"


def test_after_the_scrub_watcher_and_old_reader_still_log_in(registry,
                                                             monkeypatch):
    """Sem as chaves antigas, o login sai do cofre e o leitor antigo continua."""
    from plugins import email_control
    registry["email"] = {}                    # sobrou o namespace, vazio
    mail.add_account("principal", "eu@gmail.com", provider="gmail",
                     otp=True, notify=True)
    mail.set_app_password("principal", "abcdefghijklmnop")
    monkeypatch.setattr(email_control, "get_plugin_config",
                        lambda ns: dict(registry.get(ns, {})))

    fake = _FakeInbox({b"9": _message("Seu codigo", "Seu código é 552211")})
    logins: list[tuple[str, bool]] = []

    def fake_open(acct):
        logins.append((acct.address, bool(mail.app_password(acct.uuid))))
        return fake

    monkeypatch.setattr(mail, "_imap_open", fake_open)
    out = email_otp_watch.poll_once(lambda payload: True)
    assert [p["code"] for p in out] == ["552211"]
    assert logins == [("eu@gmail.com", True)]

    rows = email_control._load_accounts()
    assert [r["address"] for r in rows] == ["eu@gmail.com"]
    assert rows[0]["app_password"] == "abcdefghijklmnop"   # do cofre, em memória


def test_old_panel_is_read_only_and_writes_no_secret(registry, monkeypatch):
    """O painel ⚙ antigo não tem campo de senha nem caminho de escrita."""
    from plugins import email_control
    mail.add_account("principal", "eu@gmail.com", provider="gmail")
    mail.set_app_password("principal", "abcdefghijklmnop")
    monkeypatch.setattr(email_control, "get_plugin_config",
                        lambda ns: dict(registry.get(ns, {})))

    ok, message = email_control._test({"address": "outra@example.com",
                                       "app_password": "digitada-agora"})
    assert ok is True and "principal" in message
    assert "email" not in registry, "o painel não grava no namespace antigo"
    assert "digitada-agora" not in json.dumps(registry, ensure_ascii=False)

    keys = [f.get("key") for f in (email_control.PLUGIN_SETTINGS.get("fields")
                                   or [])]
    assert "app_password" not in keys
    assert not hasattr(email_control, "save_plugin_config"), \
        "sem helper de gravação não existe caminho de escrita acidental"


if __name__ == "__main__":
    unittest.main()
