"""Resposta autônoma do WhatsApp quando a mensagem chama o assistente pelo nome.

O QUE É UM GATILHO
    Uma mensagem NOVA (no máximo MAX_AGE_S de idade — o HistorySync reenvia
    meses de conversa e nada disso é "nova"), em chat INDIVIDUAL (grupo nunca),
    que não é do próprio dono nem uma resposta do assistente (`from_me`), e
    cujo texto contém a palavra "Gino", sem diferenciar maiúsculas. Basta o
    primeiro "oi Gino" para o assistente responder sem pedir confirmação.

DEDUPE E LIMITE
    Cada id de mensagem aceito é lembrado uma única vez: o mesmo evento pode
    chegar por uma reconexão ou por duas rotinas de leitura, e responder duas
    vezes à mesma mensagem é o tipo de erro que o contato percebe na hora.
    No máximo RATE_LIMIT (5) respostas autônomas por contato por hora; acima
    disso a mensagem é ignorada e o motivo vai para o log — silêncio, não
    insistência.

COMO A RESPOSTA ACONTECE
    `start(handler)` registra o observador em core/whatsapp.py, que entrega
    mensagens de TODAS as contas pareadas (`callback(account_name, item)`).
    Antes de chamar o handler, este módulo estaciona em core/actiongate.py uma
    autorização presa àquela conta+contato: só o whatsapp_control action=send
    para a mesma conversa passa pelo gate de voz. Qualquer outra ação — e
    qualquer outro contato — continua esperando o "pode" do dono.

CÓDIGOS DE VERIFICAÇÃO
    Em chat individual, uma mensagem que é claramente um OTP (ver
    core/otp_detect.py) é ANUNCIADA ao dono pelo handler `on_otp` e nunca
    respondida ao remetente: código não é conversa. O anúncio tem prioridade
    sobre o gatilho pelo nome, então "Gino, seu código é 123456" não gera
    resposta ao contato. Grupo, mensagem do próprio dono e HistorySync antigo
    continuam de fora.
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections import OrderedDict

from core import actiongate, otp_detect, whatsapp

# O nome que chama o assistente na conversa. Não é fixo: vem do nome atual
# (assistant_name) e é atualizado quando o usuário renomeia o assistente.
_trigger_name = ""
_trigger_norm = ""

# Chat direto do dono na conta de saída (o "número do Gino"). Toda mensagem
# nesse par conta+chat é para o assistente; não exige o nome.
_direct_account = ""
_direct_chat = ""
_direct_chat_norm = ""

# Quantas respostas autônomas um contato pode receber por hora.
RATE_LIMIT = 5
RATE_WINDOW_S = 3600.0

# Idade máxima de uma mensagem para valer como "nova" (o sink usa o mesmo teto).
MAX_AGE_S = 300.0

# Teto de ids lembrados; o suficiente para muitas horas de conversa.
MAX_SEEN = 4096

_lock = threading.Lock()
_handler = None
_otp_handler = None
_logger = None
_seen: OrderedDict = OrderedDict()
_sent: dict[tuple[str, str], list[float]] = {}


def _fold(text: object) -> str:
    value = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in value if not unicodedata.combining(c)).strip().lower()


def _norm_words(text: object) -> str:
    """Lowercase, no accents, no punctuation, collapsed spaces."""
    value = _fold(text)
    value = re.sub(r"[^a-z0-9\s]", "", value)
    return " ".join(value.split())


def _digits(text: object) -> str:
    return re.sub(r"\D", "", str(text or ""))


def set_assistant_name(name: object) -> None:
    """The trigger follows the assistant's CURRENT name, not a fixed word."""
    global _trigger_name, _trigger_norm
    _trigger_name = str(name or "").strip()
    _trigger_norm = _norm_words(_trigger_name)


def set_direct_chat(account: object, chat: object) -> None:
    """The owner's direct chat on the assistant's outbound account."""
    global _direct_account, _direct_chat, _direct_chat_norm
    _direct_account = str(account or "").strip()
    _direct_chat = str(chat or "").strip()
    _direct_chat_norm = _norm_words(_direct_chat)


def trigger_name() -> str:
    return _trigger_name


def matches(text: object) -> bool:
    """O texto chama o assistente pelo nome atual? Palavra/frase inteira."""
    trigger = _trigger_norm
    if not trigger:
        return False
    body = _norm_words(text)
    return f" {trigger} " in f" {body} "


def _is_direct(account: str, item: dict) -> bool:
    account = str(account or "").strip()
    chat = str(item.get("chat") or "")
    if not account or not chat:
        return False
    # Read the settings live: changing the outbound account or the owner chat
    # must take effect without restarting the watcher.
    try:
        from memory.config_manager import get_plugin_setting
        selected = str(whatsapp.default_account_name() or _direct_account)
        owner_chat = str(get_plugin_setting("whatsapp", "owner_chat", "")
                         or _direct_chat)
    except Exception:
        selected, owner_chat = _direct_account, _direct_chat
    if not selected or not owner_chat:
        return False
    if account.lower() != selected.lower():
        return False
    if _norm_words(chat) == _norm_words(owner_chat):
        return True
    a, b = _digits(chat), _digits(owner_chat)
    return bool(a and b and a == b)


def _is_own_account(chat: object) -> bool:
    """A message sent by another paired number of this same assistant.

    Both accounts run inside JARVIS. A reply sent from the outbound account
    appears on the other account as an incoming contact message; it must never
    trigger the name-based auto-reply again (anti-loop).
    """
    digits = _digits(chat)
    if not digits:
        return False
    try:
        numbers = {_digits(a.get("number")) for a in whatsapp.list_accounts()}
    except Exception:
        return False
    numbers.discard("")
    return digits in numbers


def _default_log(message: str) -> None:
    try:
        print(f"[WhatsApp] {message}")
    except Exception:
        pass


def _log(message: str) -> None:
    fn = _logger or _default_log
    try:
        fn(message)
    except Exception:
        pass


def start(handler, logger=None, on_otp=None, assistant_name=None,
          direct_account="", direct_chat="") -> None:
    """Liga o monitoramento para todas as contas; `handler(account, item)`.

    O handler devolve True quando o pedido de resposta chegou ao assistente;
    False mantém o contador de rate limit intacto e revoga a autorização.
    `on_otp(payload)` recebe os códigos de verificação detectados.

    `assistant_name` é o gatilho por nome nos chats 1:1; `direct_account` +
    `direct_chat` identificam o chat direto do dono, onde toda mensagem é para
    o assistente e o nome não é exigido.
    """
    global _handler, _logger, _otp_handler
    if not callable(handler):
        raise TypeError("whatsapp_watch.start precisa de um handler callable")
    if logger is not None:
        _logger = logger
    if assistant_name is not None:
        set_assistant_name(assistant_name)
    set_direct_chat(direct_account, direct_chat)
    with _lock:
        _handler = handler
        if on_otp is not None:
            _otp_handler = on_otp
    whatsapp.add_watcher(_dispatch)


def stop() -> None:
    global _handler, _otp_handler
    with _lock:
        _handler = None
        _otp_handler = None
    whatsapp.remove_watcher(_dispatch)


def reset() -> None:
    """Esquece ids e contadores (testes e troca de conta)."""
    with _lock:
        _seen.clear()
        _sent.clear()


def _fresh(item: dict) -> bool:
    try:
        stamp = float(item.get("ts") or 0)
    except Exception:
        return False
    return abs(time.time() - stamp) <= MAX_AGE_S


def is_candidate(item: dict, account: str = "") -> bool:
    """Nova, individual, de outra pessoa; nome atual ou chat direto do dono."""
    if not isinstance(item, dict):
        return False
    if item.get("from_me") or item.get("group"):
        return False
    chat = str(item.get("chat") or "")
    if not chat or chat.endswith("g.us"):
        return False
    if not _fresh(item):
        return False
    if _is_direct(account, item):
        return True
    # Never let one of JARVIS's own paired numbers trigger the name scheme on
    # another account (anti-loop between the two WhatsApp connections).
    if _is_own_account(chat):
        return False
    return matches(item.get("text"))


def is_direct(account: str, item: dict) -> bool:
    return _is_direct(str(account or ""), item or {})


def _seen_key(account: str, item: dict):
    message_id = str(item.get("id") or "").strip()
    if message_id:
        return (account, message_id)
    return (account, str(item.get("chat") or ""), str(item.get("ts") or ""),
            str(item.get("text") or "")[:120])


def _first_time(account: str, item: dict) -> bool:
    """True na primeira vez que esta mensagem é vista; False depois."""
    key = _seen_key(account, item)
    with _lock:
        if key in _seen:
            return False
        _seen[key] = time.monotonic()
        while len(_seen) > MAX_SEEN:
            _seen.popitem(last=False)
    return True


def _rate_key(account: str, item: dict) -> tuple[str, str]:
    return (account, str(item.get("chat") or ""))


def _rate_left(key: tuple[str, str]) -> bool:
    now = time.monotonic()
    with _lock:
        times = [t for t in _sent.get(key, []) if now - t < RATE_WINDOW_S]
        _sent[key] = times
        return len(times) < RATE_LIMIT


def _rate_record(key: tuple[str, str]) -> None:
    with _lock:
        _sent.setdefault(key, []).append(time.monotonic())


def handle(account: str, item: dict) -> bool:
    """Processa uma mensagem; True quando a resposta autônoma foi pedida."""
    account = str(account or "").strip()
    if not is_candidate(item, account):
        return False
    with _lock:
        handler = _handler
    if handler is None:
        return False
    if not _first_time(account, item):
        return False

    direct = _is_direct(account, item)
    who = str(item.get("name") or item.get("chat") or "")
    key = _rate_key(account, item)
    if not direct and not _rate_left(key):
        _log(f"limite autônomo atingido para {who} ({account}): "
             f"{RATE_LIMIT} respostas na última hora — ignorando.")
        return False

    chat = str(item.get("chat") or "")
    seconds = actiongate.allow_whatsapp_reply(account, chat, who)
    try:
        delivered = bool(handler(account, item))
    except Exception as e:
        actiongate.revoke_whatsapp_reply(account, chat)
        _log(f"o pedido de resposta autônoma falhou: {e}")
        return False
    if not delivered:
        actiongate.revoke_whatsapp_reply(account, chat)
        return False

    if not direct:
        _rate_record(key)
    label = "chat direto do dono" if direct else (
        f"mensagem de {who or chat} menciona {_trigger_name or 'o nome'}")
    _log(f"{label}; resposta autônoma pedida "
         f"(autorização de {seconds:.0f}s).")
    return True


def otp_candidate(item: dict) -> dict | None:
    """O código de verificação numa mensagem nova e individual, ou None."""
    if not isinstance(item, dict):
        return None
    if item.get("from_me") or item.get("group"):
        return None
    chat = str(item.get("chat") or "")
    if not chat or chat.endswith("g.us"):
        return None
    if not _fresh(item):
        return None
    return otp_detect.detect(item.get("text"),
                             sender=item.get("name") or chat)


def handle_otp(account: str, item: dict) -> bool:
    """Anuncia um OTP; True quando a mensagem foi consumida como código."""
    account = str(account or "").strip()
    otp = otp_candidate(item)
    if otp is None:
        return False
    with _lock:
        handler = _otp_handler
    if handler is None:
        return False
    if not _first_time(account, item):
        return True
    who = str(item.get("name") or item.get("chat") or "")
    chat = str(item.get("chat") or "")
    try:
        delivered = handler(dict(otp, origin="whatsapp", account=account,
                                 chat=chat, sender=who)) is not False
    except Exception as e:
        _log(f"o anúncio do código de {who or chat} falhou: {e}")
        return True
    if delivered:
        _log(f"código de verificação de {who or chat} ({account}) anunciado.")
    else:
        _log(f"código de {who or chat} reconhecido, mas não anunciado "
             "(assistente dormindo, mudo ou sem sessão ativa).")
    return True


def _dispatch(account: str, item: dict) -> None:
    """Observador registrado em core/whatsapp.py (roda na thread do neonize)."""
    if handle_otp(account, item):
        return
    handle(account, item)
