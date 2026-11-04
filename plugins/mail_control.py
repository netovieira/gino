"""E-mail por voz: contas, estado real, não lidos, ler um e enviar.

Este plugin é a boca e os olhos; quem fala IMAP/SMTP é core/email.py, que
guarda a senha de app no cofre (config/api_keys.json, 600) e a lista de contas
em plugin_config.mail — o mesmo molde do WhatsApp.

MULTI-CONTA: `account` (nome) escolhe a conta. Com várias contas e sem nome, a
ferramenta pergunta qual em vez de escolher sozinha — mandar e-mail pela conta
errada é exatamente o erro que não dá para desfazer.

PRIVACIDADE: `recent`/`unread` entregam REMETENTE e ASSUNTO. O corpo só sai com
action=read, e só depois de o dono pedir. Nada disso vira fala sem passar pelo
guarda de fala (core/speech_guard.py) — ver `core.email.safe_line`.

ENVIO: `send` NÃO envia. Ele estaciona o pedido e devolve a frase que o modelo
deve repetir: o dono precisa dizer em voz alta "confirmo enviar o e-mail". Só o
portão de voz do main.py confirma, e só com a voz do dono reconhecida. Texto
digitado nunca envia e-mail.

    "tem e-mail novo?"                      → action: unread
    "lê o e-mail do banco"                  → action: read, query: banco
    "manda um e-mail pro fulano..."         → action: send (fica esperando a voz)
    "checa a conta do trabalho"             → action: status, account: trabalho
"""
from __future__ import annotations

import time

from core import email as mail

NS = "mail"


def _accounts() -> list[dict]:
    try:
        return mail.list_accounts()
    except Exception:
        return []


def _ambiguous(account: str) -> str | None:
    """None se dá para agir; senão a frase pedindo qual conta usar."""
    accts = _accounts()
    if len(accts) <= 1:
        return None
    names = [a["name"] for a in accts]
    if account and account.lower() in [n.lower() for n in names]:
        return None
    if mail.default_account_name():
        return None
    return ("você tem mais de uma conta de e-mail: " + ", ".join(names)
            + ". Qual eu uso?")


def _when(ts) -> str:
    try:
        value = float(ts or 0)
    except Exception:
        return ""
    if value <= 0:
        return ""
    today = time.strftime("%d/%m/%Y", time.localtime())
    day = time.strftime("%d/%m/%Y", time.localtime(value))
    if day == today:
        return time.strftime("%H:%M", time.localtime(value))
    return time.strftime("%d/%m %H:%M", time.localtime(value))


def _header_lines(items: list[dict]) -> str:
    """Remetente + assunto. Nunca o corpo — nem um trecho dele."""
    out = []
    for m in items:
        when = _when(m.get("ts"))
        sender = str(m.get("sender") or m.get("from") or "?")
        subject = str(m.get("subject") or "(sem assunto)")
        out.append(f"{when + ' — ' if when else ''}{sender}: {subject}")
    return "\n".join(out)


def _show(player, title: str, body: str) -> None:
    if player:
        try:
            player.show_content(title, body)
        except Exception:
            pass


# ── as ações ─────────────────────────────────────────────────────────────────

def _do_accounts() -> tuple[str, str]:
    accts = _accounts()
    if not accts:
        return ("nenhuma conta de e-mail configurada ainda — abra o painel "
                "E-MAIL no computador e cadastre a primeira", "sem contas")
    lines = []
    for a in accts:
        if a.get("state") == "connected":
            estado = "conectada"
        elif a.get("state") == "auth":
            estado = "precisa de senha de app"
        elif a.get("state") == "error":
            estado = "sem conexão"
        else:
            estado = "não testada"
        mark = " (padrão)" if a.get("default") else ""
        lines.append(f"{a['name']}{mark}: {a.get('address') or 'sem endereço'} — "
                     f"{estado}" + ("" if a.get("has_password")
                                    else " · falta a senha de app"))
    body = "\n".join(lines)
    connected = [a["name"] for a in accts if a.get("state") == "connected"]
    spoken = mail.safe_line(
        f"{len(accts)} conta(s) de e-mail; {len(connected)} conectada(s)")
    return spoken, body


def _do_status(account: str) -> tuple[str, str]:
    if not _accounts():
        return ("nenhuma conta de e-mail configurada ainda", "sem contas")
    if not account and len(_accounts()) > 1:
        lines = [f"{a['name']}: {a.get('state')} — {a.get('detail') or 'sem detalhe'}"
                 for a in _accounts()]
        return (mail.safe_line(f"{len(lines)} contas de e-mail configuradas"),
                "\n".join(lines))
    line = mail.status_line(account or None)
    return mail.safe_line(line), line


def _do_test(account: str) -> tuple[str, str]:
    if not account:
        amb = _ambiguous(account)
        if amb:
            return amb, "conta ambígua"
        account = mail.default_account_name() or (
            _accounts()[0]["name"] if _accounts() else "")
    if not account:
        return ("nenhuma conta de e-mail configurada ainda", "sem contas")
    entry = mail.test_account(account)
    state = str(entry.get("state") or "")
    detail = str(entry.get("detail") or "")
    if state == "connected":
        spoken = f"a conta {entry.get('name')} conectou de verdade: {entry.get('address')}"
    elif state == "auth":
        spoken = (f"a conta {entry.get('name')} não autenticou ({detail}); "
                  "precisa de senha de app válida")
    else:
        spoken = f"a conta {entry.get('name')} não conectou: {detail}"
    body = f"{entry.get('name')} [{state}] {entry.get('address')}\n{detail}"
    return mail.safe_line(spoken), body


def _do_unread(limit: int, account: str) -> tuple[str, str]:
    if not _accounts():
        return ("nenhuma conta de e-mail configurada ainda", "sem contas")
    amb = _ambiguous(account)
    if amb:
        return amb, "conta ambígua"
    try:
        mail.refresh(account or None)
    except Exception as e:
        return (f"não consegui checar a caixa agora: {e}", str(e))
    items = mail.recent(limit, account=account or None)
    label = f" do {account}" if account and len(_accounts()) > 1 else ""
    count = mail.unread_count(account or None)
    if not items:
        empty = f"nenhum e-mail novo{label}"
        return empty, empty
    body = _header_lines(items)
    spoken = mail.safe_line(
        f"{count} e-mail(s) não lido(s){label}. Os mais recentes: "
        + "; ".join(f"{m.get('sender') or m.get('from')}, {m.get('subject') or 'sem assunto'}"
                    for m in items[:3]))
    return spoken, body


def _match(items: list[dict], query: str) -> list[dict]:
    q = str(query or "").strip().lower()
    if not q:
        return items
    def _has(item: dict) -> bool:
        fields = (item.get("sender"), item.get("from"), item.get("subject"))
        return any(q in str(f or "").lower() for f in fields)
    return [m for m in items if _has(m)]


def _do_read(query: str, account: str) -> tuple[str, str]:
    """Lê UM corpo — só aqui, e só porque o dono pediu."""
    if not _accounts():
        return ("nenhuma conta de e-mail configurada ainda", "sem contas")
    amb = _ambiguous(account)
    if amb:
        return amb, "conta ambígua"
    items = mail.recent(20, account=account or None)
    hits = _match(items, query)
    if not hits:
        if not query:
            return ("qual e-mail eu leio? diga o remetente ou o assunto", "falta a busca")
        return (f"não achei e-mail com '{query}' no que já chegou", "sem resultado")
    target = hits[0]
    owner = account or str(target.get("account") or "")
    ok, subject, body = mail.fetch_body(
        owner, str(target.get("uid") or target.get("message_id") or ""))
    if not ok:
        return (f"não consegui abrir o e-mail: {body}", str(body))
    sender = str(target.get("sender") or target.get("from") or "?")
    header = f"{sender} — {subject or '(sem assunto)'}"
    shown = f"{header}\n\n{body}"
    spoken = mail.safe_line(
        f"O e-mail é de {sender}, assunto «{subject or 'sem assunto'}». "
        f"O texto está na tela.")
    return spoken, shown


def _do_send(to: str, subject: str, body: str, account: str) -> tuple[str, str]:
    """Estaciona o envio. O e-mail NÃO sai por aqui.

    A frase devolvida NÃO é sanitizada de propósito: ela carrega a instrução
    exata que o dono precisa dizer ("confirmo enviar o e-mail") e um guarda de
    fala que reescrevesse isso deixaria o portão de voz inalcançável. O texto é
    endereço validado + assunto ditado pelo dono, não conteúdo de terceiro.
    """
    if not _accounts():
        return ("nenhuma conta de e-mail configurada ainda — cadastre em E-MAIL",
                "sem contas")
    amb = _ambiguous(account)
    if amb:
        return amb, "conta ambígua"
    ok, msg = mail.request_send(to, subject, body, account=account or None)
    return msg, msg


def _do_cancel() -> tuple[str, str]:
    return mail.cancel_send(), "cancelado"


def _do_pending() -> tuple[str, str]:
    pending = mail.pending_send()
    if not pending:
        return ("nenhum envio de e-mail esperando confirmação", "nada pendente")
    line = (f"e-mail para {pending.get('to')} com o assunto "
            f"«{pending.get('subject') or '(sem assunto)'}» esperando a sua voz "
            f"dizer «{mail.CONFIRM_MARKER} o e-mail»")
    return line, line


# ── entrada da ferramenta ────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    action  = str(params.get("action") or "").strip().lower()
    account = str(params.get("account") or "").strip()
    to      = str(params.get("to") or "").strip()
    subject = str(params.get("subject") or "").strip()
    body    = str(params.get("body") or params.get("text") or "")
    query   = str(params.get("query") or "").strip()
    try:
        limit = int(params.get("limit") or 8)
    except Exception:
        limit = 8

    try:
        if action in ("accounts", "contas", "listar_contas", "quais_contas"):
            spoken, body_out = _do_accounts()
            title = "E-MAIL — contas"
        elif action in ("status", "estado"):
            spoken, body_out = _do_status(account)
            title = "E-MAIL — estado"
        elif action in ("test", "testar", "connect", "conectar", "checar"):
            spoken, body_out = _do_test(account)
            title = "E-MAIL — conexão"
        elif action in ("read", "ler", "abrir"):
            spoken, body_out = _do_read(query, account)
            title = "E-MAIL — leitura"
        elif action in ("send", "enviar", "manda", "mandar"):
            spoken, body_out = _do_send(to, subject, body, account)
            title = "E-MAIL — envio (esperando a voz do dono)"
        elif action in ("cancel", "cancelar"):
            spoken, body_out = _do_cancel()
            title = "E-MAIL — envio cancelado"
        elif action in ("pending", "pendente"):
            spoken, body_out = _do_pending()
            title = "E-MAIL — envio pendente"
        else:  # unread / inbox / recent — o padrão, porque é o que mais se pede
            spoken, body_out = _do_unread(limit, account)
            title = "E-MAIL — não lidos"
    except Exception as e:
        return mail.safe_line(f"o e-mail falhou: {e}")

    _show(player, title, body_out)
    if player:
        try:
            player.write_log(f"{getattr(player, '_asst_name', 'JARVIS')}: {spoken[:400]}")
        except Exception:
            pass
    return spoken


PLUGIN = {
    "name": "mail_control",
    "description": (
        "The user's OWN e-mail account(s), via IMAP/SMTP (Gmail, Outlook, iCloud, "
        "Yahoo or any IMAP host): list the accounts, test the connection, say "
        "how many messages are unread and from whom, show recent headers "
        "(sender + subject), read one message aloud when asked, and prepare an "
        "e-mail to send. Actions: "
        "accounts — list the configured accounts; "
        "status — is a mailbox connected (real state, not a guess); "
        "test — really connect to the mailbox and report the result; "
        "unread — the unread messages (sender, subject, time) so you can "
        "summarise them ('tem email novo?', 'o que chegou?'); "
        "read — fetch the body of ONE message that matches a word "
        "('lê o email da Maria'); the body is only fetched when the user asks; "
        "send — prepare a new e-mail with to/subject/body. "
        "IMPORTANT: send NEVER sends. It parks the message and returns a "
        "sentence asking the owner to say, out loud, 'confirmo enviar o e-mail'. "
        "Only the owner's recognised voice can release it; you cannot confirm "
        "and neither can typed text. Repeat the parked sentence and do not "
        "claim the e-mail was sent before the result says it was. "
        "NEVER include the body of a message in a spoken answer unless the user "
        "asked to read it: alerts are sender + subject only. "
        "The user may have SEVERAL accounts: when they name one ('manda pelo "
        "trabalho'), pass account=<name>; when they don't and more than one is "
        "configured, the tool asks which — relay that question, do not guess."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "unread (what arrived) | read (body of one "
                               "message) | send (park, then the owner's voice "
                               "confirms) | accounts | status | test | cancel "
                               "| pending",
            },
            "account": {
                "type": "STRING",
                "description": "Which e-mail account to use when the user has "
                               "more than one (e.g. 'pessoal', 'trabalho'). "
                               "Omit when they have one, or when they didn't "
                               "say — the tool will ask.",
            },
            "to": {
                "type": "STRING",
                "description": "send: the recipient's full e-mail address.",
            },
            "subject": {
                "type": "STRING",
                "description": "send: the subject, in the user's own words.",
            },
            "body": {
                "type": "STRING",
                "description": "send: exactly what the user wants written, in "
                               "their own language. Never invent or embellish it.",
            },
            "query": {
                "type": "STRING",
                "description": "read: a word from the sender or the subject "
                               "(e.g. 'banco', 'nota fiscal', 'Maria')",
            },
            "limit": {
                "type": "INTEGER",
                "description": "unread: how many messages (default 8)",
            },
        },
        "required": ["action"],
    },
}


# ── o botão do painel de configuração ───────────────────────────────────────

def _connect_action(values: dict | None = None) -> tuple[bool, str]:
    """Botão 'TESTAR TUDO' em ⚙ → PLUGIN SETTINGS → EMAIL.

    Não aceita senha: senha de app só entra pelo painel E-MAIL (campo write-only)
    ou por `set_app_password`. Aqui só testamos o que já está no cofre.
    """
    accts = _accounts()
    if not accts:
        return False, "nenhuma conta configurada — abra o painel E-MAIL"
    states = mail.connect_all()
    bad = [s for s in states if not s.get("connected")]
    if not bad:
        return True, ("conectado: " + ", ".join(s.get("name") or "?"
                                                for s in states))
    return False, "; ".join(
        f"{s.get('name') or '?'}: {s.get('state')} ({s.get('detail') or '-'})"
        for s in bad)


def _account_choices() -> list[str]:
    try:
        return [a["name"] for a in _accounts()]
    except Exception:
        return []


PLUGIN_SETTINGS = {
    "namespace": NS,
    "title": "E-MAIL (IMAP/SMTP, várias contas)",
    "fields": [
        {"key": "default_account",
         "label": "Conta de e-mail padrão (quando você não disser qual)",
         "type": "choice", "options": _account_choices(), "default": ""},
    ],
    "action": {"label": "TESTAR TODAS AS CONTAS", "run": _connect_action},
}
