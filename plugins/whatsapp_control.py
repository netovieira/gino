"""WhatsApp por voz: ler o que chegou, enviar, parear o aparelho.

Este plugin é a boca e os olhos; quem conversa com o WhatsApp é
core/whatsapp.py (neonize). O pareamento é um QR só, na primeira vez; depois a
sessão fica em config/whatsapp_web/ e o assistente reconecta sozinho.

MULTI-CONTA: o usuário pode ter mais de um número pareado (pessoal, trabalho…).
Cada ação aceita `account` (nome da conta). Sem `account` e com várias contas,
respondemos pedindo qual — nunca escolhemos sozinhos pra não mandar a mensagem
pela conta errada.

PARIDADE DE MECANISMOS (ordem do dono, 07/11/2026 20:4x, literal: "os fluxos
tem que usar os mesmos mecanismos que você, se um gate pode você pode […]
certifique-se que todas as funções são acessíveis por você em qualquer momento
por voz ou texto"). O app responde em áudio quando a entrada dele é voz; por
isso `send_voice` existe e chama exatamente as duas funções internas que o
turno de resposta já chamava — `core.voice_note.synthesize` (síntese) e
`core.whatsapp.send_media` (entrega) — sem uma linha duplicada de TTS ou de
envio. O portão também é um só: a rota de voz sai pelo MESMO
`core/actiongate.py` da rota de texto, porque as duas são a mesma ferramenta
no mesmo tier (core/tool_bridge.py: "whatsapp_control": TIER_MEDIUM) e o gate
não olha a action.

E o silêncio que disfarçava: action desconhecida ou argumento faltando NÃO cai
mais na listagem de recentes. Devolve erro com a lista das actions válidas —
listar conversas no lugar de um envio pedido é o que fez o assistente achar
que `send_audio` tinha funcionado.

    "tem mensagem nova no zap?"              → action: recent
    "mande um zap pro João que chego já"     → action: send
    "manda uma nota de voz pro João"         → action: send_voice
    "manda pelo trabalho"                    → action: send, account: trabalho
    "manda a foto do boleto"                 → action: send_file
    "o que a Maria falou ontem?"             → action: search
    "pareie o WhatsApp"                      → action: pair  (QR na tela)
"""
from __future__ import annotations

import difflib
import os
import re
import time
import unicodedata

from core import voice_note
from core import whatsapp
from memory.config_manager import get_plugin_setting, save_plugin_config

NS = "whatsapp"


def _accounts() -> list[dict]:
    try:
        return whatsapp.list_accounts()
    except Exception:
        return []


# ── destino por rótulo ───────────────────────────────────────────────────────
#
# O usuário fala "manda pra mim", "manda pra conta principal" e espera que o
# próprio número dele seja o destino. Esse número mora em `owner_chat`; os
# nomes das contas (accounts[].name) também valem como destino (o número da
# própria conta). Nunca aproximamos nomes: só rótulo exato, senão um contato
# parecido receberia mensagem que não era para ele.

_OWNER_LABELS = frozenset({
    "mim", "pra mim", "para mim", "eu", "me", "eu mesmo", "mim mesmo",
    "minha conta", "conta principal", "minha conta principal",
    "principal", "meu numero", "numero principal", "meu numero principal",
    "meu whatsapp", "whatsapp principal", "meu zap", "zap principal",
    "meu celular", "meu telefone", "conta do dono", "numero do dono", "dono",
})


def _fold(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return " ".join(text.split())


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _owner_chat() -> str:
    try:
        return str(get_plugin_setting(NS, "owner_chat", "") or "").strip()
    except Exception:
        return ""


def _owner_missing() -> str:
    return ("o seu número ainda não está registrado no WhatsApp do Gino: "
            "preencha 'Seu número/JID' (owner_chat) em "
            "⚙ → PLUGIN SETTINGS → WHATSAPP, com DDD e 55. "
            "Sem isso eu não sei para quem mandar.")


def _account_variants(acct: dict) -> set[str]:
    name = _fold(acct.get("name"))
    values = {name, _fold(acct.get("uuid"))}
    values.update(_fold(f"{prefix} {name}")
                  for prefix in ("conta", "minha conta", "whatsapp", "zap",
                                 "numero"))
    values.discard("")
    return values


def _account_is_owner(acct: dict) -> bool:
    owner = _digits(_owner_chat())
    return bool(owner) and _digits(acct.get("number")) == owner


def is_owner_label(value: object) -> bool:
    """O rótulo aponta para o dono (mim/eu/principal/conta do dono)?"""
    key = _fold(value)
    if not key:
        return False
    if key in _OWNER_LABELS:
        return True
    for acct in _accounts():
        if _account_is_owner(acct) and key in _account_variants(acct):
            return True
        # A conta histórica "principal" é o número do dono, mesmo offline.
        if _fold(acct.get("name")) == "principal" \
                and key in ("principal", "conta principal"):
            return True
    return False


def _account_label(value: object) -> dict | None:
    key = _fold(value)
    if not key:
        return None
    for acct in _accounts():
        if key in _account_variants(acct):
            return acct
    return None


def _resolve_recipient(to: object) -> tuple[str, str]:
    """Rótulo → número. Devolve (destinatário, erro); erro vazio = pode enviar.

    Contato comum volta como veio, para o core/whatsapp.py resolver na agenda.
    """
    raw = str(to or "").strip()
    if not raw:
        return "", "falta o destinatário"
    if is_owner_label(raw):
        owner = _owner_chat()
        if not owner:
            return "", _owner_missing()
        return owner, ""
    acct = _account_label(raw)
    if acct is not None:
        number = str(acct.get("number") or "").strip()
        if not number:
            return "", (f"a conta '{acct.get('name')}' não está conectada "
                        "agora, então eu não sei o número dela")
        return number, ""
    return raw, ""


def _missing_destination() -> str:
    if _owner_chat():
        return ("para quem? diga o nome do contato, o número, ou 'mim' "
                "para o seu próprio número")
    return ("para quem? diga o nome do contato ou o número — e o seu próprio "
            "número ainda não está registrado: preencha 'owner_chat' em "
            "⚙ → PLUGIN SETTINGS → WHATSAPP.")


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
    # A conta de saída escolhida no check é aplicada sem perguntar. Perguntar
    # só quando o usuário tem várias e nenhuma foi definida.
    try:
        if whatsapp.default_account_name():
            return None
    except Exception:
        pass
    return ("você tem mais de um WhatsApp pareado: " + ", ".join(names)
            + ". Qual eu uso?")


def _unknown_account(account: str) -> str | None:
    """Frase de erro quando a conta nomeada não existe; None se dá para usar.

    É o lado do argumento da regra do dono: nem action errada nem conta errada
    podem virar listagem de conversas. Hoje, com uma conta digitada errado, o
    `account` não bate em `_ambiguous` e a resposta era "qual eu uso?" — uma
    pergunta sobre uma conta que o usuário acabou de nomear. Sem nenhuma conta
    na lista (WhatsApp ainda não pareado, ou `_accounts()` indisponível) não
    inventamos recusa: quem tenta é o core, e ele diz que falta parear.
    """
    raw = str(account or "").strip()
    if not raw:
        return None
    accts = _accounts()
    if not accts:
        return None
    names = [str(a.get("name") or "") for a in accts]
    if any(_fold(n) == _fold(raw) for n in names):
        return None
    if _account_label(raw) is not None:
        return None
    return (f"não conheço a conta de WhatsApp '{raw}': nada foi enviado. "
            "Contas pareadas: " + ", ".join(n for n in names if n) + ".")


# ── o catálogo fechado de actions ────────────────────────────────────────────
#
# A lista é fechada de propósito (ordem do dono, 07/11/2026): antes, QUALQUER
# string que não batesse caía no `else` e rodava `recent`. O assistente pedia
# `send_audio`, recebia uma lista de conversas e não tinha como saber que o
# envio não aconteceu. Agora action desconhecida ou ausente devolve erro com as
# actions válidas, e só `recent` (e seus apelidos de leitura) lista mensagens.
_CANONICAL_ACTIONS: dict[str, tuple[str, ...]] = {
    "recent": ("recent", "inbox", "ler", "ler_mensagens", "mensagens", "novas",
               "chegaram", "ver_mensagens"),
    "search": ("search", "buscar", "procurar", "procurar_mensagem"),
    "send": ("send", "enviar", "manda", "mandar", "enviar_mensagem",
             "mandar_mensagem"),
    # Nota de voz: o texto é falado pelo motor de voz do app. Os apelidos que
    # em 07/11/2026 caíam no `recent` ("send_audio", "audio", "voice") agora
    # fazem o que o dono pediu.
    "send_voice": ("send_voice", "voice", "voice_note", "send_audio", "audio",
                   "nota_de_voz", "enviar_audio", "mandar_audio",
                   "enviar_nota_de_voz", "mandar_nota_de_voz", "falar", "ptt"),
    # Arquivo que já existe (foto, vídeo, documento, áudio de arquivo): o tipo
    # é escolhido em um ponto só, `core.whatsapp._send_any`, pela extensão.
    "send_file": ("send_file", "send_media", "media", "enviar_arquivo",
                  "mandar_arquivo", "enviar_midia", "enviar_media",
                  "mandar_midia", "enviar_foto", "mandar_foto", "enviar_video",
                  "mandar_video", "enviar_documento", "mandar_documento"),
    "open_media": ("open_media", "abrir_midia", "abrir_arquivo", "open_file"),
    "open": ("open", "open_app", "open_window", "abrir", "abrir_app",
             "abrir_janela", "lancar", "launch", "desktop"),
    "status": ("status", "estado", "conectado"),
    "accounts": ("accounts", "contas", "listar_contas", "quais_contas"),
    "pair": ("pair", "parear", "connect", "conectar", "qr"),
    "contacts": ("contacts", "contatos", "agenda"),
}

#: Todo apelido aceito → nome canônico. `run()` só conhece quem está aqui.
ACTION_ALIASES: dict[str, str] = {alias: canonical
                                  for canonical, aliases in
                                  _CANONICAL_ACTIONS.items()
                                  for alias in aliases}

#: Os nomes que o erro de action desconhecida devolve ao modelo.
VALID_ACTIONS: tuple[str, ...] = tuple(_CANONICAL_ACTIONS)

#: Actions que precisam de uma conta pareada existente para fazer sentido.
_ACCOUNT_ACTIONS = frozenset({"recent", "search", "send", "send_voice",
                              "send_file", "open_media", "contacts"})


def _norm_action(value: object) -> str:
    """Normaliza a action vinda do modelo sem perder o sublinhado.

    `_fold` não serve aqui: ele troca tudo que não é letra/dígito por espaço,
    e "send_file" viraria "send file". Caixa, acento, hífen e espaço viram o
    que o catálogo usa; o que não bater em `ACTION_ALIASES` é action
    desconhecida — nunca mais "recent" por defeito de escrita.
    """
    raw = unicodedata.normalize("NFKD", str(value or ""))
    raw = "".join(c for c in raw if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")


def _unknown_action(action: str) -> tuple[str, str]:
    """(spoken, body) — recusa honesta, sem listagem de mensagens.

    Nomear a action pedida e as MAIS PRÓXIMAS é o que fecha o ciclo para quem
    chamou por voz/texto: o erro tem que dizer o que existe, não só "deu
    errado". A lista completa vem atrás, para o caso de não haver vizinho.
    """
    valid = ", ".join(VALID_ACTIONS)
    if not str(action or "").strip():
        return ("falta a action do WhatsApp: nada foi feito. Actions válidas: "
                f"{valid} — para ler o que chegou use 'recent'.",
                "falta a action")
    near = difflib.get_close_matches(_norm_action(action), VALID_ACTIONS, n=3)
    hint = f" As actions mais próximas de '{action}' são: {', '.join(near)}." \
        if near else ""
    return (f"não conheço a action '{action}' do WhatsApp: nada foi enviado "
            "e eu não vou listar mensagens no lugar disso."
            + hint + f" Actions válidas: {valid}.",
            f"action desconhecida: {action}")


PLUGIN = {
    "name": "whatsapp_control",
    "description": (
        "The user's OWN WhatsApp account(s), connected through their phone "
        "number(s): read the messages that arrived, search old ones, send a "
        "text message, send a VOICE NOTE (action=send_voice — the assistant's "
        "own speech synthesis, delivered as a WhatsApp voice message), send an "
        "existing file (photo / video / audio file / document), open a file "
        "someone sent, open the WhatsApp DESKTOP APPLICATION WINDOW "
        "(action=open), check the connection, list the paired accounts, or pair "
        "a phone with a QR code. Use this EVERY time the user mentions WhatsApp "
        "/ zap / 'mensagem no whatsapp' — including to send. Anything the app "
        "can do on WhatsApp is reachable through this tool: when the owner "
        "asks for a voice note use send_voice — do not fall back to text and "
        "do not claim audio is unavailable. The user may have "
        "SEVERAL accounts (e.g. pessoal / trabalho): when they name one "
        "('manda pelo trabalho'), pass account=<name>; when they don't and "
        "more than one is paired, the tool asks which — relay that question, do "
        "not guess. IMPORTANT: when the user asks to OPEN / SHOW / LAUNCH the "
        "WhatsApp app itself ('abre o WhatsApp', 'abre a janela do zap') they "
        "want the WINDOW on screen, not a message summary — call action=open "
        "exactly ONCE and do NOT call recent. An action this tool does not "
        "know comes back as an explicit error listing the valid actions; it "
        "never returns the inbox. Do NOT use send_message for "
        "WhatsApp: that tool is the old keystroke path and it cannot read "
        "anything back."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "recent (what arrived) | search (find in what "
                               "arrived) | send (text message) | send_voice "
                               "(VOICE NOTE: the text you pass is spoken by "
                               "the assistant's own voice engine and delivered "
                               "as audio) | send_file (an existing file: "
                               "photo, video, audio file, document — the type "
                               "is taken from the file itself) | open_media "
                               "(open a photo/audio someone sent) | open "
                               "(launch the WhatsApp desktop app window — use "
                               "this for 'abre o WhatsApp', 'abrir a janela "
                               "do zap', 'open WhatsApp') | status (is it "
                               "connected) | accounts (list the paired "
                               "accounts) | pair (first-time QR) | contacts "
                               "(who is in the address book). Any other value "
                               "is refused with this list — never silently "
                               "read as recent.",
            },
            "account": {
                "type": "STRING",
                "description": "Which WhatsApp account to use when the user has "
                               "more than one (e.g. 'pessoal', 'trabalho'). Omit "
                               "when they have one, or when they didn't say — "
                               "the tool will ask. A name that is not a paired "
                               "account is refused, never guessed.",
            },
            "to": {
                "type": "STRING",
                "description": "send/send_voice/send_file: the contact name as "
                               "the user said it, or the phone number with "
                               "country code. "
                               "When the user says 'manda pra mim', 'pra mim "
                               "mesmo' or 'pra conta principal', pass 'mim' — "
                               "the tool resolves it to the owner's own number "
                               "from owner_chat. An account label (e.g. "
                               "'trabalho') resolves to that account's number.",
            },
            "text": {
                "type": "STRING",
                "description": "send: the text to send in the user's own "
                               "language. It goes out SIGNED with the speaker's "
                               "name ('*Gino:*' on the first line), because it "
                               "is your message. send_voice: the words to be "
                               "SPOKEN (up to 1200 characters) — a voice note "
                               "is never signed, because the voice itself says "
                               "who speaks. Never invent an emotional or "
                               "personal message on the user's behalf — if the "
                               "user is dictating words to send AS THEMSELVES "
                               "(e.g. a love message to their partner), pass "
                               "dictated=true so it goes out without any "
                               "signature.",
            },
            "dictated": {
                "type": "BOOLEAN",
                "description": "send: true ONLY when the user dictated the "
                               "exact words to be sent as the user themselves "
                               "(personal/family message). Then the text is NOT "
                               "signed with the assistant's name — it is the "
                               "user's voice. Default false: your own text, "
                               "signed with the current speaker name. Ignored "
                               "by send_voice: audio is never signed.",
            },
            "path": {
                "type": "STRING",
                "description": "send_file: the full path of the file (or the "
                               "name of a file that already arrived here, e.g. "
                               "'foto da Maria'). Never guess a path. The type "
                               "that goes out — photo, video, audio (an .ogg / "
                               ".opus leaves as a voice note), document — is "
                               "decided by core.whatsapp on the file itself.",
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


def _flag(value: object) -> bool:
    """Booleano de parâmetro de ferramenta: aceita True e as strings óbvias."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "sim", "yes", "on"}


def _do_send(to: str, text: str, account: str = "",
             dictated: bool = False) -> tuple[str, str]:
    """Rota de TEXTO. É também a referência do portão: `send_voice` e
    `send_file` saem pelo mesmo `core/actiongate.py` que esta sai, porque as
    três são a mesma ferramenta no mesmo tier (core/tool_bridge.py) — o portão
    nunca olha a action, e é assim que tem que ser (ordem do dono, 07/11/2026).
    """
    if not to:
        return _missing_destination(), "falta o destinatário"
    if not text:
        return ("o que eu mando? a mensagem está vazia", "falta a mensagem")
    target, why = _resolve_recipient(to)
    if why:
        return why, why
    amb = _ambiguous(account)
    if amb:
        return amb, "conta ambígua"
    # `signed=` do core é o ponto único: texto que EU produzi leva o nome do
    # alto-falante atual; texto ditado pelo dono para outra pessoa é dele e sai
    # sem assinatura (assinar seria forjar a fala dele).
    ok, msg = whatsapp.send(target, text, account=account or None,
                            signed=not dictated)
    if not ok:
        return f"falhou: {msg}", msg
    return msg, msg


def _do_send_voice(to: str, text: str, account: str = "") -> tuple[str, str]:
    """NOTA DE VOZ: o mesmo mecanismo do turno de resposta, agora a pedido.

    Ordem do dono (07/11/2026): o app entregava áudio quando a entrada dele era
    voz — a ferramenta não tinha como pedir isso. Aqui não há nem uma linha de
    TTS ou de envio reimplementada: `core.voice_note.synthesize` é a função que
    `main.JarvisLive._synthesize_whatsapp_audio` (o turno de resposta) chama, e
    `core.whatsapp.send_media` é a função que `_send_whatsapp_answer` chama. A
    duração da nota (`seconds` da AudioMessage) é calculada na entrega, pelo
    próprio neonize, a partir do Ogg Opus 48 kHz mono canônico que a síntese
    produziu — esta rota não estima nada.

    Sem áudio não existe plano B silencioso: se o motor de voz ou o ffmpeg
    falharem, nada é enviado e o motivo é dito. Trocar para texto por conta
    própria seria exatamente o que o dono mandou acabar.
    """
    if not to:
        return _missing_destination(), "falta o destinatário"
    if not text:
        return ("o que eu falo? a nota de voz está sem texto", "falta o texto")
    target, why = _resolve_recipient(to)
    if why:
        return why, why
    amb = _ambiguous(account)
    if amb:
        return amb, "conta ambígua"
    path, _spoken = voice_note.synthesize(text)
    if not path:
        return ("não consegui gerar o áudio agora (motor de voz ou ffmpeg "
                "indisponíveis): NADA foi enviado. Quer que eu mande o mesmo "
                "texto como mensagem escrita?", "sem áudio")
    try:
        ok, msg = whatsapp.send_media(target, path, "", account=account or None)
    except Exception as e:
        ok, msg = False, str(e)
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
    if not ok:
        return f"falhou: {msg}", msg
    return msg, msg


def _do_send_file(to: str, path: str, caption: str, account: str = "") -> tuple[str, str]:
    """Mídia que JÁ EXISTE (foto, vídeo, documento, áudio de arquivo).

    O tipo certo não é decidido aqui: `core.whatsapp._send_any` escolhe pelo
    arquivo e, para .ogg/.opus, monta o AudioMessage como PTT com mimetype e
    waveform canônicos — a mesma entrega que a nota de voz sintetizada usa.
    """
    if not to:
        return _missing_destination(), "falta o destinatário"
    if not path:
        return ("qual arquivo? diga o caminho ou o nome dele", "falta o arquivo")
    target, why = _resolve_recipient(to)
    if why:
        return why, why
    amb = _ambiguous(account)
    if amb:
        return amb, "conta ambígua"
    ok, msg = whatsapp.send_media(target, path, caption, account=account or None)
    if not ok:
        return f"falhou: {msg}", msg
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
    """A ferramenta. Action errada ou argumento faltando é ERRO, nunca inbox.

    O `else` que existia aqui mandava qualquer action desconhecida para
    `recent`: chamadas como `send_audio`, `voice` ou `msg_type` devolviam a
    lista das últimas conversas, e isso era lido como se o envio tivesse
    acontecido (07/11/2026). Desde a ordem do dono sobre paridade,
    o catálogo é fechado (`ACTION_ALIASES`) e o que não está nele responde com
    as actions válidas — sem painel, sem log de "envio", sem mensagem fantasma.
    """
    action  = _norm_action(parameters.get("action"))
    account = str(parameters.get("account") or "").strip()
    to      = str(parameters.get("to") or "").strip()
    text    = str(parameters.get("text") or "").strip()
    query   = str(parameters.get("query") or "").strip()
    path    = str(parameters.get("path") or "").strip()
    caption = str(parameters.get("caption") or "").strip()
    dictated = _flag(parameters.get("dictated"))
    try:
        limit = int(parameters.get("limit") or 8)
    except Exception:
        limit = 8

    canonical = ACTION_ALIASES.get(action)
    if canonical is None:
        spoken, _body = _unknown_action(str(parameters.get("action") or ""))
        return spoken
    if canonical in _ACCOUNT_ACTIONS:
        wrong_account = _unknown_account(account)
        if wrong_account:
            return wrong_account

    try:
        if canonical == "status":
            spoken, body = _do_status(account)
            title = "WHATSAPP — estado"
        elif canonical == "accounts":
            spoken, body = _do_accounts()
            title = "WHATSAPP — contas"
        elif canonical == "pair":
            spoken, body = _do_pair(account)
            title = "WHATSAPP — pareamento"
        elif canonical == "send_file":
            spoken, body = _do_send_file(to, path or query or text, caption, account)
            title = "WHATSAPP — envio de arquivo"
        elif canonical == "send_voice":
            spoken, body = _do_send_voice(to, text, account)
            title = "WHATSAPP — nota de voz"
        elif canonical == "open_media":
            spoken, body = _do_open_media(to, query or path, account)
            title = "WHATSAPP — abrir mídia"
        elif canonical == "open":
            spoken, body = _do_open_window()
            title = "WHATSAPP — abrir aplicativo"
        elif canonical == "send":
            spoken, body = _do_send(to, text, account, dictated)
            title = "WHATSAPP — envio"
        elif canonical == "search":
            spoken, body = _do_search(query, limit, account)
            title = "WHATSAPP — busca"
        elif canonical == "contacts":
            spoken, body = _do_contacts(limit, account)
            title = "WHATSAPP — contatos"
        else:  # recent / inbox / ler — só quando foi isso que se pediu
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
        {"key": "default_account",
         "label": "Conta de saída (respostas ao dono, lembretes e envios sem conta)",
         "type": "choice", "options": _account_choices(), "default": "principal"},
        {"key": "owner_chat",
         "label": "Seu número/JID no WhatsApp do Gino (chat direto)",
         "type": "text", "default": "",
         "placeholder": "ex.: 5511999999999"},
        {"key": "owner_notify",
         "label": "Mandar lembretes também pelo WhatsApp",
         "type": "toggle", "default": True},
        {"key": "media_download", "label": "Baixar a mídia que chegar",
         "type": "toggle", "default": True},
    ],
    "action": {"label": "CONECTAR / PAREAR (gera o QR)", "run": _connect_action},
}
