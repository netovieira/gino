"""Códigos de verificação que chegam por e-mail, anunciados em voz alta.

A CONTA
    Vem do registro NOVO de e-mail (`core/email.py`, `plugin_config.mail`), a
    mesma lista que a tela 📧 E-MAIL edita: só as contas marcadas com `otp`.
    A senha de app sai do cofre (`mail_secrets.<uuid>`) pelas funções do
    registro — este módulo nunca vê, imprime ou grava a senha. Sem conta
    marcada, ele só registra o que falta no log e fica quieto.

A VARREDURA
    Não há laço próprio: o passo de código roda DENTRO da varredura única de
    `core/email.py` (`poll_once`), na conexão que ela já abriu, caixa em modo
    SOMENTE LEITURA (`BODY.PEEK`: ler um código não marca o e-mail como lido).
    A cadência continua a mesma de antes — 60 s (`POLL_S`), garantida pelo
    intervalo que `core.email.start()` usa quando existe conta com `otp`.
    Mensagem já vista pelo UID é pulada; mensagem antiga demais (FRESH_S) é
    lembrada sem anúncio, porque um código vencido não serve para nada. O
    histórico de UIDs fica em disco (`memory/email_otp_seen.json`, chave
    "endereço|uid") para um reinício do assistente não anunciar o mesmo código
    duas vezes.

O ANÚNCIO
    `handler(payload)` recebe o resultado de core/otp_detect.py com
    `origin="email"`. Se devolver False (sessão ainda não pronta, por exemplo),
    a mensagem NÃO é marcada como vista e o próximo ciclo tenta de novo; código
    já vencido é descartado na checagem de idade.

SEM ANÚNCIO DUPLO
    `scan_account` devolve também os UIDs que eram código, e a varredura não
    fala "chegou um e-mail" para eles: o dono já ouviu o código. Ler um código
    não marca a mensagem como lida em nenhum dos dois caminhos.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from core import otp_detect

POLL_S = 60.0
FRESH_S = 15 * 60.0
SCAN_DAYS = 2           # o servidor só devolve UIDs recentes; código velho não serve
SCAN = 40
MAX_SEEN = 2000

_BASE = Path(__file__).resolve().parent.parent
_DEFAULT_STORE = _BASE / "memory" / "email_otp_seen.json"

_lock = threading.Lock()
_handler = None
_logger = None
_store_path = _DEFAULT_STORE
_seen: dict[str, None] | None = None
_last_warning = ""

# Citação e assinatura fora: um código citado no histórico de uma resposta não
# é um código novo. Mesmas regras de antes (o corpo vem do registro novo).
_QUOTE_TAIL = re.compile(
    r"(^\s*Em .{5,120}escreveu:\s*$)|(^\s*On .{5,120}wrote:\s*$)|"
    r"(^\s*-{2,}\s*Mensagem original\s*-{2,})|(^\s*_{10,}\s*$)",
    re.IGNORECASE | re.MULTILINE)
_SIGNATURE = re.compile(r"^--\s?$", re.MULTILINE)


def _default_log(message: str) -> None:
    try:
        print(f"[OTP/email] {message}")
    except Exception:
        pass


def _log(message: str) -> None:
    fn = _logger or _default_log
    try:
        fn(message)
    except Exception:
        pass


def _warn(message: str) -> None:
    global _last_warning
    if message == _last_warning:
        return
    _last_warning = message
    _log(message)


def _registry():
    """O registro autoritativo de e-mail (import tardio: evita ciclo)."""
    from core import email as registry
    return registry


def _load_seen() -> dict[str, None]:
    global _seen
    with _lock:
        if _seen is not None:
            return _seen
    loaded: dict[str, None] = {}
    try:
        raw = json.loads(_store_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            for key in raw:
                loaded[str(key)] = None
    except FileNotFoundError:
        pass
    except Exception as e:
        _log(f"não consegui ler o histórico de códigos vistos: {e}")
    with _lock:
        if _seen is None:
            _seen = loaded
        return _seen


def _save_seen(seen: dict[str, None]) -> None:
    keys = list(seen.keys())[-MAX_SEEN:]
    try:
        _store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = _store_path.with_name(_store_path.name + ".tmp")
        tmp.write_text(json.dumps(keys, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_store_path)
    except Exception as e:
        _log(f"não consegui gravar o histórico de códigos vistos: {e}")


def _key(account: str, msg: dict) -> str:
    uid = str(msg.get("uid") or "").strip()
    if uid:
        return f"{account}|{uid}"
    return (f"{account}|{msg.get('from') or ''}|{msg.get('subject') or ''}"
            f"|{msg.get('ts') or ''}")


def _fresh(msg: dict, now: float) -> bool:
    try:
        ts = float(msg.get("ts") or 0)
    except Exception:
        return True
    return ts <= 0 or abs(now - ts) <= FRESH_S


def is_seen(account: str, msg: dict) -> bool:
    return _key(str(account or ""), msg) in _load_seen()


def process_messages(rows: list[dict], *, account: str = "", handler=None,
                     now: float | None = None) -> list[dict]:
    """Dedupe + detector + anúncio; devolve os OTPs efetivamente entregues."""
    account = str(account or "")
    handler = handler if handler is not None else _handler
    now = time.time() if now is None else float(now)
    seen = _load_seen()
    announced: list[dict] = []
    changed = False
    with _lock:
        for msg in rows or []:
            if not isinstance(msg, dict):
                continue
            key = _key(account, msg)
            if key in seen:
                continue
            if not _fresh(msg, now):
                seen[key] = None
                changed = True
                continue
            otp = otp_detect.detect(
                msg.get("body") or msg.get("text") or "",
                subject=msg.get("subject") or "",
                sender=msg.get("from") or msg.get("sender") or "")
            if otp is None:
                seen[key] = None
                changed = True
                continue
            payload = dict(otp, origin="email", account=account,
                           uid=str(msg.get("uid") or ""))
            delivered = handler is None
            if callable(handler):
                try:
                    delivered = handler(payload) is not False
                except Exception as e:
                    _log(f"o anúncio do código de "
                         f"{payload.get('app') or 'um e-mail'} falhou: {e}")
                    # Um anúncio pode ter saído antes do erro; repetir seria pior.
                    delivered = True
            if not delivered:
                continue                     # tenta de novo no próximo ciclo
            seen[key] = None
            changed = True
            if handler is not None:
                announced.append(payload)
        while len(seen) > MAX_SEEN:
            seen.pop(next(iter(seen)))
    if changed:
        _save_seen(seen)
    return announced


def _trim_quoted(text: str) -> str:
    """Corpo sem citação/assinatura, como o leitor de e-mail já entregava."""
    text = str(text or "").replace("\r\n", "\n")
    cuts: list[int] = []
    m = _QUOTE_TAIL.search(text)
    if m and m.start() > 40:
        cuts.append(m.start())
    sig = _SIGNATURE.search(text)
    if sig and sig.start() > 40:
        cuts.append(sig.start())
    if cuts:
        text = text[:min(cuts)]
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith(">"))


def scan_account(acct: dict, conn, *, handler=None,
                 now: float | None = None) -> tuple[list[dict], set[str]]:
    """Lê UMA caixa numa conexão já aberta e anuncia os códigos.

    `acct` é o metadado público do registro novo (nome, endereço, uuid — nunca
    a senha) e `conn` veio de `core.email.open_inbox` (somente leitura).
    Devolve (anúncios, UIDs que eram código) — o segundo conjunto é o que
    impede a varredura de avisar "chegou um e-mail" sobre um código já falado.
    """
    account = str(acct.get("address") or "")
    registry = _registry()
    rows = registry.recent_headers(conn, days=SCAN_DAYS, limit=SCAN)
    now = time.time() if now is None else float(now)
    seen = _load_seen()
    pending: list[dict] = []
    code_uids: set[str] = set()
    for row in rows:
        row = dict(row)
        if _key(account, row) in seen:
            continue
        uid = str(row.get("uid") or "")
        if not _fresh(row, now):
            pending.append(row)          # process_messages lembra sem anunciar
            continue
        subject = row.get("subject") or ""
        sender = row.get("from") or ""
        body = ""
        if otp_detect.detect("", subject=subject, sender=sender) is not None:
            code_uids.add(uid)           # o assunto já diz: corpo não é preciso
        else:
            try:
                body = _trim_quoted(registry.peek_body(conn, uid))
            except Exception as e:
                _log(f"não consegui ler o corpo de um e-mail: {e}")
                body = ""
            if otp_detect.detect(body, subject=subject, sender=sender) is not None:
                code_uids.add(uid)
        row["body"] = body
        pending.append(row)
    announced = process_messages(pending, account=account, handler=handler, now=now)
    return announced, code_uids


def _sweep_pass(acct: dict, conn) -> set[str]:
    """O passo que roda DENTRO da varredura única de `core/email.py`."""
    _announced, codes = scan_account(acct, conn, handler=_handler)
    return codes


def poll_once(handler=None, now: float | None = None) -> list[dict]:
    """Uma varredura manual (dry-run), caixa por caixa, pelo registro novo.

    Não muda nada no servidor: abre somente leitura, lê cabeçalho e, quando
    precisa, o corpo com BODY.PEEK. Devolve os códigos anunciados.
    """
    global _last_warning
    try:
        registry = _registry()
        accounts = registry.otp_accounts()
    except Exception as e:
        _warn(f"o registro de e-mail está indisponível: {e}")
        return []
    if not accounts:
        _warn("nenhuma conta de e-mail está marcada para anunciar códigos: "
              "marque em 📧 E-MAIL. Nenhum código será anunciado até lá.")
        return []

    announced: list[dict] = []
    for acct in accounts:
        label = str(acct.get("name") or acct.get("address") or "?")
        conn = None
        try:
            conn = registry.open_inbox(acct.get("name") or acct.get("uuid"))
            got, _codes = scan_account(acct, conn, handler=handler, now=now)
        except Exception as e:
            _warn(f"não consegui ler a caixa {label}: {e}")
            continue
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass
        _last_warning = ""
        announced.extend(got)
    return announced


def start(handler=None, logger=None, interval: float = POLL_S) -> None:
    """Liga o anúncio de códigos na varredura única de e-mail.

    Não cria laço próprio: registra `handler(payload)` (o anúncio falado) e o
    passo que `core/email.py` roda dentro do ciclo dela. É a MESMA conexão
    somente leitura — um leitor por caixa, sem segundo IMAP.
    """
    global _handler, _logger
    if logger is not None:
        _logger = logger
    if handler is not None:
        _handler = handler
    registry = _registry()
    registry.set_otp_processor(_sweep_pass)
    registry.start(interval=max(1.0, float(interval)))


def stop() -> None:
    """Desliga o passo de códigos (o laço de e-mail é de quem o criou)."""
    global _handler
    with _lock:
        _handler = None
    try:
        _registry().set_otp_processor(None)
    except Exception:
        pass


def reset(store_path: str | Path | None = None) -> None:
    """Esquece o histórico em memória (testes); `store_path` troca o arquivo."""
    global _seen, _store_path, _last_warning
    with _lock:
        _seen = None
        if store_path is not None:
            _store_path = Path(store_path)
    _last_warning = ""
