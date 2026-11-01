"""Códigos de verificação que chegam por e-mail, anunciados em voz alta.

A CONTA
    Usa a MESMA credencial do plugin de e-mail (`plugin_config.email`): endereço
    e senha de app, sem pedir nada novo. Sem ela, este módulo não inventa conta
    nenhuma — só diz o que falta no log e fica quieto.

A VARREDURA
    A cada POLL_S segundos abre a pasta de entrada em modo SOMENTE LEITURA
    (BODY.PEEK: ler um código não marca o e-mail como lido) e olha as SCAN
    mensagens mais recentes. Mensagem já vista pelo UID é pulada; mensagem
    antiga demais (FRESH_S) é lembrada sem anúncio, porque um código vencido
    não serve para nada. O histórico de UIDs fica em disco para um reinício do
    assistente não anunciar o mesmo código duas vezes.

O ANÚNCIO
    `handler(payload)` recebe o resultado de core/otp_detect.py com
    `origin="email"`. Se devolver False (sessão ainda não pronta, por exemplo),
    a mensagem NÃO é marcada como vista e o próximo ciclo tenta de novo; código
    já vencido é descartado na checagem de idade.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from core import otp_detect

POLL_S = 60.0
FRESH_S = 15 * 60.0
SCAN_DAYS = 2           # o servidor só devolve UIDs recentes; código velho não serve
SCAN = 40
MAX_SEEN = 2000

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_BASE = Path(__file__).resolve().parent.parent
_DEFAULT_STORE = _BASE / "memory" / "email_otp_seen.json"

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_handler = None
_logger = None
_store_path = _DEFAULT_STORE
_seen: dict[str, None] | None = None
_last_warning = ""


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


def _mail():
    from plugins import email_control
    return email_control


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


def _scan_criteria() -> str:
    """`SINCE` em inglês (formato IMAP), independente do locale da máquina."""
    t = time.gmtime(time.time() - SCAN_DAYS * 86400)
    return f"SINCE {t.tm_mday:02d}-{_MONTHS[t.tm_mon - 1]}-{t.tm_year}"


def _messages_for_account(mail, cfg: dict) -> list[dict]:
    conn = mail._imap(cfg)
    try:
        conn.select(mail.FOLDER, readonly=True)
        rows = mail._recent(conn, _scan_criteria(), scan=SCAN)
        now = time.time()
        pending = []
        seen = _load_seen()
        for row in rows:
            row = dict(row)
            if _key(cfg["address"], row) in seen:
                continue
            if _fresh(row, now):
                early = otp_detect.detect(
                    "", subject=row.get("subject") or "",
                    sender=row.get("from") or "")
                if early is None:
                    try:
                        row["body"] = mail._body(conn, row.get("uid") or "")
                    except Exception as e:
                        _log(f"não consegui ler o corpo de um e-mail: {e}")
                        row["body"] = ""
            pending.append(row)
        return pending
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def poll_once(handler=None, now: float | None = None) -> list[dict]:
    """Uma varredura de todas as contas. Devolve os códigos anunciados."""
    global _last_warning
    try:
        mail = _mail()
    except Exception as e:
        _warn(f"plugin de e-mail indisponível: {e}")
        return []
    try:
        accounts = mail._load_accounts()
    except Exception as e:
        _warn(f"não consegui ler a configuração de e-mail: {e}")
        return []
    if not accounts:
        _warn("o e-mail não está configurado para leitura: falta endereço e "
              "senha de app em ⚙ → PLUGIN SETTINGS → EMAIL. Nenhum código "
              "será anunciado até isso existir.")
        return []

    announced: list[dict] = []
    for acct in accounts:
        name = str(acct.get("name") or acct.get("address") or "").strip()
        try:
            cfg = mail._resolve(None, name or None)
        except Exception as e:
            _warn(f"a conta de e-mail '{name}' não tem credencial de leitura "
                  f"completa (endereço + senha de app): {e or 'faltando'}")
            continue
        try:
            rows = _messages_for_account(mail, cfg)
        except Exception as e:
            _warn(f"não consegui ler a caixa {cfg['address']}: {e}")
            continue
        _last_warning = ""
        announced.extend(process_messages(
            rows, account=cfg["address"], handler=handler, now=now))
    return announced


def _loop(interval: float) -> None:
    while not _stop.is_set():
        try:
            poll_once()
        except Exception as e:
            _log(f"a varredura de códigos falhou: {e}")
        _stop.wait(interval)


def start(handler=None, logger=None, interval: float = POLL_S) -> None:
    """Liga a varredura em background; `handler(payload)` anuncia cada código."""
    global _handler, _logger, _thread
    if logger is not None:
        _logger = logger
    with _lock:
        if handler is not None:
            _handler = handler
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(
            target=_loop, args=(max(1.0, float(interval)),), daemon=True,
            name="jarvis-email-otp")
        _thread.start()


def stop() -> None:
    global _thread, _handler
    _stop.set()
    with _lock:
        thread, _thread = _thread, None
        _handler = None
    if thread is not None and thread.is_alive() \
            and thread is not threading.current_thread():
        thread.join(timeout=5)


def reset(store_path: str | Path | None = None) -> None:
    """Esquece o histórico em memória (testes); `store_path` troca o arquivo."""
    global _seen, _store_path, _last_warning
    with _lock:
        _seen = None
        if store_path is not None:
            _store_path = Path(store_path)
    _last_warning = ""
