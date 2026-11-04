"""O e-mail multi-conta: CRUD, boot, inbox, privacidade e o portão de envio.

Nada aqui fala com a rede nem com a caixa de verdade: o IMAP e o SMTP são
fakes. O cofre (config/api_keys.json) é substituído por um dicionário em
memória, então estes testes nunca tocam na configuração real do dono.

O que o teste vigia, e por quê:
  * metadados persistidos NUNCA carregam senha — a senha mora no cofre;
  * `connect_all` devolve o estado REAL de cada conta (connected/auth/error);
  * o boot fala uma frase por conta que falhou e fica mudo quando todas sobem;
  * a varredura guarda cabeçalho (remetente, assunto, data, message-id) e nunca
    o corpo — e nada de corpo entra em frase falada;
  * `send` só sai com a voz do dono: sem PCM (texto digitado) é recusado.
"""
from __future__ import annotations

import json
import sys
import time
from email.utils import formatdate
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
from core import email as mail


# ── cofre falso + diretórios temporários ────────────────────────────────────

@pytest.fixture()
def vault(monkeypatch, tmp_path):
    """plugin_config em memória; nenhum arquivo real é lido ou escrito."""
    data: dict[str, dict] = {}

    def get_cfg(namespace):
        return dict(data.get(namespace, {}))

    def save_cfg(namespace, values):
        cur = data.setdefault(namespace, {})
        cur.update(values)

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
    yield data
    mail.reset()


# ── fakes de IMAP/SMTP ─────────────────────────────────────────────────────

class FakeIMAP:
    """Só o que core/email.py usa: select, uid(SEARCH/FETCH), logout."""

    def __init__(self, headers: dict[bytes, bytes] | None = None,
                 fail: Exception | None = None):
        self.headers = headers or {}
        self.fail = fail
        self.specs: list[str] = []
        self.selected_ro = None
        self.logged_out = False

    def select(self, folder, readonly=False):
        self.selected_ro = bool(readonly)
        return "OK", [b"1"]

    def uid(self, command, *args):
        if self.fail is not None:
            raise self.fail
        if command == "SEARCH":
            return "OK", [b" ".join(self.headers.keys())]
        if command == "FETCH":
            requested = args[0]
            spec = args[1]
            self.specs.append(str(spec))
            # Um UID só ou um bloco "1,2,3" (o registro busca cabeçalho em lote);
            # a resposta traz o UID no metadado, como o servidor de verdade.
            if isinstance(requested, bytes):
                keys = [k.strip() for k in requested.split(b",") if k.strip()]
            else:
                keys = [k.strip().encode()
                        for k in str(requested).split(",") if k.strip()]
            out = []
            for key in keys:
                raw = self.headers.get(key, b"")
                if not raw:
                    continue
                out.append((b"1 (UID " + key + b" BODY[] {%d}" % len(raw), raw))
            return "OK", out or [None]
        return "OK", [None]

    def logout(self):
        self.logged_out = True
        return "BYE", []


def _header(subject: str, sender: str = "Maria <maria@example.com>",
            message_id: str = "<abc@example.com>",
            date: str = "Sat, 01 Nov 2026 10:00:00 -0300") -> bytes:
    return (f"From: {sender}\r\nSubject: {subject}\r\n"
            f"Date: {date}\r\nMessage-ID: {message_id}\r\n\r\n").encode()


class FakeSMTP:
    def __init__(self):
        self.sent: list[tuple[str, list[str], str]] = []
        self.login_args = None
        self.quit_called = False

    def login(self, user, password):
        self.login_args = (user, password)

    def sendmail(self, sender, recipients, message):
        self.sent.append((sender, list(recipients), message))
        return {}

    def starttls(self, context=None):
        return None

    def quit(self):
        self.quit_called = True


# ── CRUD ───────────────────────────────────────────────────────────────────

def test_add_list_delete_without_password_in_metadata(vault):
    mail.add_account("trabalho", "eu@empresa.com", provider="outlook",
                     app_password_value="segredo-de-app")
    mail.add_account("pessoal", "eu@gmail.com", provider="gmail")
    mail.set_app_password("pessoal", "outra-senha")

    accounts = mail.list_accounts()
    assert [a["name"] for a in accounts] == ["trabalho", "pessoal"]
    assert accounts[1]["has_password"] is True
    assert accounts[0]["has_password"] is True
    for acc in accounts:
        assert "password" not in acc
        assert "app_password" not in acc
        assert "segredo-de-app" not in json.dumps(acc)

    # Metadados persistidos: nenhuma chave de credencial em lugar nenhum.
    stored = json.dumps(vault["mail"], ensure_ascii=False)
    assert "segredo-de-app" not in stored
    assert "outra-senha" not in stored
    assert "password" not in stored
    assert "eu@empresa.com" in stored      # o endereço, sim, é metadado

    # A senha está no cofre, em namespace separado.
    assert vault["mail_secrets"]["mail-trabalho"] == "segredo-de-app"

    # CRUD round-trip: editar, promover a padrão, remover.
    assert mail.update_account("pessoal", new_name="contas-pessoais",
                               notify=True, default=True)
    assert mail.update_account("contas-pessoais", address="novo@gmail.com")
    listed = {a["name"]: a for a in mail.list_accounts()}
    assert listed["contas-pessoais"]["address"] == "novo@gmail.com"
    assert listed["contas-pessoais"]["notify"] is True
    assert listed["contas-pessoais"]["default"] is True
    assert mail.default_account_name() == "contas-pessoais"

    inbox = vault["mail"]["accounts"][0]
    assert inbox["provider"] == "outlook"
    assert inbox["imap_host"] == "outlook.office365.com"
    assert inbox["smtp_security"] == "starttls"

    assert mail.remove_account("trabalho") is True
    assert [a["name"] for a in mail.list_accounts()] == ["contas-pessoais"]
    assert vault["mail_secrets"]["mail-trabalho"] == ""   # apagada do cofre


def test_add_account_rejects_duplicate_name(vault):
    mail.add_account("trabalho", "eu@empresa.com")
    with pytest.raises(ValueError):
        mail.add_account("Trabalho", "outro@empresa.com")
    assert len(mail.list_accounts()) == 1


def test_zero_accounts_is_silent(vault):
    assert mail.list_accounts() == []
    assert mail.connect_all() == []
    assert mail.recent() == []
    assert mail.status_line() == "nenhuma conta de e-mail configurada ainda"
    assert mail.pending_send() is None


def test_provider_preset_and_manual_override(vault):
    acc = mail.add_account("manual", "eu@servidor.com", provider="manual",
                          imap_host="mail.servidor.com", imap_port=143,
                          imap_ssl=False, smtp_host="mail.servidor.com",
                          smtp_port=25, smtp_security="plain")
    listed = {a["name"]: a for a in mail.list_accounts()}["manual"]
    assert listed["imap_host"] == "mail.servidor.com"
    assert listed["imap_port"] == 143
    assert listed["imap_ssl"] is False
    assert listed["smtp_security"] == "plain"
    assert acc.uuid.startswith("mail-")


# ── migração do formato antigo (plugin_config.email, uma conta só) ──────────

LEGACY_PASSWORD = "abcd efgh ijkl mnop"      # como o Gmail mostra a senha de app


@pytest.fixture()
def legacy_email(monkeypatch, vault):
    """O formato antigo: endereço, nome de exibição e senha na RAIZ do namespace.

    O plugin antigo importa os helpers de config por nome no import do módulo,
    então o monkeypatch do config_manager não chega nele: aqui ele é apontado
    para o mesmo dicionário em memória.
    """
    vault["email"] = {
        "account_name": "principal",
        "address": "eu@gmail.com",
        "app_password": LEGACY_PASSWORD,
        "name": "Anthero Vieira Neto",
        "imap_host": "",
        "smtp_host": "",
    }
    from plugins import email_control
    monkeypatch.setattr(email_control, "get_plugin_config",
                        lambda ns: dict(vault.get(ns, {})))
    # O plugin não tem mais caminho de escrita; se alguém reintroduzir um, este
    # patch (opcional de propósito) continua funcionando.
    monkeypatch.setattr(email_control, "save_plugin_config",
                        lambda ns, values: vault.setdefault(ns, {}).update(values),
                        raising=False)
    return vault


def test_migrate_legacy_moves_account_and_secret_to_new_layer(legacy_email):
    acct = mail.migrate_legacy()
    assert acct is not None

    listed = {a["name"]: a for a in mail.list_accounts()}
    assert list(listed) == ["principal"]
    rec = listed["principal"]
    assert rec["address"] == "eu@gmail.com"
    assert rec["display_name"] == "Anthero Vieira Neto"
    assert rec["provider"] == "gmail"
    assert rec["uuid"] == "mail-principal"
    assert rec["default"] is True
    assert rec["auto_start"] is True
    assert rec["notify"] is True
    assert rec["imap_host"] == "imap.gmail.com" and rec["imap_port"] == 993
    assert rec["smtp_host"] == "smtp.gmail.com"
    assert rec["has_password"] is True
    assert mail.default_account_name() == "principal"
    assert acct.dir.is_dir(), "cada conta tem seu diretório de estado"

    # O segredo está no cofre, SEM os espaços que o Gmail mostra; e não está
    # nos metadados, em hipótese nenhuma.
    assert legacy_email["mail_secrets"]["mail-principal"] == "abcdefghijklmnop"
    assert mail.app_password("principal") == "abcdefghijklmnop"
    stored = json.dumps(legacy_email["mail"], ensure_ascii=False)
    assert "abcdefghijklmnop" not in stored
    assert "abcd efgh" not in stored
    assert "app_password" not in stored
    assert "password" not in stored
    assert "eu@gmail.com" in stored          # endereço é metadado, senha não


def test_migrate_legacy_is_idempotent(legacy_email):
    first = mail.migrate_legacy()
    second = mail.migrate_legacy()
    assert first is not None and second is not None
    assert second.uuid == first.uuid
    assert len(mail.list_accounts()) == 1
    assert mail.app_password(first.uuid) == "abcdefghijklmnop"


def test_migrate_legacy_leaves_old_reader_working(legacy_email):
    """A transição: o plugin antigo (e o vigia de OTP) continua lendo a raiz."""
    mail.migrate_legacy()
    from plugins import email_control
    rows = email_control._load_accounts()
    assert [r["name"] for r in rows] == ["principal"]
    cfg = email_control._resolve(None, None)
    assert cfg["address"] == "eu@gmail.com"
    assert cfg["app_password"] == "abcdefghijklmnop"
    assert legacy_email["email"]["address"] == "eu@gmail.com"


def _full_message(subject: str, body: str,
                  sender: str = "Google <no-reply@accounts.google.com>",
                  message_id: str = "<code@google.com>",
                  date: str | None = None) -> bytes:
    """Cabeçalho + corpo: serve ao refresh (só cabeçalho) e ao OTP (corpo).

    A data é AGORA por padrão: o vigia descarta código com mais de 15 minutos.
    """
    if date is None:
        date = formatdate(time.time(), localtime=True)
    return (f"From: {sender}\r\nSubject: {subject}\r\nDate: {date}\r\n"
            f"Message-ID: {message_id}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}").encode()


def test_single_loop_reads_the_otp_mailbox_once(legacy_email, monkeypatch,
                                                tmp_path):
    """Um laço, uma conexão: código e e-mail novo saem do MESMO ciclo.

    A conta migrada fica marcada com `otp`, então o passo de códigos roda
    dentro da varredura — a caixa é aberta uma vez só. A mensagem que virou
    código NÃO é anunciada de novo como "chegou um e-mail".
    """
    mail.migrate_legacy()
    from core import email_otp_watch as otp_watch
    otp_watch.reset(store_path=tmp_path / "seen.json")
    try:
        fake = FakeIMAP({
            b"7": _full_message("Código de verificação do Google",
                                "Seu código de verificação é 482913"),
            b"8": _full_message("Reuniao de segunda", "Segue a pauta.",
                                sender="Maria <maria@example.com>",
                                message_id="<m8@example.com>"),
        })
        opened: list[str] = []
        monkeypatch.setattr(
            mail, "_imap_open", lambda a: (opened.append(a.name), fake)[1])
        monkeypatch.setattr(mail, "start", lambda **kw: None)  # sem thread
        codes: list[dict] = []
        otp_watch.start(codes.append)          # registra o passo, não cria laço
        mails: list[dict] = []
        new = mail.poll_once(hook=mails.append)

        assert opened == ["principal"], "uma conexão por caixa por ciclo"
        assert [p["code"] for p in codes] == ["482913"]
        assert codes[0]["origin"] == "email"
        assert [m["subject"] for m in new] == ["Reuniao de segunda"]
        assert [m["subject"] for m in mails] == ["Reuniao de segunda"], \
            "o e-mail do código não vira também 'chegou um e-mail'"
        assert fake.selected_ro is True, "ler não pode marcar como lido"
        assert all("BODY.PEEK" in spec for spec in fake.specs)
        assert "eu@gmail.com" in mail._otp_covered_addresses()
    finally:
        otp_watch.stop()
        otp_watch.reset()


def test_mailboxes_without_otp_are_swept_without_reading_bodies(vault,
                                                                monkeypatch):
    """`notify` vale para toda conta; o corpo só é baixado onde há `otp`."""
    mail.add_account("trabalho", "eu@empresa.com", provider="outlook",
                     notify=True)
    mail.set_app_password("trabalho", "senha")
    fake = FakeIMAP({b"5": _header("Reuniao de segunda")})
    attempted: list[str] = []

    def fake_open(acct):
        attempted.append(acct.name)
        return fake

    monkeypatch.setattr(mail, "_imap_open", fake_open)
    seen: list[dict] = []
    new = mail.poll_once(hook=seen.append)
    assert attempted == ["trabalho"], "toda conta habilitada é varrida uma vez"
    assert len(new) == 1
    assert new[0]["subject"] == "Reuniao de segunda"
    assert seen and seen[0]["account"] == "trabalho"
    assert fake.selected_ro is True
    assert not any("BODY.PEEK[]" in spec for spec in fake.specs), \
        "conta sem `otp` não tem o corpo baixado"


# ── conexão: estado REAL ───────────────────────────────────────────────────

def test_connect_all_reports_connected_auth_and_error(vault, monkeypatch):
    mail.add_account("gmail", "eu@gmail.com", provider="gmail")
    mail.set_app_password("gmail", "senha-gmail")
    mail.add_account("sem-senha", "eu@outlook.com", provider="outlook")
    mail.add_account("rede", "eu@icloud.com", provider="icloud")
    mail.set_app_password("rede", "senha-icloud")

    def fake_open(acct):
        if acct.address == "eu@gmail.com":
            return FakeIMAP()
        if acct.address == "eu@icloud.com":
            raise OSError("network is unreachable")
        raise AssertionError("a conta sem senha não deveria tentar conectar")

    monkeypatch.setattr(mail, "_imap_open", fake_open)

    states = {s["name"]: s for s in mail.connect_all(settle_s=0.2)}
    assert states["gmail"]["state"] == "connected"
    assert states["gmail"]["connected"] is True
    assert states["sem-senha"]["state"] == "auth"
    assert "senha" in states["sem-senha"]["detail"]
    assert states["rede"]["state"] == "error"
    assert states["rede"]["connected"] is False


def test_imap_auth_error_is_auth_not_error(vault, monkeypatch):
    import imaplib
    mail.add_account("gmail", "eu@gmail.com", provider="gmail")
    mail.set_app_password("gmail", "errada")

    def fake_open(_acct):
        raise imaplib.IMAP4.error(
            "[AUTHENTICATIONFAILED] Invalid credentials (Failure)")

    monkeypatch.setattr(mail, "_imap_open", fake_open)
    entry = mail.test_account("gmail")
    assert entry["state"] == "auth"
    assert "recus" in entry["detail"]


def test_connect_all_is_parallel_and_bounded(vault, monkeypatch):
    import threading
    import time
    mail.add_account("lenta", "lenta@example.com")
    mail.set_app_password("lenta", "x")
    release = threading.Event()
    started = threading.Event()

    def slow_open(_acct):
        started.set()
        release.wait(0.5)
        raise OSError("demorou demais")

    monkeypatch.setattr(mail, "_imap_open", slow_open)
    begin = time.monotonic()
    states = mail.connect_all(settle_s=0.1)
    elapsed = time.monotonic() - begin
    release.set()
    assert started.is_set()
    assert elapsed < 3.0, "o boot não pode ficar refém de uma conta lenta"
    assert states[0]["state"] == "error"


# ── aviso de boot: uma frase por conta que falhou ──────────────────────────

class FakeUI:
    def __init__(self):
        self.muted = False
        self.lines: list[str] = []
        self.mail_opened = 0

    def write_log(self, text) -> None:
        self.lines.append(str(text))

    def open_mail(self) -> None:
        self.mail_opened += 1


class Notice:
    def __init__(self, ready: bool = True):
        obj = object.__new__(main.JarvisLive)
        obj.ui = FakeUI()
        obj.said: list[str] = []
        obj._say_now = lambda text, **kwargs: obj.said.append(str(text))
        obj._wait_boot_speech_ready = lambda timeout=25.0: ready
        self.obj = obj


def test_startup_notice_only_for_auth_that_needs_a_hand():
    notice = Notice()
    states = [
        {"name": "trabalho", "connected": False, "state": "auth"},
        {"name": "pessoal", "connected": True, "state": "connected"},
        {"name": "antiga", "connected": False, "state": "error"},
    ]
    lines = main.JarvisLive._mail_startup_notice(notice.obj, states)
    assert len(lines) == 1, "falha transitória no boot não fala na 1ª tentativa"
    assert "trabalho" in lines[0]
    assert "antiga" not in lines[0]
    assert all("na mão" in line for line in lines)
    assert notice.obj.said == lines
    assert notice.obj.ui.mail_opened == 1


def test_startup_notice_is_silent_for_transient_error():
    notice = Notice()
    states = [{"name": "trabalho", "connected": False, "state": "error"}]
    assert main.JarvisLive._mail_startup_notice(notice.obj, states) == []
    assert notice.obj.said == []
    assert notice.obj.ui.mail_opened == 0


def test_startup_notice_silent_when_all_connect():
    notice = Notice()
    states = [{"name": "trabalho", "connected": True, "state": "connected"}]
    assert main.JarvisLive._mail_startup_notice(notice.obj, states) == []
    assert notice.obj.said == []
    assert notice.obj.ui.mail_opened == 0


def test_startup_notice_falls_back_to_panel_without_audio():
    notice = Notice(ready=False)
    states = [{"name": "trabalho", "connected": False, "state": "auth"}]
    lines = main.JarvisLive._mail_startup_notice(notice.obj, states)
    assert len(lines) == 1
    assert notice.obj.said == []
    assert any("trabalho" in line for line in notice.obj.ui.lines)


class FakeLoop:
    def __init__(self):
        self.calls = []

    def call_soon_threadsafe(self, callback, *args):
        self.calls.append((callback, args))


def test_mail_retry_notice_schedules_module_message_on_ui_loop():
    obj = object.__new__(main.JarvisLive)
    obj.ui = FakeUI()
    obj.said = []
    obj._say_now = lambda text, **kwargs: obj.said.append((str(text), kwargs))
    obj._loop = FakeLoop()
    message = "Não consegui conectar o e-mail trabalho de novo; sigo tentando sozinho."
    main.JarvisLive._mail_retry_notice(obj, {"kind": "down", "message": message})
    assert obj.said == [], "a thread de reconexão não fala direto"
    assert len(obj._loop.calls) == 1
    callback, args = obj._loop.calls[0]
    callback(*args)
    assert obj.said == [(message, {"log": True, "origin": "email"})]


def test_start_mail_registers_retry_notice_before_connect(monkeypatch):
    obj = object.__new__(main.JarvisLive)
    obj.ui = FakeUI()
    obj._say_now = lambda *a, **k: None
    obj._wait_boot_speech_ready = lambda timeout=25.0: True
    order: list[str] = []
    captured: dict = {}

    def fake_set(handler=None):
        order.append("set")
        captured["handler"] = handler

    def fake_connect(*a, **k):
        order.append("connect")
        return []

    monkeypatch.setattr(mail, "set_retry_notice", fake_set)
    monkeypatch.setattr(mail, "connect_all", fake_connect)
    monkeypatch.setattr(mail, "start", lambda *a, **k: None)
    main.JarvisLive._start_mail(obj)

    assert order == ["set", "connect"], "hook registrado antes da 1ª tentativa"
    handler = captured["handler"]
    assert callable(handler)
    assert getattr(handler, "__self__", None) is obj
    assert (getattr(handler, "__func__", None)
            is main.JarvisLive._mail_retry_notice)


def test_start_mail_with_zero_accounts_never_raises(vault):
    obj = object.__new__(main.JarvisLive)
    obj.ui = FakeUI()
    obj._say_now = lambda *a, **k: None
    obj._wait_boot_speech_ready = lambda timeout=25.0: True
    main.JarvisLive._start_mail(obj)          # sem conta: retorna em silêncio
    assert obj.ui.lines == []


# ── inbox: cabeçalho sim, corpo não ────────────────────────────────────────

def test_refresh_extracts_headers_and_never_the_body(vault, monkeypatch):
    mail.add_account("gmail", "eu@gmail.com", provider="gmail")
    mail.set_app_password("gmail", "senha")
    fake = FakeIMAP({b"7": _header("Nota fiscal de outubro"),
                     b"9": _header("LinkedIn: alguém te mandou uma mensagem",
                                   sender="LinkedIn <messages-noreply@linkedin.com>",
                                   message_id="<li@linkedin.com>")})
    monkeypatch.setattr(mail, "_imap_open", lambda _a: fake)

    new = mail.refresh("gmail")
    assert len(new) == 2
    assert fake.selected_ro is True, "ler não pode marcar como lido"
    assert all("HEADER.FIELDS" in spec for spec in fake.specs)
    assert all("BODY[]" not in spec or "PEEK[HEADER" in spec
               for spec in fake.specs)

    items = mail.recent(5, account="gmail")
    assert len(items) == 2
    top = items[0]
    assert top["sender"] == "LinkedIn"
    assert top["from"] == "messages-noreply@linkedin.com"
    assert top["subject"].startswith("LinkedIn:")
    assert top["message_id"] == "<li@linkedin.com>"
    assert top["has_body"] is True
    assert top["body_stored"] is False
    assert "body" not in top
    assert isinstance(top["date"], str) and top["date"]

    # O jsonl em disco guarda só o cabeçalho: nada de corpo, nada de senha.
    raw = mail.recent(5, account="gmail")[0]["account"]
    assert raw == "gmail"
    stored = (mail._MEMORY_DIR / "mail_inbox_mail-gmail.jsonl").read_text()
    assert "Nota fiscal de outubro" in stored
    assert "senha" not in stored

    # Segunda varredura: os mesmos UIDs não duplicam o inbox.
    assert mail.refresh("gmail") == []
    assert len(mail.recent(50, account="gmail")) == 2


def test_privacy_body_never_reaches_spoken_or_notification(vault, monkeypatch):
    mail.add_account("gmail", "eu@gmail.com")
    mail.set_app_password("gmail", "senha")
    body_marker = "SEGREDO-DO-CORPO-123"
    raw = (b"From: Maria <maria@example.com>\r\nSubject: Assunto publico\r\n"
           b"Date: Sat, 01 Nov 2026 10:00:00 -0300\r\n"
           b"Message-ID: <m1@example.com>\r\n\r\n" + body_marker.encode())
    fake = FakeIMAP({b"3": raw})
    monkeypatch.setattr(mail, "_imap_open", lambda _a: fake)

    new = mail.refresh("gmail")
    assert len(new) == 1
    line = mail.alert_line(new[0])
    assert "Maria" in line and "Assunto publico" in line
    assert body_marker not in line
    assert body_marker not in json.dumps(new[0])

    # O aviso do main usa a mesma frase e nada do corpo.
    obj = object.__new__(main.JarvisLive)
    obj.ui = FakeUI()
    obj.said = []
    obj._say_now = lambda text, **kwargs: obj.said.append(str(text))
    obj._speech_route = lambda *a, **k: {"muted": False, "subject": ""}
    main.JarvisLive._mail_new_message(obj, new[0])
    assert len(obj.said) == 1
    assert body_marker not in obj.said[0]
    assert "Maria" in obj.said[0]


def test_alert_line_runs_through_speech_guard(vault, monkeypatch):
    item = {"sender": "Banco", "subject": "Ligue para 11999998888 agora"}
    line = mail.alert_line(item)
    assert "11999998888" not in line


def test_muted_route_keeps_mail_notice_on_screen():
    obj = object.__new__(main.JarvisLive)
    obj.ui = FakeUI()
    obj.said = []
    obj._say_now = lambda text, **kwargs: obj.said.append(str(text))
    obj._speech_route = lambda *a, **k: {"muted": True, "subject": "e-mail"}
    main.JarvisLive._mail_new_message(obj, {"sender": "Maria", "subject": "oi"})
    assert obj.said == []
    assert any("Maria" in line for line in obj.ui.lines)


# ── envio: só a voz do dono ────────────────────────────────────────────────

class FakeSpeaker:
    owner_name = "Anthero"

    def __init__(self, owner=True):
        self.owner = owner

    def embedding(self, _pcm):
        return [1.0, 0.0] if self.owner else [0.0, 1.0]

    def identify(self, _emb, device=None):
        if self.owner:
            return {"name": "Anthero", "score": 1.0, "known": True,
                    "authorized": True}
        return {"name": None, "score": 0.0, "known": False,
                "authorized": False}


def _voice_obj(owner=True, pcm=b"\x01\x00" * 16000):
    obj = object.__new__(main.JarvisLive)
    obj.ui = FakeUI()
    obj._speaker = FakeSpeaker(owner)
    obj.said = []
    obj.heard: list[str] = []
    obj._say_now = lambda text, **kwargs: obj.said.append(str(text))
    obj._log_heard = lambda text: obj.heard.append(str(text))
    obj._note_owner_speech = lambda *_a, **_k: None
    obj._current_pcm = pcm
    return obj


def _park(vault, monkeypatch, smtp=None):
    mail.add_account("gmail", "eu@gmail.com")
    mail.set_app_password("gmail", "senha")
    if smtp is not None:
        monkeypatch.setattr(mail, "_smtp_open", lambda _a: smtp)
    ok, msg = mail.request_send("dest@example.com", "Oi", "Mensagem do dono")
    assert ok, msg
    return msg


def test_request_send_never_sends_by_itself(vault, monkeypatch):
    smtp = FakeSMTP()
    _park(vault, monkeypatch, smtp)
    assert smtp.sent == []
    assert mail.pending_send() is not None
    # Sem a voz: recusa e o pedido continua parado.
    ok, msg = mail.confirm_send(voice_ok=False)
    assert ok is False
    assert "voz" in msg
    assert smtp.sent == []
    assert mail.pending_send() is not None


def test_confirm_with_voice_sends_once(vault, monkeypatch):
    smtp = FakeSMTP()
    _park(vault, monkeypatch, smtp)
    ok, msg = mail.confirm_send(voice_ok=True)
    assert ok is True
    assert "enviado" in msg
    assert len(smtp.sent) == 1
    sender, recipients, raw = smtp.sent[0]
    assert sender == "eu@gmail.com"
    assert recipients == ["dest@example.com"]
    import email as email_mod
    parsed = email_mod.message_from_string(raw)
    assert "Mensagem do dono" in parsed.get_payload(decode=True).decode("utf-8")
    assert parsed["Subject"] == "Oi"
    assert mail.pending_send() is None


def test_typed_text_never_authorises_send(vault, monkeypatch):
    smtp = FakeSMTP()
    _park(vault, monkeypatch, smtp)
    obj = _voice_obj(pcm=None)                 # texto digitado: sem áudio
    handled = main.JarvisLive._handle_mail_send_confirmation(
        obj, "confirmo enviar o e-mail", None)
    assert handled is True
    assert obj.said and "não foi enviado" in obj.said[-1]
    assert smtp.sent == []
    assert mail.pending_send() is not None, "o pedido continua esperando"


def test_other_voice_never_authorises_send(vault, monkeypatch):
    smtp = FakeSMTP()
    _park(vault, monkeypatch, smtp)
    obj = _voice_obj(owner=False)
    main.JarvisLive._handle_mail_send_confirmation(
        obj, "confirmo enviar o e-mail", obj._current_pcm)
    assert smtp.sent == []
    assert "não foi enviado" in obj.said[-1]


def test_owner_voice_sends_and_speaks_the_result(vault, monkeypatch):
    smtp = FakeSMTP()
    _park(vault, monkeypatch, smtp)
    obj = _voice_obj()
    main.JarvisLive._handle_mail_send_confirmation(
        obj, "confirmo enviar o e-mail", obj._current_pcm)
    assert len(smtp.sent) == 1
    assert "E-mail enviado" in obj.said[-1]


def test_cancel_drops_the_parked_send(vault, monkeypatch):
    smtp = FakeSMTP()
    _park(vault, monkeypatch, smtp)
    obj = _voice_obj()
    main.JarvisLive._handle_mail_send_confirmation(obj, "cancela", obj._current_pcm)
    assert mail.pending_send() is None
    assert "cancelado" in obj.said[-1]
    assert smtp.sent == []


def test_confirmation_without_pending_is_consumed_not_parked(vault):
    obj = _voice_obj()
    assert main.JarvisLive._mail_send_answer_expected(
        obj, "confirmo enviar o e-mail") is True
    main.JarvisLive._handle_mail_send_confirmation(
        obj, "confirmo enviar o e-mail", obj._current_pcm)
    assert "Não havia envio" in obj.said[-1]
    assert mail.pending_send() is None


def test_unrelated_sentence_is_not_an_answer(vault, monkeypatch):
    _park(vault, monkeypatch, FakeSMTP())
    obj = _voice_obj()
    assert main.JarvisLive._mail_send_answer_expected(obj, "e o tempo hoje?") is True
    assert main.JarvisLive._handle_mail_send_confirmation(
        obj, "e o tempo hoje?", obj._current_pcm) is False
    assert obj.said == []


def test_request_send_validates_address(vault):
    mail.add_account("gmail", "eu@gmail.com")
    mail.set_app_password("gmail", "senha")
    ok, msg = mail.request_send("nao-e-endereco", "oi", "corpo")
    assert ok is False
    assert "endereço" in msg
    assert mail.pending_send() is None


def test_request_send_needs_password(vault):
    mail.add_account("gmail", "eu@gmail.com")
    ok, msg = mail.request_send("dest@example.com", "oi", "corpo")
    assert ok is False
    assert "senha de app" in msg


def test_tool_refuses_to_send_and_parks(vault, monkeypatch):
    from plugins import mail_control
    smtp = FakeSMTP()
    mail.add_account("gmail", "eu@gmail.com")
    mail.set_app_password("gmail", "senha")
    monkeypatch.setattr(mail, "_smtp_open", lambda _a: smtp)
    out = mail_control.run({"action": "send", "to": "dest@example.com",
                            "subject": "Oi", "body": "texto"})
    assert "confirmo enviar" in out
    assert smtp.sent == []
    assert mail.pending_send() is not None


def test_legacy_plugin_send_also_parks(vault, monkeypatch):
    """O plugin antigo não pode ser um atalho para o SMTP."""
    from plugins import email_control
    smtp = FakeSMTP()
    mail.add_account("gmail", "eu@gmail.com")
    mail.set_app_password("gmail", "senha")
    monkeypatch.setattr(mail, "_smtp_open", lambda _a: smtp)
    spoken, _panel = email_control.send("dest@example.com", "Oi", "corpo")
    assert "confirmo enviar" in spoken
    assert smtp.sent == []


def test_bridge_tier_gates_mail_send():
    from core import tool_bridge
    assert tool_bridge.ACTION_TIERS["mail_control"] == tool_bridge.TIER_MEDIUM
    assert tool_bridge.ACTION_TIERS.get("mail_control") is not None
    tier = tool_bridge._ESCALATING_ARGS["mail_control"]
    assert "send" in tier
