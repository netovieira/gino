"""Quem falou agora — o contexto que os gates consultam ANTES de negar.

Um único slot por processo (a captura de voz é serial): ``set_recent`` grava
o veredito de ``core/owner_decision`` da fala corrente e ``describe_recent``
devolve o RÓTULO NOMINAL dessa fala para logs e fala — nunca embedding, id de
voz, telefone, e-mail ou dado pessoal.

É só o espelho do veredito: este módulo não decide nada. ``privileged`` aqui
vem de ``Verdict.privileged`` (voiceprint do dono) ou da senha digitada
aberta por ``_owner_text_gate``; nada neste arquivo concede privilégio.
"""
from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_recent: dict[str, Any] = {}

# Origens honestas de um pedido (motivo de fim de sessão e trilha de Grants).
ORIGIN_TIMEOUT = "timeout"
ORIGIN_ECHO = "eco"
ORIGIN_TRANSIENT = "transitorio"
ORIGIN_MIC = "mic"
ORIGIN_CANCEL = "cancelamento"
ORIGIN_STRIKES = "tentativas"
ORIGIN_COHERENCE = "incoerencia"
ORIGIN_OWNER = "dono"
ORIGIN_TEXT = "texto"
ORIGIN_ENROLL = "cadastro-voz"
ORIGIN_UNKNOWN = "?"
_KNOWN_ORIGINS = frozenset({ORIGIN_TIMEOUT, ORIGIN_ECHO, ORIGIN_TRANSIENT,
                            ORIGIN_MIC, ORIGIN_CANCEL, ORIGIN_STRIKES,
                            ORIGIN_COHERENCE, ORIGIN_OWNER, ORIGIN_TEXT,
                            ORIGIN_ENROLL})


def set_recent(*, who: str = "", person: str = "", privileged: bool = False,
               confirmed_by: str = "", score: float = 0.0,
               device: str = "", origin: str = "",
               now: float | None = None) -> dict[str, Any]:
    """Registra a fala corrente para os gates. ``person`` é rótulo nominal."""
    moment = time.monotonic() if now is None else float(now)
    entry = {
        "who": str(who or "").strip().lower(),
        "person": str(person or "").strip().lower(),
        "privileged": bool(privileged),
        "confirmed_by": str(confirmed_by or "").strip().lower(),
        "score": float(score or 0.0),
        "device": str(device or ""),
        "origin": str(origin or ORIGIN_UNKNOWN),
        "at": moment,
    }
    with _lock:
        _recent.clear()
        _recent.update(entry)
    return entry


def recent() -> dict[str, Any]:
    with _lock:
        return dict(_recent)


def clear() -> None:
    with _lock:
        _recent.clear()


def is_owner_turn(*, max_age_seconds: float = 90.0,
                  now: float | None = None) -> bool:
    """A fala mais recente foi confirmada como o dono (veredito privilegiado
    OU senha digitada no ato). Prazo curto: um turno velho não autoriza um
    pedido que chegou depois — a criação de grant exige a prova na frase.
    """
    with _lock:
        snap = dict(_recent)
    if not snap or not snap.get("privileged"):
        return False
    if str(snap.get("confirmed_by") or "") not in (
            "voiceprint_owner", "owner_voiceprint", "senha_digitada",
            "owner_password"):
        return False
    moment = time.monotonic() if now is None else float(now)
    return (moment - float(snap.get("at") or 0.0)) <= max(0.0, max_age_seconds)


def origin_from_detail(detail: Any) -> str:
    """Classe o motivo de uma recusa/fim pelo texto do veredito.

    eco → eco; microfone sem perfil → mic; fala curta/transitória →
    transitorio; resto → ?. É taxonomia de log, não decisão.
    """
    text = str(detail or "").lower()
    if "eco" in text or "tts" in text or "playback" in text:
        return ORIGIN_ECHO
    if "sem cadastro" in text or "sem perfil" in text or "microfone" in text:
        return ORIGIN_MIC
    if ("curta" in text or "incompleto" in text or "sem fala" in text
            or "sem audio" in text.replace("áudio", "audio")
            or "transit" in text):
        return ORIGIN_TRANSIENT
    return ORIGIN_UNKNOWN


def describe(snap: dict[str, Any] | None = None) -> str:
    """RÓTULO NOMINAL da fala — nunca id numérico, embedding ou dado pessoal.

    É a linha que os logs e a boca usam; 'voz=1732731597' (id de voz) está
    proibida por design aqui: sem o id em ``person``, isto devolve
    'quem=?, origem=?'.
    """
    data = snap if snap is not None else recent()
    if not data:
        return "quem=?, origem=?"
    who = str(data.get("who") or "?")
    person = str(data.get("person") or "")
    privileged = bool(data.get("privileged"))
    if privileged and (who in ("owner", "dono") or person):
        label = "dono" if who in ("owner", "dono") else person
        return f"quem={label}, origem=owner"
    if person:
        return f"quem={person}, origem=?"
    if who in ("outro", "other"):
        return "quem=não-dono, origem=?"
    origin = str(data.get("origin") or ORIGIN_UNKNOWN)
    if origin not in _KNOWN_ORIGINS:
        origin = ORIGIN_UNKNOWN
    return f"quem=?, origem={origin}"


def describe_recent() -> str:
    return describe()
