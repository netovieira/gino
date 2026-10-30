"""WhatsApp por voz: ler o que chegou, enviar, parear o aparelho.

Este plugin é a boca e os olhos; quem conversa com o WhatsApp é
core/whatsapp.py (neonize). O pareamento é um QR só, na primeira vez; depois a
sessão fica em config/whatsapp_web/ e o assistente reconecta sozinho.

MULTI-CONTA: o usuário pode ter mais de um número pareado (pessoal, trabalho…).
Cada ação aceita `account` (nome da conta). Sem `account` e com várias contas,
respondemos pedindo qual — nunca escolhemos sozinhos pra não mandar a mensagem
pela conta errada.

    "tem mensagem nova no zap?"              → action: recent
    "mande um zap pro João que chego já"     → action: send
    "manda pelo trabalho"                    → action: send, account: trabalho
    "o que a Maria falou ontem?"             → action: search
    "pareie o WhatsApp"                      → action: pair  (QR na tela)
"""
from __future__ import annotations

import time

from core import whatsapp
from memory.config_manager import get_plugin_setting, save_plugin_config

NS = "whatsapp"


def _accounts() -> list[dict]:
    try:
        return whatsapp.list_accounts()
    except Exception:
        return []


def _ambiguous(account: str) -> str | None:
    """None se dá para agir; senão, a frase pedindo qual conta usar.

    Uma conta só → sempre age nela. Várias e o usuário nomeou uma que existe →
    age nela. Várias e não nomeou → pergunta, porque escolher a conta de envio
    por conta própria é o tipo de erro que manda assunto pessoal pro trabalho.
    """
    accts = _accounts()
    if len(accts) <= 1:
        return None
    names = [a["name"] for a in accts]
    if account and account.lower() in [n.lower() for n in names]:
        return None
    return ("você tem mais de um WhatsApp pareado: " + ", ".join(names)
            + ". Qual eu uso?")


PLUGIN = {
    "name": "whatsapp_control",
    "description": (
        "The user's OWN WhatsApp account(s), connected through their phone "
        "number(s): read the messages that arrived, search old ones, send a "
        "message, send a file (photo / video / audio / document), open a file "
        "someone sent, open the WhatsApp DESKTOP APPLICATION WINDOW "
        "(action=open), check the connection, list the paired accounts, or pair "
        "a phone with a QR code. Use this EVERY time the user mentions WhatsApp "
        "/ zap / 'mensagem no whatsapp' — including to send. The user may have "
        "SEVERAL accounts (e.g. pessoal / trabalho): when they name one "
        "('manda pelo trabalho'), pass account=<name>; when they don't and "
        "more than one is paired, the tool asks which — relay that question, do "
        "not guess. IMPORTANT: when the user asks to OPEN / SHOW / LAUNCH the "
        "WhatsApp app itself ('abre o WhatsApp', 'abre a janela do zap') they "
        "want the WINDOW on screen, not a message summary — call action=open "
        "exactly ONCE and do NOT call recent. Do NOT use send_message for "
        "WhatsApp: that tool is the old keystroke path and it cannot read "
        "anything back."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "recent (what arrived) | search (find in what "
                               "arrived) | send (text message) | send_file "
                               "(photo, video, audio, document) | open_media "
                               "(open a photo/audio someone sent) | open "
                               "(launch the WhatsApp desktop app window — use "
                               "this for 'abre o WhatsApp', 'abrir a janela "
                               "do zap', 'open WhatsApp') | status (is it "
                               "connected) | accounts (list the paired "
                               "accounts) | pair (first-time QR) | contacts "
                               "(who is in the address book)",
            },
            "account": {
                "type": "STRING",
                "description": "Which WhatsApp account to use when the user has "
                               "more than one (e.g. 'pessoal', 'trabalho'). Omit "
                               "when they have one, or when they didn't say — "
                               "the tool will ask.",
            },
            "to": {
                "type": "STRING",
                "description": "send/send_file: the contact name as the user "
                               "said it, or the phone number with country code",
            },
            "text": {
                "type": "STRING",
                "description": "send: exactly what the user wants said, in their "
                               "own language. Never invent or embellish it.",
            },
            "path": {
                "type": "STRING",
                "description": "send_file: the full path of the file (or the "
                               "name of a file that already arrived here, e.g. "
                               "'foto da Maria'). Never guess a path.",
            },
            "caption": {
                "type": "STRING",
                "description": "send_file: optional caption, in the user's own "
                               "words. Leave empty if they said nothing else.",
            },
            "query": {
                "type": "STRING",
                "description": "search: a word from the message or the contact "
                               "name (e.g. 'boleto', 'Maria')",
            },
            "limit": {
                "type": "INTEGER",
                "description": "recent/search: how many messages (default 8)",
            },
        },
        "required": ["action"],
    },
}


# ── formatação falada ────────────────────────────────────────────────────────

def _when(ts: int) -> str:
    try:
        t = time.localtime(int(ts))
        today = time.strftime("%d/%m/%Y", time.localtime())
        day = time.strftime("%d/%m/%Y", t)
        return time.strftime("%H:%M", t) if day == today else time.strftime("%d/%m %H:%M", t)
    except Exception:
        return "?"


def _lines(items: list[dict], empty: str) -> str:
    if not items:
        return empty
    out = []
    for m in items:
        who = m.get("name") or m.get("chat") or "?"
        mark = "eu" if m.get("from_me") else who
        out.append(f"{_when(m.get('ts', 0))} — {mark}: {m.get('text', '')}")
    return "\n".join(out)


def _show(player, title: str, body: str) -> None:
    if player:
        try:
            player.show_content(title, body)
        except Exception:
            pass


# ── as ações ─────────────────────────────────────────────────────────────────

def _do_status(account: str = "") -> tuple[str, str]:
    line = whatsapp.status_line(account or None)
    st = whatsapp.status(account or None)
    if st.get("qr_path"):
        line += f" (QR: {st['qr_path']})"
    if st.get("connected"):
        line += (" — foto, vídeo, áudio e documento liberados" if st.get("ffmpeg")
                 else " — sem ffmpeg: foto e documento sim, áudio/vídeo não")
    return line, line


def _do_accounts() -> tuple[str, str]:
    accts = _accounts()
    if not accts:
        return ("nenhum WhatsApp pareado ainda — peça 'parear o WhatsApp'",
                "sem contas")
    lines = []
    for a in accts:
        estado = ("conectado" if a["connected"] else
                  "pareado, offline" if a["paired"] else "não pareado")
        mark = " (default)" if a["default"] else ""
        lines.append(f"{a['name']}{mark}: {estado}"
                     + (f" — {a['number']}" if a.get("number") else ""))
    body = "\n".join(lines)
    return (f"{len(accts)} conta(s) de WhatsApp: " + "; ".join(
        a["name"] for a in accts), body)


def _do_pair(account: str = "") -> tuple[str, str]:
    """Pareia (QR na tela). Com um `account` novo, CRIA a conta antes — é assim
    que se liga um segundo número: 'pareie o WhatsApp trabalho'."""
    if account:
        existing = [a["name"].lower() for a in _accounts()]
        if account.lower() not in existing:
            try:
                whatsapp.add_account(account)
            except Exception as e:
                return (f"não consegui criar a conta '{account}': {e}", str(e))
    elif len(_accounts()) > 1:
        return (_ambiguous(account) or "", "ambíguo")
    ok, msg = whatsapp.start(on_message=_notify_new_message,
                             account=(account or None))
    if ok:
        spoken = ("escaneie o QR que abri na tela com o WhatsApp do seu celular: "
                  "Aparelhos conectados → Conectar um aparelho") if "QR" in msg \
                 else msg
        return spoken, spoken
    return f"não consegui conectar o WhatsApp: {msg}", f"não consegui conectar: {msg}"


def _do_send(to: str, text: str, account: str = "") -> tuple[str, str]:
    if not to:
        return ("para quem? diga o nome do contato ou o número", "falta o destinatário")
    if not text:
        return ("o que eu mando? a mensagem está vazia", "falta a mensagem")
    amb = _ambiguous(account)
    if amb:
        return amb, "conta ambígua"
    ok, msg = whatsapp.send(to, text, account=account or None)
    return msg, msg


def _do_send_file(to: str, path: str, caption: str, account: str = "") -> tuple[str, str]:
    if not to:
        return ("para quem? diga o nome do contato ou o número", "falta o destinatário")
    if not path:
        return ("qual arquivo? diga o caminho ou o nome dele", "falta o arquivo")
    amb = _ambiguous(account)
    if amb:
        return amb, "conta ambígua"
    ok, msg = whatsapp.send_media(to, path, caption, account=account or None)
    return msg, msg


def _do_open_media(to: str, hint: str, account: str = "") -> tuple[str, str]:
    _ok, msg = whatsapp.open_media(to, hint, account=account or None)
    return msg, msg


def _do_open_window() -> tuple[str, str]:
    """Abre a JANELA do WhatsApp Desktop instalado — não lê mensagem nenhuma.

    O usuário pede “abre ozap”, “abre a janela do WhatsApp” etc. e quer ver
    o app na tela, não um resumo do inbox. Aqui o plugin delega ao open_app
    (que conhece o snap `whatsapp-desktop-linux`, o atalho .desktop, etc.)
    para que, mesmo que o modelo tenha caído nesta ferramenta, a ação certa
    aconteça em vez de virar um `recent` silencioso.
    """
    try:
        from actions.open_app import open_app as _oa
    except Exception as e:
        return (f"não consegui abrir o WhatsApp no desktop ({e})",
                f"open_app indisponível: {e}")
    msg = _oa({"app_name": "WhatsApp"}) or ""
    ok = msg.lower().startswith("opened")
    spoken = ("abri a janela do WhatsApp aqui no computador."
              if ok else f"não consegui abrir a janela do WhatsApp: {msg}")
    return spoken, msg


def _do_recent(limit: int, account: str = "") -> tuple[str, str]:
    amb = _ambiguous(account)
    if amb:
        return amb, "conta ambígua"
    items = whatsapp.recent(limit, account=account or None)
    label = f" do {account}" if account and len(_accounts()) > 1 else ""
    empty = f"nenhuma mensagem nova no WhatsApp{label}"
    body = _lines(items, empty)
    spoken = body if items else empty
    return spoken, body


def _do_search(query: str, limit: int, account: str = "") -> tuple[str, str]:
    if not query:
        return ("o que eu procuro? diga uma palavra da mensagem", "falta a busca")
    amb = _ambiguous(account)
    if amb:
        return amb, "conta ambígua"
    items = whatsapp.search(query, limit, account=account or None)
    body = _lines(items, f"não achei nada com '{query}' no WhatsApp")
    return body, body


def _do_contacts(limit: int, account: str = "") -> tuple[str, str]:
    amb = _ambiguous(account)
    if amb:
        return amb, "conta ambígua"
    names = whatsapp.contact_names(limit, account=account or None)
    if not names:
        return ("a agenda do WhatsApp ainda não carregou — "
                "isso acontece depois do primeiro pareamento", "agenda vazia")
    body = ", ".join(names)
    return body, body


# ── o aviso de mensagem nova (opcional, off por padrão) ─────────────────────

def _notify_wanted(account_name: str = "") -> bool:
    """Avisar em voz? A flag é por conta; sem conta nomeada, usa a default."""
    try:
        for a in _accounts():
            if (not account_name and a.get("default")) or \
               a["name"].lower() == account_name.lower():
                return bool(a.get("notify"))
        return bool(get_plugin_setting(NS, "notify", False))
    except Exception:
        return False


def _what_arrived(item: dict) -> str:
    """O que dizer quando a mensagem é um anexo sem texto."""
    text = str(item.get("text") or "")
    if text.startswith("[") and "]" in text:
        return text
    return f"\"{text[:200]}\""


def _notify_new_message(item: dict) -> None:
    """Chamado do thread do WhatsApp quando chega mensagem de outra pessoa."""
    if not _notify_wanted() or item.get("group"):
        return
    who = item.get("name") or item.get("chat") or "alguém"
    print(f"[WhatsApp] nova mensagem de {who}: {_what_arrived(item)[:200]}")


def voice_sink(player):
    """Um sink que FALA a mensagem nova, para o main.py usar no boot.

    Chamado do thread do WhatsApp, longe do asyncio: request_say é o canal
    thread-safe que o main.py já publica na UI (plugin_say).
    """
    def _speak(item: dict) -> None:
        if not _notify_wanted() or item.get("group"):
            return
        who = item.get("name") or item.get("chat") or "alguém"
        say = getattr(player, "request_say", None)
        if callable(say):
            say(f"[WHATSAPP] A new WhatsApp message just arrived from {who}: "
                f"{_what_arrived(item)}. Tell the user in one short sentence, "
                f"in their language. Call no tools.")
    return _speak


# ── o teste do botão da UI ───────────────────────────────────────────────────

def _connect_action(values: dict | None = None) -> tuple[bool, str]:
    """Botão 'CONECTAR / PAREAR' em ⚙ → PLUGIN SETTINGS → WHATSAPP.

    Aplica o `default_account` escolhido e faz o pareamento da conta default
    (ou da nomeada). Para LIGAR UM SEGUNDO NÚMERO por voz: 'pareie o WhatsApp
    trabalho' — o action=pair cria a conta e mostra o QR dela.
    """
    vals = values or {}
    try:
        # A escolha de default vem do choice; o resto do config de contas é
        # gerido por add/remove/pair, não por este formulário.
        if vals.get("default_account"):
            whatsapp.set_default_account(str(vals["default_account"]))
    except Exception:
        pass
    if not whatsapp.available():
        return False, "falta o neonize: pip install neonize"
    accts = _accounts()
    default = next((a["name"] for a in accts if a.get("default")), None)
    ok, msg = whatsapp.start(on_message=_notify_new_message, account=default)
    st = whatsapp.status(default)
    if st.get("connected"):
        return True, f"conectado como {st['number'] or 'seu número'}"
    if st.get("qr_path"):
        return True, f"escaneie o QR: {st['qr_path']}"
    return ok, msg


def _account_choices() -> list[str]:
    try:
        return [a["name"] for a in whatsapp.list_accounts()] or ["principal"]
    except Exception:
        return ["principal"]


# ── entrada da ferramenta ────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    action  = str(parameters.get("action") or "").strip().lower()
    account = str(parameters.get("account") or "").strip()
    to      = str(parameters.get("to") or "").strip()
    text    = str(parameters.get("text") or "").strip()
    query   = str(parameters.get("query") or "").strip()
    path    = str(parameters.get("path") or "").strip()
    caption = str(parameters.get("caption") or "").strip()
    try:
        limit = int(parameters.get("limit") or 8)
    except Exception:
        limit = 8

    try:
        if action in ("status", "estado", "conectado"):
            spoken, body = _do_status(account)
            title = "WHATSAPP — estado"
        elif action in ("accounts", "contas", "listar_contas", "quais_contas"):
            spoken, body = _do_accounts()
            title = "WHATSAPP — contas"
        elif action in ("pair", "parear", "connect", "conectar", "qr"):
            spoken, body = _do_pair(account)
            title = "WHATSAPP — pareamento"
        elif action in ("send_file", "enviar_arquivo", "mandar_arquivo", "media"):
            spoken, body = _do_send_file(to, path or query or text, caption, account)
            title = "WHATSAPP — envio de arquivo"
        elif action in ("open_media", "abrir_midia", "abrir_arquivo", "open_file"):
            spoken, body = _do_open_media(to, query or path, account)
            title = "WHATSAPP — abrir mídia"
        elif action in ("open", "open_app", "open_window", "abrir", "abrir_app",
                        "abrir_janela", "lancar", "launch", "desktop"):
            spoken, body = _do_open_window()
            title = "WHATSAPP — abrir aplicativo"
        elif action in ("send", "enviar", "manda", "mandar"):
            spoken, body = _do_send(to, text, account)
            title = "WHATSAPP — envio"
        elif action in ("search", "buscar", "procurar", "procurar_mensagem"):
            spoken, body = _do_search(query, limit, account)
            title = "WHATSAPP — busca"
        elif action in ("contacts", "contatos", "agenda"):
            spoken, body = _do_contacts(limit, account)
            title = "WHATSAPP — contatos"
        else:  # recent / inbox / ler — o padrão, porque é o que mais se pede
            spoken, body = _do_recent(limit, account)
            title = "WHATSAPP — mensagens"
    except Exception as e:
        return f"o WhatsApp falhou: {e}"

    _show(player, title, body)
    if player:
        try:
            player.write_log(f"{getattr(player, '_asst_name', 'JARVIS')}: {spoken[:400]}")
        except Exception:
            pass
    return spoken


PLUGIN_SETTINGS = {
    "namespace": NS,
    "title": "WHATSAPP (seu número, via QR)",
    "fields": [
        {"key": "default_account", "label": "Conta default (a que ele usa sem você nomear)",
         "type": "choice", "options": _account_choices(), "default": "principal"},
        {"key": "media_download", "label": "Baixar a mídia que chegar",
         "type": "toggle", "default": True},
    ],
    "action": {"label": "CONECTAR / PAREAR (gera o QR)", "run": _connect_action},
}
