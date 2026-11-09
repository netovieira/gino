"""WhatsApp de verdade: os números do usuário, pareados uma vez por QR.

O caminho antigo (actions/send_message.py) abria o app do WhatsApp e DIGITAVA
por automação de teclado. Isso nunca foi uma conexão: não lê nada, não sabe se a
janela certa recebeu o texto, e na sessão Wayland desta máquina mal funciona.
Aqui o WhatsApp entra pela mesma porta que o WhatsApp Web usa — o protocolo
multi-dispositivo, via neonize (whatsmeow). O celular escaneia um QR uma vez e
a sessão fica em disco; a partir daí o assistente lê o que chega e envia o que o
usuário ditar, sem janela nenhuma no meio.

MULTI-CONTA: cada número é um `_Account` com seu próprio uuid neonize, thread,
session.db, inbox e pasta de mídia. O config guarda a lista em
`plugin_config.whatsapp.accounts`; sem ela, migra a conta única antiga (o
"jarvis", cuja sessão já mora em config/whatsapp_web/session.db). Toda função
pública aceita `account=None` — resolve para a default, ou para a nomeada.

MÍDIA: foto, vídeo, áudio, documento e figurinha, nos dois sentidos. O ffmpeg
que o neonize precisa para medir e converter não vem do apt (instalar exigiria
root): ele mora em tools/ffmpeg/, extraído dos .deb do Ubuntu, e este módulo o
põe no PATH antes de qualquer coisa tocar no neonize.

O QUE CHEGA VIRA ARQUIVO, E O ARQUIVO VIRA CONTEÚDO: o download é uma thread
(``_download_media``) porque o link do WhatsApp morre; quando o arquivo pousa,
o caminho entra no próprio item (``media["path"]``) e ``recent()``/``search()``
devolvem esse caminho a quem for ler a mídia. É por aí que uma foto recebida
chega à visão (``core/vision.describe_file``, chamada por
``actions/file_processor.py``) e que um áudio chega à transcrição do app
(``main._transcribe_whatsapp_audio``, que espera o mesmo caminho). Sem o campo
`path` o arquivo existiria no disco e continuaria invisível: a resposta sobre a
foto sairia da legenda, não da foto.

EVENTOS DO DONO: o whatsmeow entrega o horário em MILISSEGUNDOS e, depois da
migração para LID, o chat como ``<id>@lid`` (um identificador interno, não o
telefone). Este módulo normaliza os dois — ver ``_stamp_seconds`` e
``_chat_identity`` — para a mensagem do dono ser reconhecida como conversa com
ele, uma única vez por id, e avisa o app por ``add_owner_wake_hook`` (quem
decide acordar). Com o assistente dormindo, o aviso fica na gaveta
(``pending_owner``/``drain_owner_pending``) e o texto já está no inbox em disco.

O QUE ISTO NÃO FAZ, de propósito:
  - grupos para envio (o nome do grupo não está no catálogo de contatos);
  - transcrição de áudio (o assistente ouve o microfone, não arquivos).

O QUE ISTO CUSTA: é protocolo não oficial. Funciona, é o mesmo motor de muitos
bots, mas uso agressivo (disparo em massa, spam) é o que gera bloqueio — o
número é o do usuário.

    from core import whatsapp
    whatsapp.start()                              # sobe todas as auto_start
    whatsapp.send("João", "chego em 10min",       # texto ditado pelo dono:
                  signed=False)                   # sai sem assinatura
    whatsapp.send("João", "resposta do Gino")     # texto do assistente: *Gino:*
    whatsapp.send("João", "oi", account="trabalho")
    whatsapp.recent(5)                            # o que chegou na default
    whatsapp.list_accounts()                      # [{name, uuid, connected, …}]
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent

# ffmpeg/ffprobe próprios (ver o cabeçalho). Antes de qualquer import do
# neonize, porque é ele quem decide, uma vez só, se a mídia vai funcionar.
_FFMPEG_BIN = _BASE / "tools" / "ffmpeg" / "bin"


def _ensure_ffmpeg() -> None:
    """Garante ffmpeg+ffprobe no PATH: o do sistema manda, o do projeto é reserva.

    Depois de um `apt install ffmpeg` esta função não faz nada — o binário
    global já resolve. O de tools/ffmpeg/ só entra quando não existe nenhum,
    e pode ser apagado sem quebrar nada.
    """
    try:
        if shutil.which("ffmpeg") and shutil.which("ffprobe"):
            return
        if not ((_FFMPEG_BIN / "ffmpeg").exists() and (_FFMPEG_BIN / "ffprobe").exists()):
            return
        atual = os.environ.get("PATH", "")
        if str(_FFMPEG_BIN) not in atual.split(os.pathsep):
            os.environ["PATH"] = (f"{_FFMPEG_BIN}{os.pathsep}{atual}"
                                  if atual else str(_FFMPEG_BIN))
    except Exception:
        pass


_ensure_ffmpeg()

# Namespace em config/api_keys.json → plugin_config.
NS = "whatsapp"

# A conta histórica mora direto em config/whatsapp_web/ (session.db, paired,
# whatsapp_qr.png). Contas novas ganham um subdiretório por uuid, e inbox/mídia
# com sufixo — sem tocar no que já existe no disco.
_SESSIONS_ROOT = _BASE / "config" / "whatsapp_web"
_LEGACY_UUID   = "jarvis"

INBOX_KEEP     = 300          # mensagens mantidas em disco por conta
INBOX_TRIM_EVERY = 25         # regrava o arquivo a cada N mensagens
RECONNECT_S    = 10.0         # espera entre tentativas depois de uma queda
START_WAIT_S   = 8.0          # quanto start() espera por um sinal concreto
# Boot: depois da última tentativa, uma janela curta para as contas mais lentas
# fecharem o handshake. Sem ela uma conexão de 10 s viraria alarme falso; e não
# é laço de reconexão — isso é trabalho do `_run`, uma vez por conta.
BOOT_SETTLE_S  = 6.0
FRESH_TS_S     = 24 * 3600    # só guarda o que chegou nas últimas 24h
MEDIA_MAX_MB   = 20           # acima disso a mídia fica no servidor
MEDIA_KEEP_DAYS = 7           # quanto tempo os arquivos baixados ficam aqui

# Envio só está "confirmado" quando chega recibo. O servidor devolve o ID na
# hora e manda o ReceiptEv depois; esperar demais atrapalha a conversa, então
# o padrão é curto e `wait_delivery` aceita outro valor por chamada.
DELIVERY_WAIT_S  = 8.0        # espera padrão por recibo de entrega/leitura
_DELIVERY_KEEP_S = 300.0      # por quanto tempo o estado de entrega fica na memória

# O servidor aceita textos enormes, mas uma parede de texto não é mensagem.
# Acima deste tamanho `send` divide em partes numeradas, sempre em fronteira
# de frase/palavra — nunca corta nem acrescenta reticências. O limite é uma
# escolha de legibilidade, não um teto do WhatsApp.
MAX_TEXT_CHARS = 4000

# Fim de frase de verdade: pontuação seguida de espaço (não `main.py`).
_SENTENCE_CUT_RE = re.compile(r"[.!?…][\"')\]»’”]*(?=\s)")
_URL_ANY_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# Mensagem do dono entregue como turno: o id fica lembrado este tempo para o
# mesmo evento (reconexão, duas rotinas lendo o mesmo store) não virar dois
# turnos. Curto de propósito: id não é único para sempre, e o que interessa é
# cobrir o reenvio.
OWNER_ID_KEEP_S = 900.0

# Quantos avisos do dono ficam na gaveta esperando o próximo despertar.
OWNER_PENDING_KEEP = 50

# Idade máxima de uma mensagem para valer como aviso na hora (tela/alto-falante
# e o gancho de despertar). Mais velha que isto é histórico de reconexão: fica
# no inbox, mas não acorda ninguém.
NOTICE_AGE_S = 300.0

_lock       = threading.RLock()
# Neonize's Go runtime dies with "concurrent map writes" if two clients are
# constructed and have their events registered at the same time. The boot used
# to start both WhatsApp accounts in parallel and killed the whole app. This
# lock serializes only construction/registration; connect() itself stays
# outside it because it blocks for the lifetime of the session.
_client_init_lock = threading.RLock()
_inbox_lock = threading.RLock()
_logger     = None

# ID da mensagem enviada → estado de entrega. Vive só na memória: o recibo
# chega na mesma execução do app e não há o que recuperar depois de reiniciar.
_delivery_lock = threading.Lock()
_deliveries: dict[str, dict] = {}
# Recibo que chegou ANTES de o envio ser registrado. O `send_*` só devolve o
# ID depois do ack do servidor, e o destinatário pode confirmar no mesmo
# instante (áudio para uma conversa que está com o celular online, por
# exemplo). O ReceiptEv da outra thread invalidava um ID que ainda não estava
# no mapa — e o envio ficava "aguardando confirmação de entrega" para sempre,
# mesmo tendo sido entregue. Aqui o recibo espera o registro.
_early_receipts: dict[str, dict] = {}
_EARLY_RECEIPT_KEEP_S = 120.0

# O neonize carrega um runtime Go (whatsmeow) que segura threads do processo.
# Depois disso o Python nunca mais sai sozinho ao fim do mainloop, então quem
# encerra o app precisa chamar hard_exit() — ver a explicação lá embaixo. O
# runtime é UM por processo, compartilhado por todas as contas.
_go_loaded  = False


class _Account:
    """Uma conexão WhatsApp: estado + caminhos próprios."""

    def __init__(self, name: str, uuid: str, default: bool = False,
                 auto_start: bool = True, notify: bool = False):
        self.name       = name
        self.uuid       = uuid
        self.default    = default
        self.auto_start = auto_start
        self.notify     = notify
        # caminhos
        if uuid == _LEGACY_UUID:
            self.dir        = _SESSIONS_ROOT
            self.inbox_file = _BASE / "memory" / "whatsapp_inbox.jsonl"
            self.media_dir  = _BASE / "memory" / "whatsapp_media"
        else:
            self.dir        = _SESSIONS_ROOT / uuid
            self.inbox_file = _BASE / "memory" / f"whatsapp_inbox_{uuid}.jsonl"
            self.media_dir  = _BASE / "memory" / f"whatsapp_media_{uuid}"
        self.session_db  = self.dir / "session.db"
        self.paired_mark = self.dir / "paired"
        self.qr_png      = self.dir / "whatsapp_qr.png"
        # runtime
        self.client     = None
        self.thread     = None
        self.connected  = False
        self.me         = {"number": "", "name": ""}
        self.qr_path    = ""
        self.qr_at      = 0.0
        self.error      = ""
        self.stopping   = False
        self.sink       = None          # callback de mensagem nova (por conta)
        self.appended   = 0             # contador para o trim do inbox
        self.contacts   = {"at": 0.0, "items": []}


_accounts: dict[str, _Account] = {}

# Observadores de TODAS as contas. Diferente do sink (`on_message`), que é por
# conta e substituído a cada start(), estes vivem no processo inteiro — é o que
# o monitoramento autônomo usa para seguir vendo mensagens mesmo quando uma
# conta é reconectada ou pareada depois do boot.
_watchers: list = []


def add_watcher(callback) -> None:
    """Chama `callback(account_name, item)` a cada mensagem de qualquer conta."""
    with _lock:
        if callback not in _watchers:
            _watchers.append(callback)


def remove_watcher(callback) -> None:
    with _lock:
        try:
            _watchers.remove(callback)
        except ValueError:
            pass


# ── aviso ao dono: acordar, e a gaveta de quem está dormindo ────────────────
#
# Mensagem do DONO no chat direto não é "recado de terceiro": é conversa com
# ele, e enquanto o assistente dorme ela não pode virar nada. Dois caminhos:
#   1. `add_owner_wake_hook(cb)` — quem quiser ser chamado na hora (main.py
#      usa para acordar e falar o aviso, a regra que o dono pediu);
#   2. sem gancho, ou com o gancho dizendo False, o aviso fica na gaveta
#      (`pending_owner`/`drain_owner_pending`) para ser entregue no despertar.
# O conteúdo em si nunca se perde: ele já está no inbox em disco, que é o que
# sobrevive a um reinício; a gaveta é só a lista do que ainda não foi avisado.
_alert_lock = threading.Lock()
_alert_hooks: list = []
_owner_pending: list[dict] = []

# Ids de mensagens do dono já transformadas em turno por esta conta.
_owner_id_lock = threading.Lock()
_owner_ids: dict[tuple[str, str], float] = {}


def add_owner_wake_hook(callback) -> None:
    """Chama `callback(account, item)` a cada mensagem do DONO (inclusive dormindo).

    O callback roda na thread do neonize: quem acorda a interface precisa
    agendar no loop (`call_soon_threadsafe`), não mexer na UI daqui. Devolver
    True marca o aviso como entregue; False/None o deixa na gaveta.
    """
    with _alert_lock:
        if callback not in _alert_hooks:
            _alert_hooks.append(callback)


def remove_owner_wake_hook(callback) -> None:
    with _alert_lock:
        try:
            _alert_hooks.remove(callback)
        except ValueError:
            pass


def pending_owner() -> list[dict]:
    """Os avisos do dono que ainda não foram entregues (do mais antigo)."""
    with _alert_lock:
        return [dict(item) for item in _owner_pending]


def drain_owner_pending() -> list[dict]:
    """Entrega a gaveta: devolve os avisos pendentes e esvazia a lista."""
    with _alert_lock:
        items = [dict(item) for item in _owner_pending]
        _owner_pending.clear()
        return items


def _stash_owner_item(account: str, item: dict) -> None:
    """Guarda o aviso na gaveta, sem repetir o mesmo id."""
    alert = dict(item)
    alert["account"] = account
    mid = str(alert.get("id") or "").strip()
    with _alert_lock:
        if mid and any(str(old.get("id") or "") == mid and
                       str(old.get("account") or "") == account
                       for old in _owner_pending):
            return
        _owner_pending.append(alert)
        del _owner_pending[:-OWNER_PENDING_KEEP]


def _owner_arrived(account: str, item: dict) -> None:
    """Mensagem do dono: avisa agora quem estiver ligado, senão põe na gaveta."""
    with _alert_lock:
        hooks = list(_alert_hooks)
    for hook in hooks:
        try:
            if hook(account, dict(item)):
                return
        except Exception as e:
            _log(f"o aviso de mensagem do dono falhou: {e}")
    _stash_owner_item(account, item)


def _first_owner_sighting(account: str, message_id: object) -> bool:
    """Primeira vez que ESTA conta vê esta mensagem do dono? (dedupe por id)"""
    mid = str(message_id or "").strip()
    if not mid:
        return True
    now = time.monotonic()
    key = (str(account or ""), mid)
    with _owner_id_lock:
        for stale in [k for k, at in _owner_ids.items()
                      if now - at > OWNER_ID_KEEP_S]:
            _owner_ids.pop(stale, None)
        if key in _owner_ids:
            return False
        _owner_ids[key] = now
    return True


# ── contas: carregar, listar, adicionar, remover ─────────────────────────────

def _entry_to_account(entry: dict) -> _Account:
    return _Account(
        name       = str(entry.get("name") or entry.get("uuid") or "principal").strip(),
        uuid       = str(entry.get("uuid") or _LEGACY_UUID).strip(),
        default    = bool(entry.get("default", False)),
        auto_start = bool(entry.get("auto_start", True)),
        notify     = bool(entry.get("notify", False)),
    )


def _load_accounts_from_config() -> list[dict]:
    try:
        from memory.config_manager import get_plugin_config
        cfg = get_plugin_config(NS) or {}
    except Exception:
        cfg = {}
    accts = cfg.get("accounts")
    if isinstance(accts, list) and accts:
        return [a for a in accts if isinstance(a, dict)]
    # Migração: o config antigo era uma conta só, com auto_start/notify na
    # raiz do namespace. Vira uma conta "principal" com o uuid histórico, pra
    # sessão.db existente continuar valendo.
    return [{
        "name": "principal", "uuid": _LEGACY_UUID, "default": True,
        "auto_start": bool(cfg.get("auto_start", True)),
        "notify": bool(cfg.get("notify", False)),
    }]


def _ensure_accounts() -> None:
    if _accounts:
        return
    for entry in _load_accounts_from_config():
        a = _entry_to_account(entry)
        _accounts[a.uuid] = a
    # The settings check is authoritative: it may have changed after the
    # accounts list was last written by pair/remove.
    try:
        from memory.config_manager import get_plugin_setting
        chosen = str(get_plugin_setting(NS, "default_account", "") or "").strip()
    except Exception:
        chosen = ""
    if chosen:
        key = chosen.lower()
        for a in _accounts.values():
            if a.uuid.lower() == key or a.name.lower() == key:
                for other in _accounts.values():
                    other.default = other is a
                break
    if _accounts and not any(a.default for a in _accounts.values()):
        next(iter(_accounts.values())).default = True


def default_account_name() -> str:
    """The account the assistant uses when the user did not name one.

    The settings check (plugin_config.whatsapp.default_account) wins; the
    per-account ``default`` flag is the fallback for older configs.
    """
    _ensure_accounts()
    try:
        from memory.config_manager import get_plugin_setting
        chosen = str(get_plugin_setting(NS, "default_account", "") or "").strip()
    except Exception:
        chosen = ""
    if chosen:
        key = chosen.lower()
        for a in _accounts.values():
            if a.uuid.lower() == key or a.name.lower() == key:
                return a.name
    for a in _accounts.values():
        if a.default:
            return a.name
    return ""


def _save_accounts() -> None:
    try:
        from memory.config_manager import save_plugin_config
        save_plugin_config(NS, {
            "accounts": [
                {"name": a.name, "uuid": a.uuid, "default": a.default,
                 "auto_start": a.auto_start, "notify": a.notify}
                for a in _accounts.values()
            ],
        })
    except Exception as e:
        _log(f"não consegui salvar a lista de contas: {e}")


def list_accounts() -> list[dict]:
    """As contas configuradas e seu estado ao vivo."""
    _ensure_accounts()
    out = []
    for a in _accounts.values():
        out.append({
            "name": a.name, "uuid": a.uuid, "default": a.default,
            "auto_start": a.auto_start, "notify": a.notify,
            "connected": a.connected,
            "paired": a.paired_mark.exists() and a.session_db.exists(),
            "number": a.me.get("number", ""),
            "error": a.error,
        })
    return out


def account_names() -> list[str]:
    _ensure_accounts()
    return [a.name for a in _accounts.values()]


def add_account(name: str) -> _Account:
    """Cria uma conta nova (ainda não pareada) e persiste a lista."""
    _ensure_accounts()
    safe = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "conta"
    uuid = f"jarvis-{safe}"
    i = 2
    while uuid in _accounts:
        uuid = f"jarvis-{safe}-{i}"; i += 1
    a = _Account(name=(name or safe).strip(), uuid=uuid, default=False,
                 auto_start=True, notify=False)
    _accounts[uuid] = a
    _save_accounts()
    return a


def remove_account(name_or_uuid: str) -> bool:
    """Para a conta e apaga sessão/inbox/mídia dela do disco."""
    _ensure_accounts()
    key = str(name_or_uuid or "").strip().lower()
    for uuid, a in list(_accounts.items()):
        if a.uuid.lower() != key and a.name.lower() != key:
            continue
        # Removing the outbound account is allowed after an explicit UI
        # confirmation: promote another account first so sends never lose a
        # destination.
        if a.default and len(_accounts) > 1:
            replacement = next(other for other in _accounts.values()
                               if other is not a)
            replacement.default = True
            try:
                from memory.config_manager import save_plugin_config
                save_plugin_config(NS, {"default_account": replacement.name})
            except Exception:
                pass
        _stop_account(a)
        del _accounts[uuid]
        try:
            shutil.rmtree(a.dir, ignore_errors=True)
            a.inbox_file.unlink(missing_ok=True)
            shutil.rmtree(a.media_dir, ignore_errors=True)
        except Exception:
            pass
        _save_accounts()
        if not _accounts:
            try:
                from memory.config_manager import save_plugin_config
                save_plugin_config(NS, {"default_account": ""})
            except Exception:
                pass
        return True
    return False


def update_account(name_or_uuid: str, new_name: str | None = None,
                   auto_start: bool | None = None,
                   notify: bool | None = None,
                   default: bool | None = None) -> bool:
    """Edit one paired account (name/flags/outbound). False when not found or
    when the new name collides with another account."""
    _ensure_accounts()
    key = str(name_or_uuid or "").strip().lower()
    target = next((a for a in _accounts.values()
                   if a.uuid.lower() == key or a.name.lower() == key), None)
    if target is None:
        return False
    if new_name is not None:
        name = str(new_name).strip()
        if not name:
            return False
        if any(other is not target and other.name.casefold() == name.casefold()
               for other in _accounts.values()):
            return False
        target.name = name
    if auto_start is not None:
        target.auto_start = bool(auto_start)
    if notify is not None:
        target.notify = bool(notify)
    if default is True:
        for other in _accounts.values():
            other.default = other is target
    elif default is False and target.default and len(_accounts) > 1:
        target.default = False
        replacement = next(other for other in _accounts.values()
                           if other is not target)
        replacement.default = True
    _save_accounts()
    if any(a.default and a is target for a in _accounts.values()):
        try:
            from memory.config_manager import save_plugin_config
            save_plugin_config(NS, {"default_account": target.name})
        except Exception:
            pass
    return True


def set_default_account(name_or_uuid: str) -> bool:
    _ensure_accounts()
    key = str(name_or_uuid or "").strip().lower()
    found = None
    for a in _accounts.values():
        hit = a.uuid.lower() == key or a.name.lower() == key
        a.default = hit
        if hit:
            found = a
    if found is not None:
        _save_accounts()
        try:
            from memory.config_manager import save_plugin_config
            save_plugin_config(NS, {"default_account": found.name})
        except Exception as e:
            _log(f"não consegui salvar a conta de saída: {e}")
    return found is not None


def _A(account: str | None = None) -> _Account:
    """Resolve `account` (nome, uuid, ou None=padrão) para um _Account.

    Com uma única conta configurada, qualquer valor — até um nome errado — cai
    nela: é o caso do usuário com um número só, e reclamar do nome seria pior
    que atender. Com várias, o nome precisa bater.
    """
    _ensure_accounts()
    if not _accounts:
        a = _Account("principal", _LEGACY_UUID, default=True)
        _accounts[a.uuid] = a
        return a
    if account is None or str(account).strip() == "":
        chosen = default_account_name()
        if chosen:
            for a in _accounts.values():
                if a.name == chosen:
                    return a
        for a in _accounts.values():
            if a.default:
                return a
        return next(iter(_accounts.values()))
    key = str(account).strip().lower()
    for a in _accounts.values():
        if a.uuid.lower() == key or a.name.lower() == key:
            return a
    if len(_accounts) == 1:
        return next(iter(_accounts.values()))
    raise KeyError(f"conta de WhatsApp '{account}' não existe; tenho: "
                   + ", ".join(a.name for a in _accounts.values()))


# ── utilidades ───────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    try:
        print(f"[WhatsApp] {msg}")
    except Exception:
        pass
    cb = _logger
    if callable(cb):
        try:
            cb(msg)
        except Exception:
            pass


def _norm(s: str) -> str:
    """Minúsculas e sem acento — 'João' e 'joao' precisam cair no mesmo contato."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).strip().lower()


def _digits(s: str) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _stamp_seconds(value: object) -> int:
    """Timestamp de mensagem em SEGUNDOS.

    O proto do neonize chama o campo de ``Timestamp`` e o whatsmeow o preenche
    em MILISSEGUNDOS (é o mesmo número que o pedido de HistorySync manda em
    ``oldestMsgTimestampMS``). Lido como segundos, todo evento parecia de 1970:
    nada entrava no inbox, mídia nenhuma era baixada, o aviso de sala nunca
    saía e o watcher via toda mensagem como velha — a "entrada morta" de
    01/11. Segundos continuam aceitos (eventos antigos e os testes).
    """
    try:
        value = int(value or 0)
    except Exception:
        value = 0
    if value <= 0:
        return int(time.time())
    while value > 10_000_000_000:      # em segundos, só depois de 2286
        value //= 1000
    return value


def _jid_user(jid) -> str:
    try:
        return str(getattr(jid, "User", "") or "")
    except Exception:
        return ""


def _jid_server(jid) -> str:
    try:
        return str(getattr(jid, "Server", "") or "")
    except Exception:
        return ""


def _chat_identity(src, from_me: bool) -> tuple[str, str]:
    """O outro lado do chat: (chat, lid).

    Depois da migração para LID o WhatsApp manda o chat como
    ``<número>@lid`` — um identificador interno que NÃO é telefone. O telefone
    (PN) vem no JID alternativo do evento: ``SenderAlt`` para quem escreveu e
    ``RecipientAlt`` para o destinatário de uma mensagem do próprio aparelho.
    Sem resolver isso, o chat do dono nunca batia com ``owner_chat`` nem com o
    número pareado: a mensagem dele era tratada como de terceiro e o assistente
    respondia que o circuito estava vivo sem ter visto nada.

    Devolve o telefone quando ele existe (é para lá que a resposta vai) e o
    LID à parte, só para o log.
    """
    chat_jid = getattr(src, "Chat", None)
    chat_user = _jid_user(chat_jid)
    chat_server = _jid_server(chat_jid)
    if chat_server not in ("", "s.whatsapp.net", "lid"):
        # Grupo (@g.us), status, lista de transmissão: o chat é ele mesmo.
        return f"{chat_user}@{chat_server}", ""
    alt = getattr(src, "RecipientAlt" if from_me else "SenderAlt", None)
    alt_user = _jid_user(alt)
    phone = ""
    if alt_user and _jid_server(alt) != "lid" and _digits(alt_user):
        phone = _digits(alt_user)
    if not phone and chat_server != "lid":
        phone = _digits(chat_user)
    return (phone or chat_user), (chat_user if chat_server == "lid" else "")


def account_number(account: str | None = None) -> str:
    """O número pareado desta conta, só dígitos ('' enquanto não conectou).

    É o "número do próprio assistente" naquela conta. O dono pode escrever no
    chat direto dele (o WhatsApp chama isso de "mensagem para você mesmo"):
    aí o outro lado do chat é este número, não o ``owner_chat`` do config.
    """
    try:
        a = _A(account)
    except KeyError:
        return ""
    with _lock:
        number = _digits(a.me.get("number") or "")
    if number:
        return number
    for entry in list_accounts():
        if entry.get("name") == a.name:
            return _digits(entry.get("number") or "")
    return ""


def sent_by_me(message_id: object) -> bool:
    """Este id é de uma mensagem que ESTE processo enviou?

    O evento de uma mensagem enviada pelo celular chega com ``from_me=True`` e
    é exatamente o que o dono quer que o assistente responda; já o eco de um
    envio do próprio assistente não pode reabrir a conversa (laço). O ID é a
    única coisa que separa os dois: ``send()``/``send_media()`` registram o ID
    devolvido pelo servidor em ``_deliveries`` antes de qualquer resposta.
    """
    mid = str(message_id or "").strip()
    if not mid:
        return False
    with _delivery_lock:
        return mid in _deliveries


def available() -> bool:
    """neonize instalado? (o import é caro e só interessa quando alguém conecta)"""
    import importlib.util
    try:
        return importlib.util.find_spec("neonize") is not None
    except Exception:
        return False


def paired(account: str | None = None) -> bool:
    """Já houve um pareamento bem-sucedido alguma vez (a sessão está em disco)."""
    try:
        a = _A(account)
    except KeyError:
        return False
    try:
        return a.paired_mark.exists() and a.session_db.exists()
    except Exception:
        return False


def status(account: str | None = None) -> dict:
    try:
        a = _A(account)
    except KeyError as e:
        return {"available": available(), "error": str(e), "account": str(account)}
    with _lock:
        alive = bool(a.thread and a.thread.is_alive())
        return {
            "available": available(),
            "running": alive,
            "connected": a.connected,
            "paired": a.paired_mark.exists() and a.session_db.exists(),
            "number": a.me.get("number", ""),
            "name": a.me.get("name", ""),
            "qr_path": a.qr_path if os.path.exists(a.qr_path or "") else "",
            "error": a.error,
            "ffmpeg": ffmpeg_ready(),
            "account": a.name,
        }


# ── o QR ─────────────────────────────────────────────────────────────────────

def _draw_qr(data, acct: _Account) -> bool:
    """O neonize entrega a referência crua do QR; quem desenha é a gente."""
    try:
        import segno
        code = data.decode() if isinstance(data, (bytes, bytearray)) else str(data)
        acct.dir.mkdir(parents=True, exist_ok=True)
        # Escuro sobre claro, borda larga: é o que a câmera de um celular
        # consegue ler na tela de um monitor sem precisar de zoom.
        segno.make(code, error="m").save(str(acct.qr_png), scale=10, border=4,
                                         dark="#000000", light="#ffffff")
    except Exception as e:
        acct.error = f"não consegui desenhar o QR: {e}"
        _log(f"[{acct.name}] {acct.error}")
        return False

    first = False
    with _lock:
        first = not acct.qr_path
        acct.qr_path = str(acct.qr_png)
        acct.qr_at = time.time()
    if first:
        _log(f"[{acct.name}] QR pronto em {acct.qr_png}")
        _open_qr(acct.qr_png)
    return True


def _open_qr(qr_png: Path) -> None:
    """Abre o QR no visualizador de imagens do sistema, uma vez por sessão.

    xdg-open roda como processo externo: não é chamada de GUI, então pode sair
    de qualquer thread. Se não existir, o usuário ainda tem o caminho no log.
    """
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    try:
        subprocess.Popen(["xdg-open", str(qr_png)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        _log(f"não consegui abrir o QR automaticamente ({e}) — abra {qr_png}")


def qr_age(account: str | None = None) -> float:
    try:
        a = _A(account)
    except KeyError:
        return -1.0
    with _lock:
        return time.time() - a.qr_at if a.qr_at else -1.0


# ── inbox ────────────────────────────────────────────────────────────────────

def _append_inbox(item: dict, acct: _Account) -> None:
    try:
        # The password command is consumed by main.py before the model, but
        # this inbox is written first. Persist a marker, never the secret; the
        # in-memory item keeps the text the gate needs.
        safe = item
        try:
            from core import owner_auth as _owner_auth
            if _owner_auth.is_password_command(item.get("text")):
                safe = dict(item)
                safe["text"] = _owner_auth.REDACTED
        except Exception:
            pass
        acct.inbox_file.parent.mkdir(parents=True, exist_ok=True)
        with _inbox_lock:
            with acct.inbox_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(safe, ensure_ascii=False) + "\n")
                acct.appended += 1
                if acct.appended >= INBOX_TRIM_EVERY:
                    acct.appended = 0
                    _trim_inbox(acct)
    except Exception as e:
        _log(f"não consegui gravar no inbox: {e}")


def _trim_inbox(acct: _Account) -> None:
    """Mantém só as INBOX_KEEP mensagens mais recentes no arquivo."""
    try:
        if not acct.inbox_file.exists():
            return
        lines = acct.inbox_file.read_text(encoding="utf-8").splitlines()
        if len(lines) <= INBOX_KEEP:
            return
        acct.inbox_file.write_text("\n".join(lines[-INBOX_KEEP:]) + "\n",
                                   encoding="utf-8")
    except Exception:
        pass


def _prune_media(acct: _Account, *, older_than_days: float | None = None,
                 now: float | None = None) -> int:
    """Apaga mídia baixada mais velha que MEDIA_KEEP_DAYS; devolve o total.

    Só arquivos regulares, só filhos diretos da pasta da conta. Symlink não é
    seguido nem removido, e um arquivo tocado no último minuto fica para a
    próxima passagem — mtime velho é a única prova aceita de que é descartável.
    """
    removed = 0
    try:
        media = acct.media_dir
        if media == media.parent or not media.is_dir():
            return 0
        days = MEDIA_KEEP_DAYS if older_than_days is None else float(older_than_days)
        moment = time.time() if now is None else float(now)
        cutoff = moment - max(0.0, days) * 86400.0
        for p in media.iterdir():
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                mtime = p.stat().st_mtime
                if mtime < cutoff and moment - mtime > 60.0:
                    p.unlink()
                    removed += 1
            except Exception:
                continue
    except Exception:
        pass
    return removed


def prune_media(account: str | None = None, *,
                older_than_days: float | None = None,
                now: float | None = None) -> int:
    """Limpeza real da mídia do WhatsApp; idempotente.

    Sem ``account`` passa por todas as contas configuradas. O ciclo de
    manutenção do audio_ledger chama esta função de hora em hora, e cada
    conexão bem-sucedida dispara uma passagem em segundo plano.
    """
    _ensure_accounts()
    if account is not None:
        try:
            targets = [_A(account)]
        except KeyError:
            return 0
    else:
        targets = list(_accounts.values())
    removed = 0
    for acct in targets:
        removed += _prune_media(acct, older_than_days=older_than_days,
                                now=now)
    if removed:
        _log(f"mídia do WhatsApp: {removed} arquivo(s) antigo(s) removido(s)")
    return removed


def _prune_media_async(acct: _Account) -> None:
    removed = _prune_media(acct)
    if removed:
        _log(f"[{acct.name}] mídia antiga: {removed} arquivo(s) removido(s)")


def _load_inbox(acct: _Account) -> list[dict]:
    try:
        if not acct.inbox_file.exists():
            return []
        out = []
        for ln in acct.inbox_file.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out
    except Exception:
        return []


def _with_media_path(item: dict, acct: _Account) -> dict:
    """Item de inbox + o caminho do arquivo, quando ele está no disco.

    Quem lê o inbox (a ferramenta, depois o modelo) precisa do arquivo EXATO
    para chamar a visão ou a transcrição — sem o campo `path` a foto baixada
    existe no disco mas é invisível para o fluxo, e a resposta vira suposição.
    Grupo nunca expõe caminho: mídia de grupo não é baixada nem lida.
    """
    try:
        media = item.get("media")
        if not isinstance(media, dict) or item.get("group"):
            return item
        found = media_path(item, acct.name)
        out = dict(item)
        out["media"] = ({**media, "path": found} if found
                        else {k: v for k, v in media.items() if k != "path"})
        return out
    except Exception:
        return item


def recent(limit: int = 10, only_incoming: bool = True,
           account: str | None = None) -> list[dict]:
    """As últimas mensagens recebidas, da mais nova para a mais antiga.

    Mídia baixada vem com ``media["path"]`` — o arquivo local pronto para ser
    lido pela visão (foto) ou pela transcrição (áudio).
    """
    try:
        a = _A(account)
    except KeyError:
        return []
    items = [m for m in _load_inbox(a) if not (only_incoming and m.get("from_me"))]
    return [_with_media_path(m, a)
            for m in list(reversed(items))[:max(1, int(limit or 10))]]


def search(query: str, limit: int = 8, account: str | None = None) -> list[dict]:
    """Procura no que chegou e no que foi enviado, por texto ou por contato."""
    try:
        a = _A(account)
    except KeyError:
        return []
    q = _norm(query)
    if not q:
        return []
    hits = [m for m in _load_inbox(a)
            if q in _norm(m.get("text", "")) or q in _norm(m.get("name", ""))]
    return [_with_media_path(m, a)
            for m in list(reversed(hits))[:max(1, int(limit or 8))]]


# ── contatos ─────────────────────────────────────────────────────────────────

def _contact_index(acct: _Account) -> list[dict]:
    """Catálogo nome → JID, em cache por 60s (ler o store é barato, mas não
    de graça, e o usuário costuma pedir dois envios seguidos)."""
    with _lock:
        client = acct.client
        fresh = time.time() - acct.contacts["at"] < 60.0
        if fresh and acct.contacts["items"]:
            return acct.contacts["items"]
    if client is None:
        return []
    try:
        raw = client.contact.get_all_contacts()
    except Exception as e:
        _log(f"[{acct.name}] catálogo de contatos indisponível: {e}")
        return []
    items = []
    for c in raw:
        try:
            jid = c.JID
            info = c.Info
            name = (info.FullName or info.FirstName or info.PushName
                    or info.BusinessName or "")
            if not name:
                continue
            items.append({"name": name, "norm": _norm(name), "jid": jid,
                          "user": jid.User})
        except Exception:
            continue
    with _lock:
        acct.contacts = {"at": time.time(), "items": items}
    return items


def resolve(recipient: str, account: str | None = None
            ) -> tuple[object | None, list[str]]:
    """De 'João' ou '11 99999-9999' para algo que o WhatsApp entenda.

    Devolve (destino, alternativas). Destino None com alternativas preenchidas
    significa ambiguidade: quem chama deve perguntar qual, não escolher sozinho.
    """
    try:
        a = _A(account)
    except KeyError:
        return None, []
    raw = str(recipient or "").strip()
    if not raw:
        return None, []

    # JID explícito de grupo: o "nome" do chat já é o endereço (`<id>@g.us`).
    # Sem isto o fallback de número mais abaixo montaria um JID de telefone com
    # os dígitos do grupo e a resposta do gatilho iria para o lugar errado.
    if raw.lower().endswith("@g.us"):
        user = raw.split("@", 1)[0]
        try:
            from neonize.utils import build_jid
            return build_jid(user, "g.us"), []
        except Exception:
            return raw, []

    digits = _digits(raw)
    letters = re.sub(r"[\d\s+().-]", "", raw)
    # Só dígitos (com ou sem +55, espaços, traços) → é um número, não um nome.
    if digits and not letters and len(digits) >= 8:
        try:
            from neonize.utils import build_jid
            return build_jid(digits), []
        except Exception:
            return digits, []

    q = _norm(raw)
    index = _contact_index(a)
    exact  = [c for c in index if c["norm"] == q]
    starts = [c for c in index if c["norm"].startswith(q)]
    subs   = [c for c in index if q in c["norm"]]

    for bucket in (exact, starts, subs):
        if len(bucket) == 1:
            return bucket[0]["jid"], []
        if len(bucket) > 1:
            names = [c["name"] for c in bucket][:5]
            return None, names

    # Nome desconhecido, mas o usuário soletrou um número no meio da frase.
    if len(digits) >= 10:
        try:
            from neonize.utils import build_jid
            return build_jid(digits), []
        except Exception:
            return digits, []
    return None, []


def contact_names(limit: int = 40, account: str | None = None) -> list[str]:
    try:
        a = _A(account)
    except KeyError:
        return []
    return [c["name"] for c in _contact_index(a)][:max(1, int(limit or 40))]



# ── envio ────────────────────────────────────────────────────────────────────

def _client_state(acct: _Account) -> tuple[object, bool, str]:
    with _lock:
        return acct.client, acct.connected, acct.error


# ── confirmação de entrega ───────────────────────────────────────────────────
#
# Sucesso de envio NÃO é "não deu exceção": o servidor devolve um ID e depois
# manda um recibo (entregue/lido). Guardamos o ID de cada envio aqui e o
# ReceiptEv atualiza o estado — é o que permite dizer "entregue" com prova.

# Valores de Receipt.Type no proto do neonize (sem importar neonize aqui).
_RECEIPT_DELIVERED = 1
_RECEIPT_READ      = 4
_RECEIPT_PLAYED    = 6


def _prune_deliveries(now: float | None = None) -> None:
    """Descarta estados antigos. Só é chamada com `_delivery_lock` na mão."""
    now = time.time() if now is None else now
    for mid in [m for m, rec in _deliveries.items()
                if now - rec["at"] > _DELIVERY_KEEP_S]:
        _deliveries.pop(mid, None)
    for mid, rec in list(_early_receipts.items()):
        if now - float(rec.get("at") or 0.0) > _EARLY_RECEIPT_KEEP_S:
            _early_receipts.pop(mid, None)


def _remember_early_receipt(msg_id: str, state: str) -> None:
    """Guarda um recibo de um ID que ainda não foi registrado (corrida).

    Chamado com `_delivery_lock` na mão. `_remember_send` consome esta entrada
    e já nasce sabendo que a mensagem foi entregue/lida.
    """
    rec = _early_receipts.setdefault(msg_id, {"at": time.time(),
                                              "delivered": False,
                                              "read": False})
    rec["at"] = time.time()
    if state == "read":
        rec["read"] = True
    if state in ("read", "delivered"):
        rec["delivered"] = True


def _remember_send(resp, acct: "_Account | None" = None) -> str:
    """Guarda o ID devolvido no SendResponse e devolve o ID (ou "")."""
    msg_id = getattr(resp, "ID", "")
    if not isinstance(msg_id, str) or not msg_id.strip():
        return ""
    msg_id = msg_id.strip()
    with _delivery_lock:
        _prune_deliveries()
        early = _early_receipts.pop(msg_id, None) or {}
        event = threading.Event()
        if early.get("delivered"):
            event.set()
        _deliveries[msg_id] = {
            "at": time.time(),
            "account": getattr(acct, "name", "") or "",
            "delivered": bool(early.get("delivered")),
            "read": bool(early.get("read")),
            "event": event,
        }
    return msg_id


def _receipt_state(evt) -> str:
    try:
        kind = int(getattr(evt, "Type", 0) or 0)
    except Exception:
        return ""
    if kind in (_RECEIPT_READ, _RECEIPT_PLAYED):
        return "read"
    if kind == _RECEIPT_DELIVERED:
        return "delivered"
    return ""


def _on_receipt(evt, acct: "_Account | None" = None) -> None:
    """ReceiptEv: o destinatário recebeu/leu alguma coisa que mandamos."""
    state = _receipt_state(evt)
    if not state:
        return
    try:
        ids = [str(mid) for mid in (getattr(evt, "MessageIDs", None) or [])]
    except Exception:
        return
    with _delivery_lock:
        for mid in ids:
            rec = _deliveries.get(mid)
            if rec is None:
                # O recibo pode chegar antes do send_* devolver o ID (a corrida
                # que deixava áudio entregue com "aguardando confirmação").
                _remember_early_receipt(mid, state)
                continue
            if state == "read":
                rec["read"] = True
                rec["delivered"] = True
            else:
                rec["delivered"] = True
            rec["event"].set()


def wait_delivery(msg_id: str, timeout: float | None = None) -> dict:
    """Espera o recibo de entrega/leitura de uma mensagem já enviada.

    Devolve {"id", "state", "delivered", "read", "waited"}. `state` é
    "delivered"/"read" quando o recibo chegou, "pending" no timeout e
    "unknown" para um ID que este processo não enviou (ou já esqueceu).
    Espera no máximo `timeout` segundos (padrão DELIVERY_WAIT_S).
    """
    mid = str(msg_id or "").strip()
    try:
        timeout = (DELIVERY_WAIT_S if timeout is None else max(0.0, float(timeout)))
    except Exception:
        timeout = DELIVERY_WAIT_S
    with _delivery_lock:
        rec = _deliveries.get(mid)
    if rec is None:
        return {"id": mid, "state": "unknown", "delivered": False,
                "read": False, "waited": 0.0}
    t0 = time.monotonic()
    rec["event"].wait(timeout)
    waited = round(time.monotonic() - t0, 3)
    read, delivered = bool(rec["read"]), bool(rec["delivered"])
    return {
        "id": mid,
        "state": "read" if read else ("delivered" if delivered else "pending"),
        "delivered": delivered,
        "read": read,
        "waited": waited,
    }


def _delivery_outcome(msg_id: str, sent_phrase: str, label: str,
                      timeout: float | None = None) -> tuple[bool, str]:
    """Frase falada com o estado REAL do envio — nunca "mandei" sem ID."""
    if not msg_id:
        return False, (f"o envio para {label} não foi confirmado pelo WhatsApp: "
                       f"nenhum identificador de mensagem voltou")
    result = wait_delivery(msg_id, timeout)
    if result["state"] in ("delivered", "read"):
        return True, (f"{sent_phrase} e o WhatsApp confirmou a entrega "
                      f"para {label}")
    return True, (f"{sent_phrase} para {label}, aguardando confirmação "
                  f"de entrega")


def current_speaker() -> str:
    """O alto-falante atual: o `assistant_name` do config (Gino, Aurora…).

    É o mesmo nome que o app usa em `_asst_name`, agora resolvido fora dele para
    as ações de ferramenta (plugins/actions) assinarem sem hardcode. Uma persona
    que responde informa o próprio nome em `speaker=`.
    """
    try:
        from memory.config_manager import get_assistant_name
        name = str(get_assistant_name() or "").strip()
    except Exception:
        name = ""
    return name or "Gino"


def persona_send_block(speaker: str, account: str | None) -> str:
    """Motivo de recusa de envio ("" = pode enviar) para quem assina.

    Regra do dono (09/11/2026): persona sem número direto NÃO envia por conta
    nenhuma. As palavras dela saem só pela conta principal do assistente — o
    chat onde o dono a chama pelo nome. Quem tem ``direct_whatsapp`` declarado
    envia pela conta dela como sempre; nome que não é persona cadastrada (o
    próprio assistente) nunca é bloqueado. Sem cadastro legível, nada é
    bloqueado: a recusa exige a prova do cadastro, nunca um palpite.
    """
    name = str(speaker or "").strip()
    wanted = str(account or "").strip()
    if not name or not wanted:
        # Sem assinatura ou sem conta pedida: é o chat do assistente.
        return ""
    if name.casefold() == current_speaker().casefold():
        return ""
    try:
        from core import personas as _personas
        persona = _personas.get(name)
    except Exception:
        return ""
    if not isinstance(persona, dict) or _personas.own_account(persona):
        return ""
    try:
        canonical = _personas._resolve_whatsapp_account(wanted).casefold()
    except Exception:
        canonical = wanted.casefold()
    if canonical == _personas.assistant_account().casefold():
        return ""
    return (f"{name} não tem WhatsApp próprio: ela responde quando o dono "
            f"chama pelo nome no chat do assistente")


def signed_text(text: str, speaker: str = "Gino") -> str:
    """Mensagem ESCRITA leva o nome de quem fala; áudio nunca leva.

    Regra do dono (03/11/2026): no texto, a primeira linha é ``*Gino:*`` (ou o
    nome da persona) sozinha; na voz o nome não é dito — a voz já identifica.
    Se a assinatura já estiver lá, não duplica.
    """
    body = str(text or "").strip()
    name = str(speaker or "").strip() or "Gino"
    if not body:
        return body
    try:
        from core import speech_guard as _guard
        # O texto pode chegar já assinado por outro nome do mesmo app (a
        # persona atual): reconhecer os dois evita ``*Aline:*\n*Gino:*``.
        if _guard.strip_speaker(body, (name, current_speaker())) != body:
            return body
    except Exception:
        pass
    return f"*{name}:*\n{body}"


def _avoid_url_cut(text: str, cut: int) -> int:
    """Move um corte que caiu dentro de URL para antes/depois dela inteira."""
    for m in _URL_ANY_RE.finditer(text):
        if m.start() < cut < m.end():
            # URL maior que o limite: mantê-la inteira vale mais que o limite.
            return m.start() if m.start() > 0 else m.end()
        if m.start() >= cut:
            break
    return cut


def _split_cut(text: str, limit: int) -> int:
    """Índice do melhor corte de `text` que não passe de `limit`.

    Prioridade: fim de frase → espaço → palavra inteira (mesmo que a parte
    fique maior que o limite). Nunca corta no meio de palavra nem de URL.
    """
    cut = 0
    for m in _SENTENCE_CUT_RE.finditer(text):
        end = m.end()
        if end > limit:
            break
        if end >= max(1, limit // 4):
            cut = end
    if cut:
        return _avoid_url_cut(text, cut)
    window = text[:limit]
    ws = max(window.rfind(" "), window.rfind("\n"), window.rfind("\t"))
    if ws > 0:
        return _avoid_url_cut(text, ws)
    # Palavra/URL sozinha maior que o limite: vai inteira na parte.
    nxt = min((i for i in (text.find(" ", limit), text.find("\n", limit),
                           text.find("\t", limit)) if i >= 0), default=-1)
    return _avoid_url_cut(text, nxt) if nxt > 0 else len(text)


def split_text(text: str, limit: int = MAX_TEXT_CHARS) -> list[str]:
    """Divide um texto longo em partes legíveis, sem perder um caractere.

    A concatenação das partes (com um espaço entre elas) reconstrói o texto
    original a menos de espaços nas bordas. Cada parte termina em pontuação
    real quando existe uma fronteira de frase disponível; nenhuma parte ganha
    "..." artificial.
    """
    body = str(text or "").strip()
    if not body:
        return []
    if limit <= 0 or len(body) <= limit:
        return [body]
    parts: list[str] = []
    rest = body
    while len(rest) > limit:
        cut = _split_cut(rest, limit)
        if cut <= 0 or cut >= len(rest):
            break
        part = rest[:cut].strip()
        rest = rest[cut:].strip()
        if part:
            parts.append(part)
    if rest:
        parts.append(rest)
    return parts or [body]


def _number_parts(parts: list[str]) -> list[str]:
    """Numera as partes; a assinatura, se houver, fica sozinha na 1ª linha."""
    total = len(parts)
    first = parts[0]
    m = re.match(r"^(\*[^*\n]+:\*)\n", first)
    if m:
        parts[0] = f"{m.group(1)}\n(1/{total}) {first[m.end():]}"
    else:
        parts[0] = f"(1/{total}) {first}"
    for i in range(1, total):
        parts[i] = f"({i + 1}/{total}) {parts[i]}"
    return parts


def send(recipient: str, text: str, account: str | None = None,
         speaker: str | None = None,
         signed: bool = True) -> tuple[bool, str]:
    """Envia texto. Devolve (ok, frase) — a frase é o que o assistente fala.

    PONTO ÚNICO da assinatura do TEXTO: nenhum chamador monta ``*Nome:*`` por
    conta própria. ``signed=True`` (padrão) assina o que o assistente produziu;
    ``speaker`` escolhe o nome (persona) e vazio usa o alto-falante atual.
    ``signed=False`` é para o texto DITADO pelo dono para outra pessoa: ali a
    mensagem é dele e sai sem assinatura nenhuma. Áudio/caption não passam por
    aqui e nunca levam nome.

    Texto maior que ``MAX_TEXT_CHARS`` sai em partes numeradas ``(1/N)``, cada
    uma quebrada em fronteira de frase/palavra — o conteúdo inteiro chega, sem
    corte e sem reticências inventadas.
    """
    text = str(text or "").strip()
    if not text:
        return False, "a mensagem está vazia"
    # Rota de saída de persona (regra do dono, 09/11/2026): quem não tem
    # número direto não envia por conta que não é a do assistente. A recusa
    # acontece antes de qualquer resolução de destino — nada sai, nada é
    # assinado, nada é inventado.
    blocked = persona_send_block(speaker or current_speaker(), account)
    if blocked:
        return False, blocked
    parts = split_text(text, MAX_TEXT_CHARS)
    if signed:
        parts[0] = signed_text(parts[0], speaker or current_speaker())
    if len(parts) > 1:
        parts = _number_parts(parts)
    try:
        a = _A(account)
    except KeyError as e:
        return False, str(e)

    client, connected, err = _client_state(a)
    if not available():
        return False, ("o WhatsApp não está ligado: falta instalar o neonize "
                       "(pip install neonize)")
    if client is None or not connected:
        hint = ("pareie em ⚙ → PLUGIN SETTINGS → WHATSAPP (ações: pair)"
                if not paired(a.name) else "o WhatsApp ainda está reconectando")
        return False, f"{hint}{(' — ' + err) if err else ''}"

    target, choices = resolve(recipient, a.name)
    if target is None:
        if choices:
            return False, ("achei mais de um contato com esse nome: "
                           + ", ".join(choices) + ". Diga qual.")
        return False, (f"não achei '{recipient}' na agenda do WhatsApp. "
                       f"Diga o número com DDD, por favor.")

    # O rótulo é o nome de verdade do contato; um número desconhecido cai no
    # que o usuário disse, que é o melhor nome disponível.
    label = _display(target, a) or recipient
    ids: list[str] = []
    for index, part in enumerate(parts, 1):
        try:
            resp = client.send_message(target, part)
        except Exception as e:
            if len(parts) > 1:
                return False, (f"o WhatsApp recusou a parte {index}/{len(parts)} "
                               f"do envio para {label}: {e}")
            return False, f"o WhatsApp recusou o envio para {label}: {e}"
        msg_id = _remember_send(resp, a)
        ids.append(msg_id)
        _append_inbox({"ts": int(time.time()), "chat": _user_of(target),
                       "name": str(label), "from_me": True, "group": False,
                       "text": part[:600], "id": msg_id}, a)
    phrase = ("enviei a mensagem" if len(parts) == 1
              else f"enviei a mensagem em {len(parts)} partes")
    return _delivery_outcome(ids[-1], phrase, label)


def _user_of(jid) -> str:
    try:
        return jid.User
    except Exception:
        return _digits(str(jid))


# ── mídia ────────────────────────────────────────────────────────────────────
#
# Duas metades: reconhecer/guardar o que chega (o download é feito na hora,
# porque o link expira e o servidor do WhatsApp cobra por pedir de novo) e
# mandar arquivo escolhido pelo usuário. O texto do inbox fica sempre legível —
# "[foto]", "[áudio de 12s]" — para o modelo poder falar sobre isso sem ter
# que abrir arquivo nenhum.

def ffmpeg_ready() -> bool:
    """O neonize só converte/mede mídia se achar ffmpeg E ffprobe no PATH."""
    _ensure_ffmpeg()
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


# campo do protobuf → (tipo falado, extensão provável)
_MEDIA_FIELDS = (
    ("imageMessage", "foto", ".jpg"),
    ("videoMessage", "vídeo", ".mp4"),
    ("ptvMessage", "vídeo", ".mp4"),
    ("audioMessage", "áudio", ".ogg"),
    ("documentMessage", "documento", ""),
    ("stickerMessage", "figurinha", ".webp"),
)

# chegam embrulhados: some da tela depois de ver / conversa de visualização única
_ENVELOPES = ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2",
              "viewOnceMessageV2Extension", "documentWithCaptionMessage")

_EXT_BY_MIME = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "video/mp4": ".mp4", "video/3gpp": ".3gp",
    "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
    "audio/aac": ".aac", "application/pdf": ".pdf",
}

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".heic"}
_VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".3gp", ".avi"}
_AUDIO_EXT = {".ogg", ".opus", ".mp3", ".m4a", ".wav", ".aac", ".flac", ".amr"}


def _unwrap(msg):
    """Tira o embrulho (efêmera / ver uma vez / legenda de documento)."""
    for _ in range(4):
        inner = None
        for name in _ENVELOPES:
            try:
                if msg.HasField(name) and getattr(msg, name).HasField("message"):
                    inner = getattr(msg, name).message
                    break
            except Exception:
                continue
        if inner is None:
            return msg
        msg = inner
    return msg


def _media_of(msg) -> dict | None:
    """Qual mídia esta mensagem carrega — ou None, se for só texto."""
    if msg is None:
        return None
    msg = _unwrap(msg)
    for field, kind, ext in _MEDIA_FIELDS:
        try:
            if not msg.HasField(field):
                continue
        except Exception:
            continue
        body = getattr(msg, field)
        mime = (getattr(body, "mimetype", "") or "").split(";")[0].strip().lower()
        ext = _EXT_BY_MIME.get(mime, ext)
        name = ""
        if kind == "documento":
            name = (getattr(body, "fileName", "") or "").strip()
            if name:
                ext = Path(name).suffix.lower() or ext
        if kind == "áudio" and (getattr(body, "ptt", False)
                                or getattr(body, "PTT", False)):
            kind = "mensagem de voz"
        return {"kind": kind, "mime": mime, "name": name,
                "seconds": int(getattr(body, "seconds", 0) or 0),
                "size": int(getattr(body, "fileLength", 0) or 0),
                "ext": ext}
    return None


def _spoken_media(media: dict) -> str:
    """'foto' · 'áudio de 12s' · 'documento contrato.pdf' — para o inbox."""
    kind = media.get("kind") or "mídia"
    if kind in ("áudio", "mensagem de voz") and media.get("seconds"):
        return f"{kind} de {int(media['seconds'])}s"
    if kind == "documento" and media.get("name"):
        return f"documento {media['name']}"
    return kind


def _slug(text: str) -> str:
    s = _norm(text)
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:24] or "whatsapp"


def _media_filename(item: dict) -> str:
    """Nome estável: dá para reencontrar o arquivo só com o que está no inbox."""
    media = item.get("media") or {}
    ext = media.get("ext") or ".bin"
    return (f"{int(item.get('ts') or 0)}_{_slug(item.get('name') or '')}"
            f"_{item.get('id') or 'semid'}{ext}")


def media_dest(item: dict, account: str | None = None) -> str:
    """O caminho ONDE a mídia desta mensagem vai ficar — exista o arquivo ou não.

    O download roda numa thread separada do aviso de mensagem nova: quem
    precisa transcrever um áudio tem que esperar por este caminho, porque
    ``media_path`` devolve "" enquanto o arquivo ainda não apareceu.
    """
    try:
        a = _A(account)
    except KeyError:
        return ""
    return str(a.media_dir / _media_filename(item))


def media_path(item: dict, account: str | None = None) -> str:
    """O arquivo local desta mensagem, se ele foi baixado."""
    p = media_dest(item, account)
    return p if p and Path(p).exists() else ""


def download_wanted() -> bool:
    try:
        from memory.config_manager import get_plugin_setting
        return bool(get_plugin_setting(NS, "media_download", True))
    except Exception:
        return True


def _media_saved(item: dict, dest: Path, *, already: bool = False) -> None:
    """Registra no item o arquivo que está no disco — e avisa no log.

    O caminho dentro do item é o elo que faltava entre "chegou uma foto" e
    "descreva essa foto": sem ele a mensagem é só o rótulo ``[foto]`` e
    qualquer resposta sobre o conteúdo sai da imaginação do modelo, não do
    arquivo. ``recent()``/``search()`` devolvem este mesmo caminho para quem
    processa o arquivo (visão, PDF, transcrição).

    O log não escreve caminho nem número: a linha é só o tipo e o tamanho,
    porque o mesmo texto pode ser lido em voz alta.
    """
    media = item.get("media")
    if not isinstance(media, dict):
        return
    media = {**media, "path": str(dest)}
    item["media"] = media
    try:
        kb = dest.stat().st_size // 1024
    except OSError:
        kb = 0
    _log(f"{_spoken_media(media)} salva para processamento ({kb} KB)"
         + (" — já estava baixada" if already else ""))


def _download_media(client, message, item: dict, acct: _Account) -> None:
    """Baixa o anexo para a pasta de mídia da conta (link do WhatsApp expira)."""
    media = item.get("media") or {}
    if not item.get("id") or not download_wanted():
        return
    size = int(media.get("size") or 0)
    if size and size > MEDIA_MAX_MB * 1024 * 1024:
        _log(f"{_spoken_media(media)} não baixada: {size // 1048576} MB "
             f"(acima do limite de {MEDIA_MAX_MB} MB)")
        return
    dest = acct.media_dir / _media_filename(item)
    if dest.exists():
        _media_saved(item, dest, already=True)
        return
    try:
        acct.media_dir.mkdir(parents=True, exist_ok=True)
        client.download_any(message, str(dest))
        if dest.exists() and dest.stat().st_size == 0:
            dest.unlink()
    except Exception as e:
        _log(f"não consegui baixar {_spoken_media(media)}: {e}")
        try:
            if dest.exists():
                dest.unlink()
        except Exception:
            pass
        return
    if dest.exists():
        _media_saved(item, dest)


def _find_local(hint: str, acct: _Account) -> Path | None:
    """Acha um arquivo já baixado pelo nome do contato ou pelo tipo falado."""
    try:
        if not acct.media_dir.exists():
            return None
        want = _norm(hint)
        if not want:
            return None
        best = None
        for p in acct.media_dir.iterdir():
            if p.is_file() and want.replace(" ", "") in _norm(p.name).replace("_", ""):
                if best is None or p.stat().st_mtime > best.stat().st_mtime:
                    best = p
        return best
    except Exception:
        return None


def _resolve_file(path: str, acct: _Account) -> Path | None:
    """Caminho do usuário → arquivo de verdade (absoluto, ~, relativo, ou baixado)."""
    raw = str(path or "").strip().strip('"').strip("'")
    if not raw:
        return None
    p = Path(os.path.expandvars(raw)).expanduser()
    for cand in (p, _BASE / p, Path.cwd() / p):
        try:
            if cand.is_file():
                return cand
        except Exception:
            continue
    return _find_local(raw, acct)


# O WhatsApp só reconhece como mensagem de voz (PTT) um áudio OGG/Opus cujo
# mimetype traz o codec EXPLÍCITO. O neonize monta o AudioMessage com
# `magic.from_buffer(...)`, que devolve "audio/ogg" puro — o celular do
# destinatário então não exibe/entrega o áudio (o dispositivo pareado, que não
# valida o mimetype, baixa normalmente, o que já enganou uma autópsia antes).
_AUDIO_MIME_PTT = "audio/ogg; codecs=opus"


def _audio_waveform(file: Path) -> bytes:
    """64 amostras (0..100) do PCM, como o WhatsApp grava no PTT.

    O campo é opcional, mas o cliente do celular usa a forma de onda para
    desenhar a mensagem de voz; mandar a barra vazia é mais um desvio do
    formato canônico. Falha aqui nunca impede o envio: devolve b"".
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return b""
    try:
        r = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(file), "-t", "120",
             "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1",
             "-ar", "8000", "pipe:1"],
            capture_output=True, timeout=30)
        pcm = r.stdout or b""
        if len(pcm) < 128:
            return b""
        import array
        samples = array.array("h")
        samples.frombytes(pcm[:len(pcm) // 2 * 2])
        step = max(1, len(samples) // 64)
        peaks = []
        for i in range(64):
            chunk = samples[i * step:(i + 1) * step]
            peak = max((abs(v) for v in chunk), default=0)
            peaks.append(min(100, int(peak * 100 / 32768)))
        return bytes(peaks)
    except Exception:
        return b""


def _build_audio_message(client, file: Path, ptt: bool):
    """AudioMessage do neonize com mimetype/waveform canônicos de PTT.

    Devolve None quando o cliente não expõe `build_audio_message` (versões
    antigas/testes) — quem chamou cai no `send_audio` normal.
    """
    build = getattr(client, "build_audio_message", None)
    if not callable(build):
        return None
    msg = build(str(file), ptt=ptt)
    audio = getattr(msg, "audioMessage", None)
    if audio is None:
        return None
    if ptt:
        audio.mimetype = _AUDIO_MIME_PTT
        waveform = _audio_waveform(file)
        if waveform:
            audio.waveform = waveform
    return msg


def _send_any(client, target, file: Path, caption) -> tuple[str, object]:
    """Manda o arquivo pelo tipo certo. Devolve (tipo falado, SendResponse)."""
    ext = file.suffix.lower()
    if ext in _IMAGE_EXT:
        resp = client.send_image(target, str(file), caption=caption)
        return "foto", resp
    if ext in _VIDEO_EXT:
        resp = client.send_video(target, str(file), caption=caption)
        return "vídeo", resp
    if ext in _AUDIO_EXT:
        # PTT é o "segurar e falar": só faz sentido em ogg/opus.
        ptt = ext in (".ogg", ".opus")
        msg = None
        try:
            msg = _build_audio_message(client, file, ptt)
        except Exception as e:
            _log(f"não consegui preparar o áudio como PTT ({e}); "
                 f"tentando o caminho padrão do neonize")
        if msg is not None:
            _log(f"áudio {file.name}: "
                 f"mime={getattr(msg.audioMessage, 'mimetype', '')!r} "
                 f"ptt={ptt}")
            resp = client.send_message(target, msg)
        else:
            _log(f"áudio {file.name}: caminho padrão do neonize "
                 f"(sem mimetype PTT corrigido)")
            resp = client.send_audio(target, str(file), ptt=ptt)
        return "áudio", resp
    if ext == ".webp":
        resp = client.send_sticker(target, str(file))
        return "figurinha", resp
    resp = client.send_document(target, str(file), caption=caption,
                                filename=file.name)
    return "documento", resp


def _convert_for_whatsapp(src: Path, video: bool) -> Path | None:
    """Container/codec fora do padrão (mkv, avi, flac…) → mp4 ou ogg/opus."""
    if not ffmpeg_ready():
        return None
    import tempfile
    ext = ".mp4" if video else ".ogg"
    out = Path(tempfile.gettempdir()) / f"wa_{int(time.time())}_{_slug(src.stem)}{ext}"
    codec = (["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
              "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
              "-movflags", "+faststart"] if video
             else ["-vn", "-c:a", "libopus", "-b:a", "48k"])
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src)]
                           + codec + [str(out)],
                           capture_output=True, timeout=600)
        if r.returncode == 0 and out.exists() and out.stat().st_size:
            return out
        _log(f"conversão para {ext} falhou: "
             f"{(r.stderr or b'').decode('utf-8', 'replace')[:200]}")
    except Exception as e:
        _log(f"conversão para {ext} falhou: {e}")
    return None


def send_media(recipient: str, path: str, caption: str = "",
               account: str | None = None) -> tuple[bool, str]:
    """Envia foto, vídeo, áudio, figurinha ou documento."""
    try:
        a = _A(account)
    except KeyError as e:
        return False, str(e)
    client, connected, err = _client_state(a)
    if not available():
        return False, ("o WhatsApp não está ligado: falta instalar o neonize "
                       "(pip install neonize)")
    if client is None or not connected:
        hint = ("pareie em ⚙ → PLUGIN SETTINGS → WHATSAPP (ações: pair)"
                if not paired(a.name) else "o WhatsApp ainda está reconectando")
        return False, f"{hint}{(' — ' + err) if err else ''}"

    file = _resolve_file(path, a)
    if file is None:
        return False, (f"não achei o arquivo '{path}'. Diga o caminho completo "
                       f"ou mande o nome de um arquivo que chegou no WhatsApp.")
    if not str(recipient or "").strip():
        return False, "para quem? diga o contato ou o número"

    target, choices = resolve(recipient, a.name)
    if target is None:
        if choices:
            return False, ("achei mais de um contato com esse nome: "
                           + ", ".join(choices) + ". Diga qual.")
        return False, (f"não achei '{recipient}' na agenda do WhatsApp. "
                       f"Diga o número com DDD, por favor.")

    label = _display(target, a) or recipient
    ext = file.suffix.lower()
    caption = str(caption or "").strip() or None
    try:
        kind, resp = _send_any(client, target, file, caption)
    except Exception as e:
        # Container que o WhatsApp não engole (mkv, avi, flac…) tem conserto
        # local: o ffmpeg converte e a gente tenta de novo, uma vez.
        converted = None
        if ext in _VIDEO_EXT or ext in _AUDIO_EXT:
            converted = _convert_for_whatsapp(file, video=ext in _VIDEO_EXT)
        if converted is None:
            return False, f"o WhatsApp recusou o envio para {label}: {e}"
        try:
            kind, resp = _send_any(client, target, converted, caption)
            file = converted
            ext = converted.suffix.lower()
        except Exception as e2:
            return False, f"o WhatsApp recusou o envio para {label}: {e2}"

    msg_id = _remember_send(resp, a)
    if kind == "áudio":
        # Sem o ID no log, "áudio enviado" era indistinguível de um envio sem
        # resposta; o mimetype efetivo é registrado em _send_any.
        _log(f"áudio enviado id={msg_id or '(sem id)'} "
             f"arquivo={file.name}")
    item = {"ts": int(time.time()), "chat": _user_of(target), "name": str(label),
            "from_me": True, "group": False,
            "text": f"[{kind}{(': ' + caption) if caption else ''}]",
            "id": msg_id, "media": {"kind": kind, "name": file.name, "ext": ext}}
    _append_inbox(item, a)
    artigo = "a" if kind in ("foto", "figurinha") else "o"
    return _delivery_outcome(msg_id, f"mandei {artigo} {kind}", label)


def open_media(recipient: str = "", hint: str = "",
               account: str | None = None) -> tuple[bool, str]:
    """Abre no visualizador do sistema a última mídia recebida (de alguém)."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False, "não tem tela aqui para abrir o arquivo"
    try:
        a = _A(account)
    except KeyError as e:
        return False, str(e)

    want = _norm(recipient or "")
    found = ""
    for item in reversed(_load_inbox(a)):
        if not item.get("media"):
            continue
        if want and want not in _norm(item.get("name", "")):
            continue
        found = media_path(item, a.name)
        if found:
            break
    if not found:
        local = _find_local(hint or recipient, a)
        if local is not None:
            found = str(local)
    if not found:
        if want:
            return False, (f"não tenho o arquivo de {recipient} aqui — pode ser "
                           f"que ainda esteja baixando, ou que fosse grande demais.")
        return False, "não achei nenhuma mídia recente guardada aqui"

    try:
        subprocess.Popen(["xdg-open", found],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return False, f"achei o arquivo em {found}, mas não consegui abrir ({e})"
    return True, f"abri o arquivo {os.path.basename(found)}"


def _display(jid, acct: _Account) -> str:
    user = _user_of(jid)
    for c in _contact_index(acct):
        if c["user"] == user:
            return c["name"]
    return ""


# ── eventos do neonize (fechados por conta) ──────────────────────────────────

def _set_paired(acct: _Account) -> None:
    try:
        acct.dir.mkdir(parents=True, exist_ok=True)
        acct.paired_mark.write_text(time.strftime("%Y-%m-%d %H:%M:%S\n"),
                                    encoding="utf-8")
    except Exception:
        pass


def _on_connected(client, _evt, acct: _Account) -> None:
    number, name = "", ""
    try:
        dev = client.me or client.get_me()
        number = getattr(dev.JID, "User", "") or ""
        name = getattr(dev, "PushName", "") or ""
    except Exception:
        pass
    with _lock:
        acct.connected = True
        acct.error = ""
        acct.qr_path = ""
        acct.qr_at = 0.0
        acct.me["number"] = number
        acct.me["name"] = name
    _set_paired(acct)
    _log(f"[{acct.name}] conectado como {name or 'sem nome'} "
         f"({number or 'número desconhecido'})")
    # Limpeza fora do handler: a pasta pode ter gigabytes de anexos velhos e o
    # evento de conexão não deve esperar por I/O.
    try:
        threading.Thread(target=_prune_media_async, args=(acct,),
                         name=f"jarvis-whatsapp-prune-{acct.uuid}",
                         daemon=True).start()
    except Exception:
        pass


def _on_disconnected(_client, _evt, acct: _Account) -> None:
    with _lock:
        acct.connected = False
    _log(f"[{acct.name}] desconectado — tentando de novo em instantes")


def _on_logged_out(_client, _evt, acct: _Account) -> None:
    """O celular removeu este aparelho (ou alguém saiu da conta)."""
    with _lock:
        acct.connected = False
        acct.error = ("o celular desconectou este aparelho — pareie de novo "
                      "(⚙ → PLUGIN SETTINGS → WHATSAPP)")
    try:
        acct.paired_mark.unlink()
    except Exception:
        pass
    _log(f"[{acct.name}] {acct.error}")


def _on_stream_replaced(client, _evt, acct: _Account) -> None:
    """Outro processo/aparelho tomou o stream desta sessão.

    O whatsmeow não derruba o `connect()` nesse caso: o app continua dizendo
    "conectado" mas não recebe mais nada (foi o que aconteceu no diagnóstico
    de "replaced stream error"). Aqui marcamos a queda e pedimos a desconexão
    de verdade, uma vez por client, para o laço de `_run` reconectar.
    """
    with _lock:
        acct.connected = False
        acct.error = ("a sessão foi substituída por outro processo/aparelho — "
                      "reconectando")
    _log(f"[{acct.name}] {acct.error}")
    if getattr(client, "_jarvis_stream_replaced", False):
        return
    try:
        client._jarvis_stream_replaced = True
    except Exception:
        pass

    def _force_disconnect():
        # Deixa o handler do Go voltar antes de chamar o Go de novo; o
        # `Disconnect` é o que faz o `connect()` bloqueado retornar.
        time.sleep(0.5)
        for step in ("disconnect", "stop"):
            try:
                getattr(client, step)()
            except Exception:
                pass

    threading.Thread(target=_force_disconnect,
                     name=f"jarvis-whatsapp-reconnect-{acct.name}",
                     daemon=True).start()


def _chat_name(chat_user: str, pushname: str, is_group: bool,
               acct: _Account) -> str:
    if is_group:
        return f"grupo {str(chat_user).split('@')[0]}"
    for c in _contact_index(acct):
        if c["user"] == chat_user:
            return c["name"]
    return pushname or chat_user


def _is_direct_chat(acct: _Account, item: dict) -> bool:
    """A conversa é o chat direto do dono nesta conta?

    A regra de "chat direto" mora em core/whatsapp_watch.py (número pareado da
    própria conta ou o ``owner_chat`` da conta de saída). Import tardio porque
    o watcher é quem importa este módulo; consultar aqui evita duas definições
    de "chat do dono" que possam divergir.
    """
    try:
        from core import whatsapp_watch
        return bool(whatsapp_watch.is_direct(acct.name, item))
    except Exception:
        return False


def _is_owner_turn(acct: _Account, item: dict) -> bool:
    """A mensagem é do chat direto do dono (= turno, não aviso de terceiro)?"""
    try:
        from core import whatsapp_watch
        return bool(whatsapp_watch.armed() and _is_direct_chat(acct, item))
    except Exception:
        return False


def _calls_assistant(acct: _Account, item: dict) -> bool:
    """O texto nomeia o assistente (ou menciona o número desta conta)?"""
    try:
        from core import whatsapp_watch
        return bool(whatsapp_watch.armed()
                    and whatsapp_watch.calls_assistant(item, acct.name))
    except Exception:
        return False


def _is_owner_order(acct: _Account, item: dict, direct: bool) -> bool:
    """A mensagem abre UM turno do dono (e por isso é deduplicada por id).

    Duas formas: o chat direto (qualquer mensagem dele) e o eco do próprio
    aparelho num chat individual de terceiro quando o texto chama o
    assistente pelo nome — o dono escrevendo "Gino, fala pra Carol..." no chat
    da Carol. Grupo nunca entra no fluxo, então também nunca abre turno.
    """
    if direct:
        return True
    if not item.get("from_me") or item.get("group"):
        return False
    return _calls_assistant(acct, item)


def _on_message(client, ev, acct: _Account) -> None:
    try:
        from neonize.utils import extract_text
    except Exception as e:
        # Antes isto era um `return` mudo: uma versão de neonize sem
        # extract_text fazia TODA mensagem sumir sem uma linha no log.
        _log(f"[{acct.name}] não consegui importar extract_text do neonize: {e}")
        return
    try:
        src   = ev.Info.MessageSource
        group = bool(getattr(src, "IsGroup", False))
        from_me = bool(getattr(src, "IsFromMe", False))
        chat, lid = _chat_identity(src, from_me)
        group = group or chat.endswith("g.us")
        stamp = _stamp_seconds(ev.Info.Timestamp)
        try:
            text = (extract_text(ev.Message) or "").strip()
        except Exception:
            text = ""
        media = _media_of(ev.Message)
        if media:
            # O texto do inbox tem que dizer o que é, sem abrir arquivo: é
            # assim que ele responde "a Maria mandou foto?" sem adivinhar.
            rotulo = f"[{_spoken_media(media)}]"
            text = f"{rotulo} {text}".strip() if text else rotulo
        elif not text:
            text = "[mensagem sem texto: reação ou evento]"

        item = {
            "ts": stamp,
            "chat": chat,
            "chat_lid": lid,
            "name": _chat_name(chat, ev.Info.Pushname or "", group, acct),
            "from_me": from_me,
            "group": group,
            # Inteiro: uma mensagem longa do dono não pode perder o fim antes
            # de virar turno. O inbox em disco guarda só as últimas mensagens,
            # então o tamanho continua contido.
            "text": text,
            "id": ev.Info.ID or "",
            "media": media,
        }
        # Rastro mínimo de que o evento chegou ao Python. Sem esta linha um
        # MessageEv perdido era indistinguível de uma conexão muda; só chat
        # individual (grupo NUNCA é lido nem vira turno). O LID vai junto
        # porque é ele que aparece no celular do dono.
        if not group:
            _log(f"[{acct.name}] evento {item['id'] or '(sem id)'} de {chat}"
                 + (f"/{lid}" if lid else "")
                 + f" ({'do dono/outro aparelho' if from_me else 'recebida'})")

        # Turno do dono: a mensagem é UM turno, uma vez só. O mesmo evento pode
        # voltar numa reconexão (ou ser lido por duas rotinas), e responder
        # duas vezes é o erro que ele percebe na hora. Vale para o chat direto
        # e para o eco com o nome escrito num chat individual de terceiro.
        owner_direct = _is_direct_chat(acct, item)
        if _is_owner_order(acct, item, owner_direct) \
                and not _first_owner_sighting(acct.name, item["id"]):
            _log(f"[{acct.name}] mensagem {item['id']} do dono repetida — "
                 f"já virou turno, ignorando esta cópia")
            return

        # O HistorySync do WhatsApp reenvia meses de conversa de uma vez. Guardo
        # só o que é recente de verdade, senão o inbox local vira um arquivo de
        # histórico alheio ao que o usuário veio perguntar. (Com o timestamp
        # certo isto volta a valer para TODA mensagem — ver `_stamp_seconds`.)
        if abs(time.time() - stamp) < FRESH_TS_S:
            _append_inbox(item, acct)

        # O link da mídia morre: quem quer o arquivo tem que pedir agora.
        # O dono mandando do celular também conta — no chat direto dele
        # (próprio número pareado ou owner_chat) o áudio precisa ser baixado
        # para ser transcrito e respondido em áudio.
        #
        # ÁUDIO `from_me` em chat individual de terceiro também desce: o dono
        # gravando uma nota no chat do Renan é uma ordem para o assistente
        # ("Gino, avisa ele que..."), e o arquivo é a única prova de quem
        # falou (voiceprint). Imagem/vídeo dele no chat alheio continua sem
        # download — só o áudio é uma ordem.
        if media and abs(time.time() - stamp) < FRESH_TS_S:
            want_media = not from_me
            _audio = str(media.get("kind") or "").lower() in (
                "áudio", "audio", "mensagem de voz")
            if not want_media:
                chat_digits = _digits(chat)
                with _lock:
                    own_digits = _digits(acct.me.get("number") or "")
                want_media = bool(chat_digits and chat_digits == own_digits)
                if not want_media:
                    try:
                        from memory.config_manager import get_plugin_setting
                        owner = _digits(get_plugin_setting(NS, "owner_chat", "") or "")
                    except Exception:
                        owner = ""
                    want_media = bool(chat_digits and owner and chat_digits == owner)
            if not want_media and _audio and not group:
                want_media = True
            if want_media:
                threading.Thread(target=_download_media,
                                 args=(client, ev.Message, item, acct),
                                 name="jarvis-whatsapp-media", daemon=True).start()

        # A regra do dono: notificação de fundo enquanto ele dorme o ACORDA.
        # Aqui a conversa é com ele, então o aviso é sempre dele — o gancho
        # (main.py) decide se acorda na hora; sem gancho a mensagem fica na
        # gaveta e é entregue no próximo despertar. O eco de um envio DESTE
        # processo não é mensagem nova e não acorda ninguém.
        if owner_direct and abs(time.time() - stamp) < NOTICE_AGE_S \
                and not sent_by_me(item["id"]):
            _owner_arrived(acct.name, item)

        # O aviso falado na sala é para TERCEIROS. No chat direto do dono a
        # mensagem é um turno da conversa: o watcher responde no WhatsApp, e o
        # aviso de ambiente produziria uma segunda resposta em voz alta para o
        # mesmo pedido — exatamente a duplicação que o dono não quer.
        cb = acct.sink
        if cb and not from_me and abs(time.time() - stamp) < NOTICE_AGE_S:
            if not (owner_direct and _is_owner_turn(acct, item)):
                try:
                    cb(item)
                except Exception as e:
                    _log(f"o aviso de mensagem nova falhou: {e}")

        for watcher in list(_watchers):
            try:
                watcher(acct.name, item)
            except Exception as e:
                _log(f"[{acct.name}] observador de mensagens falhou: {e}")
    except Exception as e:
        _log(f"[{acct.name}] mensagem ilegível: {e}")


# ── ciclo de vida ────────────────────────────────────────────────────────────

def _build_client(acct: _Account, NewClient, ev):
    """Create one Neonize client with its handlers, serialized.

    The Go runtime behind Neonize is not safe for two concurrent
    ``NewClient``/event registrations; doing so aborts the process with
    ``fatal error: concurrent map writes`` (seen at boot with two accounts).
    The lock covers only this setup — ``connect()`` runs outside it because it
    blocks for the whole session.
    """
    with _client_init_lock:
        client = NewClient(str(acct.session_db), uuid=acct.uuid)
        client.event.qr(lambda _c, data, a=acct: _draw_qr(data, a))
        client.event(ev.ConnectedEv)(
            lambda _c, _e, a=acct: _on_connected(client, _e, a))
        client.event(ev.DisconnectedEv)(
            lambda _c, _e, a=acct: _on_disconnected(client, _e, a))
        client.event(ev.LoggedOutEv)(
            lambda _c, _e, a=acct: _on_logged_out(client, _e, a))
        client.event(ev.MessageEv)(
            lambda _c, _e, a=acct: _on_message(client, _e, a))
        # Recibo de entrega/leitura: sem isto "enviado" era só ausência
        # de exceção. O handler é opcional em versões antigas do neonize.
        receipt_ev = getattr(ev, "ReceiptEv", None)
        if receipt_ev is not None:
            client.event(receipt_ev)(
                lambda _c, _e, a=acct: _on_receipt(_e, a))
        replaced_ev = getattr(ev, "StreamReplacedEv", None)
        if replaced_ev is not None:
            client.event(replaced_ev)(
                lambda _c, _e, a=acct: _on_stream_replaced(_c, _e, a))
        return client


def _run(acct: _Account) -> None:
    """Thread de UMA conta: `connect()` bloqueia, e é por isso que ela existe."""
    global _go_loaded
    try:
        from neonize.client import NewClient
        from neonize import events as ev
        _go_loaded = True
    except Exception as e:
        acct.error = f"neonize não pôde ser carregado: {e}"
        _log(f"[{acct.name}] {acct.error}")
        return

    acct.dir.mkdir(parents=True, exist_ok=True)
    while not acct.stopping:
        client = None
        try:
            client = _build_client(acct, NewClient, ev)
            with _lock:
                acct.client = client
                acct.error = ""
            _log(f"[{acct.name}] conectando…" if paired(acct.name)
                 else f"[{acct.name}] conectando (primeiro pareamento: QR à vista)")
            client.connect()          # bloqueia até cair a conexão
        except Exception as e:
            with _lock:
                acct.error = f"falha na conexão: {e}"
            _log(f"[{acct.name}] {acct.error}")
        finally:
            with _lock:
                acct.connected = False
            if client is not None:
                for step in ("disconnect", "stop"):
                    try:
                        getattr(client, step)()
                    except Exception:
                        pass
                with _lock:
                    if acct.client is client:
                        acct.client = None
        if acct.stopping:
            break
        time.sleep(RECONNECT_S)


def start(on_message=None, logger=None, account: str | None = None
          ) -> tuple[bool, str]:
    """Sobe a conexão de UMA conta (idempotente), ou de todas as auto_start.

    `account=None` sobe todas as contas marcadas auto_start que ainda não estão
    rodando e devolve a frase da default (ou da primeira). É o que o boot chama.
    Com um nome, sobe só aquela — é o que o botão CONECTAR chama.
    """
    global _logger
    if logger is not None:
        _logger = logger

    if account is not None:
        try:
            a = _A(account)
        except KeyError as e:
            return False, str(e)
        return _start_account(a, on_message)

    _ensure_accounts()
    phrase = ""
    ok_any = False
    targets = [a for a in _accounts.values() if a.auto_start] or list(_accounts.values())
    for a in targets:
        ok, phrase_a = _start_account(a, on_message)
        ok_any = ok_any or ok
        if a.default:
            phrase = phrase_a
    return ok_any, (phrase or "conectando…")


def _start_account(acct: _Account, on_message=None) -> tuple[bool, str]:
    with _lock:
        if on_message is not None:
            acct.sink = on_message
        if acct.thread and acct.thread.is_alive():
            if acct.connected:
                return True, (f"o WhatsApp {acct.name} já está conectado "
                              f"({acct.me.get('number') or 'sem número'})")
            return True, f"o WhatsApp {acct.name} está conectando"
        if not available():
            return False, ("o WhatsApp precisa do neonize: rode "
                           "pip install neonize e abra o JARVIS de novo")
        acct.stopping = False

    acct.thread = threading.Thread(target=_run, args=(acct,),
                                   name=f"jarvis-whatsapp-{acct.uuid}", daemon=True)
    acct.thread.start()

    deadline = time.time() + START_WAIT_S
    while time.time() < deadline:
        time.sleep(0.1)
        if acct.connected:
            return True, f"conectado como {acct.me.get('number')} ({acct.name})"
        if acct.qr_path:
            return True, (f"preciso que você escaneie o QR do {acct.name} com o "
                          f"celular: {acct.qr_path}")
        if acct.error:
            return False, acct.error
    return True, "conectando…"


def _boot_entry(acct: _Account, error: str = "") -> dict:
    """Estado REAL de uma conta para o aviso de boot (nunca "pedido aceito")."""
    with _lock:
        connected = bool(acct.connected)
        detail = str(acct.error or error or "")
        number = _digits(acct.me.get("number") or "")
        qr = bool(acct.qr_path)
    try:
        is_paired = bool(acct.paired_mark.exists() and acct.session_db.exists())
    except Exception:
        is_paired = False
    if connected:
        state = "connected"
    elif qr:
        state = "auth"          # QR na tela / sessão vencida: mão humana
    elif detail:
        state = "error"         # neonize ausente, falha de conexão, etc.
    else:
        state = "auth"          # handshake não fechou na janela: mão humana
    return {"name": acct.name, "uuid": acct.uuid, "connected": connected,
            "state": state, "paired": is_paired, "number": number,
            "qr": qr, "detail": detail}


def connect_all(on_message=None, logger=None,
                settle_s: float = BOOT_SETTLE_S) -> list[dict]:
    """Sobe cada conta habilitada uma vez e devolve o estado REAL de cada uma.

    É o que o boot chama. As contas sobem em SEQUÊNCIA (cada `_start_account`
    espera no máximo `START_WAIT_S` pelo connected/QR/erro antes da próxima):
    o runtime Go do Neonize aborta com "concurrent map writes" quando dois
    clientes são criados/conectados ao mesmo tempo. Uma tentativa só: não há
    laço aqui — o `_run` já reconecta uma queda, com o intervalo de sempre.

    Cada item: ``{"name", "uuid", "connected", "state", "paired", "number",
    "qr", "detail"}``, com ``state`` em ``"connected"``, ``"auth"`` (QR
    pendente, sessão vencida ou handshake incompleto: precisa de mão humana)
    ou ``"error"``.
    """
    global _logger
    if logger is not None:
        _logger = logger
    _ensure_accounts()
    targets = ([a for a in _accounts.values() if a.auto_start]
               or list(_accounts.values()))
    if not targets:
        return []
    if not available():
        return [_boot_entry(a, error="o WhatsApp precisa do neonize")
                for a in targets]

    # Sequential on purpose: Neonize's Go runtime aborts the whole process on
    # concurrent client construction/connection. _start_account waits for the
    # handshake (connected/QR/error) before the next account starts.
    for acct in targets:
        _start_account(acct, on_message)
    # Janela de tolerância só para quem ainda não deu sinal nenhum. Conta com
    # QR na tela ou erro já está decidida: esperar mais só atrasa o aviso.
    settle_deadline = time.time() + max(0.0, float(settle_s or 0.0))
    while time.time() < settle_deadline and any(
            not a.connected and not a.qr_path and not a.error
            for a in targets):
        time.sleep(0.1)
    return [_boot_entry(a) for a in targets]


def _stop_account(acct: _Account) -> None:
    with _lock:
        acct.stopping = True
        client = acct.client
        acct.connected = False
    if client is not None:
        for step in ("disconnect", "stop"):
            try:
                getattr(client, step)()
            except Exception:
                pass
    if acct.thread and acct.thread.is_alive():
        try:
            acct.thread.join(timeout=2)
        except Exception:
            pass
    acct.thread = None


def stop(account: str | None = None) -> None:
    """Para uma conta (nome/uuid) ou todas (sem argumento)."""
    if account is not None:
        try:
            _stop_account(_A(account))
        except KeyError:
            return
        return
    for a in list(_accounts.values()):
        _stop_account(a)
    if _go_loaded:
        _stop_go()


def _stop_go() -> None:
    """Pede ao whatsmeow que pare todos os clientes (não desliga o runtime)."""
    try:
        from neonize import gocode
        gocode.StopAll()
    except Exception:
        pass


def hard_exit(code: int = 0) -> None:
    """
    Encerra o processo de verdade. Precisa existir porque o runtime Go do
    neonize mantém threads vivas depois do mainloop: sem isto o "Sair" fecha a
    janela e o processo fica pendurado até alguém matar.

    Só age quando o WhatsApp chegou a carregar — se ele nunca subiu, um
    `os._exit` cortaria a saída de arquivos/threads do resto do app sem motivo.
    """
    if not _go_loaded:
        return
    try:
        _stop_go()
    except Exception:
        pass
    try:
        import sys
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
    except Exception:
        pass
    os._exit(code)


def status_line(account: str | None = None) -> str:
    """Uma frase só, para o assistente falar sem enfeite."""
    st = status(account)
    who = f" {st.get('account')}" if len(_accounts) > 1 else ""
    if not st["available"]:
        return "o WhatsApp não está instalado (falta o neonize)"
    if st["connected"]:
        num = st["number"] or "seu número"
        return f"o WhatsApp{who} está conectado ({num})"
    if st["qr_path"]:
        return f"o WhatsApp{who} está esperando o QR ser escaneado: {st['qr_path']}"
    if st["error"]:
        return f"o WhatsApp{who} não está conectado: {st['error']}"
    if st["paired"]:
        return f"o WhatsApp{who} está reconectando"
    return f"o WhatsApp{who} ainda não foi pareado — peça 'parear o WhatsApp'"
