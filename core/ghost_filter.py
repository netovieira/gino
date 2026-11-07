"""Filtro de vozes fantasmas — UC-3.

O microfone ouve TV, música, teclado e estática, e o STT devolve
transcrições plausíveis a partir desses sons. Silero VAD e RNNoise já
filtram no caminho bruto; este módulo é a ÚLTIMA linha de defesa antes de
o texto ser entregue ao cérebro.

O que decide, e como:

  1. **Classificação de fonte** (core.source_class.classify):
     ``tv`` / ``musica`` / ``ruido`` -> descarta. ``pessoa`` / ``silencio``
     continua; ``ruido`` com baixa confiança também passa (é o piso de
     segurança: um bloco sem informação não pode calar o dono).

  2. **Filtro de repetição**:
     frase idêntica (folded) que apareceu nos últimos ``repeat_window_s``
     segundos é descartada. Eco de TV tem esse comportamento: "Inscreva-se
     e ative o sininho" repetido em cada vídeo.

  3. **Evento ``audio_classified``**:
     cada chamada produz um payload ``{"label": str, "confidence": float,
     "action": str}`` para o activity.log. Não escreve em disco aqui — o
     caller loga; o resultado é devolvido para que ele faça isso.

O filtro NUNCA levanta exceção: em qualquer erro a fala passa. É a
segurança do dono de sempre ser ouvido.
"""
from __future__ import annotations

import threading
import time
import unicodedata
from collections import deque
from typing import Any, Callable

from core.source_class import classify as _classify_source, MUSIC, NOISE, TV

# ── rótulos que podem descartar a fala ──────────────────────────────────────
#: Fontes não-humanas: quando o classificador tem certeza, descarta.
_NON_HUMAN: frozenset[str] = frozenset({TV, MUSIC, NOISE})
#: Confiança mínima para o descarte. Abaixo disso: passa.
#: TV/música com confiança baixa pode ser o dono em ambiente ruidoso.
_MIN_CONFIDENCE_DISCARD = 0.55
#: Repetição: janela de tempo (s) e contagem mínima dentro da janela.
_DEFAULT_REPEAT_WINDOW_S = 30.0
_DEFAULT_REPEAT_MIN_COUNT = 2


def _fold(text: Any) -> str:
    """Minúsculo, sem acentos, espaços únicos."""
    raw = unicodedata.normalize("NFKD", str(text or "")).lower()
    ascii_ = "".join(c for c in raw if not unicodedata.combining(c))
    return " ".join(ascii_.split())


class GhostFilter:
    """Filtro de transcrições fantasma.

    Instanciar UMA vez por processo e chamar :meth:`check` para cada
    transcrição que chega do STT com o PCM correspondente.

    ``repeat_window_s``  : janela de detecção de repetição (s).
    ``repeat_min_count`` : número de ocorrências para considerar repetição.
    ``on_classify``      : callback opcional para o evento ``audio_classified``;
                           recebe o payload dict.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        repeat_window_s: float = _DEFAULT_REPEAT_WINDOW_S,
        repeat_min_count: int = _DEFAULT_REPEAT_MIN_COUNT,
        on_classify: Callable[[dict[str, Any]], None] | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self._sample_rate = int(sample_rate or 16_000)
        self._repeat_window_s = float(repeat_window_s)
        self._repeat_min_count = max(2, int(repeat_min_count))
        self._on_classify = on_classify or (lambda _p: None)
        self._log_fn = logger or (lambda _m: None)
        self._lock = threading.Lock()
        # deque[(t_monotonic, folded_text)]
        self._recent: deque[tuple[float, str]] = deque(maxlen=200)

    # ── consulta principal ───────────────────────────────────────────────────

    def check(
        self,
        text: str,
        pcm: bytes | Any = None,
    ) -> dict[str, Any]:
        """Decide se a transcrição deve passar.

        Retorna ``{"action": "accept"|"reject", "reason": str,
        "label": str, "confidence": float}``.

        ``action="reject"`` → o caller deve descartar a fala.
        ``action="accept"`` → a fala segue para o cérebro.
        """
        n = _fold(text)
        if not n:
            return {"action": "accept", "reason": "empty",
                    "label": "", "confidence": 0.0}

        label = ""
        confidence = 0.0

        # 1. Classificação de fonte (se PCM disponível)
        if pcm is not None:
            try:
                reading = _classify_source(pcm, sample_rate=self._sample_rate)
                label = str(reading.label)
                confidence = float(reading.confidence)
            except Exception as exc:
                self._log(f"[Ghost] source_class falhou: {exc}")

            if (label in _NON_HUMAN
                    and confidence >= _MIN_CONFIDENCE_DISCARD):
                result = {"action": "reject", "reason": label,
                          "label": label, "confidence": confidence}
                self._emit(result)
                return result

        # 2. Filtro de repetição
        repeat_count = self._count_recent(n)
        if repeat_count >= self._repeat_min_count - 1:
            result = {"action": "reject", "reason": "repeat",
                      "label": label or "pessoa", "confidence": confidence}
            self._emit(result)
            return result

        # Aceita: registra no histórico
        self._record(n)
        result = {"action": "accept", "reason": "",
                  "label": label or "pessoa", "confidence": confidence}
        self._emit(result)
        return result

    # ── repetição ────────────────────────────────────────────────────────────

    def _count_recent(self, folded: str) -> int:
        """Quantas vezes ``folded`` apareceu na janela deslizante."""
        now = time.monotonic()
        cutoff = now - self._repeat_window_s
        with self._lock:
            # descarta entradas fora da janela
            while self._recent and self._recent[0][0] < cutoff:
                self._recent.popleft()
            return sum(1 for _t, txt in self._recent if txt == folded)

    def _record(self, folded: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._recent.append((now, folded))

    # ── eventos e log ────────────────────────────────────────────────────────

    def _emit(self, payload: dict[str, Any]) -> None:
        try:
            self._on_classify(payload)
        except Exception:
            pass

    def _log(self, msg: str) -> None:
        try:
            self._log_fn(msg)
        except Exception:
            pass

    # ── utilidades para testes ────────────────────────────────────────────────

    def reset(self) -> None:
        """Zera o histórico de repetição (sem apagar callbacks)."""
        with self._lock:
            self._recent.clear()

    @property
    def repeat_window_s(self) -> float:
        return self._repeat_window_s

    @property
    def recent_count(self) -> int:
        with self._lock:
            return len(self._recent)
