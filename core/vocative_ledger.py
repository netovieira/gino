"""Frescor de vocativo POR PESSOA (regra do dono, 10/11/2026).

A palavra dele: "a regra de cinco minutos ela vale, mas ela vale só para a
pessoa que te fez a pergunta inicial ... se alguém do grupo virar 'obrigado', o
obrigado do outro você não pode responder, porque ele não falou Gino, só eu
falei Gino, então a permissão de escuta de cinco minutos ela tem que ser
referente ao usuário que te fez a pergunta".

O que isto corrige na arquitetura: o estado "acabei de ser chamado pelo nome"
era do assistente, não de ninguém. Uma variável global do tipo "minha última
fala terminou em pergunta" autoriza a próxima voz que chegar — seja ela de quem
nomeou ou de quem nunca abriu a boca para me chamar. Aqui o estado é uma chave
por PESSOA: quem nomeou, quando nomeou, e se ainda é ele o interlocutor da
janela.

Este módulo só mede e lembra. Não decide turno, não conhece modelo, não abre
portão nenhum: quem julga é o chamador. A chave de cada canal é construída pela
MESMA identidade de contato que o resto da casa já usa:

* voz de sala — :func:`voice_key`, pelo altofalante da diarização/portão
  progressivo (``main.py``);
* WhatsApp — :func:`whatsapp_key`, pelo ``sender_jid`` normalizado de
  :mod:`core.group_policy` (em chat individual o chat É a pessoa; em grupo é o
  autor, nunca o grupo).

Falha fechada: chave vazia, desconhecida ou expirada nunca é frescor. Nomear é
o que abre a janela; a janela desliza só para quem a abriu, e morre com o tempo
dele — o de :data:`DEFAULT_WINDOW_S` quando o chamador não passa outro.
"""
from __future__ import annotations

import os
import threading
import time
import unicodedata
from typing import Any, Callable

#: A janela padrão de "fui chamado por esta pessoa". O teto de cinco minutos é
#: o número que o dono dá de boca; um canal com janela própria (a escuta depois
#: de uma pergunta minha, por exemplo) passa a sua em :func:`holds`.
DEFAULT_WINDOW_S = 300.0

#: Teto de pessoas lembradas: muitas horas de sala e de conversa.
MAX_KEYS = 512

#: Prefixo da chave do canal de voz, para uma chave de WhatsApp nunca colidir
#: com uma de sala.
VOICE_CHANNEL = "sala"

#: O balde de quem NÃO tem identidade disponível (voz sem diarização/portão,
#: texto digitado). Existe para o comportamento de sempre continuar valendo
#: quando a casa não tem como distinguir as pessoas — e para essa falta de
#: identidade nunca virar a identidade de alguém por acaso: quem tem nome na
#: sala cai no balde dele, não neste.
NO_IDENTITY = "sem-identidade"

#: Como termina uma fala minha que espera resposta (a janela de pergunta).
QUESTION_ENDINGS = ("?", "?\"", "?'", "?»", "?)")

_LOCK = threading.Lock()
_NOTES: dict[str, float] = {}
_ORDER: list[str] = []
_LOG: Callable[[str], None] = lambda _m: None
_WINDOW_S = DEFAULT_WINDOW_S


# ── configuração (testes e vivo) ────────────────────────────────────────────

def configure(*, log: Callable[[str], None] | None = None,
              window_s: float | None = None) -> None:
    """Redireciona o canal de log e a janela padrão (testes/vivo)."""
    global _LOG, _WINDOW_S
    with _LOCK:
        if log is not None:
            _LOG = log
        if window_s is not None:
            _WINDOW_S = max(0.0, float(window_s))


def default_window_s() -> float:
    """A janela padrão de escuta (um canal com janela própria passa a sua)."""
    with _LOCK:
        return _WINDOW_S


def reset() -> None:
    """Esquece tudo (testes, troca de sala/conta)."""
    with _LOCK:
        _NOTES.clear()
        _ORDER.clear()


def _log(message: str) -> None:
    try:
        _LOG(message)
    except Exception:
        pass


# ── as chaves: identidade de contato, não rótulo de conversa ───────────────

def _fold(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or "")).strip().lower()
    return "".join(c for c in raw if not unicodedata.combining(c))


def voice_key(speaker: Any) -> str:
    """A chave de quem falou na sala. '' = ninguém identificado (sem janela)."""
    name = _fold(speaker)
    if not name:
        return ""
    return f"{VOICE_CHANNEL}:{name}"


def whatsapp_key(sender: Any, account: Any = "") -> str:
    """A chave de quem escreveu no WhatsApp: o número, nunca o chat sozinho.

    O número passa pelo ``normalize_jid`` de :mod:`core.group_policy` — a mesma
    normalização da allow-list de grupos e da porta do número da persona, para
    não existir segunda identidade de contato que possa divergir.
    """
    raw = str(sender or "").strip()
    if not raw:
        return ""
    try:
        from core import group_policy
        jid = group_policy.normalize_jid(raw)
    except Exception:                            # pragma: no cover - sem core
        jid = "".join(c for c in raw if c.isdigit())
    jid = _fold(jid)
    if not jid:
        return ""
    return f"wa:{_fold(account)}:{jid}"


# ── o ledger ────────────────────────────────────────────────────────────────

def note(key: Any, *, now: float | None = None) -> bool:
    """Registra que ESTA pessoa me chamou pelo nome agora. False sem chave."""
    got = str(key or "").strip()
    if not got:
        return False
    moment = time.monotonic() if now is None else float(now)
    with _LOCK:
        if got in _NOTES:
            _ORDER.remove(got)
        _NOTES[got] = moment
        _ORDER.append(got)
        while len(_ORDER) > MAX_KEYS:
            _NOTES.pop(_ORDER.pop(0), None)
    return True


def age(key: Any, *, now: float | None = None) -> float | None:
    """Segundos desde a última nomeação desta pessoa; None se ela não nomeou."""
    got = str(key or "").strip()
    if not got:
        return None
    moment = time.monotonic() if now is None else float(now)
    with _LOCK:
        marked = _NOTES.get(got)
    return None if marked is None else moment - marked


def fresh(key: Any, *, window_s: float | None = None,
          now: float | None = None) -> bool:
    """Esta pessoa me chamou pelo nome dentro da janela dela?"""
    limit = default_window_s() if window_s is None else float(window_s)
    spent = age(key, now=now)
    return spent is not None and spent <= limit


def last_key() -> str:
    """Quem foi a ÚLTIMA pessoa a me chamar pelo nome ('' = ninguém)."""
    with _LOCK:
        return _ORDER[-1] if _ORDER else ""


def holds(key: Any, *, window_s: float | None = None,
          now: float | None = None) -> bool:
    """A janela está aberta PARA ESTA PESSOA?

    Duas condições, e as duas são a regra do dono: a nomeação é dela e está
    dentro da janela, e ela continua sendo a interlocutora atual — quem nomeou
    depois dela passou a ser o dono da janela. É isto que fecha a porta do
    "obrigado" de quem nunca disse o nome.
    """
    got = str(key or "").strip()
    if not got:
        return False
    if last_key() != got:
        return False
    return fresh(got, window_s=window_s, now=now)


def snapshot(*, now: float | None = None) -> dict[str, float | None]:
    """Quem nomeou e há quanto tempo (idades; nunca conteúdo de fala)."""
    moment = time.monotonic() if now is None else float(now)
    with _LOCK:
        return {key: moment - marked for key, marked in _NOTES.items()}


# ── a janela de escuta depois de UMA PERGUNTA MINHA ─────────────────────────

def marked_at(key: Any) -> float | None:
    """O instante (monotonic) da última nomeação desta pessoa; None se nunca."""
    got = str(key or "").strip()
    if not got:
        return None
    with _LOCK:
        return _NOTES.get(got)


def ends_in_question(text: Any) -> bool:
    """A minha última fala terminou em pergunta (abre janela de resposta)?"""
    body = str(text or "").strip()
    return body.endswith(QUESTION_ENDINGS)


def listening_ok(key: Any, *, last_reply: Any = "",
                 ended_at: float | None = None,
                 window_s: float | None = None,
                 now: float | None = None) -> dict[str, Any]:
    """ESTA pessoa pode me responder sem repetir o nome, agora?

    É a regra do dono inteira numa função, sem estado próprio: a minha última
    fala terminou em pergunta, faz menos que a janela desde que eu terminei de
    falar, e a janela é DELA — foi ela que me chamou pelo nome e ninguém chamou
    depois. O veredito vem com motivo, para a trilha dizer por que o "obrigado"
    entrou ou por que ele morreu.

    Falha fechada: sem ``ended_at``, sem chave ou com chave de outra pessoa é
    ``ok=False``. ``key`` vazia é "não sei quem falou", e não sei quem falou não
    abre porta de ninguém.
    """
    moment = time.monotonic() if now is None else float(now)
    limit = default_window_s() if window_s is None else float(window_s)
    out: dict[str, Any] = {"ok": False, "motivo": "", "key": str(key or "")}
    if not ends_in_question(last_reply):
        out["motivo"] = "minha ultima fala nao terminou em pergunta"
        return out
    try:
        ended = float(ended_at or 0.0)
    except (TypeError, ValueError):
        ended = 0.0
    if not ended:
        out["motivo"] = "nunca terminei de falar uma resposta"
        return out
    since = moment - ended
    if since > limit:
        out["motivo"] = f"janela de escuta vencida ({since:.1f}s > {limit:.0f}s)"
        return out
    if not str(key or "").strip():
        out["motivo"] = "sem identidade de quem falou: janela de ninguem"
        return out
    spent = age(key, now=moment)
    if spent is None:
        out["motivo"] = "esta pessoa nunca me chamou pelo nome"
        return out
    if last_key() != str(key).strip():
        out["motivo"] = ("outra pessoa me chamou pelo nome depois: a janela passou "
                         "a ser dela")
        return out
    marked = marked_at(key)
    if marked is not None and marked < ended - limit:
        # A nomeação é anterior à própria janela contada do fim da minha fala:
        # a pergunta que abriu a escuta é de outra conversa. É assim que a
        # nomeação EXPIRA mesmo quando a minha resposta demorou a sair.
        out["motivo"] = ("a nomeacao desta pessoa expirou: a pergunta que abriu "
                         "a janela e de antes da janela")
        return out
    out["ok"] = True
    out["motivo"] = f"janela aberta para esta pessoa ({spent:.1f}s desde o nome)"
    return out
