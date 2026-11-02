"""Conclusão de execução que chega dormindo: acorda e fala UMA vez.

WHY THIS EXISTS
    "Dormir" (main.py `sleep()`) fecha o microfone, não os alto-falantes. Uma
    execução pesada pode terminar minutos depois, com o dono longe do teclado.
    Até aqui os anúncios de conclusão exigiam `_awake` e eram engolidos em
    silêncio justamente nesse caso; a única "entrega" era uma linha no painel.

    A notícia não é uma pergunta: ela tem que acordar o assistente (microfone
    reabre, estado de sono limpo) e sair na hora. Não se espera o dono falar
    para liberar a fala, e não se guarda o aviso na gaveta de "fala guardada"
    para o próximo turno.

IDEMPOTÊNCIA
    Um mesmo id de conclusão (id do job/subagente, ou o texto de um aviso sem
    id) é anunciado no máximo uma vez por processo. A marca é posta ANTES do
    wake/say: uma falha de áudio não vira repetição no ciclo seguinte.
"""
from __future__ import annotations

import threading
import time

# Motivo que aparece no log: "SYS: Acordei — conclusão de execução."
WAKE_REASON = "conclusão de execução"

# Um aviso sem id (o executor responde à notificação) não tem chave estável;
# o mesmo texto só sai de novo depois desta janela.
REPEAT_WINDOW_S = 300.0

# Bound on the in-memory marks: a long session may announce thousands of
# completions; the oldest keys are dropped first.
MAX_MARKS = 500

_LOCK = threading.RLock()
_ANNOUNCED: dict[str, float] = {}


def _clock(now) -> float:
    if now is None:
        return time.monotonic()
    try:
        return float(now)
    except (TypeError, ValueError):
        return time.monotonic()


def text_key(text: str) -> str:
    """Stable fallback key for an announcement that carries no task id."""
    clean = " ".join(str(text or "").split()).casefold()
    return f"texto:{clean[:200]}" if clean else ""


def was_announced(ident, now=None) -> bool:
    """True when this id (or the same fallback text) already went out."""
    key = str(ident or "").strip()
    if not key:
        return False
    with _LOCK:
        seen = _ANNOUNCED.get(key)
    if seen is None:
        return False
    if key.startswith("texto:"):
        return (_clock(now) - seen) < REPEAT_WINDOW_S
    return True


def claim(ident, now=None) -> bool:
    """Mark ``ident`` as announced. False when it was already announced.

    The mark is written before anything is spoken, so two threads racing on
    the same completion can never both win it.
    """
    key = str(ident or "").strip()
    if not key:
        return False
    current = _clock(now)
    with _LOCK:
        seen = _ANNOUNCED.get(key)
        if seen is not None and (not key.startswith("texto:")
                                 or (current - seen) < REPEAT_WINDOW_S):
            return False
        _ANNOUNCED[key] = current
        if len(_ANNOUNCED) > MAX_MARKS:
            for old in sorted(_ANNOUNCED, key=_ANNOUNCED.get)[
                    : len(_ANNOUNCED) - MAX_MARKS]:
                _ANNOUNCED.pop(old, None)
    return True


def announce(ident, text, *, wake=None, say=None, is_awake=None,
             log=None, now=None) -> bool:
    """Announce one completion exactly once, waking the assistant if needed.

    ``wake(reason)`` clears the sleep state (main.py `wake()`), ``say(text)``
    queues the line through `_say_now`, and ``is_awake()`` reports the current
    state. Return True only for the call that actually announced it.
    """
    line = " ".join(str(text or "").split())
    if not line:
        return False
    if not claim(ident, now=now):
        if callable(log):
            try:
                log(f"SYS: conclusão {str(ident)[:60]} já anunciada — "
                    "não repito.")
            except Exception:
                pass
        return False
    asleep = False
    try:
        asleep = not bool(is_awake()) if callable(is_awake) else False
    except Exception:
        asleep = False
    if asleep and callable(wake):
        try:
            wake(WAKE_REASON)
        except Exception as exc:  # noqa: BLE001 - the line still has to play
            print(f"[Wake] acordar para anunciar falhou: {exc}")
    if callable(say):
        try:
            say(line)
        except Exception as exc:  # noqa: BLE001 - already claimed
            print(f"[Wake] anúncio da conclusão falhou: {exc}")
            return False
    return True


def reset() -> None:
    """Forget every mark (tests / owner request)."""
    with _LOCK:
        _ANNOUNCED.clear()
