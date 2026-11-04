"""Janela deliberada para cadastrar a voz do dono por aparelho.

O índice de vozes sempre teve um único conjunto de amostras do dono, capturado
no microfone da máquina (perfil ``pc``). Uma nota de voz gravada no celular é
a MESMA pessoa, mas o timbre/canal muda o suficiente para ficar abaixo do
limiar global — e baixar o limiar seria abrir o portão para terceiros. A saída
é um perfil por aparelho, agrupado pela IDENTIDADE do falante (o dono), nunca
pelo aparelho: qualquer perfil do dono autoriza o dono, nenhum perfil promove
outra pessoa.

Este módulo é só a janela de captura. Ele NÃO fala com o microfone, com o
WhatsApp nem com o índice: recebe embeddings já extraídos, decide se a nota é
coerente com as anteriores e devolve as amostras para quem for gravar. A regra
de ouro é que nada aprende sozinho:

* a janela nasce FECHADA e só abre por ``arm()`` — o chamador só arma depois
  de confirmar a voz do dono no microfone da máquina;
* cada nota precisa ser coerente com as já aceitas NESTA janela (a média do
  agrupamento), não pode ser quase idêntica a outra (nota repetida), precisa
  passar a checagem de qualidade e, quando informada, vir sempre da mesma
  origem (conta + conversa);
* a janela fecha sozinha no tempo limite ou por ``cancel()``; ao fechar, as
  amostras são descartadas da memória, então uma nota que chegue depois não
  tem onde encostar.

Sem Qt, sem rede e sem modelo: testável com embeddings sintéticos e relógio
injetado.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

import numpy as np

DEFAULT_DEVICE = "celular"
DEFAULT_MIN_NOTES = 3
DEFAULT_MAX_NOTES = 6
DEFAULT_TIMEOUT_S = 600.0
DEFAULT_COHERENCE = 0.60
DEFAULT_DUPLICATE = 0.995
DEFAULT_MIN_SECONDS = 1.0
DEFAULT_MAX_SECONDS = 30.0

# A request to attest a capture chain the index has never seen. It is short on
# purpose: the owner confirms at the microphone that already recognises him,
# and an unanswered request dies instead of waiting forever.
DEFAULT_ATTEST_TIMEOUT_S = 180.0
DEFAULT_ATTEST_MAX_SAMPLES = 8

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _normalize(value: Any) -> np.ndarray:
    """L2-normalize one embedding; empty/zero vectors are not usable."""
    arr = np.asarray(value, dtype=np.float64).reshape(-1)
    if arr.size == 0:
        raise ValueError("embedding vazio")
    norm = float(np.linalg.norm(arr))
    if norm <= 1e-12:
        raise ValueError("embedding nulo")
    return arr / norm


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        return -1.0
    return float(np.dot(a, b))


class DeviceEnrollment:
    """One armed window that may collect the owner's phone samples.

    The caller owns the security decisions around it: who may call ``arm``,
    where the notes come from and when to persist. This class only enforces
    the window's own invariants (time, count, coherence, source, duplicates).
    """

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        min_notes: int = DEFAULT_MIN_NOTES,
        max_notes: int = DEFAULT_MAX_NOTES,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        coherence: float = DEFAULT_COHERENCE,
        duplicate: float = DEFAULT_DUPLICATE,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.device = str(device or DEFAULT_DEVICE).strip() or DEFAULT_DEVICE
        self.min_notes = max(1, int(min_notes))
        self.max_notes = max(self.min_notes, int(max_notes))
        self.timeout_s = max(1.0, float(timeout_s))
        self.coherence = float(coherence)
        self.duplicate = float(duplicate)
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._armed_at = 0.0
        self._expires_at = 0.0
        self._armed_by = ""
        self._source = ""
        self._samples: list[np.ndarray] = []
        self._count = 0
        self._rejected = 0
        self._last_reason = ""
        self._closed_reason = ""

    # ── tempo ────────────────────────────────────────────────────────────────
    def _now(self, now: float | None) -> float:
        return float(self._clock() if now is None else now)

    def _is_open_locked(self, moment: float) -> bool:
        return bool(self._expires_at) and moment < self._expires_at

    def _close_locked(self, reason: str) -> None:
        self._samples = []
        self._expires_at = 0.0
        self._closed_reason = str(reason or "fechada")
        self._last_reason = self._closed_reason

    # ── ciclo de vida ────────────────────────────────────────────────────────
    def arm(self, by: str = "", now: float | None = None) -> dict:
        """Open a fresh window, discarding anything from a previous one."""
        moment = self._now(now)
        with self._lock:
            self._armed_at = moment
            self._expires_at = moment + self.timeout_s
            self._armed_by = str(by or "")[:80]
            self._source = ""
            self._samples = []
            self._count = 0
            self._rejected = 0
            self._last_reason = ""
            self._closed_reason = ""
            return self._status_locked(moment)

    def expire(self, now: float | None = None) -> bool:
        """Close the window if its deadline passed. True when it just closed."""
        moment = self._now(now)
        with self._lock:
            if not self._expires_at or self._is_open_locked(moment):
                return False
            self._close_locked("tempo esgotado")
            return True

    def cancel(self, reason: str = "cancelado",
               now: float | None = None) -> dict:
        """Close now and forget every sample. Idempotent and safe to repeat."""
        moment = self._now(now)
        with self._lock:
            if self._is_open_locked(moment):
                self._close_locked(reason)
            return self._status_locked(moment)

    def active(self, now: float | None = None) -> bool:
        moment = self._now(now)
        self.expire(moment)
        with self._lock:
            return self._is_open_locked(moment)

    def remaining(self, now: float | None = None) -> float:
        moment = self._now(now)
        self.expire(moment)
        with self._lock:
            if not self._is_open_locked(moment):
                return 0.0
            return max(0.0, self._expires_at - moment)

    # ── estado ───────────────────────────────────────────────────────────────
    def _coherence_locked(self) -> tuple[float, float]:
        if not self._samples:
            return 0.0, 0.0
        if len(self._samples) == 1:
            return 1.0, 1.0
        arr = np.stack(self._samples)
        mean = _normalize(arr.mean(axis=0))
        sims = arr @ mean
        return float(np.mean(sims)), float(np.min(sims))

    def _status_locked(self, moment: float) -> dict:
        active = self._is_open_locked(moment)
        coherence_mean, coherence_min = self._coherence_locked()
        remaining = max(0.0, self._expires_at - moment) if active else 0.0
        return {
            "active": bool(active),
            "device": self.device,
            "count": int(self._count),
            "rejected": int(self._rejected),
            "min_notes": int(self.min_notes),
            "max_notes": int(self.max_notes),
            "needed": max(0, self.min_notes - self._count),
            "complete": bool(self._count >= self.min_notes),
            "remaining_s": round(remaining, 1),
            "coherence": float(self.coherence),
            "coherence_mean": round(coherence_mean, 4),
            "coherence_min": round(coherence_min, 4),
            "source": self._source,
            "armed_by": self._armed_by,
            "armed_at": round(self._armed_at, 3),
            "closed_reason": self._closed_reason,
            "last_reason": self._last_reason,
        }

    def status(self, now: float | None = None) -> dict:
        """Public snapshot. ``samples`` is a copy and only exists while open."""
        moment = self._now(now)
        self.expire(moment)
        with self._lock:
            data = self._status_locked(moment)
            data["samples"] = [s.copy() for s in self._samples]
            return data

    # ── entrada de uma nota ──────────────────────────────────────────────────
    def _reject_locked(self, reason: str, moment: float) -> dict:
        self._rejected += 1
        self._last_reason = str(reason)
        result = self._status_locked(moment)
        result.update({"accepted": False, "reason": str(reason),
                       "score": -1.0, "samples": []})
        return result

    def consider(
        self,
        embedding: Any,
        quality: dict | None = None,
        source: str = "",
        now: float | None = None,
    ) -> dict:
        """Offer one note to the window.

        ``quality`` is the ``core.speaker_id.audio_quality`` dict; when given,
        a rejected clip (short/silent/clipped) is refused before the embedding
        is considered. ``source`` is an opaque caller label (account + chat);
        the first ACCEPTED note binds later notes to the same source.

        Returns ``accepted`` (bool), ``reason``, ``count``, ``complete`` and a
        copy of the accepted samples. A rejection never changes the cluster.
        """
        moment = self._now(now)
        with self._lock:
            if not self._is_open_locked(moment):
                if self._expires_at:
                    self._close_locked("tempo esgotado")
                return self._reject_locked("janela fechada", moment)
            try:
                vec = _normalize(embedding)
            except (TypeError, ValueError):
                return self._reject_locked("embedding inválido", moment)
            if self._count >= self.max_notes:
                return self._reject_locked("limite de notas da janela", moment)
            if isinstance(quality, dict) and quality:
                if not quality.get("ok", True):
                    return self._reject_locked(
                        str(quality.get("reason") or "áudio recusado"), moment)
                try:
                    seconds = float(quality.get("seconds") or 0.0)
                except (TypeError, ValueError):
                    seconds = 0.0
                if seconds and not (DEFAULT_MIN_SECONDS <= seconds
                                    <= DEFAULT_MAX_SECONDS):
                    return self._reject_locked(
                        f"duração {seconds:.1f}s fora de "
                        f"{DEFAULT_MIN_SECONDS:.0f}-{DEFAULT_MAX_SECONDS:.0f}s",
                        moment)
            source = str(source or "").strip()[:200]
            if self._source and source and source != self._source:
                return self._reject_locked(
                    "origem diferente da primeira nota aceita", moment)
            score = 1.0
            if self._samples:
                for old in self._samples:
                    if _cosine(vec, old) >= self.duplicate:
                        return self._reject_locked(
                            f"nota repetida (sim={_cosine(vec, old):.3f})",
                            moment)
                cluster = _normalize(np.stack(self._samples).mean(axis=0))
                score = _cosine(vec, cluster)
                if score < self.coherence:
                    return self._reject_locked(
                        f"fora do agrupamento (sim={score:.2f} < "
                        f"{self.coherence:.2f})", moment)
            if not self._source and source:
                self._source = source
            self._samples.append(vec)
            self._count += 1
            self._last_reason = ""
            result = self._status_locked(moment)
            result.update({
                "accepted": True,
                "reason": "",
                "score": round(float(score), 4),
                "samples": [s.copy() for s in self._samples],
            })
            return result


def _slugify(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = _SLUG_RE.sub("-", text).strip("-")
    return text


class DeviceAttestation:
    """An expiring request to register a capture chain never seen before.

    A note from an unknown device is NOT an authorisation, even if its
    embedding happens to score high: the samples wait here, outside the
    index, until the owner confirms his identity on an ALREADY KNOWN device
    (the microphone whose profile the index holds). Only then does ``confirm``
    hand the samples back, labelled with the new device, for the caller to add
    to the shared pool.

    There is no text path. ``owner_ok`` is produced by the caller from the
    voiceprint gate on the known device; this class never opens anything on
    its own and drops the samples on expiry or cancel.
    """

    def __init__(
        self,
        timeout_s: float = DEFAULT_ATTEST_TIMEOUT_S,
        max_samples: int = DEFAULT_ATTEST_MAX_SAMPLES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.timeout_s = max(1.0, float(timeout_s))
        self.max_samples = max(1, int(max_samples))
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._device = ""
        self._source = ""
        self._expires_at = 0.0
        self._created_at = 0.0
        self._samples: list[np.ndarray] = []
        self._last_reason = ""

    def _now(self, now: float | None) -> float:
        return float(self._clock() if now is None else now)

    def _pending_locked(self, moment: float) -> bool:
        return bool(self._expires_at) and moment < self._expires_at

    def _clear_locked(self, reason: str) -> None:
        self._samples = []
        self._expires_at = 0.0
        self._device = ""
        self._source = ""
        self._last_reason = str(reason or "")

    def begin(self, device: str, samples: Any, source: str = "",
              now: float | None = None) -> dict:
        """Register the wait for one unknown device.

        Notes from the same chain accumulate; a request for another device
        starts over with the new samples.
        """
        label = _slugify(device)
        vectors: list[np.ndarray] = []
        try:
            for sample in samples or []:
                try:
                    vectors.append(_normalize(sample))
                except (TypeError, ValueError):
                    continue
        except TypeError:
            vectors = []
        moment = self._now(now)
        with self._lock:
            if not label or not vectors:
                return {"pending": False, "reason": "sem dispositivo ou amostra",
                        "device": label, "count": 0}
            # Notes from the same new chain accumulate into one request; a
            # different device replaces the pending one.
            existing: list[np.ndarray] = []
            if self._device == label and self._pending_locked(moment):
                existing = list(self._samples)
            self._device = label
            self._source = str(source or "").strip()[:200]
            self._created_at = moment
            self._expires_at = moment + self.timeout_s
            self._samples = (existing + vectors)[-self.max_samples:]
            self._last_reason = ""
            return self.status(now=moment)

    def pending(self, now: float | None = None) -> dict | None:
        """Open request, or None. An expired request is dropped here."""
        moment = self._now(now)
        with self._lock:
            if not self._expires_at:
                return None
            if not self._pending_locked(moment):
                self._clear_locked("pedido expirado")
                return None
            return {
                "device": self._device,
                "source": self._source,
                "count": len(self._samples),
                "remaining_s": round(max(0.0, self._expires_at - moment), 1),
                "created_at": round(self._created_at, 3),
            }

    def expire(self, now: float | None = None) -> bool:
        moment = self._now(now)
        with self._lock:
            if not self._expires_at or self._pending_locked(moment):
                return False
            self._clear_locked("pedido expirado")
            return True

    def cancel(self, reason: str = "cancelado",
               now: float | None = None) -> dict:
        moment = self._now(now)
        with self._lock:
            self._clear_locked(reason)
            return self.status(now=moment)

    def confirm(self, known_device: str, owner_ok: bool,
                now: float | None = None) -> dict:
        """Owner confirmed on a known device: release the samples.

        ``owner_ok`` must come from the voiceprint of the known microphone.
        A false/absent confirmation keeps the request open (until expiry) and
        never returns samples, so a third party cannot use this window.
        """
        moment = self._now(now)
        known = _slugify(known_device)
        with self._lock:
            if not self._expires_at:
                return {"confirmed": False, "reason": "nenhum pedido pendente",
                        "device": "", "known_device": known, "samples": []}
            if not self._pending_locked(moment):
                self._clear_locked("pedido expirado")
                return {"confirmed": False, "reason": "pedido expirado",
                        "device": "", "known_device": known, "samples": []}
            if not owner_ok:
                return {"confirmed": False,
                        "reason": "identidade não confirmada por voz",
                        "device": self._device, "known_device": known,
                        "samples": []}
            if not known or known == self._device:
                return {"confirmed": False,
                        "reason": "confirmação precisa vir de um dispositivo "
                                  "já conhecido",
                        "device": self._device, "known_device": known,
                        "samples": []}
            result = {
                "confirmed": True,
                "reason": "",
                "device": self._device,
                "known_device": known,
                "source": self._source,
                "samples": [s.copy() for s in self._samples],
            }
            self._clear_locked("confirmado")
            return result

    def status(self, now: float | None = None) -> dict:
        moment = self._now(now)
        with self._lock:
            active = self._pending_locked(moment)
            return {
                "pending": bool(active),
                "device": self._device,
                "source": self._source,
                "count": len(self._samples),
                "remaining_s": (round(max(0.0, self._expires_at - moment), 1)
                                if active else 0.0),
                "last_reason": self._last_reason,
            }
