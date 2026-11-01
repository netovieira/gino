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

O QUE ISTO NÃO FAZ, de propósito:
  - grupos para envio (o nome do grupo não está no catálogo de contatos);
  - transcrição de áudio (o assistente ouve o microfone, não arquivos).

O QUE ISTO CUSTA: é protocolo não oficial. Funciona, é o mesmo motor de muitos
bots, mas uso agressivo (disparo em massa, spam) é o que gera bloqueio — o
número é o do usuário.

    from core import whatsapp
    whatsapp.start()                              # sobe todas as auto_start
    whatsapp.send("João", "chego em 10min")       # conta default
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
FRESH_TS_S     = 24 * 3600    # só guarda o que chegou nas últimas 24h
MEDIA_MAX_MB   = 20           # acima disso a mídia fica no servidor
MEDIA_KEEP_DAYS = 7           # quanto tempo os arquivos baixados ficam aqui

_lock       = threading.RLock()
_inbox_lock = threading.RLock()
_logger     = None
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
        acct.inbox_file.parent.mkdir(parents=True, exist_ok=True)
        with _inbox_lock:
            with acct.inbox_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
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


def _prune_media(acct: _Account) -> None:
    """Apaga mídia baixada mais velha que MEDIA_KEEP_DAYS."""
    try:
        if not acct.media_dir.exists():
            return
        cutoff = time.time() - MEDIA_KEEP_DAYS * 86400
        for p in acct.media_dir.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
            except Exception:
                continue
    except Exception:
        pass


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


def recent(limit: int = 10, only_incoming: bool = True,
           account: str | None = None) -> list[dict]:
    """As últimas mensagens recebidas, da mais nova para a mais antiga."""
    try:
        a = _A(account)
    except KeyError:
        return []
    items = [m for m in _load_inbox(a) if not (only_incoming and m.get("from_me"))]
    return list(reversed(items))[:max(1, int(limit or 10))]


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
    return list(reversed(hits))[:max(1, int(limit or 8))]


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


def send(recipient: str, text: str, account: str | None = None) -> tuple[bool, str]:
    """Envia texto. Devolve (ok, frase) — a frase é o que o assistente fala."""
    text = str(text or "").strip()
    if not text:
        return False, "a mensagem está vazia"
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
    try:
        client.send_message(target, text)
    except Exception as e:
        return False, f"o WhatsApp recusou o envio para {label}: {e}"
    _append_inbox({"ts": int(time.time()), "chat": _user_of(target),
                   "name": str(label), "from_me": True, "group": False,
                   "text": text[:600], "id": ""}, a)
    return True, f"enviado para {label}"


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


def media_path(item: dict, account: str | None = None) -> str:
    """O arquivo local desta mensagem, se ele foi baixado."""
    try:
        a = _A(account)
    except KeyError:
        return ""
    p = a.media_dir / _media_filename(item)
    return str(p) if p.exists() else ""


def _download_wanted() -> bool:
    try:
        from memory.config_manager import get_plugin_setting
        return bool(get_plugin_setting(NS, "media_download", True))
    except Exception:
        return True


def _download_media(client, message, item: dict, acct: _Account) -> None:
    """Baixa o anexo para a pasta de mídia da conta (link do WhatsApp expira)."""
    media = item.get("media") or {}
    if not item.get("id") or not _download_wanted():
        return
    size = int(media.get("size") or 0)
    if size and size > MEDIA_MAX_MB * 1024 * 1024:
        _log(f"{_spoken_media(media)} não baixada: {size // 1048576} MB "
             f"(acima do limite de {MEDIA_MAX_MB} MB)")
        return
    dest = acct.media_dir / _media_filename(item)
    if dest.exists():
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


def _send_any(client, target, file: Path, caption) -> str:
    """Manda o arquivo pelo tipo certo. Devolve o tipo, falado."""
    ext = file.suffix.lower()
    if ext in _IMAGE_EXT:
        client.send_image(target, str(file), caption=caption)
        return "foto"
    if ext in _VIDEO_EXT:
        client.send_video(target, str(file), caption=caption)
        return "vídeo"
    if ext in _AUDIO_EXT:
        # PTT é o "segurar e falar": só faz sentido em ogg/opus.
        client.send_audio(target, str(file), ptt=ext in (".ogg", ".opus"))
        return "áudio"
    if ext == ".webp":
        client.send_sticker(target, str(file))
        return "figurinha"
    client.send_document(target, str(file), caption=caption, filename=file.name)
    return "documento"


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
        kind = _send_any(client, target, file, caption)
    except Exception as e:
        # Container que o WhatsApp não engole (mkv, avi, flac…) tem conserto
        # local: o ffmpeg converte e a gente tenta de novo, uma vez.
        converted = None
        if ext in _VIDEO_EXT or ext in _AUDIO_EXT:
            converted = _convert_for_whatsapp(file, video=ext in _VIDEO_EXT)
        if converted is None:
            return False, f"o WhatsApp recusou o envio para {label}: {e}"
        try:
            kind = _send_any(client, target, converted, caption)
            file = converted
            ext = converted.suffix.lower()
        except Exception as e2:
            return False, f"o WhatsApp recusou o envio para {label}: {e2}"

    item = {"ts": int(time.time()), "chat": _user_of(target), "name": str(label),
            "from_me": True, "group": False,
            "text": f"[{kind}{(': ' + caption) if caption else ''}]",
            "id": "", "media": {"kind": kind, "name": file.name, "ext": ext}}
    _append_inbox(item, a)
    artigo = "a" if kind in ("foto", "figurinha") else "o"
    return True, f"mandei {artigo} {kind} para {label}"


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


def _chat_name(chat_user: str, pushname: str, is_group: bool,
               acct: _Account) -> str:
    if is_group:
        return f"grupo {chat_user}"
    for c in _contact_index(acct):
        if c["user"] == chat_user:
            return c["name"]
    return pushname or chat_user


def _on_message(client, ev, acct: _Account) -> None:
    try:
        from neonize.utils import extract_text
    except Exception:
        return
    try:
        src   = ev.Info.MessageSource
        chat  = getattr(getattr(src, "Chat", None), "User", "") or ""
        group = bool(getattr(src, "IsGroup", False)) or chat.endswith("g.us")
        from_me = bool(getattr(src, "IsFromMe", False))
        stamp = int(ev.Info.Timestamp or time.time())
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
            "name": _chat_name(chat, ev.Info.Pushname or "", group, acct),
            "from_me": from_me,
            "group": group,
            "text": text[:600],
            "id": ev.Info.ID or "",
            "media": media,
        }
        # O HistorySync do WhatsApp reenvia meses de conversa de uma vez. Guardo
        # só o que é recente de verdade, senão o inbox local vira um arquivo de
        # histórico alheio ao que o usuário veio perguntar.
        if abs(time.time() - stamp) < FRESH_TS_S:
            _append_inbox(item, acct)

        # O link da mídia morre: quem quer o arquivo tem que pedir agora.
        if media and not from_me and abs(time.time() - stamp) < FRESH_TS_S:
            threading.Thread(target=_download_media,
                             args=(client, ev.Message, item, acct),
                             name="jarvis-whatsapp-media", daemon=True).start()

        cb = acct.sink
        if cb and not from_me and abs(time.time() - stamp) < 300:
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
