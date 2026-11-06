"""E-mail de verdade: as contas do dono, IMAP/SMTP, no molde do WhatsApp.

O caminho antigo (plugins/email_control.py) guardava a senha de app DENTRO do
config, junto com os metadados da conta, e só existia uma conta "extra" por
remendo de migração. Aqui a conta é um objeto com estado próprio, como o
``_Account`` do WhatsApp:

    from core import email
    email.add_account("trabalho", "eu@empresa.com", provider="outlook")
    email.set_app_password("trabalho", "...")     # vai para o cofre, nunca para cá
    email.connect_all()                            # estado REAL de cada conta
    email.refresh("trabalho")                      # cabeçalhos no inbox jsonl
    email.recent(5, account="trabalho")            # remetente + assunto

O QUE FICA ONDE — e por que
    * Metadados (nome, endereço, provedor, hosts, flags) em
      ``plugin_config.mail.accounts`` — o mesmo lugar onde o WhatsApp guarda as
      contas dele.
    * A senha de app em ``plugin_config.mail_secrets.<uuid>``, que é o cofre já
      existente: ``config/api_keys.json``, criado com permissão 600. Este módulo
      nunca devolve a senha em ``list_accounts()`` (só ``has_password``), nunca a
      imprime e nunca a escreve em log. Uma senha apagada vira string vazia,
      porque ``save_plugin_config`` faz merge e não tem remoção.
    * Estado por conta em ``config/mail/<uuid>/`` (marker de último teste) e o
      inbox em ``memory/mail_inbox_<uuid>.jsonl`` — a mesma convenção de nomes
      do WhatsApp (``config/whatsapp_web/<uuid>/``, ``memory/whatsapp_inbox_*.jsonl``).

MIGRAÇÃO DO FORMATO ANTIGO
    ``migrate_legacy()`` porta a conta única que morava na raiz de
    ``plugin_config.email`` para ``plugin_config.mail.accounts``, levando a
    senha de app para o cofre. ``purge_legacy()`` apaga as chaves antigas de
    vez (a senha fica no cofre, em UM lugar só): ``save_plugin_config`` faz
    merge e não remove chave, então a limpeza é uma reescrita do arquivo com
    backup 600 antes. Nada é impresso nem logado nesse caminho.
    O vigia de códigos (``core/email_otp_watch.py``) lê ESTA camada desde a
    unificação: não existe mais um segundo leitor das caixas.

PRIVACIDADE (regra dura, não é preferência)
    O conteúdo de terceiro nunca é falado. O aviso usa remetente + assunto, e
    passa por ``core/speech_guard.py`` antes de sair. O corpo NUNCA é buscado
    numa varredura: ``refresh()`` pede só ``BODY.PEEK[HEADER.FIELDS ...]`` e o
    jsonl guarda ``has_body`` (existe corpo lá no servidor) sem o texto. Só
    ``fetch_body()`` — chamado quando o dono pede "lê o e-mail do banco" —
    baixa o corpo, e ele não entra em nenhuma frase falada por este módulo.
    Com FILTROS configurados, uma regra ``classificar`` com ``body: true``
    pode pedir um TRECHO do corpo (``FILTER_BODY_CHARS``) para o gancho do
    cérebro — sempre pelo MESMO ``BODY.PEEK`` somente leitura, nunca falado
    pelo aviso, nunca gravado no jsonl, e entregue como DADO: corpo de e-mail
    não é ordem e nunca autoriza nada.

FILTRO POR CONTA (FLUXO MAPEAMENTO EMAIL, passos 2 e 4)
    Regras em ``plugin_config.mail.accounts[].filters`` (ao lado de
    ``notify``/``otp``; sem store paralelo). Sem regra = comportamento de
    sempre. Com regras: a primeira que casa decide ``avisa`` / ``ignora`` /
    ``classificar``; fora de todas as regras = NENHUM aviso (nem fala, nem
    SYS). ``match``: contains | exact | regex simples; campos: from, subject.
    Quem quer "avisa tudo MENOS marketing" acrescenta no fim uma regra
    apanhadora (``match: regex``, ``pattern: ".+"``, ``action: avisa``).

ENVIO É IRREVERSÍVEL
    ``request_send()`` só estaciona o pedido; ``confirm_send(voice_ok=True)``
    envia. O único caminho que passa ``voice_ok=True`` é o portão de voz do
    main.py, depois do reconhecimento da voz do dono (``_speaker_order_identity``).
    A ferramenta (plugins/mail_control.py) e o bridge nunca confirmam: texto
    digitado não envia e-mail.

LEITURA NÃO MARCA COMO LIDO
    Tudo usa BODY.PEEK e seleciona a caixa em modo somente leitura: o assistente
    ler os e-mails não apaga o negrito da caixa de entrada do dono.
"""
from __future__ import annotations

import email
import imaplib
import json
import os
import random
import re
import shutil
import smtplib
import socket
import ssl
import tempfile
import threading
import time
from email.header import decode_header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid, parseaddr, parsedate_to_datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent

# Namespace dos metadados em plugin_config. O cofre é um namespace separado de
# propósito: nenhuma tela que liste "mail" esbarra numa senha.
NS = "mail"
SECRET_NS = "mail_secrets"
LEGACY_NS = "email"      # namespace do plugin antigo (uma conta, senha na raiz)
# Chaves do formato antigo que a limpeza remove. `accounts` (formato
# intermediário) sai junto quando fica vazio — nunca tocamos em outra coisa.
LEGACY_KEYS = ("address", "app_password", "name", "account_name",
               "imap_host", "smtp_host")

_STATE_ROOT = _BASE / "config" / "mail"
_MEMORY_DIR = _BASE / "memory"

INBOX_KEEP        = 300        # cabeçalhos mantidos por conta em disco
INBOX_TRIM_EVERY  = 25         # regrava o arquivo a cada N itens
CONNECT_TIMEOUT_S = 12.0       # teto de uma conexão IMAP/SMTP
START_WAIT_S      = 10.0       # quanto o connect_all espera por conta
BOOT_SETTLE_S     = 6.0        # janela curta para as contas mais lentas

# Reconexão: falha de rede NUNCA vira conta desligada. A espera cresce de
# verdade (5 s, 15 s, 45 s, 135 s, teto de 4 min) e ganha jitter para várias
# contas não baterem no servidor no mesmo instante. O teto é de ESPERA, não de
# tentativas: enquanto a conta estiver habilitada, o laço continua.
BACKOFF_BASE_S    = 5.0
BACKOFF_FACTOR    = 3.0
BACKOFF_MAX_S     = 240.0
BACKOFF_JITTER    = 0.2        # ±20% em cada espera
POLL_S            = 180.0      # intervalo da varredura de não lidos
OTP_POLL_S        = 60.0       # cadência do passo de códigos (a mesma de antes)
SCAN              = 60         # quantos não lidos recentes uma varredura olha
BODY_CHARS        = 4000       # teto de corpo devolvido a quem pedir
# Timeout de leitura APÓS a janela TCP/TLS aberta: o teto de 12 s valia para o
# login também, e o Gmail sob carga respondeu a autenticação em 12,5-13 s — o
# socket cortava antes do "OK". A conexão continua limitada a CONNECT_TIMEOUT_S;
# depois de aberta, a leitura (login/select/FETCH) ganha este teto maior.
IMAP_READ_TIMEOUT_S = 30.0
# Filtro por conta (FLUXO MAPEAMENTO EMAIL, passos 2 e 4): regras gravadas no
# MESMO registro da conta em plugin_config.mail (ao lado de notify/otp).
# Sem regra nenhuma o comportamento é o de sempre; com regras, e-mail que
# NÃO casa com nenhuma NÃO gera aviso nenhum (nem fala, nem SYS).
FILTER_ACTIONS    = ("avisa", "ignora", "classificar")
FILTER_MATCHES    = ("contains", "exact", "regex")
FILTER_MAX_RULES  = 40         # teto de regras por conta (config do dono)
FILTER_BODY_CHARS = 1200       # trecho do corpo entregue ao cérebro por regra
PENDING_PATH      = _MEMORY_DIR / "mail_send_pending.json"
CONFIRM_MARKER    = "confirmo enviar"
CONFIRM_TIMEOUT_S = 1800.0     # 30 min: o mesmo prazo do pedido de reinício

# Presets dos provedores conhecidos. `imap` = (host, porta, ssl); `smtp` =
# (host, porta, "ssl" | "starttls"). Manual aceita qualquer host: é o mesmo
# caminho, só que sem preset.
PROVIDERS: dict[str, dict] = {
    "gmail": {
        "label": "Gmail",
        "imap": ("imap.gmail.com", 993, True),
        "smtp": ("smtp.gmail.com", 465, "ssl"),
        "note": "Gmail exige senha de app (Conta Google → Segurança → "
                "Verificação em duas etapas → Senhas de app). A senha normal "
                "é recusada.",
    },
    "outlook": {
        "label": "Outlook / Hotmail",
        "imap": ("outlook.office365.com", 993, True),
        "smtp": ("smtp.office365.com", 587, "starttls"),
        "note": "Outlook pessoal: senha de app se a verificação em duas etapas "
                "estiver ligada. Conta corporativa Microsoft 365 recusa senha "
                "e exige OAuth (ainda não suportado aqui).",
    },
    "icloud": {
        "label": "iCloud",
        "imap": ("imap.mail.me.com", 993, True),
        "smtp": ("smtp.mail.me.com", 587, "starttls"),
        "note": "iCloud exige uma senha específica de app (Apple ID → "
                "Segurança → Senhas de app).",
    },
    "yahoo": {
        "label": "Yahoo",
        "imap": ("imap.mail.yahoo.com", 993, True),
        "smtp": ("smtp.mail.yahoo.com", 465, "ssl"),
        "note": "Yahoo exige senha de app gerada na segurança da conta.",
    },
    "manual": {
        "label": "Outro (IMAP/SMTP manual)",
        "imap": ("", 993, True),
        "smtp": ("", 587, "starttls"),
        "note": "Preencha host e porta do seu provedor.",
    },
}
DEFAULT_PROVIDER = "gmail"

_ADDR_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_lock   = threading.RLock()
_logger = None

# Reconexão automática: um worker por conta, parado de verdade em stop/reset.
_retry_lock    = threading.Lock()
_retry_threads: dict[str, threading.Thread] = {}
_retry_stop    = threading.Event()

# Aviso ao dono: só o SEGUNDO erro consecutivo e a recuperação chamam isto.
_notice_lock   = threading.Lock()
_notice_hook   = None


def _log(message: str) -> None:
    fn = _logger or (lambda msg: print(f"[Mail] {msg}"))
    try:
        fn(message)
    except Exception:
        pass


# ── sanitização de tudo que sai daqui ────────────────────────────────────────

def _safe_spoken(text: str, lang: str = "pt") -> str:
    """Toda frase FALADA derivada de e-mail passa pelo guarda de fala.

    Remetente e assunto são conteúdo de terceiro: podem trazer telefone,
    caminho ou nome de ferramenta. O guarda reescreve antes de a frase existir
    como fala. Se o guarda não estiver disponível, a frase volta como veio —
    nunca inventamos uma versão "mais segura" que ninguém revisou.
    """
    raw = _one_line(text)
    if not raw:
        return ""
    try:
        from core import speech_guard as _guard
        out = str(_guard.sanitize(raw, lang=lang).text or "").strip()
        return out or raw
    except Exception as e:
        _log(f"guarda de fala indisponível: {e}")
        return raw


def safe_line(text: str, lang: str = "pt") -> str:
    """Público: qualquer string derivada de e-mail antes de ser dita/escrita."""
    return _safe_spoken(text, lang=lang)


def _one_line(value: object, limit: int = 160) -> str:
    text = _CTRL_RE.sub(" ", str(value or ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _decode_header(value: object) -> str:
    """Cabeçalho MIME (RFC 2047) → texto legível, sem quebrar em bytes ruins."""
    raw = str(value or "")
    if not raw:
        return ""
    try:
        parts = decode_header(raw)
    except Exception:
        return _one_line(raw)
    out: list[str] = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            for codec in (enc, "utf-8", "latin-1"):
                if not codec:
                    continue
                try:
                    out.append(chunk.decode(codec))
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            else:
                out.append(chunk.decode("utf-8", "replace"))
        else:
            out.append(str(chunk))
    return _one_line(" ".join("".join(out).split()))


# ── cofre: a senha de app mora no config/api_keys.json (600) ─────────────────
def _vault() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        return get_plugin_config(SECRET_NS) or {}
    except Exception as e:
        _log(f"cofre de senhas indisponível: {e}")
        return {}


def _protect_vault() -> None:
    """Best effort: o cofre que já guarda as outras chaves é 600."""
    try:
        from memory.config_manager import CONFIG_FILE
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:
        pass


def app_password(name_or_uuid: str) -> str:
    """A senha de app da conta; string vazia quando não há. Nunca logada."""
    a = _find(name_or_uuid)
    if a is None:
        return ""
    return re.sub(r"\s+", "", str(_vault().get(a.uuid) or ""))


def has_password(name_or_uuid: str) -> bool:
    return bool(app_password(name_or_uuid))


def set_app_password(name_or_uuid: str, password: str | None) -> bool:
    """Grava (ou apaga, com vazio) a senha de app no cofre. Nunca no jsonl."""
    a = _find(name_or_uuid)
    if a is None:
        return False
    secret = re.sub(r"\s+", "", str(password or ""))
    try:
        from memory.config_manager import save_plugin_config
        save_plugin_config(SECRET_NS, {a.uuid: secret})
        _protect_vault()
        return bool(secret)
    except Exception as e:
        _log(f"não consegui gravar a senha de app da conta {a.name}: {e}")
        return False


# ── a conta ──────────────────────────────────────────────────────────────────

def _clean_rule(entry: object) -> dict | None:
    """Uma regra de filtro validada, ou None se for descartável.

    Chaves: ``from`` e/ou ``subject`` (padrão), ``match`` (contains | exact |
    regex simples), ``action`` (avisa | ignora | classificar) e ``body``
    (bool: a regra de classificar pede também um TRECHO do corpo — dado,
    nunca ordem). regex passa por compilação e teto de tamanho; padrão
    inválido derruba a regra inteira (a gente não adivinha regra do dono).
    """
    if not isinstance(entry, dict):
        return None
    field = str(entry.get("field") or "").strip().lower()
    pattern = str(entry.get("pattern") or "").strip()
    if field not in ("from", "subject") or not pattern:
        # Forma do dono no config: {"from": "banco"} / {"subject": "nota"}.
        field, pattern = "", ""
        for key in ("from", "subject"):
            value = str(entry.get(key) or "").strip()
            if value:
                field, pattern = key, value
                break
    if not field or len(pattern) > 200:
        return None
    action = str(entry.get("action") or "").strip().lower()
    if action not in FILTER_ACTIONS:
        return None
    match = str(entry.get("match") or "contains").strip().lower()
    if match not in FILTER_MATCHES:
        return None
    if match == "regex":
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error:
            return None
    return {"field": field, "pattern": pattern, "match": match,
            "action": action, "body": bool(entry.get("body")),
            "label": _one_line(entry.get("label") or "", 60)}


def _clean_filters(entries: object) -> list[dict]:
    if not isinstance(entries, list):
        return []
    out: list[dict] = []
    for entry in entries[:FILTER_MAX_RULES]:
        rule = _clean_rule(entry)
        if rule is None:
            _log("regra de filtro de e-mail inválida ignorada")
            continue
        out.append(rule)
    return out


def _rule_hits(rule: dict, item: dict) -> bool:
    """A regra casa com o cabeçalho? Só campos do cabeçalho, nunca o corpo."""
    if rule.get("field") == "from":
        haystacks = (str(item.get("from") or ""), str(item.get("sender") or ""))
    else:
        haystacks = (str(item.get("subject") or ""),)
    pattern = str(rule.get("pattern") or "")
    match = rule.get("match") or "contains"
    for text in haystacks:
        if not text:
            continue
        if match == "exact":
            if text.strip().casefold() == pattern.strip().casefold():
                return True
        elif match == "regex":
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    return True
            except re.error:
                return False
        else:
            if pattern.casefold() in text.casefold():
                return True
    return False


def match_filter(item: dict, rules: list[dict]) -> dict | None:
    """A PRIMEIRA regra que casa (ordem = prioridade), ou None."""
    for rule in rules or []:
        try:
            if _rule_hits(rule, item):
                return rule
        except Exception:
            continue
    return None


def _filter_trace(acct, item: dict, rule: dict | None, action: str) -> None:
    """Rastro de toda decisão de filtro (passos 2-4 do fluxo do dono).

    Linha de log interna pelo MESMO ``_log`` da camada — nunca aviso, nunca
    fala, nunca turno: o passo ``ignora`` continua silêncio por projeto.
    Guarda o identificador interno (uid), a conta, a ação resultante e o
    MOTIVO do match (posição e campo da regra que casou, ou fora-do-filtro).
    Nunca padrão de regra, remetente, assunto ou corpo — nada pessoal.
    """
    try:
        if rule is None:
            motivo = "fora-do-filtro"
        else:
            try:
                pos = (acct.filters or []).index(rule) + 1
            except ValueError:
                pos = 0
            motivo = f"regra{pos}-{rule.get('field') or '?'}"
        _log(f"filtro {acct.name}: uid={item.get('uid') or '?'} "
             f"acao={action} motivo={motivo}")
    except Exception:
        pass          # o rastro nunca derruba a varredura


class MailAccount:
    """Uma conta de e-mail: metadados, caminhos e estado de conexão."""

    def __init__(self, name: str, uuid: str, address: str = "",
                 display_name: str = "", provider: str = DEFAULT_PROVIDER,
                 imap_host: str = "", imap_port: int = 993,
                 imap_ssl: bool = True, smtp_host: str = "",
                 smtp_port: int = 587, smtp_security: str = "starttls",
                 default: bool = False, auto_start: bool = True,
                 notify: bool = False, otp: bool = False,
                 filters: list | None = None):
        self.name         = name
        self.uuid         = uuid
        self.address      = address
        self.display_name = display_name
        self.provider     = provider
        self.imap_host    = imap_host
        self.imap_port    = int(imap_port or 993)
        self.imap_ssl     = bool(imap_ssl)
        self.smtp_host    = smtp_host
        self.smtp_port    = int(smtp_port or 587)
        self.smtp_security = (smtp_security or "starttls").lower()
        self.default      = default
        self.auto_start   = auto_start
        self.notify       = notify
        # `otp` = esta caixa também é lida em busca de códigos de verificação.
        # O passo de OTP roda DENTRO da varredura única (core/email_otp_watch.py
        # é a política); a flag decide em quais caixas vale pagar o custo de
        # baixar o corpo das mensagens novas.
        self.otp          = otp
        # Regras de filtro (passos 2/4 do fluxo do dono): lista validada de
        # {field, pattern, match, action, body}. Vazia = comportamento antigo.
        self.filters      = _clean_filters(filters)
        # caminhos (convenção do WhatsApp: um diretório por conta + inbox jsonl)
        self.dir          = _STATE_ROOT / uuid
        self.state_file   = self.dir / "state.json"
        self.inbox_file   = _MEMORY_DIR / f"mail_inbox_{uuid}.jsonl"
        # runtime
        self.connected   = False
        self.state       = "unknown"    # connected | auth | error | unknown
        self.error       = ""
        self.detail      = ""
        self.last_check  = 0.0
        self.appended    = 0
        self.unread      = 0
        # diagnóstico/reconexão (só em memória; nada de segredo aqui)
        self.attempts       = 0      # tentativas de conexão desde o boot
        self.fail_streak    = 0      # falhas CONSECUTIVAS (zera ao conectar)
        self.down_notified  = False  # já avisamos o dono que caiu?
        self.next_retry_s   = 0.0    # espera da próxima tentativa (só log)
        self._stage         = ""     # etapa da tentativa em curso (dns/tcp/...)

    # -- caminhos ---------------------------------------------------------
    def ensure_dir(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def public(self) -> dict:
        """O que a UI e a ferramenta podem ver: nunca a senha."""
        return {
            "name": self.name, "uuid": self.uuid, "address": self.address,
            "display_name": self.display_name, "provider": self.provider,
            "provider_label": _provider_label(self.provider),
            "imap_host": self.imap_host, "imap_port": self.imap_port,
            "imap_ssl": self.imap_ssl, "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port, "smtp_security": self.smtp_security,
            "default": self.default, "auto_start": self.auto_start,
            "notify": self.notify, "otp": self.otp,
            "filters": [dict(r) for r in self.filters],
            "has_password": has_password(self.uuid),
            "connected": self.connected, "state": self.state,
            "error": self.error, "detail": self.detail,
            "last_check": self.last_check, "unread": self.unread,
        }


_accounts: dict[str, MailAccount] = {}


def _provider_label(key: str) -> str:
    preset = PROVIDERS.get(str(key or "").lower()) or {}
    return str(preset.get("label") or key or "manual")


def _provider_defaults(provider: str, imap_host: str = "",
                       imap_port: int | None = None, imap_ssl: bool | None = None,
                       smtp_host: str = "", smtp_port: int | None = None,
                       smtp_security: str = "") -> dict:
    """Host/porta do preset, a menos que o dono tenha dito outra coisa."""
    key = str(provider or DEFAULT_PROVIDER).lower()
    preset = PROVIDERS.get(key) or PROVIDERS[DEFAULT_PROVIDER]
    i_host, i_port, i_ssl = preset["imap"]
    s_host, s_port, s_sec = preset["smtp"]
    return {
        "imap_host": str(imap_host or i_host),
        "imap_port": int(imap_port or i_port),
        "imap_ssl": bool(i_ssl if imap_ssl is None else imap_ssl),
        "smtp_host": str(smtp_host or s_host),
        "smtp_port": int(smtp_port or s_port),
        "smtp_security": str(smtp_security or s_sec).lower(),
    }


def provider_choices() -> list[str]:
    return [f"{key} — {info['label']}" for key, info in PROVIDERS.items()]


def _entry_to_account(entry: dict) -> MailAccount:
    provider = str(entry.get("provider") or DEFAULT_PROVIDER).lower()
    defaults = _provider_defaults(
        provider, entry.get("imap_host") or "", entry.get("imap_port") or None,
        entry.get("imap_ssl"),
        entry.get("smtp_host") or "", entry.get("smtp_port") or None,
        entry.get("smtp_security") or "")
    uuid = str(entry.get("uuid") or "").strip()
    if not uuid:
        safe = re.sub(r"[^a-z0-9]+", "-",
                      str(entry.get("name") or "conta").lower()).strip("-")
        uuid = f"mail-{safe or 'conta'}"
    return MailAccount(
        name=str(entry.get("name") or entry.get("address") or "principal").strip(),
        uuid=uuid,
        address=str(entry.get("address") or "").strip(),
        display_name=str(entry.get("display_name") or "").strip(),
        provider=provider if provider in PROVIDERS else "manual",
        default=bool(entry.get("default", False)),
        auto_start=bool(entry.get("auto_start", True)),
        notify=bool(entry.get("notify", False)),
        otp=bool(entry.get("otp", False)),
        filters=entry.get("filters"),
        **defaults,
    )


def _load_entries() -> list[dict]:
    try:
        from memory.config_manager import get_plugin_config
        cfg = get_plugin_config(NS) or {}
    except Exception:
        cfg = {}
    accts = cfg.get("accounts")
    if not isinstance(accts, list):
        return []
    return [a for a in accts if isinstance(a, dict)]


def _ensure_accounts() -> None:
    if _accounts:
        return
    for entry in _load_entries():
        try:
            a = _entry_to_account(entry)
        except Exception as e:
            _log(f"conta de e-mail inválida ignorada: {e}")
            continue
        _accounts[a.uuid] = a
    if _accounts and not any(a.default for a in _accounts.values()):
        next(iter(_accounts.values())).default = True


def _save_accounts() -> None:
    """Grava SÓ os metadados. Nenhuma chave de senha existe neste dicionário."""
    try:
        from memory.config_manager import save_plugin_config
        # A escolha da conta padrão vai no MESMO write: `default_account_name`
        # não pode ser chamada durante o save (ela recarregaria a lista vazia).
        chosen = next((a.name for a in _accounts.values() if a.default), "")
        save_plugin_config(NS, {
            "accounts": [
                {"name": a.name, "uuid": a.uuid, "address": a.address,
                 "display_name": a.display_name, "provider": a.provider,
                 "imap_host": a.imap_host, "imap_port": a.imap_port,
                 "imap_ssl": a.imap_ssl, "smtp_host": a.smtp_host,
                 "smtp_port": a.smtp_port, "smtp_security": a.smtp_security,
                 "default": a.default, "auto_start": a.auto_start,
                 "notify": a.notify, "otp": a.otp,
                 "filters": [dict(r) for r in a.filters]}
                for a in _accounts.values()
            ],
            "default_account": chosen,
        })
        _protect_vault()
    except Exception as e:
        _log(f"não consegui salvar a lista de contas: {e}")


def list_accounts() -> list[dict]:
    """As contas configuradas e seu estado ao vivo (sem senha, nunca)."""
    _ensure_accounts()
    return [a.public() for a in _accounts.values()]


def account_names() -> list[str]:
    _ensure_accounts()
    return [a.name for a in _accounts.values()]


def total_unread() -> int:
    _ensure_accounts()
    return sum(int(a.unread or 0) for a in _accounts.values())


def _find(name_or_uuid: str) -> MailAccount | None:
    _ensure_accounts()
    key = str(name_or_uuid or "").strip().lower()
    if not key:
        return None
    for a in _accounts.values():
        if a.uuid.lower() == key or a.name.lower() == key \
                or (a.address and a.address.lower() == key):
            return a
    return None


def _A(account: str | None = None) -> MailAccount:
    """Resolve nome/uuid/None (default) para uma conta. Levanta KeyError."""
    _ensure_accounts()
    if not _accounts:
        raise KeyError("nenhuma conta de e-mail configurada")
    if account is None or str(account).strip() == "":
        chosen = default_account_name()
        for a in _accounts.values():
            if a.name == chosen:
                return a
        for a in _accounts.values():
            if a.default:
                return a
        return next(iter(_accounts.values()))
    found = _find(str(account))
    if found is None:
        raise KeyError(f"não tenho a conta de e-mail '{account}'; tenho: "
                       + ", ".join(a.name for a in _accounts.values()))
    return found


def default_account_name() -> str:
    """A conta usada quando o dono não diz qual.

    A escolha gravada em ``plugin_config.mail.default_account`` manda; a flag
    ``default`` de cada conta é o reserva (configs antigas e o add_account).
    """
    _ensure_accounts()
    try:
        from memory.config_manager import get_plugin_setting
        chosen = str(get_plugin_setting(NS, "default_account", "") or "").strip()
    except Exception:
        chosen = ""
    if chosen:
        key = chosen.casefold()
        for a in _accounts.values():
            if a.name.casefold() == key or a.uuid.lower() == chosen.lower():
                return a.name
    for a in _accounts.values():
        if a.default:
            return a.name
    return ""


def _unique_uuid(name: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-") or "conta"
    uuid = f"mail-{safe}"
    i = 2
    while uuid in _accounts:
        uuid = f"mail-{safe}-{i}"
        i += 1
    return uuid


def add_account(name: str, address: str = "", provider: str = DEFAULT_PROVIDER,
                display_name: str = "", imap_host: str = "",
                imap_port: int | None = None, imap_ssl: bool | None = None,
                smtp_host: str = "", smtp_port: int | None = None,
                smtp_security: str = "", default: bool = False,
                auto_start: bool = True, notify: bool = False,
                otp: bool = False, filters: list | None = None,
                app_password_value: str = "") -> MailAccount:
    """Cria uma conta e persiste os METADADOS (a senha, se veio, vai ao cofre)."""
    _ensure_accounts()
    label = str(name or "").strip() or str(address or "").strip()
    if not label:
        raise ValueError("a conta precisa de um nome")
    if any(a.name.casefold() == label.casefold() for a in _accounts.values()):
        raise ValueError(f"já existe uma conta de e-mail chamada '{label}'")
    provider = str(provider or DEFAULT_PROVIDER).lower()
    if provider not in PROVIDERS:
        provider = "manual"
    defaults = _provider_defaults(provider, imap_host, imap_port, imap_ssl,
                                  smtp_host, smtp_port, smtp_security)
    a = MailAccount(name=label, uuid=_unique_uuid(label),
                    address=str(address or "").strip(),
                    display_name=str(display_name or "").strip(),
                    provider=provider, default=bool(default),
                    auto_start=bool(auto_start), notify=bool(notify),
                    otp=bool(otp), filters=filters, **defaults)
    a.ensure_dir()
    if default:
        for other in _accounts.values():
            other.default = False
    _accounts[a.uuid] = a
    _save_accounts()
    if app_password_value:
        set_app_password(a.uuid, app_password_value)
    return a


def update_account(name_or_uuid: str, new_name: str | None = None,
                   address: str | None = None, provider: str | None = None,
                   display_name: str | None = None,
                   imap_host: str | None = None,
                   imap_port: int | None = None,
                   imap_ssl: bool | None = None,
                   smtp_host: str | None = None,
                   smtp_port: int | None = None,
                   smtp_security: str | None = None,
                   auto_start: bool | None = None,
                   notify: bool | None = None,
                   otp: bool | None = None,
                   filters: list | None = None,
                   default: bool | None = None) -> bool:
    """Edita uma conta. False = não achou, nome repetido ou entrada inválida.

    Trocar o provedor reescreve host/porta do preset, a menos que o dono também
    tenha passado host/porta na mesma chamada (override explícito vence).
    """
    target = _find(name_or_uuid)
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
    if address is not None:
        addr = str(address).strip()
        if addr and not _ADDR_RE.match(addr):
            return False
        target.address = addr
    if provider is not None:
        key = str(provider or "").lower()
        if key not in PROVIDERS:
            return False
        target.provider = key
        defaults = _provider_defaults(key)
        target.imap_host = defaults["imap_host"]
        target.imap_port = defaults["imap_port"]
        target.imap_ssl = defaults["imap_ssl"]
        target.smtp_host = defaults["smtp_host"]
        target.smtp_port = defaults["smtp_port"]
        target.smtp_security = defaults["smtp_security"]
    if display_name is not None:
        target.display_name = str(display_name).strip()
    if imap_host is not None:
        target.imap_host = str(imap_host).strip()
    if imap_port is not None:
        try:
            target.imap_port = int(imap_port)
        except (TypeError, ValueError):
            return False
    if imap_ssl is not None:
        target.imap_ssl = bool(imap_ssl)
    if smtp_host is not None:
        target.smtp_host = str(smtp_host).strip()
    if smtp_port is not None:
        try:
            target.smtp_port = int(smtp_port)
        except (TypeError, ValueError):
            return False
    if smtp_security is not None:
        sec = str(smtp_security or "").lower()
        if sec not in ("ssl", "starttls", "plain"):
            return False
        target.smtp_security = sec
    if auto_start is not None:
        target.auto_start = bool(auto_start)
    if notify is not None:
        target.notify = bool(notify)
    if otp is not None:
        target.otp = bool(otp)
    if filters is not None:
        cleaned = _clean_filters(filters)
        if filters and len(cleaned) != len(filters):
            return False          # regra inválida: nada é gravado pela metade
        target.filters = cleaned
    if default is True:
        for other in _accounts.values():
            other.default = other is target
    elif default is False and target.default and len(_accounts) > 1:
        target.default = False
        next(other for other in _accounts.values() if other is not target).default = True
    target.ensure_dir()
    _save_accounts()
    return True


def set_default_account(name_or_uuid: str) -> bool:
    target = _find(name_or_uuid)
    if target is None:
        return False
    for other in _accounts.values():
        other.default = other is target
    _save_accounts()
    return True


def remove_account(name_or_uuid: str) -> bool:
    """Apaga a conta, o estado em disco, o inbox e a senha do cofre."""
    target = _find(name_or_uuid)
    if target is None:
        return False
    was_default = target.default
    _accounts.pop(target.uuid, None)
    try:
        shutil.rmtree(target.dir, ignore_errors=True)
    except Exception:
        pass
    try:
        target.inbox_file.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        from memory.config_manager import save_plugin_config
        save_plugin_config(SECRET_NS, {target.uuid: ""})   # merge: apaga o valor
        _protect_vault()
    except Exception as e:
        _log(f"não consegui limpar a senha da conta removida: {e}")
    if was_default and _accounts:
        next(iter(_accounts.values())).default = True
    _save_accounts()
    return True


# ── migração do formato antigo: uma conta na raiz de plugin_config.email ────

def migrate_legacy(legacy: dict | None = None) -> MailAccount | None:
    """Porta a conta única de ``plugin_config.email`` para esta camada.

    O formato antigo guardava tudo na RAIZ do namespace: ``address``,
    ``app_password``, ``name`` (nome de exibição), ``account_name`` e hosts
    vazios — o plugin caía em imap.gmail.com/smtp.gmail.com. Aqui a conta vira
    uma entrada de ``plugin_config.mail.accounts`` e a senha de app vai para o
    cofre (``mail_secrets.<uuid>``), com o espaço que o Gmail mostra na senha
    de app removido: senha de app colada com espaço não autentica.

    Nada é apagado do namespace antigo — o processo que ainda está rodando lê
    dali até o reinício (a limpeza é depois).

    Idempotente: rodar de novo não cria segunda conta nem mexe na senha que já
    está no cofre. Devolve a conta quando havia o que migrar (ou quando ela já
    existia) e ``None`` quando o formato antigo não tem endereço.
    """
    src = legacy if isinstance(legacy, dict) else None
    if src is None:
        try:
            from memory.config_manager import get_plugin_config
            src = get_plugin_config(LEGACY_NS) or {}
        except Exception as e:
            _log(f"não consegui ler a configuração antiga de e-mail: {e}")
            return None
    if not isinstance(src, dict):
        return None

    # Formato intermediário (`accounts: [...]`) também serve: a primeira conta
    # com endereço é a que o dono tinha. A raiz vence quando tem endereço.
    entry: dict = {}
    rows = src.get("accounts")
    if isinstance(rows, list):
        entry = next((a for a in rows
                      if isinstance(a, dict) and str(a.get("address") or "").strip()),
                     {}) or {}
    legacy_root = bool(str(src.get("address") or "").strip())
    if legacy_root:
        entry = src
    address = str(entry.get("address") or "").strip()
    if not address:
        return None

    if legacy_root:
        # Na raiz, `name` é o nome de exibição; o nome da CONTA é account_name.
        acct_name = str(src.get("account_name") or "").strip() or "principal"
        display_name = str(src.get("name") or "").strip()
    else:
        acct_name = str(entry.get("name") or "").strip() or "principal"
        display_name = str(entry.get("name_display")
                           or entry.get("name") or "").strip()

    password = re.sub(r"\s+", "",
                      str(entry.get("app_password") or src.get("app_password") or ""))
    imap_host = str(entry.get("imap_host") or src.get("imap_host") or "").strip()
    smtp_host = str(entry.get("smtp_host") or src.get("smtp_host") or "").strip()
    # Host vazio = Gmail (o padrão do plugin antigo). Outro host = manual, com
    # o que o dono tinha gravado.
    provider = "manual" if imap_host and "gmail" not in imap_host.lower() else "gmail"
    extra = ({"imap_host": imap_host, "smtp_host": smtp_host}
             if provider == "manual" else {})

    _ensure_accounts()
    existing = next((a for a in _accounts.values()
                     if a.address and a.address.casefold() == address.casefold()),
                    None)
    if existing is not None:
        if password and not has_password(existing.uuid):
            set_app_password(existing.uuid, password)
        return existing
    try:
        acct = add_account(acct_name, address, provider=provider,
                           display_name=display_name, default=True,
                           auto_start=True, notify=True, otp=True, **extra)
    except ValueError as e:
        _log(f"não consegui migrar a conta antiga de e-mail: {e}")
        return None
    if password:
        set_app_password(acct.uuid, password)
    return acct


def purge_legacy(backup: bool = True) -> list[str]:
    """Remove DE VERDADE as chaves antigas de ``plugin_config.email``.

    ``save_plugin_config`` faz merge e não sabe apagar chave, então a remoção é
    uma reescrita do arquivo inteiro: backup com permissão 600 primeiro (se o
    backup falhar, nada é tocado), troca das chaves depois, escrita atômica no
    fim. Devolve os NOMES removidos — os valores não são lidos, impressos nem
    logados em nenhum ponto deste caminho.

    Só as chaves de identidade/credencial do formato antigo saem
    (``LEGACY_KEYS``); qualquer outra coisa guardada no namespace fica. Quando o
    namespace esvazia, ele é removido do arquivo. Idempotente: sem as chaves,
    devolve lista vazia e não escreve nada.
    """
    from memory import config_manager as cm
    path = Path(getattr(cm, "CONFIG_FILE", "") or "")
    if not str(path) or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"não consegui ler a configuração para a limpeza: {e}")
        return []
    pc = data.get("plugin_config")
    ns = pc.get(LEGACY_NS) if isinstance(pc, dict) else None
    if not isinstance(ns, dict):
        return []
    removed = [key for key in LEGACY_KEYS if key in ns]
    if not removed:
        return []
    if backup:
        try:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dest = path.with_name(f"{path.name}.backup-{stamp}")
            shutil.copy2(path, dest)
            os.chmod(dest, 0o600)
        except Exception as e:
            _log(f"não consegui fazer o backup da configuração: {e}")
            return []                     # sem backup, não mexe em nada

    for key in removed:
        ns.pop(key, None)
    if not ns:
        pc.pop(LEGACY_NS, None)
    try:
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".",
                                   suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=4)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        _log(f"não consegui reescrever a configuração sem as chaves antigas: {e}")
        return []
    _log("chaves antigas de e-mail removidas: " + ", ".join(removed))
    return removed


# ── conexão: estado REAL, nunca recibo de ferramenta ────────────────────────

def _imap_open(acct: MailAccount):
    """Abre e autentica a caixa. Levanta a exceção do imaplib/socket.

    ``acct._stage`` marca a etapa em curso ANTES de cada passo que pode
    falhar. É o que permite o diagnóstico dizer "travou no TLS" em vez de um
    "erro de conexão" genérico — sem guardar senha nenhuma no caminho.
    """
    acct._stage = "preflight"
    password = app_password(acct.uuid)
    if not acct.address or not password:
        raise imaplib.IMAP4.error("faltam endereço e senha de app")
    host = acct.imap_host or str((PROVIDERS.get(acct.provider) or {}).get("imap", ("",))[0])
    if not host:
        raise imaplib.IMAP4.error("falta o host IMAP")
    acct._stage = "connect"          # DNS + TCP + TLS acontecem aqui dentro
    if acct.imap_ssl:
        conn = imaplib.IMAP4_SSL(host, acct.imap_port, timeout=CONNECT_TIMEOUT_S)
    else:
        conn = imaplib.IMAP4(host, acct.imap_port, timeout=CONNECT_TIMEOUT_S)
        acct._stage = "tls"
        try:
            conn.starttls(ssl_context=ssl.create_default_context())
        except Exception:
            pass
    # Janela aberta: a leitura do login/select/FETCH ganha teto próprio (30 s).
    # O teto de 12 s da conexão derrubava LOGIN lento do Gmail aos 12,5 s —
    # o servidor estava respondendo, não recusando; era o socket cortando.
    try:
        conn.socket().settimeout(IMAP_READ_TIMEOUT_S)
    except Exception:
        pass
    acct._stage = "login"
    conn.login(acct.address, password)
    acct._stage = "ready"
    return conn


def _imap_open_renewed(acct: MailAccount):
    """`_imap_open` com UMA segunda janela quando o servidor atrasou.

    Timeout de leitura não é recusa: a conexão antiga é descartada e tenta-se
    uma nova imediatamente, dentro da MESMA varredura. O que espera entre
    ciclos é o backoff que já existe (815a307); isto aqui só evita perder a
    rodada atual por um única resposta lenta.
    """
    try:
        return _imap_open(acct)
    except (TimeoutError, socket.timeout) as e:
        _log(f"{acct.name}: o servidor demorou ({type(e).__name__}) — "
             "abrindo uma nova janela antes de dar a tentativa por falha")
        conn = _imap_open(acct)
        _log(f"{acct.name}: reconectou na segunda janela")
        return conn


def _smtp_open(acct: MailAccount):
    password = app_password(acct.uuid)
    if not acct.address or not password:
        raise smtplib.SMTPAuthenticationError(535, b"faltam endereco e senha de app")
    host = acct.smtp_host
    if not host:
        raise smtplib.SMTPException("falta o host SMTP")
    sec = (acct.smtp_security or "starttls").lower()
    if sec == "ssl":
        conn = smtplib.SMTP_SSL(host, acct.smtp_port, timeout=CONNECT_TIMEOUT_S,
                                context=ssl.create_default_context())
    else:
        conn = smtplib.SMTP(host, acct.smtp_port, timeout=CONNECT_TIMEOUT_S)
        if sec == "starttls":
            conn.starttls(context=ssl.create_default_context())
    conn.login(acct.address, password)
    return conn


def _imap_endpoint(acct: MailAccount) -> tuple[str, int]:
    """Host/porta efetivos do IMAP (preset quando a conta não tem override)."""
    host = acct.imap_host or str(
        (PROVIDERS.get(acct.provider) or {}).get("imap", ("",))[0])
    try:
        port = int(acct.imap_port or 993)
    except (TypeError, ValueError):
        port = 993
    return str(host or ""), port


def _redact(acct: MailAccount, text: object) -> str:
    """Mensagem curta de exceção SEM a senha de app, se ela aparecer.

    Nunca chamamos isto com cabeçalho, corpo ou configuração inteira: só a
    mensagem da exceção. A senha é lida do cofre apenas para ser removida do
    texto — ela não é devolvida nem registrada.
    """
    out = _one_line(text, 160)
    try:
        secret = str(app_password(acct.uuid) or "")
    except Exception:
        secret = ""
    if len(secret) >= 3 and secret in out:
        out = out.replace(secret, "[senha omitida]")
    return out


def _stage_for(exc: Exception | None, fallback: str) -> str:
    """Etapa real da falha: dns | tcp | tls | login | select | preflight."""
    if exc is None:
        return fallback or "unknown"
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, ssl.SSLError):
        return "tls"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "login"
    if isinstance(exc, imaplib.IMAP4.error):
        # Erro do protocolo/login: se o abridor já marcou select, confia nele.
        if fallback == "preflight":
            return "preflight"
        return fallback if fallback in ("login", "select", "imap") else "login"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return fallback if fallback in ("dns", "tcp", "tls", "login", "select") else "tcp"
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError,
                        ConnectionAbortedError, BrokenPipeError)):
        return "tcp"
    if isinstance(exc, OSError):
        return fallback if fallback in ("dns", "tcp", "tls") else "tcp"
    return fallback or "unknown"


def _diag(acct: MailAccount, exc: Exception | None, stage: str,
          started: float, attempt: int) -> dict:
    """O que fica no log/estado de UMA tentativa. Só campos seguros.

    Não entra aqui senha, token, credencial, cabeçalho nem conteúdo de
    mensagem: tipo de exceção, mensagem curta (com a senha removida), host,
    porta, etapa e duração.
    """
    host, port = _imap_endpoint(acct)
    duration_ms = max(0, int(round((time.monotonic() - started) * 1000)))
    return {
        "attempt": int(attempt),
        "stage": _stage_for(exc, stage),
        "error_type": type(exc).__name__ if exc is not None else "",
        "error_message": _redact(acct, exc) if exc is not None else "",
        "host": host,
        "port": port,
        "duration_ms": duration_ms,
    }


def _write_state(acct: MailAccount, payload: dict) -> None:
    """state.json atômico: temporário no MESMO diretório + os.replace.

    Retry/reconnect pode rodar em paralelo com a varredura; escrita parcial
    nunca deve deixar o JSON quebrado. Só metadado de diagnóstico, nunca
    credencial.
    """
    try:
        acct.ensure_dir()
        fd, tmp = tempfile.mkstemp(prefix="state.", suffix=".tmp",
                                   dir=str(acct.dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, acct.state_file)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        _log(f"não consegui gravar o estado de {acct.name}: {e}")


def set_retry_notice(handler=None) -> None:
    """Registra quem mostra ao dono o aviso de queda/recuperação do e-mail.

    ``handler(payload)`` roda na thread de reconexão; quem for falar na
    interface deve agendar no loop dela (``call_soon_threadsafe``). O aviso só
    é chamado na SEGUNDA falha consecutiva e na recuperação — nunca por
    tentativa. Sem gancho, só o log registra (nada é falado por conta própria).
    """
    global _notice_hook
    _notice_hook = handler if callable(handler) else None


def _emit_notice(kind: str, acct: MailAccount) -> None:
    """Entrega o aviso de queda/recuperação a quem estiver registrado."""
    name = " ".join(str(acct.name or "").split())[:40] or "principal"
    if kind == "up":
        message = f"O e-mail {name} voltou a conectar."
    else:
        message = (f"Não consegui conectar o e-mail {name} de novo; "
                   "sigo tentando sozinho.")
    payload = {"kind": kind, "account": acct.name, "uuid": acct.uuid,
               "state": acct.state, "detail": acct.detail,
               "fail_streak": acct.fail_streak, "message": message}
    with _notice_lock:
        hook = _notice_hook
    _log(f"aviso de e-mail ({kind}) para {name}: {message}")
    if hook is None:
        return
    try:
        hook(dict(payload))
    except Exception as e:
        _log(f"o aviso de e-mail não foi entregue: {e}")


def _mark(acct: MailAccount, state: str, detail: str = "",
          diag: dict | None = None, track: bool = True) -> dict:
    """Grava o estado real na conta e devolve a entrada de boot.

    `diag` vem de `_diag()` e é persistido junto (tipo/etapa/duração), sem
    segredo. A contagem de falhas consecutivas decide o aviso: primeira falha
    é silêncio, segunda avisa uma vez, recuperação avisa uma vez. `track=False`
    é para falha de ENVIO (SMTP): não conta para a reconexão da caixa IMAP.
    """
    acct.state = state
    acct.connected = state == "connected"
    acct.detail = _one_line(detail, 200)
    acct.error = "" if acct.connected else acct.detail
    acct.last_check = time.time()
    diag = dict(diag or {})
    notice = ""
    with _lock:
        if track:
            if acct.connected:
                acct.fail_streak = 0
                if acct.down_notified:
                    acct.down_notified = False
                    notice = "up"
            else:
                acct.fail_streak += 1
                if acct.fail_streak == 2 and not acct.down_notified:
                    acct.down_notified = True
                    notice = "down"
    payload = {
        "state": state,
        "detail": acct.detail,
        "at": acct.last_check,
        "attempt": int(diag.get("attempt") or acct.attempts or 0),
        "stage": str(diag.get("stage") or acct._stage or ""),
        "error_type": str(diag.get("error_type") or ""),
        "error_message": str(diag.get("error_message") or ""),
        "host": str(diag.get("host") or ""),
        "port": int(diag.get("port") or 0),
        "duration_ms": int(diag.get("duration_ms") or 0),
        "consecutive_failures": int(acct.fail_streak),
    }
    _write_state(acct, payload)
    entry = {"name": acct.name, "uuid": acct.uuid, "address": acct.address,
             "connected": acct.connected, "state": state,
             "has_password": has_password(acct.uuid),
             "detail": acct.detail,
             "attempts": int(acct.attempts),
             "consecutive_failures": int(acct.fail_streak),
             "stage": payload["stage"]}
    if notice:
        _emit_notice(notice, acct)
    return entry


def _begin_attempt(acct: MailAccount) -> int:
    with _lock:
        acct.attempts += 1
        return acct.attempts


def _attempt(acct: MailAccount) -> dict:
    """UMA tentativa real de abrir a caixa; nunca levanta, sempre marca.

    Loga etapa, tipo de exceção, host/porta e duração — inclusive quando dá
    certo. A senha não entra no texto (nem se vier dentro da exceção).
    """
    started = time.monotonic()
    n = _begin_attempt(acct)
    stage = "preflight"
    state, detail, exc = "error", "", None
    conn = None
    try:
        if not acct.address:
            state, detail = "auth", "sem endereço de e-mail"
        elif not has_password(acct.uuid):
            state, detail = "auth", "falta a senha de app"
        else:
            stage = "connect"
            conn = _imap_open_renewed(acct)
            stage = "select"
            acct._stage = "select"
            conn.select("INBOX", readonly=True)
            stage = "ready"
            state, detail = "connected", "caixa de entrada acessível"
    except imaplib.IMAP4.error as e:
        exc = e
        state = "auth"
        detail = f"o servidor recusou: {_redact(acct, _reason(e))}"
    except (ssl.SSLError, OSError) as e:
        exc = e
        state = "error"
        detail = f"não consegui falar com o servidor: {_redact(acct, _reason(e))}"
    except Exception as e:
        exc = e
        state = "error"
        detail = _redact(acct, _reason(e))
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass
    hinted = getattr(acct, "_stage", "") or stage
    diag = _diag(acct, exc, hinted if exc is not None else stage, started, n)
    host, port = diag["host"], diag["port"]
    if state == "connected":
        _log(f"{acct.name}: tentativa {n} conectou em {diag['duration_ms']} ms "
             f"({host}:{port})")
    else:
        _log(f"{acct.name}: tentativa {n} falhou na etapa {diag['stage']} "
             f"({diag['error_type'] or 'erro'}) após {diag['duration_ms']} ms "
             f"em {host}:{port} — {diag['error_message'] or detail}")
    return _mark(acct, state, detail, diag=diag)


def test_account(name_or_uuid: str) -> dict:
    """Tenta conectar de verdade. ``state``: connected | auth | error.

    ``auth`` = o servidor recusou (senha de app errada/ausente, conta sem
    permissão): precisa de mão humana. ``error`` = rede, TLS, host errado —
    este é transitório e a reconexão automática continua.
    """
    acct = _find(name_or_uuid)
    if acct is None:
        return {"name": str(name_or_uuid or ""), "uuid": "", "address": "",
                "connected": False, "state": "error", "has_password": False,
                "detail": "conta não encontrada"}
    return _attempt(acct)



def _reason(exc: Exception) -> str:
    text = _one_line(str(exc) or exc.__class__.__name__, 160)
    low = text.lower()
    if "application-specific password" in low or "app password" in low:
        return "o provedor pede uma senha de app, não a senha normal"
    if "invalid credentials" in low or "authenticationfailed" in low \
            or "login failed" in low or "auth" in low and "refused" in low:
        return "usuário ou senha de app recusados"
    return text or "falha de conexão"


def _boot_entry(acct: MailAccount) -> dict:
    return {"name": acct.name, "uuid": acct.uuid, "address": acct.address,
            "connected": bool(acct.connected), "state": acct.state or "auth",
            "has_password": has_password(acct.uuid),
            "detail": acct.detail or acct.error,
            "auto_start": bool(acct.auto_start),
            "attempts": int(acct.attempts),
            "consecutive_failures": int(acct.fail_streak),
            "stage": str(getattr(acct, "_stage", "") or "")}


def _backoff_delay(fail_streak: int) -> float:
    """Espera antes da próxima tentativa: 5, 15, 45, 135, 240 s (com jitter).

    ``fail_streak`` é o número de falhas CONSECUTIVAS já ocorridas. O teto
    limita a espera, não as tentativas: com a conta habilitada, o laço segue.
    """
    step = max(1, int(fail_streak or 1)) - 1
    raw = min(BACKOFF_MAX_S, BACKOFF_BASE_S * (BACKOFF_FACTOR ** step))
    jittered = raw * (1.0 + random.uniform(-BACKOFF_JITTER, BACKOFF_JITTER))
    return max(1.0, min(BACKOFF_MAX_S, jittered))


def _retry_wait(seconds: float) -> bool:
    """Espera interrompível; True = pediram para parar. Testes trocam isto."""
    return _retry_stop.wait(max(0.0, float(seconds or 0.0)))


def _retry_active() -> bool:
    with _retry_lock:
        return any(t.is_alive() for t in _retry_threads.values())


def _schedule_retry(acct: MailAccount) -> threading.Thread | None:
    """Liga (uma vez por conta) o worker de reconexão em segundo plano."""
    if acct.state != "error":
        return None
    if not acct.auto_start or not has_password(acct.uuid):
        return None
    with _retry_lock:
        current = _retry_threads.get(acct.uuid)
        if current is not None and current.is_alive():
            return current
        if not any(t.is_alive() for t in _retry_threads.values()):
            _retry_stop.clear()       # um stop anterior não vale para esta conta
        worker = threading.Thread(
            target=_retry_worker, args=(acct,), daemon=True,
            name=f"jarvis-mail-retry-{acct.uuid}")
        _retry_threads[acct.uuid] = worker
        worker.start()
        _log(f"{acct.name}: falha transitória — reconexão automática ligada")
        return worker


def _retry_worker(acct: MailAccount, max_attempts: int | None = None) -> None:
    """Reconecta com backoff exponencial até conseguir (ou ser parado).

    Nunca mexe em ``auto_start``/configuração e nunca apaga credencial: se a
    conta continua habilitada, o laço continua. Erro de autenticação não é
    transitório e pausa aqui (insistir com senha errada pode travar a conta);
    erro de rede/TLS/DNS segue tentando, com teto de espera de 4 minutos.
    """
    made = 0
    while not _retry_stop.is_set():
        current = _accounts.get(acct.uuid)
        if current is not acct or not acct.auto_start:
            return
        delay = _backoff_delay(acct.fail_streak or 1)
        acct.next_retry_s = delay
        _log(f"{acct.name}: próxima tentativa em {delay:.0f} s "
             f"(falhas consecutivas: {acct.fail_streak})")
        if _retry_wait(delay):
            return
        if _retry_stop.is_set():
            return
        if acct.connected and acct.fail_streak == 0:
            acct.next_retry_s = 0.0
            return                    # a varredura recuperou antes do worker
        entry = _attempt(acct)
        made += 1
        if entry.get("connected"):
            acct.next_retry_s = 0.0
            return
        if entry.get("state") == "auth":
            _log(f"{acct.name}: reconexão pausada — precisa de senha de app "
                 "válida (a conta continua habilitada)")
            return
        if max_attempts is not None and made >= int(max_attempts):
            return


def connect_all(logger=None, settle_s: float = BOOT_SETTLE_S) -> list[dict]:
    """Tenta cada conta HABILITADA e devolve o estado REAL de cada uma.

    É o que o boot chama. A PRIMEIRA tentativa de cada conta roda em paralelo,
    com a janela limitada de sempre — o boot nunca fica refém do e-mail. O que
    falhar por rede/TLS/DNS ganha um worker de reconexão com backoff de 5 s a
    4 min; a conta NÃO é desabilitada e nenhuma política de escuta muda.
    Devolve uma entrada por conta (a primeira tentativa; o worker continua).
    """
    global _logger
    if logger is not None:
        _logger = logger
    _ensure_accounts()
    targets = [a for a in _accounts.values() if a.auto_start]
    if not targets:
        return []
    with _retry_lock:
        if not any(t.is_alive() for t in _retry_threads.values()):
            _retry_stop.clear()
    workers = []
    for acct in targets:
        worker = threading.Thread(
            target=_safe_test, args=(acct,), daemon=True,
            name=f"jarvis-mail-connect-{acct.uuid}")
        worker.start()
        workers.append(worker)
    deadline = time.time() + START_WAIT_S
    for worker in workers:
        worker.join(timeout=max(0.0, deadline - time.time()))
    settle_deadline = time.time() + max(0.0, float(settle_s or 0.0))
    while time.time() < settle_deadline and any(
            a.state == "unknown" for a in targets):
        time.sleep(0.1)
    # Fotografa a primeira tentativa ANTES de ligar o worker: o retorno do
    # boot diz o que aconteceu de fato, não o que o worker já conseguiu.
    entries = [_boot_entry(a) for a in targets]
    for acct in targets:
        if acct.state == "error":
            _schedule_retry(acct)
    return entries


def _safe_test(acct: MailAccount) -> None:
    try:
        _attempt(acct)
    except Exception as e:
        _mark(acct, "error", _redact(acct, _reason(e)))



def status(account: str | None = None) -> dict:
    """Estado de uma conta (ou da default). Sem conta, um resumo de todas."""
    _ensure_accounts()
    if account is None and len(_accounts) != 1:
        any_connected = any(a.connected for a in _accounts.values())
        return {"accounts": len(_accounts), "connected": any_connected,
                "any_connected": any_connected,
                "total_unread": total_unread(),
                "names": [a.name for a in _accounts.values()]}
    try:
        a = _A(account)
    except KeyError:
        return {"accounts": 0, "connected": False, "state": "unknown",
                "account": "", "address": "", "error": "sem contas",
                "detail": "sem contas", "has_password": False}
    return {"accounts": len(_accounts), "account": a.name, "uuid": a.uuid,
            "address": a.address, "connected": a.connected,
            "state": a.state, "error": a.error, "detail": a.detail,
            "has_password": has_password(a.uuid), "unread": a.unread,
            "last_check": a.last_check}


def status_line(account: str | None = None) -> str:
    st = status(account)
    if not st.get("accounts"):
        return "nenhuma conta de e-mail configurada ainda"
    who = f" {st.get('account')}" if st.get("accounts", 0) > 1 else ""
    if st.get("connected"):
        return f"a conta de e-mail{who} está conectada ({st.get('address') or 'sem endereço'})"
    if st.get("state") == "auth":
        return (f"a conta de e-mail{who} precisa de senha de app válida "
                f"({st.get('detail') or 'não autenticou'})")
    return f"a conta de e-mail{who} não conectou: {st.get('detail') or 'sem resposta'}"


# ── inbox: cabeçalhos em jsonl, corpo só a pedido ───────────────────────────

def _append_inbox(acct: MailAccount, item: dict) -> None:
    try:
        acct.inbox_file.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with acct.inbox_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
                acct.appended += 1
                if acct.appended >= INBOX_TRIM_EVERY:
                    acct.appended = 0
                    _trim_inbox(acct)
    except Exception as e:
        _log(f"não consegui gravar no inbox de {acct.name}: {e}")


def _trim_inbox(acct: MailAccount) -> None:
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


def _load_inbox(acct: MailAccount) -> list[dict]:
    try:
        if not acct.inbox_file.exists():
            return []
        out = []
        for line in acct.inbox_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                out.append(row)
        return out
    except Exception:
        return []


def _item_key(item: dict) -> str:
    mid = str(item.get("message_id") or "").strip()
    if mid:
        return mid
    return (f"{item.get('uid') or ''}|{item.get('from') or ''}"
            f"|{item.get('subject') or ''}|{item.get('date') or ''}")


def _header_item(acct: MailAccount, uid: str, raw: bytes) -> dict:
    """Um cabeçalho → o item do inbox. NUNCA traz o corpo."""
    msg = email.message_from_bytes(raw)
    name, addr = parseaddr(_decode_header(msg.get("From") or ""))
    subject = _decode_header(msg.get("Subject") or "")
    date_hdr = _decode_header(msg.get("Date") or "")
    message_id = _decode_header(msg.get("Message-ID") or "")
    ts = 0.0
    if date_hdr:
        try:
            ts = float(parsedate_to_datetime(date_hdr).timestamp())
        except Exception:
            ts = 0.0
    return {
        "account": acct.name,
        "uid": str(uid or ""),
        "message_id": message_id,
        "from": addr or name,
        "sender": name or addr,
        "subject": subject,
        "date": date_hdr or (formatdate(ts) if ts else ""),
        "ts": ts,
        "has_body": True,        # o corpo existe no servidor; não foi baixado
        "body_stored": False,    # o jsonl guarda cabeçalho, nunca o texto
    }


def _collect_unseen(acct: MailAccount, conn, limit: int = SCAN) -> list[dict]:
    """Os NÃO LIDOS de uma conexão já aberta: headers novos no jsonl.

    Um só lugar faz o trabalho; ``refresh()`` chama isto com conexão própria e
    a varredura de fundo com a conexão que já abriu (uma conexão por caixa por
    ciclo, nunca duas).
    """
    typ, data = conn.uid("SEARCH", None, "UNSEEN")
    ids = (data[0].split() if data and data[0] else [])
    known = {_item_key(row) for row in _load_inbox(acct)}
    new_items: list[dict] = []
    for uid in ids[-max(1, int(limit or SCAN)):]:
        uid_s = uid.decode() if isinstance(uid, bytes) else str(uid)
        try:
            typ, blob = conn.uid(
                "FETCH", uid_s,
                "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
        except Exception as e:
            _log(f"não consegui ler o cabeçalho de {acct.name}: {e}")
            continue
        if typ != "OK" or not blob or not isinstance(blob[0], tuple):
            continue
        item = _header_item(acct, uid_s, blob[0][1])
        key = _item_key(item)
        if key in known:
            continue
        known.add(key)
        _append_inbox(acct, item)
        new_items.append(item)
    acct.unread = len(ids)
    _mark(acct, "connected", "caixa de entrada acessível")
    return new_items


def refresh(name_or_uuid: str | None = None, limit: int = SCAN) -> list[dict]:
    """Busca os NÃO LIDOS recentes e grava os cabeçalhos NOVOS no jsonl.

    Só cabeçalho (``BODY.PEEK[HEADER.FIELDS ...]``) e caixa em modo somente
    leitura: nada é marcado como lido. Devolve só o que era novo para esta
    conta; falha de rede devolve [] (a conta guarda o motivo).
    """
    _ensure_accounts()
    targets = [_A(name_or_uuid)] if name_or_uuid else list(_accounts.values())
    fresh: list[dict] = []
    for acct in targets:
        if not acct.address or not has_password(acct.uuid):
            _mark(acct, "auth", "faltam endereço e senha de app")
            continue
        conn = None
        try:
            conn = _imap_open_renewed(acct)
            conn.select("INBOX", readonly=True)
            fresh.extend(_collect_unseen(acct, conn, limit))
        except imaplib.IMAP4.error as e:
            _mark(acct, "auth",
                  f"o servidor recusou: {_redact(acct, _reason(e))}")
        except Exception as e:
            _mark(acct, "error", _redact(acct, _reason(e)))
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass
    return fresh


def recent(limit: int = 8, account: str | None = None) -> list[dict]:
    """Os últimos cabeçalhos guardados, do mais novo para o mais antigo."""
    try:
        acct = _A(account)
    except KeyError:
        return []
    items = _load_inbox(acct)
    return list(reversed(items))[:max(1, int(limit or 8))]


def unread_count(account: str | None = None) -> int:
    """Não lidos: o último número que o SERVIDOR disse (0 se nunca falou)."""
    if account:
        acct = _find(account)
        return int(acct.unread) if acct else 0
    _ensure_accounts()
    return total_unread()


# ── leitura para o vigia de códigos (OTP): API pública, somente leitura ─────
# O vigia (core/email_otp_watch.py) não abre IMAP por conta própria: ele pede
# as contas a `otp_accounts()`, recebe a conexão já autenticada de
# `open_inbox()` e lê cabeçalho/corpo por estas funções. A senha nunca sai do
# cofre — quem autentica é `_imap_open`, aqui dentro.

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _since_criteria(days: int) -> str:
    """`SINCE` em inglês (formato IMAP), independente do locale da máquina."""
    n = max(1, int(days or 1))
    t = time.gmtime(time.time() - n * 86400)
    return f"SINCE {t.tm_mday:02d}-{_MONTHS[t.tm_mon - 1]}-{t.tm_year}"


def otp_accounts() -> list[dict]:
    """As contas cujo corpo pode conter código de verificação.

    Só metadado público (nome, uuid, endereço, flags) — nunca a senha. A conta
    entra aqui quando o dono marcou `otp` e existe senha de app no cofre: sem
    credencial o passo de OTP não tem o que fazer.
    """
    _ensure_accounts()
    return [a.public() for a in _accounts.values()
            if a.otp and a.address and has_password(a.uuid)]


def open_inbox(name_or_uuid: str | None = None):
    """Conexão autenticada com a INBOX aberta em modo SOMENTE LEITURA.

    Devolve a conexão do imaplib já com `select(..., readonly=True)`: quem usar
    não tem como marcar mensagem como lida sem pedir de propósito. Erros de
    rede/login sobem para quem chamou (a senha não aparece neles).
    """
    acct = _A(name_or_uuid)
    conn = _imap_open(acct)
    try:
        conn.select("INBOX", readonly=True)
    except Exception:
        try:
            conn.logout()
        except Exception:
            pass
        raise
    return conn


def recent_headers(conn, days: int = 2, limit: int = SCAN) -> list[dict]:
    """Cabeçalhos das mensagens dos últimos `days`, do mais novo ao mais velho.

    Uma conexão já aberta (de `open_inbox`), nenhuma caixa é marcada como lida
    e o corpo não é baixado: devolve ``uid, from, subject, ts, when`` — o
    ``from`` inteiro, porque o detector de OTP tira o nome de exibição dali.
    """
    try:
        typ, data = conn.uid("SEARCH", None, _since_criteria(days))
    except Exception:
        return []
    if typ != "OK" or not data or not data[0]:
        return []
    uids = (data[0] or b"").split()[-max(1, int(limit or 1)):]
    rows: list[dict] = []
    for i in range(0, len(uids), 20):
        chunk = b",".join(uids[i:i + 20])
        try:
            typ, data = conn.uid(
                "FETCH", chunk, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        except Exception:
            continue
        if typ != "OK" or not data:
            continue
        for item in data:
            if not (isinstance(item, tuple) and len(item) == 2):
                continue
            meta, raw = item
            if not raw:
                continue
            try:
                msg = email.message_from_bytes(raw)
            except Exception:
                continue
            ts = 0.0
            when = ""
            date_hdr = _decode_header(msg.get("Date") or "")
            if date_hdr:
                try:
                    dt = parsedate_to_datetime(date_hdr)
                    ts = float(dt.timestamp())
                    when = dt.strftime("%d/%m %H:%M")
                except Exception:
                    ts, when = 0.0, date_hdr[:20]
            m = re.search(rb"UID (\d+)", meta or b"")
            rows.append({
                "uid": m.group(1).decode() if m else "",
                "from": _decode_header(msg.get("From") or ""),
                "subject": _decode_header(msg.get("Subject") or ""),
                "ts": ts,
                "when": when,
            })
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows


def peek_body(conn, uid: str, limit: int = BODY_CHARS) -> str:
    """O texto de UMA mensagem, com ``BODY.PEEK[]`` (ler não marca como lido).

    Não entra em frase falada nem em jsonl: existe para o detector de códigos
    olhar o texto e devolver só o número. HTML vira texto sem tags.
    """
    key = str(uid or "").strip()
    if not key:
        return ""
    typ, data = conn.uid("FETCH", key, "(BODY.PEEK[])")
    if typ != "OK" or not data:
        return ""
    raw = next((d[1] for d in data if isinstance(d, tuple)), None)
    if not raw:
        return ""
    try:
        text = _body_text(email.message_from_bytes(raw))
    except Exception:
        return ""
    return _CTRL_RE.sub(" ", text)[:max(200, int(limit or BODY_CHARS))]


# ── o texto de um aviso: remetente + assunto, nunca o corpo ─────────────────

def alert_line(item: dict) -> str:
    """Uma frase curta e segura para falar sobre um e-mail que chegou.

    Só remetente e assunto. O corpo não é lido aqui e não há caminho neste
    módulo que o coloque numa frase falada. Passa pelo guarda de fala.
    """
    sender = _one_line(item.get("sender") or item.get("from") or "alguém", 60)
    subject = _one_line(item.get("subject") or "", 120)
    if subject:
        raw = f"Chegou um e-mail de {sender} com o assunto «{subject}»."
    else:
        raw = f"Chegou um e-mail de {sender}, sem assunto."
    return _safe_spoken(raw)


# ── corpo: só quando o dono pede, e nunca falado por este módulo ────────────

def _body_text(msg) -> str:
    """text/plain preferido; HTML só como último recurso, com tags fora."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" \
                    and "attachment" not in str(part.get("Content-Disposition") or "").lower():
                try:
                    return str(part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace"))
                except Exception:
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = str(part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace"))
                    return re.sub(r"<[^>]+>", " ", html)
                except Exception:
                    continue
        return ""
    try:
        return str(msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", "replace"))
    except Exception:
        return ""


def fetch_body(name_or_uuid: str, uid_or_key: str,
               limit: int = BODY_CHARS) -> tuple[bool, str, str]:
    """Baixa o corpo de UMA mensagem, quando o dono pedir explicitamente.

    Devolve (ok, assunto, corpo). O corpo é para a tela/leitura do dono: não é
    falado por este módulo nem entra no aviso. ``BODY.PEEK[]`` de novo — ler
    não marca como lido.
    """
    acct = _find(name_or_uuid)
    if acct is None:
        return False, "", "conta não encontrada"
    key = str(uid_or_key or "").strip()
    if not key:
        return False, "", "falta a mensagem (uid ou message-id)"
    conn = None
    try:
        conn = _imap_open(acct)
        conn.select("INBOX", readonly=True)
        uid = key
        subject = ""
        if not key.isdigit():
            for row in _load_inbox(acct):
                if _item_key(row) == key:
                    uid = str(row.get("uid") or "")
                    subject = str(row.get("subject") or "")
                    break
        if not uid.isdigit():
            return False, subject, "não achei essa mensagem no servidor"
        typ, blob = conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK" or not blob or not isinstance(blob[0], tuple):
            return False, subject, "o servidor não devolveu a mensagem"
        msg = email.message_from_bytes(blob[0][1])
        subject = subject or _decode_header(msg.get("Subject") or "")
        body = _one_line(_body_text(msg), max(200, int(limit or BODY_CHARS)))
        _mark(acct, "connected", "caixa de entrada acessível")
        return True, subject, body
    except imaplib.IMAP4.error as e:
        why = _redact(acct, _reason(e))
        _mark(acct, "auth", f"o servidor recusou: {why}")
        return False, "", why
    except Exception as e:
        why = _redact(acct, _reason(e))
        _mark(acct, "error", why)
        return False, "", why
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass


# ── medição do filtro (FASE2-TASK5) ─────────────────────────────────────────

class _FilterMetrics:
    """Contadores por conta: recebidos, filtrados, notificados, tempo."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}

    def record(self, account: str, *, received: int = 0, filtered: int = 0,
               notified: int = 0, processing_ms: float = 0.0) -> None:
        with self._lock:
            entry = self._data.setdefault(account, {
                "received": 0, "filtered": 0, "notified": 0,
                "processing_ms": 0.0, "count": 0})
            entry["received"] += received
            entry["filtered"] += filtered
            entry["notified"] += notified
            entry["processing_ms"] += processing_ms
            entry["count"] += 1 if (received or filtered or notified) else 0

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_filter_metrics = _FilterMetrics()


def filter_metrics() -> dict[str, dict]:
    """Contadores de filtro por conta (FASE2-TASK5)."""
    return _filter_metrics.snapshot()


def measure_filter() -> dict[str, dict]:
    """Roda UMA varredura e devolve as métricas do filtro (FASE2-TASK5).

    Métricas: total recebidos, filtrados (ignorados), notificados e tempo
    médio de processamento por e-mail, por conta. Registra no activity log.
    """
    _filter_metrics.clear()
    poll_once()
    metrics = _filter_metrics.snapshot()
    # Log the metrics
    for acct_name, data in metrics.items():
        avg_ms = (data["processing_ms"] / data["count"]
                  if data["count"] else 0.0)
        _log(f"métrica filtro {acct_name}: recebidos={data['received']} "
             f"filtrados={data['filtered']} notificados={data['notified']} "
             f"tempo_médio={avg_ms:.1f}ms")
    return metrics


# ── varredura em background (silenciosa por padrão) ─────────────────────────

_watch_lock = threading.Lock()
_watch_thread: threading.Thread | None = None
_watch_stop = threading.Event()
_watch_hook = None
_otp_processor = None      # callable(conta_pública, conexão) -> uids com código


def set_otp_processor(fn=None) -> None:
    """Registra o passo de códigos que roda DENTRO desta varredura.

    ``fn(conta, conn)`` recebe o metadado público da conta e a conexão já
    aberta em somente leitura, e devolve os UIDs cujo anúncio foi de código —
    esses não viram "chegou um e-mail" (o dono já ouviu o código). Quem
    registra é ``core/email_otp_watch.start()``; ``None`` desliga o passo.
    """
    global _otp_processor
    _otp_processor = fn if callable(fn) else None


def _otp_covered_addresses() -> set[str]:
    """Endereços em que o passo de códigos (core/email_otp_watch.py) roda.

    Depois da unificação é informação, não lista de exclusão: existe UM laço
    por caixa — o desta camada — e o passo de OTP viaja dentro dele, na mesma
    conexão somente leitura. Quem responde é o registro novo
    (``plugin_config.mail`` + cofre), nunca o namespace antigo.
    """
    _ensure_accounts()
    return {a.address.casefold() for a in _accounts.values()
            if a.address and a.otp and has_password(a.uuid)}


def _hook_wants_rule(hook) -> bool:
    """O gancho aceita a regra como 2º argumento? (hooks antigos: 1 só)."""
    try:
        import inspect
        sig = inspect.signature(hook)
        positional = [p for p in sig.parameters.values()
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        return len(positional) >= 2 or any(
            p.kind == p.VAR_POSITIONAL for p in sig.parameters.values())
    except (TypeError, ValueError):
        return False


def poll_once(hook=None) -> list[dict]:
    """Uma varredura de todas as contas habilitadas; devolve o que é novo.

    UMA passada por caixa: abre a conexão em somente leitura, guarda os
    cabeçalhos novos, roda o passo de códigos (quando a conta tem ``otp`` e há
    processador registrado) e só então avisa "chegou um e-mail" — mensagem que
    virou código não é avisada duas vezes. Conta com ``notify`` desligado entra
    no inbox mas não chama o gancho: o aviso em voz é decisão do dono.

    FILTRO (passos 2/4 do fluxo do dono): a conta sem regras se comporta como
    sempre. Com regras, a PRIMEIRA que casa decide: ``avisa`` fala, ``ignora``
    não gera aviso nenhum, ``classificar`` entrega ao gancho a regra junto (e,
    se a regra pedir ``body``, um TRECHO do corpo como DADO — nunca ordem,
    nunca por padrão, nunca no jsonl). E-mail fora de todas as regras não
    gera aviso nenhum: nem fala, nem SYS.
    """
    _ensure_accounts()
    hook = hook if hook is not None else _watch_hook
    processor = _otp_processor
    wants_rule = callable(hook) and _hook_wants_rule(hook)
    delivered: list[dict] = []
    for acct in list(_accounts.values()):
        if not acct.auto_start and not acct.notify and not acct.otp:
            continue
        conn = None
        started = time.monotonic()
        n = _begin_attempt(acct)
        acct._stage = "connect"
        try:
            conn = _imap_open_renewed(acct)
            acct._stage = "select"
            conn.select("INBOX", readonly=True)
            fresh = _collect_unseen(acct, conn)
            code_uids: set[str] = set()
            if acct.otp and callable(processor):
                try:
                    code_uids = {str(u) for u in (processor(acct.public(), conn) or ())}
                except Exception as e:
                    _log(f"o passo de códigos de {acct.name} falhou: {e}")
            if callable(hook) and acct.notify:
                for item in fresh:
                    item_start = time.monotonic()
                    if str(item.get("uid") or "") in code_uids:
                        continue          # já foi falado como código
                    rule = match_filter(item, acct.filters)
                    if rule is None:
                        if acct.filters:
                            _filter_trace(acct, item, None, "ignora")
                            _filter_metrics.record(
                                acct.name, received=1, filtered=1,
                                processing_ms=(time.monotonic() - item_start) * 1000)
                        continue
                    action = str((rule or {}).get("action") or "avisa")
                    if acct.filters:
                        # Rastro de cada decisão de filtro (passos 2-4 do
                        # dono); conta sem regra não tem decisão de filtro.
                        _filter_trace(acct, item, rule, action)
                    if action == "ignora":
                        _filter_metrics.record(
                            acct.name, received=1, filtered=1,
                            processing_ms=(time.monotonic() - item_start) * 1000)
                        continue
                    if action == "classificar" and (rule or {}).get("body"):
                        # O trecho sai DAQUI, da conexão já aberta e somente
                        # leitura; vai só na cópia do gancho — o jsonl fica
                        # cabeçalho puro.
                        try:
                            item["body_excerpt"] = peek_body(
                                conn, str(item.get("uid") or ""),
                                FILTER_BODY_CHARS)
                        except Exception as e:
                            _log(f"o trecho do corpo de {acct.name} falhou: {e}")
                    try:
                        if wants_rule:
                            hook(dict(item), dict(rule) if rule else None)
                        else:
                            hook(dict(item))
                    except Exception as e:
                        _log(f"aviso de e-mail novo não entregue: {e}")
                        _filter_metrics.record(
                            acct.name, received=1,
                            processing_ms=(time.monotonic() - item_start) * 1000)
                    else:
                        delivered.append(item)
                        _filter_metrics.record(
                            acct.name, received=1, notified=1,
                            processing_ms=(time.monotonic() - item_start) * 1000)
            elif acct.notify:
                # Conta com notify mas sem hook: ainda registra métricas
                for item in fresh:
                    item_start = time.monotonic()
                    if str(item.get("uid") or "") in code_uids:
                        continue
                    rule = match_filter(item, acct.filters)
                    if rule is None:
                        if acct.filters:
                            _filter_metrics.record(
                                acct.name, received=1, filtered=1,
                                processing_ms=(time.monotonic() - item_start) * 1000)
                        continue
                    action = str((rule or {}).get("action") or "avisa")
                    if action == "ignora":
                        _filter_metrics.record(
                            acct.name, received=1, filtered=1,
                            processing_ms=(time.monotonic() - item_start) * 1000)
                    else:
                        _filter_metrics.record(
                            acct.name, received=1, notified=1,
                            processing_ms=(time.monotonic() - item_start) * 1000)
            if not acct.connected or acct.fail_streak:
                # A varredura voltou a abrir a caixa: registra a recuperação
                # (e é isto que limpa a contagem de falhas consecutivas).
                _mark(acct, "connected", "caixa de entrada acessível")
        except imaplib.IMAP4.error as e:
            diag = _diag(acct, e, getattr(acct, "_stage", "") or "login",
                         started, n)
            _log(f"{acct.name}: varredura tentativa {n} recusada no login "
                 f"({_redact(acct, e)}) após {diag['duration_ms']} ms em "
                 f"{diag['host']}:{diag['port']}")
            _mark(acct, "auth",
                  f"o servidor recusou: {_redact(acct, _reason(e))}", diag=diag)
        except Exception as e:
            diag = _diag(acct, e, getattr(acct, "_stage", "") or "connect",
                         started, n)
            _log(f"{acct.name}: varredura tentativa {n} falhou na etapa "
                 f"{diag['stage']} ({diag['error_type'] or 'erro'}) após "
                 f"{diag['duration_ms']} ms em {diag['host']}:{diag['port']} "
                 f"— {diag['error_message'] or _redact(acct, e)}")
            _mark(acct, "error",
                  f"não consegui falar com o servidor: "
                  f"{_redact(acct, _reason(e))}", diag=diag)
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass
    return delivered


def start(hook=None, logger=None, interval: float = POLL_S) -> None:
    """Liga a varredura em background. Sem conta, não faz nada.

    É o ÚNICO laço que abre as caixas: cabeçalho para o inbox, aviso de
    e-mail novo (conta com ``notify``) e códigos de verificação (conta com
    ``otp``) saem do mesmo ciclo e da mesma conexão. Havendo conta com
    ``otp``, o intervalo desce para `OTP_POLL_S` — o vigia de códigos anunciava
    a cada 60 s e essa cadência é preservada.
    """
    global _watch_thread, _watch_hook, _logger
    if logger is not None:
        _logger = logger
    if hook is not None:
        _watch_hook = hook
    with _watch_lock:
        if _watch_thread is not None and _watch_thread.is_alive():
            return
        if not _load_entries():
            return
        tick = max(30.0, float(interval or POLL_S))
        if any(a.otp for a in _accounts.values()) or _otp_processor is not None:
            tick = min(tick, OTP_POLL_S)
        with _retry_lock:
            if not any(t.is_alive() for t in _retry_threads.values()):
                _retry_stop.clear()
        _watch_stop.clear()
        _watch_thread = threading.Thread(
            target=_watch_loop, args=(tick,),
            daemon=True, name="jarvis-mail-watch")
        _watch_thread.start()


def _watch_loop(interval: float) -> None:
    """Varredura periódica; quando uma caixa cai, a espera cresce.

    O intervalo normal continua sendo o do dono (180 s, ou 60 s com OTP). Se
    a varredura encontrar conta em erro transitório, a próxima tentativa usa o
    MESMO backoff exponencial (5 s a 4 min, limitado pelo intervalo) em vez de
    dormir o intervalo inteiro — rede que voltou não espera 3 minutos. Se já
    existe worker de reconexão para a conta, não duplicamos a tentativa.
    """
    fail_streak = 0
    while not _watch_stop.is_set():
        try:
            poll_once()
            down = any(a.auto_start and a.state == "error"
                       for a in _accounts.values())
            if down and not _retry_active():
                fail_streak += 1
                wait = min(float(interval), _backoff_delay(fail_streak))
                _log(f"varredura com conta em erro: nova tentativa em "
                     f"{wait:.0f} s")
            else:
                fail_streak = 0
                wait = float(interval)
        except Exception as e:
            fail_streak += 1
            wait = min(float(interval), _backoff_delay(fail_streak))
            _log(f"a varredura de e-mail falhou ({type(e).__name__}): "
                 f"{_one_line(e, 160)}; nova tentativa em {wait:.0f} s")
        _watch_stop.wait(wait)


def stop() -> None:
    global _watch_thread
    _watch_stop.set()
    _retry_stop.set()
    with _retry_lock:
        _retry_threads.clear()
    with _watch_lock:
        thread, _watch_thread = _watch_thread, None
    if thread is not None and thread.is_alive() \
            and thread is not threading.current_thread():
        thread.join(timeout=3)


def reset() -> None:
    """Esquece o estado em memória (testes e troca de config na mão).

    O pedido de envio sai só da memória: quem tiver um arquivo em disco
    continua sendo lido dali, pelo prazo normal. Reconexões em voo são
    interrompidas — nenhuma thread antiga mexe em conta da próxima rodada.
    """
    global _watch_hook, _pending, _otp_processor
    _retry_stop.set()
    with _retry_lock:
        _retry_threads.clear()
    with _lock:
        _accounts.clear()
    _watch_hook = None
    _otp_processor = None
    set_retry_notice(None)
    with _pending_lock:
        _pending = None


# ── envio: pedido estacionado, confirmação pelo fator do dono (voz ou senha) ──

_pending_lock = threading.Lock()
_pending: dict | None = None


def _load_pending() -> dict | None:
    global _pending
    with _pending_lock:
        raw = _pending
        if raw is None:
            try:
                raw = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except Exception as e:
                _log(f"não consegui ler o pedido de envio: {e}")
                return None
            if not isinstance(raw, dict):
                return None
        try:
            expired = time.time() - float(raw.get("at") or 0) > CONFIRM_TIMEOUT_S
        except Exception:
            expired = True
        if expired:
            _pending = None
            return None
        _pending = dict(raw)
        return dict(raw)


def _store_pending(payload: dict | None) -> None:
    global _pending
    with _pending_lock:
        _pending = dict(payload) if payload else None
        try:
            PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
            if not payload:
                PENDING_PATH.unlink(missing_ok=True)
                return
            fd, tmp = tempfile.mkstemp(prefix="mail_send_pending.", suffix=".tmp",
                                       dir=str(PENDING_PATH.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, PENDING_PATH)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        except Exception as e:
            _log(f"não consegui gravar o pedido de envio: {e}")


def pending_send() -> dict | None:
    """O envio esperando a frase do dono (ou None).

    Devolve para quem precisa mostrar na tela; a senha de app não está aqui e
    o corpo só existe porque foi ditado agora — ele não vai para o jsonl.
    """
    return _load_pending()


def request_send(to: str, subject: str, body: str,
                 account: str | None = None) -> tuple[bool, str]:
    """Estaciona um envio. NUNCA envia: isso é trabalho do portão de voz.

    A frase devolvida é a mesma que o modelo repete ao dono: dizer o pedido e
    pedir a confirmação FALADA. O corpo fica aqui (não vai para o jsonl do
    inbox) e só é usado por ``confirm_send``.
    """
    recipient = str(to or "").strip()
    if not recipient:
        return False, "para quem? falta o destinatário do e-mail"
    if not _ADDR_RE.match(recipient):
        return False, (f"'{recipient}' não parece um endereço de e-mail "
                       "válido; me diga o endereço completo")
    text = str(body or "").strip()
    if not text:
        return False, "o corpo do e-mail está vazio; o que eu escrevo?"
    subject = _one_line(subject or "", 140)
    try:
        acct = _A(account)
    except KeyError as e:
        return False, str(e)
    if not app_password(acct.uuid):
        return False, (f"a conta {acct.name} está sem senha de app; sem isso "
                       "eu não consigo enviar")
    payload = {"to": recipient, "subject": subject, "body": text,
               "account": acct.name, "uuid": acct.uuid,
               "at": time.time(),
               "phrase": f"{CONFIRM_MARKER} o e-mail"}
    _store_pending(payload)
    em = subject or "(sem assunto)"
    return True, (f"Preparei o e-mail para {recipient} com o assunto «{em}». "
                  f"Nada foi enviado: diga em voz alta «{CONFIRM_MARKER} o "
                  "e-mail» para eu mandar, ou «cancela» para descartar.")


def cancel_send() -> str:
    if _load_pending() is None:
        return "não havia envio de e-mail esperando confirmação"
    _store_pending(None)
    return "envio de e-mail cancelado; nada foi mandado"


def confirm_send(voice_ok: bool = False) -> tuple[bool, str]:
    """Executa o envio estacionado. Só com autorização do dono.

    ``voice_ok`` é o veredito do portão do dono: verdadeiro quando a fala
    foi confirmada pelo voiceprint (main._speaker_order_identity) OU
    quando a senha digitada correta foi verificada — desde a decisão do
    dono de 06/11/2026 os dois fatores são plenos e iguais
    (core/owner_auth.authorized_factor). Sem áudio do dono, sem senha,
    ou com a voz de outra pessoa, isto recusa e o pedido continua parado.
    Texto digitado comum (sem senha) nunca chega aqui com True.
    """
    if not voice_ok:
        return False, "envio de e-mail exige a sua voz confirmando; nada foi enviado"
    payload = _load_pending()
    if payload is None:
        return False, "não havia envio de e-mail esperando confirmação"
    return send_now(str(payload.get("to") or ""), str(payload.get("subject") or ""),
                    str(payload.get("body") or ""), str(payload.get("account") or ""))


def send_now(to: str, subject: str, body: str,
             account: str | None = None) -> tuple[bool, str]:
    """O envio propriamente dito (SMTP). Só chamado por ``confirm_send``.

    O corpo é o que o DONO ditou: não passa pelo guarda de fala, que
    reescreveria telefone, valor e caminho dentro da mensagem dele. Quem sai
    daqui com texto derivado de terceiro é o aviso falado — esse sim sanitizado.
    """
    recipient = str(to or "").strip()
    text = str(body or "").strip()
    em = _one_line(subject or "", 140)
    try:
        acct = _A(account)
    except KeyError as e:
        return False, str(e)
    if not recipient or not text:
        return False, "faltam destinatário ou corpo do e-mail"
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = em or "(sem assunto)"
    msg["From"] = formataddr((acct.display_name or acct.name, acct.address))
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=acct.address.split("@")[-1] or "localhost")
    conn = None
    try:
        conn = _smtp_open(acct)
        refused = conn.sendmail(acct.address, [recipient], msg.as_string())
    except smtplib.SMTPAuthenticationError as e:
        _mark(acct, "auth",
              f"o servidor recusou: {_redact(acct, _reason(e))}", track=False)
        return False, (f"o servidor recusou o envio pela conta {acct.name}: "
                       "senha de app inválida?")
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        why = _redact(acct, _reason(e))
        _mark(acct, "error", why, track=False)
        return False, f"não consegui enviar o e-mail: {why}"
    except Exception as e:
        return False, f"não consegui enviar o e-mail: {_redact(acct, _reason(e))}"
    finally:
        if conn is not None:
            try:
                conn.quit()
            except Exception:
                pass
    if refused:
        return False, f"o servidor recusou estes destinatários: {', '.join(refused)}"
    _store_pending(None)
    _mark(acct, "connected", "caixa de entrada acessível", track=False)
    return True, f"E-mail enviado para {recipient} pela conta {acct.name}."
