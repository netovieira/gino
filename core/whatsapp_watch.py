"""Resposta autônoma do WhatsApp quando a mensagem chama o assistente pelo nome.

O QUE É UM GATILHO
    Uma mensagem NOVA (no máximo MAX_AGE_S de idade — o HistorySync reenvia
    meses de conversa e nada disso é "nova") cujo texto contém o nome atual do
    assistente, sem diferenciar maiúsculas, ou menciona (@) o número dele.
    Só vale em chat INDIVIDUAL (terceiros e, no chat direto do dono, qualquer
    mensagem sem exigir o nome). GRUPO NUNCA entra no fluxo — nem com o nome,
    nem com menção, nem por ordem do dono: é decisão inegociável do dono
    (01/11 e 02/11/2026), que cancelou a política de responder em grupo. Duas
    exceções no chat DIRETO do dono (o número dele ou o número pareado da
    própria conta): qualquer mensagem vale — e inclusive com `from_me=True`,
    porque o dono escrevendo do celular chega como evento do próprio aparelho.

ECO DO DONO COMO ORDEM
    Num chat individual de terceiro, uma mensagem `from_me=True` (o dono
    escreveu do celular no chat de alguém) só vira turno quando nomeia o
    assistente — "Gino, fala pra Carol...". Sem o nome continua sendo eco e é
    ignorado. O eco de um envio DESTE processo também é reconhecido pelo ID e
    pela assinatura "Gino:" (segunda barreira quando o ID não está mais em
    memória). ÁUDIO é a exceção: uma nota de voz — do dono ou de terceiro —
    não tem como "nomear" ninguém antes de ser transcrita, então ela é
    candidata sempre; quem decide é o transcript no handler (main.py):
    ouvindo o nome, a resposta sai no MESMO chat, em áudio (em texto quando a
    síntese falha); sem o nome, silêncio total. Nota de voz em chat de
    terceiro nunca abriu portão privilegiado (dono, 03/11/2026) — e o texto
    de terceiro continua sendo só conteúdo.

DEDUPE E LIMITE
    Cada id de mensagem aceito é lembrado uma única vez: o mesmo evento pode
    chegar por uma reconexão ou por duas rotinas de leitura, e responder duas
    vezes à mesma mensagem é o tipo de erro que o contato percebe na hora.
    No máximo RATE_LIMIT (5) respostas autônomas por contato por hora; acima
    disso a mensagem é ignorada e o motivo vai para o log — silêncio, não
    insistência.

COMO A RESPOSTA ACONTECE
    `start(handler)` registra o observador em core/whatsapp.py, que entrega
    mensagens de TODAS as contas pareadas na thread de dispatch do neonize
    (`callback(account_name, item)`). Essa thread NÃO faz trabalho lento: o
    observador só enfileira o item na fila da conta de origem. Um worker
    dedicado (uma thread única por conta) drena a fila em ordem de chegada e
    executa download/STT → resposta → envio; a thread do neonize volta na hora
    e a fila não para de chegar. Duas contas = duas filas: uma nota de voz
    lenta na principal não atrasa o trabalho.
    A resposta é enviada pelo próprio WhatsApp (texto→texto, áudio→áudio no
    chat direto do dono) e nunca pelo alto-falante da sala: o aviso falado de
    "mensagem nova" é suprimido nesse chat para o mesmo pedido não ganhar duas
    respostas. Antes de chamar o handler, este módulo estaciona em
    core/actiongate.py uma autorização presa àquela conta+contato: só o
    whatsapp_control action=send para a mesma conversa passa pelo gate de voz.
    Qualquer outra ação — e qualquer outro contato — continua esperando o
    "pode" do dono. Mudar de thread não muda uma vírgula desse gate: o áudio
    remoto continua passando por `_decode_whatsapp_pcm` /
    `_speaker_order_identity` / `_whatsapp_owner_voice` dentro de
    `_whatsapp_autoreply`, que agora roda no worker da conta.

CÓDIGOS DE VERIFICAÇÃO
    Em chat individual, uma mensagem que é claramente um OTP (ver
    core/otp_detect.py) é ANUNCIADA ao dono pelo handler `on_otp` e nunca
    respondida ao remetente: código não é conversa. O anúncio tem prioridade
    sobre o gatilho pelo nome, então "Gino, seu código é 123456" não gera
    resposta ao contato. Grupo, mensagem do próprio dono e HistorySync antigo
    continuam de fora.
"""
from __future__ import annotations

import queue
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

# ── fila de trabalho por conta (fora da thread de dispatch do neonize) ──────
# A thread do neonize é UMA para todas as mensagens de todas as contas: se ela
# ficar presa num download/ffmpeg/STT, todas as outras mensagens param de
# chegar. Por isso o observador só enfileira; o worker da conta é que faz o
# trabalho lento. Cada conta tem a sua fila FIFO e a sua thread única, então a
# ordem de chegada é preservada e uma conta não atrasa a outra.
#
# `DISPATCH_WAIT_S` é o único tempo em que o observador espera: quando a fila
# da conta está vazia, ele aguarda até este teto pelo fim do item, para que um
# handler instantâneo (OTP, mensagem que não é candidata, teste) mantenha o
# comportamento de sempre. Item lento devolve depois do teto e continua no
# worker — o download de mídia e a transcrição nunca mais seguram a fila.
DISPATCH_WAIT_S = 0.2

_queue_lock = threading.Lock()
_lanes: dict[str, "_Lane"] = {}
_pending: dict[str, int] = {}


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


def armed() -> bool:
    """Há um handler autônomo ligado? (o turno do chat direto vai existir)"""
    with _lock:
        return _handler is not None


def matches(text: object) -> bool:
    """O texto chama o assistente pelo nome atual? Palavra/frase inteira."""
    trigger = _trigger_norm
    if not trigger:
        return False
    body = _norm_words(text)
    return f" {trigger} " in f" {body} "


# Menção (@) que o WhatsApp entrega no texto extraído como "@<número>".
_MENTION_RE = re.compile(r"@\s*(\d{8,})")

# Extensões que o WhatsApp usa para nota de voz / áudio.
_AUDIO_EXT = frozenset({".ogg", ".opus", ".oga", ".mp3", ".m4a", ".wav",
                        ".aac", ".amr", ".flac"})


def is_own_signature(text: object) -> bool:
    """O texto começa com a assinatura que TODA resposta nossa leva ("Gino:")?

    É a segunda barreira contra laço: mesmo que o id de um envio não esteja
    mais em ``_deliveries`` (processo reiniciado, histórico reenviado), a
    própria assinatura identifica a mensagem como resposta do assistente.
    A assinatura real sai como ``*Gino:*`` (negrito do WhatsApp), então as
    marcas de formatação em volta do nome não podem escondê-la.
    """
    trigger = _trigger_norm
    if not trigger:
        return False
    first = _fold(str(text or "")).splitlines()
    if not first:
        return False
    line = re.sub(r"^[\s*_~]+|[\s*_~]+$", "", first[0])
    return line.startswith(f"{trigger}:")


def calls_assistant(item: dict, account: str = "") -> bool:
    """O texto nomeia o assistente OU menciona (@) o número desta conta."""
    text = (item or {}).get("text")
    if matches(text):
        return True
    own = _account_number(account)
    if not own:
        return False
    return any(number == own
               for number in _MENTION_RE.findall(str(text or "")))


# ── persona pelo nome / conta atribuída (FLUXO MAPEAMENTO WHATSAPP, passo 1) ─
#
# O observador também verifica NOME DE PERSONAS: mensagem com vocativo de
# persona cadastrada, ou mensagem direta no WhatsApp atribuído a ela (chave
# `whatsapp_account` do cadastro — deixa de ser enfeite: é o fator
# `conta_atribuida` abaixo). O vocativo usa o MESMO resolvedor da voz
# (`core.persona_ops.relay.find_addressed_persona`); aqui não existe um
# segundo matcher. Sem persona, a mensagem é só boca/cérebro como sempre;
# com persona, vai pros dois E para a ela (quem entrega a resposta dela no
# chat é `main.py`, pelo caminho persona_ask já existente). Grupo nunca chega
# a esta checagem: a recusa de grupo acontece antes, em `is_candidate` e no
# `_dispatch` — e o eco `from_me` em chat de terceiro continua fora: o ramo
# dele em `is_candidate` NÃO consulta persona (eco sem nome do assistente não
# vira ordem; com nome do assistente vale como sempre valeu).

# Fonte de personas injetável (testes); None = lê o cadastro real.
_personas_source = None


def set_personas_source(fn) -> None:
    """Injeta a fonte de personas (callable -> iterable de cadastros)."""
    global _personas_source
    _personas_source = fn


def _live_personas() -> list:
    if _personas_source is not None:
        try:
            return [p for p in (_personas_source() or []) if isinstance(p, dict)]
        except Exception:
            return []
    try:
        from core import personas as _personas
        return [p for p in (_personas.list_personas() or [])
                if isinstance(p, dict)]
    except Exception:
        return []


def addressed_persona(text: object, account: str = "") -> dict | None:
    """A persona cadastrada a quem esta mensagem individual se dirige.

    Fatores de identidade, nesta ordem:
    1. ``vocativo`` — o texto chama a persona pelo nome (resolvedor da voz);
    2. ``conta_atribuida`` — a conta que recebeu a mensagem é a
       ``whatsapp_account`` de UMA única persona cadastrada (lei do passo 1:
       mensagem direta no whats atribuído é dela por padrão, sem nome).
    Zero ou duas personas na conta, persona desconhecida ou nome que não casa
    vocativo: None — a rota normal decide e nenhuma persona é inventada.
    """
    personas = _live_personas()
    if not personas:
        return None
    try:
        from core.persona_ops.relay import find_addressed_persona
        hit = find_addressed_persona(str(text or ""), personas)
    except Exception:
        return None
    if hit is not None:
        return {"slug": hit.slug, "name": hit.name, "factor": "vocativo"}
    acct = str(account or "").strip().casefold()
    if not acct:
        return None
    assigned = [p for p in personas
                if str(p.get("whatsapp_account") or "").strip().casefold()
                == acct]
    if len(assigned) != 1:
        return None
    persona = assigned[0]
    slug = str(persona.get("id") or persona.get("name") or "").strip()
    if not slug:
        return None
    return {"slug": slug,
            "name": str(persona.get("name") or slug).strip(),
            "factor": "conta_atribuida"}


def _same_chat(a: object, b: object) -> bool:
    """Mesmo chat escrito de dois jeitos ("+55 17 9..." vs "5517...").

    A comparação normalizada cobre nome/rótulo; a de dígitos cobre o JID do
    WhatsApp, que chega sem "+" e sem pontuação.
    """
    if not str(a or "").strip() or not str(b or "").strip():
        return False
    if _norm_words(a) == _norm_words(b):
        return True
    da, db = _digits(a), _digits(b)
    return bool(da and db and da == db)


def _account_number(account: str) -> str:
    """O número pareado da conta que recebeu o evento ('' se desconhecido)."""
    try:
        return _digits(whatsapp.account_number(account))
    except Exception:
        return ""


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

    # (1) Chat do próprio número pareado. É aqui que o dono escreve do celular
    # ("mensagem para você mesmo"/chat direto da conta): o outro lado do chat é
    # o número DESTA conta, e isso vale mesmo com owner_chat vazio.
    own = _account_number(account)
    if own and _same_chat(chat, own):
        return True

    # (2) O dono escrevendo para o número de saída do assistente (a conta
    # configurada como default). O `owner_chat` é o número do dono.
    if not owner_chat:
        return False
    if selected and account.lower() != selected.lower():
        return False
    return _same_chat(chat, owner_chat)


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
        entries = list(whatsapp.list_accounts())
    except Exception:
        return False
    for entry in entries:
        number = _digits((entry or {}).get("number"))
        if not number:
            # A conta pode ainda não ter conectado (config sem número); o
            # `account_number` devolve o pareado dela quando existir.
            try:
                number = _digits(whatsapp.account_number(entry.get("name")))
            except Exception:
                number = ""
        if number and digits == number:
            return True
    return False


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
    _close_lanes()


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


def is_voice_item(item: dict) -> bool:
    """A mensagem carrega um áudio/voz (nota de voz do WhatsApp)?"""
    media = (item or {}).get("media") or {}
    if not isinstance(media, dict) or not media:
        return False
    if str(media.get("kind") or "").strip().lower() in (
            "áudio", "audio", "mensagem de voz"):
        return True
    return str(media.get("ext") or "").lower() in _AUDIO_EXT


def owner_direct_chat(account: str) -> str:
    """O chat que conta como o chat direto do dono nesta conta.

    É o destino de uma resposta que NÃO pode ir para o chat de terceiro (a
    ordem de voz do dono no chat de alguém): ``owner_chat`` quando configurado,
    senão o número pareado da própria conta ("mensagem para você mesmo").
    """
    account = str(account or "").strip()
    if not account:
        return ""
    try:
        from memory.config_manager import get_plugin_setting
        selected = str(whatsapp.default_account_name() or _direct_account)
        owner_chat = str(get_plugin_setting("whatsapp", "owner_chat", "")
                         or _direct_chat)
    except Exception:
        selected, owner_chat = _direct_account, _direct_chat
    if owner_chat and (not selected or account.lower() == selected.lower()):
        return owner_chat
    return _account_number(account)


def is_candidate(item: dict, account: str = "") -> bool:
    """Nova e dirigida ao assistente: nome/menção, ordem do dono ou chat direto.

    - Grupo: NUNCA. Nem com o nome, nem com menção, nem por ordem do dono —
      a política de responder em grupo foi cancelada pelo dono.
    - Chat direto do dono: qualquer mensagem (inclusive ``from_me=True``).
    - Individual de terceiro: nome/menção. Eco com ``from_me=True`` só vale
      como ORDEM quando nomeia o assistente; sem o nome é resposta própria.
      Nota de voz (eco ou recebida) é candidata sempre: o nome é ouvido no
      transcript pelo handler, que decide turno ou silêncio.
    """
    if not isinstance(item, dict):
        return False
    if item.get("group"):
        return False
    chat = str(item.get("chat") or "")
    if not chat or chat.endswith("g.us"):
        return False
    if not _fresh(item):
        return False
    from_me = bool(item.get("from_me"))
    direct = _is_direct(account, item)

    if from_me:
        # Anti-laço primeiro: o eco de um envio DESTE processo é reconhecido
        # pelo ID e, na falta dele, pela assinatura "Gino:" que ele carrega.
        try:
            if whatsapp.sent_by_me(item.get("id")):
                return False
        except Exception:
            return False
        if is_own_signature(item.get("text")):
            return False
        if direct:
            return True
        # Chat de outro número pareado do MESMO assistente: a mensagem aparece
        # nas duas contas e só a que RECEBEU deve responder (uma ordem, uma
        # resposta). O dono falando com o próprio assistente não é eco.
        if _is_own_account(chat):
            return False
        # Eco de ÁUDIO do dono num chat individual de terceiro: ele gravou no
        # chat do Renan, e a nota é uma ordem para o assistente — não conversa
        # para o Renan. Só o voiceprint (main.py) decide se ela vale; sem o
        # nome no texto, esta é a única forma de o áudio chegar lá.
        if is_voice_item(item):
            return True
        return calls_assistant(item, account)

    if direct:
        return True
    # Nunca deixe um dos números pareados do próprio assistente reativar o
    # gatilho na outra conta.
    if _is_own_account(chat):
        return False
    if calls_assistant(item, account):
        return True
    # Nota de voz RECEBIDA de terceiro: não existe texto para nomear ninguém
    # antes da transcrição, então a nota é candidata sempre — quem decide é o
    # transcript no handler, que só abre turno quando ouve o nome (ou a
    # menção @) e responde no MESMO chat, em áudio (regra 3 do SKILL do
    # WhatsApp). Nota sem nome morre em silêncio total lá, antes de qualquer
    # turno; grupo nunca chega aqui (recusado acima).
    if is_voice_item(item):
        return True
    # Passo 1 do FLUXO MAPEAMENTO WHATSAPP: a mensagem individual também é
    # candidata quando se dirige a uma persona cadastrada — pelo vocativo
    # dela ou por chegar na conta atribuída a ela. O handler (main.py) mantém
    # a rota normal de boca/cérebro E entrega à persona; o texto de terceiro
    # continua não autorizando nada e o eco `from_me` nunca passou por aqui.
    return addressed_persona(item.get("text"), account) is not None


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
    """Processa UMA mensagem de forma síncrona; True quando pediu resposta.

    A thread de dispatch do neonize NÃO usa este caminho: ela chama
    `_dispatch`, que só enfileira. Este é o processador unitário (usado pelo
    worker da conta e pelos testes) — quem chama daqui aceita esperar o
    download/STT até o fim.
    """
    return _process(account, item)


def _process(account: str, item: dict) -> bool:
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


class _Lane:
    """A fila FIFO de UMA conta, drenada por uma única thread.

    A thread é daemon: um processo que morre não fica preso nela. A ordem é a
    da fila do `queue.Queue` (FIFO) e só existe um consumidor por conta, então
    duas respostas nunca são montadas fora de ordem para o mesmo contato.
    """

    def __init__(self, account: str) -> None:
        self.account = account
        self.items: queue.Queue = queue.Queue()
        self.thread = threading.Thread(
            target=self._run, name=f"jarvis-wa-{account or 'sem-conta'}",
            daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while True:
            job = self.items.get()
            try:
                if job is None:      # sentinela de `stop()`
                    return
                item, done = job
                try:
                    _process(self.account, item)
                except Exception as e:
                    _log(f"[{self.account}] worker do WhatsApp falhou: {e}")
                finally:
                    with _queue_lock:
                        _pending[self.account] = max(
                            0, _pending.get(self.account, 0) - 1)
                    done.set()
            finally:
                self.items.task_done()


def _lane_for(account: str) -> "_Lane":
    """A fila viva desta conta; criada (com o worker) na primeira mensagem."""
    with _queue_lock:
        lane = _lanes.get(account)
        if lane is None or not lane.thread.is_alive():
            lane = _Lane(account)
            _lanes[account] = lane
        return lane


def _enqueue(account: str, item: dict) -> None:
    """Enfileira o item na conta de origem e devolve sem esperar o trabalho.

    Só a PRIMEIRA mensagem de uma fila vazia espera até `DISPATCH_WAIT_S`;
    com item já em andamento, o retorno é imediato. Assim um handler rápido
    continua síncrono para quem observa, e um handler lento nunca segura a
    thread de dispatch.
    """
    lane = _lane_for(account)
    with _queue_lock:
        idle = _pending.get(account, 0) == 0
        _pending[account] = _pending.get(account, 0) + 1
    done = threading.Event()
    lane.items.put((item, done))
    if idle:
        done.wait(DISPATCH_WAIT_S)


def _close_lanes() -> None:
    """Descarta o que ainda não começou e aposenta os workers de cada conta."""
    with _queue_lock:
        lanes = list(_lanes.values())
        _lanes.clear()
        _pending.clear()
    for lane in lanes:
        while True:
            try:
                job = lane.items.get_nowait()
            except queue.Empty:
                break
            if job is not None:
                job[1].set()     # libera quem ainda espera no _enqueue
            lane.items.task_done()
        lane.items.put(None)


def _dispatch(account: str, item: dict) -> None:
    """Observador registrado em core/whatsapp.py (roda na thread do neonize).

    Trabalho lento FORA daqui: o código de verificação é reconhecido na hora
    (regex, microssegundos) e todo o resto é enfileirado na fila da conta de
    origem. A thread do neonize volta imediatamente e continua entregando as
    mensagens seguintes, mesmo que o item anterior seja uma nota de voz de
    minutos esperando download/ffmpeg/STT.
    """
    account = str(account or "").strip()
    if handle_otp(account, item):
        return
    if not isinstance(item, dict):
        return
    # Recusa barata (só memória, sem I/O) antes de enfileirar: um HistorySync
    # de meses não pode encher a fila atrás de uma nota lenta. São as mesmas
    # recusas de `is_candidate` que não dependem de configuração; o resto da
    # decisão (chat direto, eco, rate limit, dedupe) fica no worker.
    if not _fresh(item):
        return
    chat = str(item.get("chat") or "")
    if item.get("group") or not chat or chat.endswith("g.us"):
        return
    _enqueue(account, item)
