"""Decisão única de "é o dono?" (owner_decision).

Uso:
    from core import owner_decision as od
    v = od.decide(match=match, device=dev, acoustic=reading.verdict,
                  transcript=text, privileged=True)
    if v.privileged: ...   # única porta de privilégio
`match` é o dict de SpeakerId.identify; `no_profile` nunca autoriza.
"""
from __future__ import annotations

import math
import time
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping, Sequence

import numpy as np

from core import voice_cohorts

DEFAULT_OWNER_NAME = "Anthero"

# Mirrors core.speaker_id.VERDICT_INDECISIVE: under this much voiced audio the
# embedding is not comparable, so the answer is neutral, never "intruder".
MIN_VOICED_SECONDS = 0.8

# Gray band: inside it the score is too close to the limit to call either way.
GRAY_BAND_RAW = 0.05
GRAY_BAND_Z = 0.75

# Logistic slopes for confidence. Raw scores need a wider scale because they
# mix microphone and speaker variability; AS-norm scores are already in std.
RAW_CONFIDENCE_SCALE = 0.12
NORM_CONFIDENCE_SCALE = 1.0

NEAR = "near"
DIFFUSE = "diffuse"
AMBIGUOUS = "ambiguous"
SILENT = "silent"

_ACOUSTIC_ALIASES = {
    "near": NEAR, "proximo": NEAR, "próximo": NEAR, "perto": NEAR,
    "diffuse": DIFFUSE, "difuso": DIFFUSE, "sala": DIFFUSE, "tv": DIFFUSE,
    "ambig": AMBIGUOUS, "ambiguo": AMBIGUOUS, "ambíguo": AMBIGUOUS,
    "ambiguous": AMBIGUOUS, "silent": SILENT, "silencio": SILENT,
    "silêncio": SILENT,
}


class Who(str, Enum):
    OWNER = "owner"
    OTHER = "outro"
    UNCERTAIN = "incerto"


class Action(str, Enum):
    NONE = "nenhuma"
    NORMAL = "fala_normal"
    PRIVILEGED = "privilegiado"


class ProfileState(str, Enum):
    OK = "ok"
    NO_PROFILE = "no_profile"
    SHORT = "indecisive_short"
    NO_DATA = "no_data"


@dataclass(frozen=True, slots=True)
class Verdict:
    """One answer to "is this the owner?", ready for a gate.

    Invariants (pinned by tests):
      * ``privileged`` is True only when ``who is OWNER`` and
        ``action is Action.PRIVILEGED``;
      * ``profile_state == "no_profile"`` always implies ``privileged=False``;
      * ``acoustic == "diffuse"`` always implies ``privileged=False``.
    """

    who: Who
    confidence: float
    reason: str
    action: Action
    privileged: bool
    score: float
    normalized_score: float
    threshold: float
    normalized_threshold: float | None
    uncalibrated: bool
    profile_state: str
    device: str
    cohort: str
    cohort_label: str
    speaker: str | None
    acoustic: str
    speech: bool | None
    state: str
    state_changed: bool
    threshold_source: str
    threshold_reason: str
    samples: int

    @property
    def calibrated(self) -> bool:
        return not self.uncalibrated

    @property
    def can_answer(self) -> bool:
        """Normal or privileged speech is allowed (not blocked)."""
        return self.action is not Action.NONE

    def as_dict(self) -> dict[str, Any]:
        return {
            "who": self.who.value,
            "confidence": round(float(self.confidence), 4),
            "reason": self.reason,
            "action": self.action.value,
            "privileged": bool(self.privileged),
            "score": round(float(self.score), 6),
            "normalized_score": round(float(self.normalized_score), 6),
            "threshold": round(float(self.threshold), 6),
            "normalized_threshold": (None if self.normalized_threshold is None
                                     else round(float(self.normalized_threshold), 6)),
            "calibrated": self.calibrated,
            "uncalibrated": bool(self.uncalibrated),
            "profile_state": self.profile_state,
            "device": self.device,
            "cohort": self.cohort,
            "cohort_label": self.cohort_label,
            "speaker": self.speaker,
            "acoustic": self.acoustic,
            "speech": self.speech,
            "state": self.state,
            "state_changed": bool(self.state_changed),
            "threshold_source": self.threshold_source,
            "threshold_reason": self.threshold_reason,
            "samples": int(self.samples),
        }


class SpeakerState:
    """Owner presence with hysteresis; one uncertain phrase changes nothing.

    ``observe`` counts concordant units (utterances): ``confirmations`` owner
    votes are needed to enter ``owner_present`` and the same number of other
    votes to enter ``owner_absent``. A single contradiction against a stable
    state does not flip it. After ``decay_seconds`` without decisive evidence
    the state falls back to ``transicao`` and the streaks reset.

    The state is contextual, never an authorization: ``decide`` returns it for
    the conversation layer, while ``privileged`` still needs this utterance's
    owner evidence.
    """

    PRESENT = "owner_present"
    ABSENT = "owner_absent"
    TRANSITION = "transicao"

    def __init__(self, *, confirmations: int = 2, decay_seconds: float = 45.0,
                 now: float | None = None) -> None:
        self.confirmations = max(1, int(confirmations))
        self.decay_seconds = max(0.1, float(decay_seconds))
        self._state = self.TRANSITION
        self._owner_streak = 0
        self._other_streak = 0
        self._last_evidence = float(now) if now is not None else time.monotonic()
        self._last_confidence = 0.0
        self._changed = False

    @property
    def state(self) -> str:
        return self._state

    @property
    def changed(self) -> bool:
        return self._changed

    def _decay(self, now: float) -> bool:
        if now - self._last_evidence <= self.decay_seconds:
            return False
        changed = self._state != self.TRANSITION
        self._state = self.TRANSITION
        self._owner_streak = 0
        self._other_streak = 0
        self._last_evidence = now
        return changed

    def observe(self, who: Who | str, *, confidence: float = 0.0,
                now: float | None = None) -> str:
        """Feed one unit; returns the (possibly unchanged) state label."""
        moment = time.monotonic() if now is None else float(now)
        changed = self._decay(moment)
        value = _who_value(who)
        try:
            self._last_confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            self._last_confidence = 0.0
        if value == Who.OWNER.value:
            self._owner_streak += 1
            self._other_streak = 0
            self._last_evidence = moment
            if (self._owner_streak >= self.confirmations
                    and self._state != self.PRESENT):
                self._state = self.PRESENT
                changed = True
        elif value == Who.OTHER.value:
            self._other_streak += 1
            self._owner_streak = 0
            self._last_evidence = moment
            if (self._other_streak >= self.confirmations
                    and self._state != self.ABSENT):
                self._state = self.ABSENT
                changed = True
        # Uncertain: no vote, no streak reset, no stable-state flip.
        self._changed = changed
        return self._state

    def state_at(self, now: float | None = None) -> str:
        moment = time.monotonic() if now is None else float(now)
        self._changed = self._decay(moment)
        return self._state

    def reset(self, *, now: float | None = None) -> None:
        self._state = self.TRANSITION
        self._owner_streak = 0
        self._other_streak = 0
        self._last_evidence = (time.monotonic() if now is None else float(now))
        self._last_confidence = 0.0
        self._changed = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "confirmations": self.confirmations,
            "decay_seconds": self.decay_seconds,
            "owner_streak": self._owner_streak,
            "other_streak": self._other_streak,
            "last_confidence": round(self._last_confidence, 4),
            "changed": self._changed,
        }


def _slugify(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    plain = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return "".join(ch if ch.isalnum() else "-" for ch in plain.casefold()).strip("-")


def _same_name(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return _slugify(left) == _slugify(right)


def _who_value(value: Any) -> str:
    if isinstance(value, Who):
        return value.value
    raw = str(getattr(value, "value", value) or "").strip().lower()
    if raw in ("owner", "dono"):
        return Who.OWNER.value
    if raw in ("outro", "other", "outra"):
        return Who.OTHER.value
    return Who.UNCERTAIN.value


def _clean_name(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return max(-1.0, min(1.0, number))


def _match_score(match: Mapping[str, Any]) -> float | None:
    values: list[float] = []
    for key in ("effective_score", "sample_score", "score"):
        number = _finite(match.get(key))
        if number is not None:
            values.append(number)
    return max(values) if values else None


def _unit(vector: Any) -> np.ndarray | None:
    try:
        arr = np.asarray(vector, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        return None
    if arr.size == 0 or not np.all(np.isfinite(arr)):
        return None
    norm = float(np.linalg.norm(arr))
    if norm <= 1e-12:
        return None
    return arr / norm


def _reference_vectors(reference: Any) -> list[np.ndarray]:
    if reference is None:
        return []
    if isinstance(reference, Mapping):
        centroid = _unit(reference.get("centroid"))
        vectors = [centroid] if centroid is not None else []
        samples = reference.get("samples") or reference.get("vectors") or []
        for sample in samples:
            unit = _unit(sample)
            if unit is not None:
                vectors.append(unit)
        return vectors
    try:
        arr = np.asarray(reference, dtype=np.float64)
    except (TypeError, ValueError):
        return []
    if arr.ndim == 1:
        unit = _unit(arr)
        return [unit] if unit is not None else []
    if arr.ndim == 2:
        return [unit for unit in (_unit(row) for row in arr) if unit is not None]
    return []


def _embedding_score(embedding: Any, reference: Any) -> float | None:
    probe = _unit(embedding)
    vectors = _reference_vectors(reference)
    if probe is None or not vectors:
        return None
    return max(float(np.dot(probe, ref)) for ref in vectors)


def _acoustic_label(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        raw = value
    elif isinstance(value, Mapping):
        if bool(value.get("silent")):
            return SILENT
        raw = (value.get("verdict") or value.get("label")
               or value.get("acoustic") or "")
    else:
        if bool(getattr(value, "silent", False)):
            return SILENT
        raw = getattr(value, "verdict", "") or ""
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    return _ACOUSTIC_ALIASES.get(text, AMBIGUOUS)


def _sigmoid(value: float) -> float:
    if value >= 40.0:
        return 1.0
    if value <= -40.0:
        return 0.0
    return 1.0 / (1.0 + math.exp(-value))


def _preview(text: str | None, limit: int = 48) -> str:
    if not text:
        return ""
    plain = " ".join(str(text).split())
    if len(plain) <= limit:
        return plain
    return plain[: limit - 1].rstrip() + "…"


def threshold_for(
    device: str | None = None,
    *,
    registry: voice_cohorts.CohortRegistry | None = None,
    accept_z: float | None = None,
) -> voice_cohorts.Threshold:
    """Per-device limit, calibrated from the cohort distributions.

    ``registry`` is injectable for tests; the default is the read-only
    ``memory/voices/cohorts.json`` + ``index.json`` loader. Without enough
    samples the returned object says ``calibrated=False`` and carries the
    explicit default in ``reason``.
    """
    reg = registry or voice_cohorts.default_registry()
    return reg.threshold_for(device, accept_z=accept_z)


def decide(
    *,
    match: Mapping[str, Any] | None = None,
    score: float | None = None,
    embedding: Sequence[float] | None = None,
    reference: Any = None,
    device: str | None = None,
    acoustic: Any = None,
    transcript: str | None = None,
    voiced_seconds: float | None = None,
    speaker_name: str | None = None,
    owner_name: str | None = DEFAULT_OWNER_NAME,
    authorized: bool | None = None,
    is_owner: bool | None = None,
    privileged: bool = False,
    profile_state: ProfileState | str | None = None,
    state: SpeakerState | None = None,
    registry: voice_cohorts.CohortRegistry | None = None,
    threshold: float | None = None,
    accept_z: float | None = None,
    now: float | None = None,
) -> Verdict:
    """Single entry point for "is this the owner?".

    Preferred input is ``match``, the dict returned by
    ``SpeakerId.identify`` (``score``/``effective_score``, ``name``,
    ``authorized``, ``verdict``). ``score`` alone means a cosine against the
    OWNER profile; ``embedding`` + ``reference`` (centroid or samples) is the
    low-level alternative. ``device`` selects the calibrated limit,
    ``acoustic`` accepts ``mic_compare.verdict_for`` (``near``/``diffuse``/
    ``ambiguous``) or a PairReading, and ``transcript`` is advisory only.

    ``privileged=True`` asks for the stricter answer used by restart, power,
    send and unlock. The returned ``privileged`` is the only authorization:
    state, transcript and ``allow_single``-style forgiveness never grant it.
    """
    moment = time.monotonic() if now is None else float(now)
    match_map = dict(match) if isinstance(match, Mapping) else None
    match_verdict = str((match_map or {}).get("verdict") or "").strip().lower()
    if not device and match_map is not None:
        # SpeakerId.identify already says which profile answered; without an
        # explicit device this is the only honest hardware label to calibrate.
        device = (str(match_map.get("profile") or "").strip()
                  or str(match_map.get("device") or "").strip() or None)

    base = threshold_for(device, registry=registry, accept_z=accept_z)
    if threshold is not None:
        explicit = _finite(threshold)
        if explicit is None:
            raise ValueError("threshold deve ser um número entre -1 e 1")
        base = replace(
            base, threshold=explicit, normalized_threshold=None,
            calibrated=False, source="explicit",
            reason=f"limiar explícito {explicit:.3f}; sem AS-norm")

    requested_state = str(getattr(profile_state, "value", profile_state) or "").lower()
    voiced_value: float | None = None
    if voiced_seconds is not None:
        try:
            voiced_value = float(voiced_seconds)
        except (TypeError, ValueError):
            voiced_value = None
    inferred_no_profile = False
    if match_map is None and requested_state == "":
        if base.owner_profile is False:
            inferred_no_profile = True
        elif base.owner_profile is None and base.has_profile is False:
            inferred_no_profile = True
    no_profile = (requested_state == ProfileState.NO_PROFILE.value
                  or match_verdict == ProfileState.NO_PROFILE.value
                  or inferred_no_profile)
    short = (requested_state in (ProfileState.SHORT.value, "short")
             or match_verdict == ProfileState.SHORT.value
             or (voiced_value is not None
                 and voiced_value < MIN_VOICED_SECONDS))

    if no_profile:
        profile_label = ProfileState.NO_PROFILE.value
    elif short:
        profile_label = ProfileState.SHORT.value
    elif match_map is not None:
        profile_label = ProfileState.OK.value
    elif base.has_profile is True:
        profile_label = ProfileState.OK.value
    else:
        profile_label = ProfileState.NO_DATA.value

    # ── identity from the match dict or from raw owner similarity ──────────
    if match_map is not None:
        name = _clean_name(match_map.get("name"))
        auth = (bool(match_map.get("authorized")) if authorized is None
                else bool(authorized))
        if is_owner is False:
            owner_claim = False
        elif name is not None and owner_name:
            owner_claim = _same_name(name, owner_name) and bool(auth)
        elif is_owner is not None:
            owner_claim = bool(is_owner) and bool(auth)
        else:
            owner_claim = False
    else:
        name = _clean_name(speaker_name) or _clean_name(owner_name)
        auth = True if authorized is None else bool(authorized)
        owner_claim = (is_owner is not False) and bool(auth)

    # ── score ──────────────────────────────────────────────────────────────
    raw_score = _finite(score)
    if raw_score is None and match_map is not None:
        raw_score = _match_score(match_map)
    invalid = ""
    if raw_score is None and embedding is not None:
        try:
            raw_score = _embedding_score(embedding, reference)
        except Exception as exc:                       # noqa: BLE001 - gate safety
            raw_score = None
            invalid = f"embedding inválido ({exc.__class__.__name__})"
    if raw_score is None and score is not None:
        invalid = "score inválido"

    acoustic_label = _acoustic_label(acoustic)
    silent = acoustic_label == SILENT
    diffuse = acoustic_label == DIFFUSE
    near = acoustic_label == NEAR
    speech = None if transcript is None else bool(str(transcript).strip())
    preview = _preview(transcript)

    calibrated = bool(base.calibrated)
    uncalibrated = not calibrated
    if calibrated and base.normalized_threshold is not None:
        normalized = base.normalize(raw_score) if raw_score is not None else 0.0
        limit = float(base.normalized_threshold)
        margin = normalized - limit
        gray = GRAY_BAND_Z
        comparison = (f"AS-norm {normalized:.2f} vs {limit:.2f}")
        confidence_scale = NORM_CONFIDENCE_SCALE
    else:
        normalized = base.normalize(raw_score) if raw_score is not None else 0.0
        limit = float(base.threshold)
        margin = normalized - limit
        gray = GRAY_BAND_RAW
        comparison = (f"score {normalized:.3f} vs limiar {limit:.3f} "
                      f"(sem AS-norm)")
        confidence_scale = RAW_CONFIDENCE_SCALE

    device_label = str(device or "").strip() or "?"

    who = Who.UNCERTAIN
    action = Action.NONE
    confidence = 0.2
    bits: list[str] = []

    if silent:
        who = Who.UNCERTAIN
        action = Action.NONE
        confidence = 0.05
        bits.append("áudio silencioso: nenhuma ação")
    elif no_profile:
        who = Who.UNCERTAIN
        action = Action.NONE if privileged else Action.NORMAL
        confidence = 0.25
        bits.append(
            f"sem perfil de voz neste aparelho ({device_label}): "
            f"estado {ProfileState.NO_PROFILE.value}; "
            "allow_single não autoriza")
        bits.append("privilégio negado" if privileged else "fala normal liberada")
    elif short:
        seconds = (f"{voiced_value:.2f}s"
                   if voiced_value is not None else "pouco")
        who = Who.UNCERTAIN
        action = Action.NONE if privileged else Action.NORMAL
        confidence = 0.3
        bits.append(f"fala curta demais ({seconds} < {MIN_VOICED_SECONDS:.2f}s) "
                    "para decidir")
        bits.append("privilégio negado" if privileged else "fala normal liberada")
    elif raw_score is None:
        who = Who.UNCERTAIN
        action = Action.NONE if privileged else Action.NORMAL
        confidence = 0.2
        bits.append(invalid or "sem embedding/score para comparar a voz")
        bits.append("privilégio negado" if privileged else "fala normal liberada")
    else:
        probability = _sigmoid(margin / confidence_scale)
        passed = margin >= 0.0
        gray_zone = (not passed) and margin >= -gray
        if owner_claim and passed:
            who = Who.OWNER
            action = Action.PRIVILEGED if privileged else Action.NORMAL
            confidence = probability
            bits.append(f"dono confirmado: {comparison}")
        elif owner_claim and gray_zone:
            who = Who.UNCERTAIN
            action = Action.NONE if privileged else Action.NORMAL
            confidence = min(0.5, 0.5 * (1.0 - abs(margin) / gray))
            bits.append(f"faixa cinzenta, dono não confirmado: {comparison}")
            bits.append("privilégio negado" if privileged else "fala normal liberada")
        elif owner_claim:
            who = Who.OTHER
            action = Action.NONE if privileged else Action.NORMAL
            confidence = min(0.95, 1.0 - probability)
            bits.append(f"voz não corresponde ao dono: {comparison}")
            bits.append("privilégio negado" if privileged else "fala normal liberada")
        else:
            who = Who.OTHER
            action = Action.NONE if privileged else Action.NORMAL
            if name is not None and not auth:
                confidence = 0.85
                bits.append(f"voz de {name} não autorizada")
            elif name is not None:
                confidence = max(0.75, min(0.95, 1.0 - probability))
                bits.append(f"voz de {name}, não o dono")
            else:
                confidence = max(0.6, min(0.95, 1.0 - probability))
                bits.append(f"voz não identificada como o dono: {comparison}")
            if not privileged:
                bits.append("fala normal liberada")
            else:
                bits.append("privilégio negado")

    if diffuse:
        if who is Who.OWNER:
            who = Who.UNCERTAIN
        action = Action.NONE
        confidence = min(confidence, 0.40)
        bits.append("acústica difusa (sala/TV): nenhuma ação")
    elif near:
        confidence = min(1.0, confidence + 0.05)
        bits.append("microfone próximo do locutor")
    elif acoustic_label == AMBIGUOUS:
        confidence = max(0.0, confidence - 0.03)

    if privileged and not (who is Who.OWNER and not diffuse
                           and profile_label != ProfileState.NO_PROFILE.value):
        action = Action.NONE
    is_privileged = bool(privileged and who is Who.OWNER
                         and action is Action.PRIVILEGED
                         and profile_label != ProfileState.NO_PROFILE.value
                         and not diffuse and not silent)
    if not is_privileged and action is Action.PRIVILEGED:
        action = Action.NONE if privileged else Action.NORMAL

    if preview:
        bits.append(f'fala="{preview}"')

    state_label = ""
    state_changed = False
    if state is not None:
        state_label = state.observe(who, confidence=confidence, now=moment)
        state_changed = state.changed

    confidence = max(0.0, min(1.0, float(confidence)))
    reason = "; ".join(bit for bit in bits if bit)
    return Verdict(
        who=who,
        confidence=confidence,
        reason=reason,
        action=action,
        privileged=is_privileged,
        score=float(raw_score if raw_score is not None else 0.0),
        normalized_score=float(normalized),
        threshold=float(base.threshold),
        normalized_threshold=base.normalized_threshold,
        uncalibrated=uncalibrated,
        profile_state=profile_label,
        device=device_label,
        cohort=base.cohort,
        cohort_label=base.cohort_label,
        speaker=name,
        acoustic=acoustic_label,
        speech=speech,
        state=state_label,
        state_changed=state_changed,
        threshold_source=base.source,
        threshold_reason=base.reason,
        samples=int(base.samples),
    )


__all__ = [
    "Action",
    "ProfileState",
    "SpeakerState",
    "Verdict",
    "Who",
    "decide",
    "threshold_for",
    "DEFAULT_OWNER_NAME",
    "MIN_VOICED_SECONDS",
    "GRAY_BAND_RAW",
    "GRAY_BAND_Z",
]
