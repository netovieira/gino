"""Email por voz: não lidos, busca, ler um e enviar — IMAP/SMTP multi-conta.

REGISTRO AUTORITATIVO: quem manda nas contas é core/email.py
(`plugin_config.mail`, senha de app no cofre `mail_secrets`). Este plugin LÊ a
lista de lá (`_accounts_from_registry`) e, se ainda existir, o formato antigo
na raiz de `plugin_config.email` — que está sendo apagado. O painel do ⚙ desta
casa é SOMENTE LEITURA: não grava senha em lugar nenhum; criar/editar conta é
na tela 📧 E-MAIL (ui.MailOverlay), que fala com o registro novo e com o cofre.

Uma senha de app resolve tudo (conta Google → Segurança → Verificação em duas
etapas → Senhas de app); a senha normal da conta NÃO funciona aqui, porque o
Gmail não aceita mais login simples em IMAP/SMTP. As credenciais ficam em
config/api_keys.json, no mesmo lugar das outras, e nunca vão para o log.

MULTI-CONTA: `plugin_config.email.accounts` é uma lista de
`{name, address, app_password, imap_host, smtp_host, default}`. Sem a lista,
 migra o formato antigo (uma conta só, Gmail). Cada conta tem seu host IMAP/SMTP,
então dá para misturar Gmail + Outlook + IMAP genérico. Toda ação aceita
`account` (nome); sem ele e com várias, pergunta qual.

DETALHE QUE IMPORTA: a leitura usa BODY.PEEK. Pedir "UNSEEN" e ler com PEEK
devolve o conteúdo SEM tirar o negrito da mensagem na sua caixa de entrada —
o assistente lendo seus e-mails não os marca como lidos atrás de você.
"""
from __future__ import annotations

import email
import imaplib
import re
import ssl
from email.header import decode_header
from email.utils import parsedate_to_datetime

from memory.config_manager import get_plugin_config

NS = "email"

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
FOLDER    = "INBOX"
TIMEOUT   = 25
SCAN      = 200          # quantas mensagens recentes a busca alcança
READ_CHARS = 1100        # quanto do corpo é falado de uma vez

_ADDR_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


# ── contas ───────────────────────────────────────────────────────────────────

def _accounts_from_registry() -> list[dict]:
    """As contas como elas vivem hoje: core/email.py + cofre.

    A senha é pedida ao registro (que a lê do cofre) e fica só na memória
    desta chamada: nada é gravado e nada aparece em log. É o que mantém as
    ações antigas (unread/search/read/status) funcionando depois da limpeza
    do namespace `email`.
    """
    try:
        from core import email as registry
        records = registry.list_accounts()
    except Exception:
        return []
    out: list[dict] = []
    for rec in records:
        address = str(rec.get("address") or "").strip()
        if not address:
            continue
        name = str(rec.get("name") or "principal").strip() or "principal"
        try:
            secret = registry.app_password(rec.get("uuid") or name)
        except Exception:
            secret = ""
        out.append({
            "name": name,
            "address": address,
            "app_password": secret,
            "name_display": str(rec.get("display_name") or ""),
            "imap_host": str(rec.get("imap_host") or IMAP_HOST),
            "smtp_host": str(rec.get("smtp_host") or SMTP_HOST),
            "default": bool(rec.get("default")),
        })
    return out


def _load_accounts() -> list[dict]:
    """A lista de contas; migra o formato antigo (uma conta, Gmail).

    Ordem: a lista nova (`plugin_config.mail.accounts`, via core/email.py) manda;
    o formato antigo da raiz de `plugin_config.email` é só o que sobrou de uma
    configuração não migrada. Este módulo não grava em nenhum dos dois.
    """
    cfg = get_plugin_config(NS) or {}
    accts = cfg.get("accounts")
    if isinstance(accts, list) and accts:
        return [a for a in accts if isinstance(a, dict) and a.get("address")]
    modern = _accounts_from_registry()
    if modern:
        return modern
    # Formato antigo: address/app_password/name na raiz do namespace.
    if cfg.get("address"):
        return [{
            "name": "principal", "address": cfg["address"],
            "app_password": re.sub(r"\s+", "", str(cfg.get("app_password") or "")),
            "name_display": str(cfg.get("name") or ""),
            "imap_host": str(cfg.get("imap_host") or IMAP_HOST),
            "smtp_host": str(cfg.get("smtp_host") or SMTP_HOST),
            "default": True,
        }]
    return []


def _account_names() -> list[str]:
    return [a.get("name") or a.get("address", "?") for a in _load_accounts()]


def _resolve(values: dict | None, account: str | None) -> dict:
    """A conta pedida (nome), ou a default, ou a única. Levanta _NotConfigured."""
    accts = _load_accounts()
    if not accts:
        raise _NotConfigured
    chosen = None
    if account:
        key = str(account).strip().lower()
        chosen = next((a for a in accts
                       if (a.get("name") or "").lower() == key
                       or a.get("address", "").lower() == key), None)
        if chosen is None:
            raise RuntimeError(f"não tenho a conta de email '{account}'; "
                               "tenho: " + ", ".join(_account_names()))
    elif len(accts) == 1:
        chosen = accts[0]
    else:
        chosen = next((a for a in accts if a.get("default")), accts[0])
    cfg = {
        "address": str(chosen.get("address") or "").strip(),
        "app_password": re.sub(r"\s+", "", str(chosen.get("app_password") or "")),
        "name": str(chosen.get("name_display") or chosen.get("name") or "").strip(),
        "imap_host": str(chosen.get("imap_host") or IMAP_HOST).strip(),
        "smtp_host": str(chosen.get("smtp_host") or SMTP_HOST).strip(),
        "account_name": str(chosen.get("name") or "principal"),
    }
    # O que o usuário está digitando no painel vale mais que o salvo.
    if values:
        for k in ("address", "app_password", "name", "imap_host", "smtp_host"):
            v = values.get(k)
            if v not in (None, ""):
                cfg[k] = re.sub(r"\s+", "", v) if k == "app_password" else str(v).strip()
    if not cfg["address"] or not cfg["app_password"]:
        raise _NotConfigured
    return cfg


def _ambiguous(account: str) -> str | None:
    """None se dá para agir; senão a frase pedindo qual conta usar."""
    names = _account_names()
    if len(names) <= 1:
        return None
    if account and account.lower() in [n.lower() for n in names]:
        return None
    return ("você tem mais de uma conta de email: " + ", ".join(names)
            + ". Qual eu uso?")

PLUGIN = {
    "name": "email_control",
    "description": (
        "The user's email account(s), via IMAP/SMTP (Gmail, Outlook, iCloud or "
        "any IMAP host): how many are unread and from whom, search the mailbox, "
        "read one message aloud, and send an email. Actions: "
        "unread — the unread messages (sender, subject, time) so you can "
        "summarise them ('tem email novo?', 'o que chegou?'); "
        "search — find messages by a word in the sender, subject or body "
        "('procura o email do banco'); "
        "read — open the newest message that matches a word and read its text "
        "('lê o email da Maria'); "
        "send — write a new email to an address with a subject and body; "
        "accounts — list the configured accounts; status — is the mailbox "
        "configured and reachable. The user may have SEVERAL accounts: when "
        "they name one ('manda pelo trabalho', 'checa o email da empresa'), "
        "pass account=<name>; when they don't and more than one is configured, "
        "the tool asks which — relay that question, do not guess. Use this for "
        "anything about email / Gmail / inbox / 'meus emails'. Never claim to "
        "have sent an email unless the result says it was sent."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "unread | search | read | send | accounts | status",
            },
            "account": {
                "type": "STRING",
                "description": "Which email account to use when the user has "
                               "more than one (e.g. 'pessoal', 'trabalho'). "
                               "Omit when they have one, or when they didn't "
                               "say — the tool will ask.",
            },
            "query": {
                "type": "STRING",
                "description": "search/read: a word from the sender, the subject "
                               "or the body (e.g. 'banco', 'nota fiscal', 'Maria')",
            },
            "to": {
                "type": "STRING",
                "description": "send: the recipient's email address",
            },
            "subject": {"type": "STRING", "description": "send: the subject line"},
            "body": {
                "type": "STRING",
                "description": "send: the email text, in the user's own language, "
                               "exactly what they asked to say",
            },
            "limit": {
                "type": "INTEGER",
                "description": "unread/search: how many results (default 6)",
            },
        },
        "required": ["action"],
    },
}


# ── configuração ─────────────────────────────────────────────────────────────

class _NotConfigured(Exception):
    pass


def _imap(cfg: dict) -> imaplib.IMAP4_SSL:
    host = cfg.get("imap_host") or IMAP_HOST
    conn = imaplib.IMAP4_SSL(host, IMAP_PORT, timeout=TIMEOUT,
                             ssl_context=ssl.create_default_context())
    try:
        conn.login(cfg["address"], cfg["app_password"])
    except imaplib.IMAP4.error as e:
        try:
            conn.logout()
        except Exception:
            pass
        raise RuntimeError(
            f"{host} recusou o login — use uma SENHA DE APP, não a "
            f"senha normal da conta ({e})")
    return conn


def _login_error(e: Exception) -> str:
    if isinstance(e, _NotConfigured):
        return ("o email ainda não está configurado: ⚙ → PLUGIN SETTINGS → EMAIL, "
                "com o endereço e a senha de app")
    return str(e)


# ── leitura de cabeçalhos e corpo ────────────────────────────────────────────

def _hdr(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        parts = decode_header(raw)
        out = []
        for text, enc in parts:
            if isinstance(text, bytes):
                out.append(text.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(text)
        return " ".join(p.strip() for p in out if p).strip()
    except Exception:
        return str(raw)


def _when(raw: str | None) -> tuple[int, str]:
    try:
        dt = parsedate_to_datetime(raw) if raw else None
        if dt is None:
            return 0, ""
        return int(dt.timestamp()), dt.strftime("%d/%m %H:%M")
    except Exception:
        return 0, str(raw or "")[:20]


def _fetch_headers(conn, uids: list[bytes]) -> list[dict]:
    """Cabeçalhos de uma lista de UIDs, em blocos — um FETCH por mensagem em
    200 mensagens é o tipo de lentidão que faz o assistente parecer travado."""
    out = []
    for i in range(0, len(uids), 20):
        chunk = b",".join(uids[i:i + 20])
        try:
            typ, data = conn.uid(
                "FETCH", chunk,
                "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        except Exception:
            continue
        if typ != "OK" or not data:
            continue
        for item in data:
            if not (isinstance(item, tuple) and len(item) == 2):
                continue
            meta, raw = item
            m = re.search(rb"UID (\d+)", meta or b"")
            uid = m.group(1).decode() if m else ""
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            ts, when = _when(msg.get("Date"))
            out.append({
                "uid": uid,
                "from": _hdr(msg.get("From")),
                "subject": _hdr(msg.get("Subject")) or "(sem assunto)",
                "ts": ts,
                "when": when,
            })
    return out


def _search_uids(conn, criteria: str) -> list[bytes]:
    typ, data = conn.uid("SEARCH", None, criteria)
    if typ != "OK" or not data:
        return []
    return (data[0] or b"").split()


def _recent(conn, criteria: str = "ALL", scan: int = SCAN) -> list[dict]:
    uids = _search_uids(conn, criteria)[-scan:]
    rows = _fetch_headers(conn, uids)
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows


def _decode_part(part) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    for enc in (charset, "utf-8", "latin-1"):
        try:
            return payload.decode(enc, errors="strict")
        except Exception:
            continue
    return payload.decode("utf-8", errors="replace")


def _html_text(html: str) -> str:
    """Texto legível de um email que só tem HTML.

    As quebras de bloco viram linha antes do parser: sem isso o <b> de um
    título quebra a frase no meio e a leitura em voz alta sai picada.
    """
    preparado = re.sub(
        r"<\s*(br|/p|/div|/tr|/li|/h[1-6]|/table)\s*/?\s*>", "\n", html,
        flags=re.IGNORECASE)
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(preparado, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        return soup.get_text(" ")
    except Exception:
        return re.sub(r"<[^>]+>", " ", preparado)


_QUOTE_TAIL = re.compile(
    r"(^\s*Em .{5,120}escreveu:\s*$)|(^\s*On .{5,120}wrote:\s*$)|"
    r"(^\s*-{2,}\s*Mensagem original\s*-{2,})|(^\s*_{10,}\s*$)",
    re.IGNORECASE | re.MULTILINE)
_SIGNATURE = re.compile(r"^--\s?$", re.MULTILINE)


def _clean(text: str) -> str:
    """Tira a citação e a assinatura: quem ouve já conhece o próprio texto, e
    800 caracteres de histórico atrapalham a resposta."""
    text = text.replace("\r\n", "\n")
    cuts = []
    m = _QUOTE_TAIL.search(text)
    if m and m.start() > 40:
        cuts.append(m.start())
    sig = _SIGNATURE.search(text)
    if sig and sig.start() > 40:
        cuts.append(sig.start())
    if cuts:
        text = text[:min(cuts)]
    lines = [re.sub(r"[ \t]{2,}", " ", ln.strip()) for ln in text.splitlines()]
    lines = [ln for ln in lines if not ln.startswith(">")]
    out, blank = [], False
    for ln in lines:
        if not ln:
            if not blank:
                out.append("")
            blank = True
            continue
        blank = False
        out.append(ln)
    return "\n".join(out).strip()


def _body(conn, uid: str) -> str:
    typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
    if typ != "OK" or not data:
        return ""
    raw = next((d[1] for d in data if isinstance(d, tuple)), None)
    if not raw:
        return ""
    msg = email.message_from_bytes(raw)
    if msg.is_multipart():
        for part in msg.walk():
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if part.get_content_type() == "text/plain":
                return _clean(_decode_part(part))
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return _clean(_html_text(_decode_part(part)))
        return ""
    if msg.get_content_type() == "text/html":
        return _clean(_html_text(_decode_part(msg)))
    return _clean(_decode_part(msg))


def _fmt(rows: list[dict]) -> str:
    return "\n".join(
        f"{r['when']} — {r['from']}: {r['subject']}" for r in rows
    )


# ── as ações ─────────────────────────────────────────────────────────────────

def unread(limit: int = 6, values: dict | None = None, account: str = "") -> tuple[str, str]:
    cfg = _resolve(values, account or None)
    conn = _imap(cfg)
    try:
        conn.select(FOLDER, readonly=True)
        rows = _recent(conn, "UNSEEN", scan=60)[:limit]
        total = len(_search_uids(conn, "UNSEEN"))
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    if not rows:
        return "nenhum email não lido.", "EMAIL — não lidos\n  (caixa zerada)"
    head = (f"{total} não lido{'s' if total > 1 else ''}" if total
            else f"{len(rows)} não lido(s)")
    spoken = f"{head}:\n{_fmt(rows)}"
    return spoken, f"EMAIL — não lidos\n{_fmt(rows)}"


def search(query: str, limit: int = 6, values: dict | None = None,
           account: str = "") -> tuple[str, str]:
    q = str(query or "").strip().lower()
    if not q:
        return "o que eu procuro? diga uma palavra do assunto ou do remetente.", \
               "EMAIL — busca vazia"
    cfg = _resolve(values, account or None)
    conn = _imap(cfg)
    try:
        conn.select(FOLDER, readonly=True)
        rows = _recent(conn)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    hits = [r for r in rows
            if q in (r["subject"] + " " + r["from"]).lower()][:limit]
    if not hits:
        return (f"não achei nada com '{query}' nos últimos {len(rows)} emails.",
                f"EMAIL — busca '{query}'\n  (nada encontrado)")
    return (f"achei {len(hits)}:\n{_fmt(hits)}",
            f"EMAIL — busca '{query}'\n{_fmt(hits)}")


def read(query: str, values: dict | None = None, account: str = "") -> tuple[str, str]:
    q = str(query or "").strip().lower()
    cfg = _resolve(values, account or None)
    conn = _imap(cfg)
    try:
        conn.select(FOLDER, readonly=True)
        rows = _recent(conn)
        # Sem palavra-chave, "lê meu último email" é o caso comum.
        target = rows[0] if not q else next(
            (r for r in rows if q in (r["subject"] + " " + r["from"]).lower()), None)
        if target is None:
            return (f"não achei nenhum email com '{query}'.",
                    f"EMAIL — ler '{query}'\n  (nada encontrado)")
        body = _body(conn, target["uid"])
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    head = f"{target['from']} — {target['subject']} ({target['when']})"
    if not body:
        spoken = f"{head}. Só tem anexo ou imagem, sem texto para ler."
        return spoken, f"EMAIL — leitura\n{head}\n\n(sem corpo em texto)"
    snippet = body[:READ_CHARS]
    tail = "…" if len(body) > READ_CHARS else ""
    spoken = f"{head}:\n{snippet}{tail}"
    return spoken, f"EMAIL — leitura\n{head}\n\n{body[:4000]}"


def send(to: str, subject: str, body: str, values: dict | None = None,
         account: str = "") -> tuple[str, str]:
    """Envio pela camada NOVA — e só com a voz do dono.

    Este plugin nasceu antes de core/email.py e mandava direto pelo SMTP. A
    regra do dono é que e-mail é irreversível e exige a voz dele, então aqui só
    estacionamos o pedido na mesma fila de core.email.request_send; o SMTP real
    (send_now) só é alcançado pelo portão de voz do main.py. `values` não é
    usado: a ferramenta nunca manda senha nenhuma, só o que o dono ditou.
    """
    from core import email as _mail
    try:
        _ok, message = _mail.request_send(to, subject, body,
                                          account=account or None)
    except Exception as e:
        message = f"não consegui preparar o e-mail: {e}"
    return message, "E-MAIL — envio\n  " + message


def status(values: dict | None = None, account: str = "") -> tuple[str, str]:
    cfg = _resolve(values, account or None)
    conn = _imap(cfg)
    try:
        conn.select(FOLDER, readonly=True)
        unread_n = len(_search_uids(conn, "UNSEEN"))
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return (f"{cfg['address']} conectado, {unread_n} não lido(s).",
            f"EMAIL — {cfg['address']}\n  {unread_n} não lido(s)")


# ── o teste do botão da UI ───────────────────────────────────────────────────

def _test(values: dict | None = None) -> tuple[bool, str]:
    """Painel ⚙ → EMAIL: SOMENTE LEITURA; cadastro é na tela 📧 E-MAIL.

    Este painel já gravou a senha de app em texto puro dentro de
    `plugin_config.email` (a mesma senha que o cofre guarda), o que deixava
    duas cópias em disco. Agora ele não grava NADA: mostra as contas como o
    registro novo (core/email.py) as vê e manda editar na tela 📧 E-MAIL, que
    escreve só no cofre. `values` é ignorado de propósito — nenhum campo desta
    casa chega ao disco, então nem uma senha digitada aqui vira texto puro.
    """
    rows = _load_accounts()
    if not rows:
        return False, ("nenhuma conta de e-mail configurada; use a tela "
                       "📧 E-MAIL para cadastrar (a senha de app vai para o cofre)")
    parts = [f"{str(r.get('name') or r.get('address') or '?')} "
             f"({r.get('address')})" for r in rows]
    return True, ("contas no registro novo: " + ", ".join(parts)
                  + ". Editar ou testar conexão: 📧 E-MAIL — aqui é só leitura.")


# ── entrada da ferramenta ────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    action  = str(parameters.get("action") or "").strip().lower()
    account = str(parameters.get("account") or "").strip()
    query   = str(parameters.get("query") or "").strip()
    to      = str(parameters.get("to") or "").strip()
    subject = str(parameters.get("subject") or "").strip()
    body    = str(parameters.get("body") or "").strip()
    try:
        limit = int(parameters.get("limit") or 6)
    except Exception:
        limit = 6

    try:
        amb = _ambiguous(account)
        if amb and action not in ("status", "accounts", ""):
            spoken, panel = amb, "EMAIL — conta ambígua"
        elif action in ("accounts", "contas"):
            names = _account_names()
            spoken = (f"contas de email: {', '.join(names)}" if names
                      else "nenhuma conta de email configurada")
            panel = f"EMAIL — contas\n  {spoken}"
        elif action in ("unread", "novos", "nao_lidos", "não_lidos", "inbox"):
            spoken, panel = unread(limit, account=account)
        elif action in ("search", "buscar", "procurar"):
            spoken, panel = search(query, limit, account=account)
        elif action in ("read", "ler", "abrir", "leia"):
            spoken, panel = read(query, account=account)
        elif action in ("send", "enviar", "mandar", "escrever"):
            spoken, panel = send(to, subject, body, account=account)
        elif action in ("status", "estado", "test"):
            spoken, panel = status(account=account)
        else:
            spoken, panel = unread(limit, account=account)
    except Exception as e:
        spoken = (_login_error(e) if isinstance(e, _NotConfigured)
                  else f"não consegui falar com o servidor de email: {_login_error(e)}")
        panel = f"EMAIL — erro\n  {spoken}"

    if player:
        try:
            player.show_content("EMAIL", panel)
            player.write_log(f"{getattr(player, '_asst_name', 'JARVIS')}: {spoken[:400]}")
        except Exception:
            pass
    return spoken


PLUGIN_SETTINGS = {
    "namespace": NS,
    "title": "EMAIL — cadastro agora fica na tela 📧 E-MAIL (aqui é só leitura)",
    # Sem campos de propósito: este formulário gravava `app_password` no
    # namespace antigo. Nada mais aqui escreve no disco (a tela 📧 E-MAIL
    # edita as contas e guarda a senha de app no cofre).
    "fields": [],
    "action": {"label": "VER CONTAS (SOMENTE LEITURA)", "run": _test},
}
