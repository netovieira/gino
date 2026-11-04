"""Voice cohorts: group capture chains by hardware class and calibrate limits.

A single cosine threshold (the historical 0.45) is wrong for every microphone
in a different way: a laptop headset, the C920 webcam and a phone's voice note
compress the spectrum differently, so the same owner lands at different scores.
This module groups the enrolled profiles by the HARDWARE they came from
(``memory/voices/cohorts.json``), measures the two distributions that matter —
intra-class (an owner sample against that device's centroid) and inter-class
(the same samples against another speaker, or against another capture chain
when nobody else is enrolled) — and derives one limit per device from them.

The limit is the point where the score is equally far, in standard deviations,
from the owner's centroid distribution and from the impostor distribution. It
is exposed both raw (``Threshold.threshold``) and in AS-norm units
(``Threshold.normalized_threshold``), so a caller may compare either.

Everything here is deterministic and offline: numpy, json and the local voice
index. No ``main``, no microphone, no network. Reading the index is explicit
(``CohortRegistry.from_index``) or through the read-only default loader.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COHORTS_PATH = _ROOT / "memory" / "voices" / "cohorts.json"
DEFAULT_INDEX_PATH = _ROOT / "memory" / "voices" / "index.json"

DEFAULT_THRESHOLD = 0.45
DEFAULT_ACCEPT_Z = 0.0

# A mean is not a distribution: below these counts the calibration is reported
# as absent and the explicit cohort default is used instead.
MIN_INTRA_SAMPLES = 4
MIN_INTER_SCORES = 6

# Two enrolled clips of the same person are not normally identical; a tiny
# standard deviation would turn AS-norm into a hair-trigger. These floors are
# the variability assumed when the observed spread is smaller than reality.
INTRA_SD_FLOOR = 0.06
INTER_SD_FLOOR = 0.05

# Guard rails against a degenerate calibration (e.g. five near-identical clips
# of one phrase producing a 0.95 limit). Reported as ``clamped``.
THRESHOLD_FLOOR = 0.30
THRESHOLD_CEILING = 0.85

# If the owner's own clips are no closer to the centroid than the impostors
# are, there is nothing to separate and the derived limit would be noise.
MIN_SEPARATION = 0.02

# Two vectors this close carry no independent evidence; treating one as an
# impostor would make the inter-class mean look like the owner.
_DUPLICATE_COSINE = 0.9995
_EPS = 1e-12
_SLUG_RE = re.compile(r"[^a-z0-9]+")

SOURCE_DEVICE = "device"
SOURCE_COHORT = "cohort"
SOURCE_DEFAULT = "default"
SOURCE_SNAPSHOT = "snapshot"


@dataclass(frozen=True, slots=True)
class Distribution:
    """Mean/spread of a group of cosine scores (population: scores)."""

    mean: float
    std: float
    n: int
    minimum: float = 0.0
    maximum: float = 0.0

    @classmethod
    def from_values(cls, values: Sequence[float]) -> Distribution:
        arr = np.asarray(list(values), dtype=np.float64).reshape(-1)
        arr = arr[np.isfinite(arr)]
        if arr.size == 0:
            return cls(0.0, 0.0, 0)
        std = float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0
        return cls(float(np.mean(arr)), std, int(arr.size),
                   float(np.min(arr)), float(np.max(arr)))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Distribution | None:
        if not isinstance(data, Mapping):
            return None
        try:
            return cls(float(data.get("mean") or 0.0),
                       float(data.get("std") or 0.0),
                       int(data.get("n") or 0),
                       float(data.get("min") or 0.0),
                       float(data.get("max") or 0.0))
        except (TypeError, ValueError):
            return None

    def as_dict(self) -> dict[str, Any]:
        return {"mean": round(self.mean, 6), "std": round(self.std, 6),
                "n": int(self.n), "min": round(self.minimum, 6),
                "max": round(self.maximum, 6)}


@dataclass(frozen=True, slots=True)
class Threshold:
    """The limit for one capture chain, raw and AS-norm normalized.

    ``normalize(score)`` is the adaptive symmetric normalization:
    ``0.5 * ((score - intra.mean)/sd_intra + (score - inter.mean)/sd_inter)``.
    With ``calibrated=True`` the accept comparison can be made in those units
    against ``normalized_threshold`` (which is ``accept_z``); the raw
    ``threshold`` is the same boundary converted back. With calibration absent
    the raw score is returned untouched and ``normalized_threshold`` is None.
    """

    device: str
    cohort: str
    cohort_label: str
    threshold: float
    normalized_threshold: float | None
    calibrated: bool
    source: str
    reason: str
    accept_z: float = DEFAULT_ACCEPT_Z
    intra: Distribution | None = None
    inter: Distribution | None = None
    sd_intra: float | None = None
    sd_inter: float | None = None
    samples: int = 0
    has_profile: bool | None = None
    owner_profile: bool | None = None
    profile_devices: tuple[str, ...] = ()
    clamped: bool = False

    def __float__(self) -> float:
        return float(self.threshold)

    def normalize(self, score: float) -> float:
        """AS-norm score; the raw value when calibration is absent."""
        try:
            value = float(score)
        except (TypeError, ValueError):
            return 0.0
        if not self.calibrated or self.intra is None or self.inter is None:
            return value
        sd_intra = self.sd_intra or max(self.intra.std, INTRA_SD_FLOOR)
        sd_inter = self.sd_inter or max(self.inter.std, INTER_SD_FLOOR)
        if sd_intra <= _EPS or sd_inter <= _EPS:
            return value
        return 0.5 * ((value - self.intra.mean) / sd_intra
                      + (value - self.inter.mean) / sd_inter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "cohort": self.cohort,
            "cohort_label": self.cohort_label,
            "threshold": round(float(self.threshold), 6),
            "normalized_threshold": (None if self.normalized_threshold is None
                                     else round(float(self.normalized_threshold), 6)),
            "calibrated": bool(self.calibrated),
            "source": self.source,
            "reason": self.reason,
            "accept_z": round(float(self.accept_z), 6),
            "intra": self.intra.as_dict() if self.intra else None,
            "inter": self.inter.as_dict() if self.inter else None,
            "sd_intra": None if self.sd_intra is None else round(self.sd_intra, 6),
            "sd_inter": None if self.sd_inter is None else round(self.sd_inter, 6),
            "samples": int(self.samples),
            "has_profile": self.has_profile,
            "owner_profile": self.owner_profile,
            "profile_devices": list(self.profile_devices),
            "clamped": bool(self.clamped),
        }


@dataclass(frozen=True, slots=True)
class CohortSpec:
    """One hardware class: label, matching patterns and its default limit."""

    name: str
    label: str
    kind: str
    patterns: tuple[str, ...]
    default_threshold: float = DEFAULT_THRESHOLD
    devices: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, name: str, data: Mapping[str, Any]) -> CohortSpec:
        patterns = data.get("patterns") or ()
        devices = data.get("devices") or ()
        try:
            default = float(data.get("default_threshold") or DEFAULT_THRESHOLD)
        except (TypeError, ValueError):
            default = DEFAULT_THRESHOLD
        return cls(
            name=str(name),
            label=str(data.get("label") or name),
            kind=str(data.get("kind") or "device"),
            patterns=tuple(_slugify(p) for p in patterns if str(p).strip()),
            default_threshold=min(1.0, max(0.0, default)),
            devices=tuple(_slugify(d) for d in devices if str(d).strip()),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "kind": self.kind,
                "patterns": list(self.patterns),
                "default_threshold": round(float(self.default_threshold), 6),
                "devices": list(self.devices)}


def _slugify(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    plain = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return _SLUG_RE.sub("-", plain.casefold()).strip("-")


def _vector(value: Any) -> np.ndarray | None:
    """One finite, non-zero vector scaled to unit length, else None."""
    try:
        arr = np.asarray(value, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        return None
    if arr.size == 0 or not np.all(np.isfinite(arr)):
        return None
    norm = float(np.linalg.norm(arr))
    if norm <= _EPS:
        return None
    return arr / norm


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return max(-1.0, min(1.0, float(np.dot(a, b))))


def _dedupe(vectors: Sequence[np.ndarray]) -> list[np.ndarray]:
    """Drop repeated/near-identical vectors (migrated profiles copy samples)."""
    out: list[np.ndarray] = []
    for vec in vectors:
        if all(_cosine(vec, kept) < _DUPLICATE_COSINE for kept in out):
            out.append(vec)
    return out


def _matches_pattern(slug: str, patterns: Sequence[str]) -> bool:
    """A short alias like ``pc`` matches a token, never a substring.

    Without this rule an ALSA node called ``...-pcm...`` would be classified as
    a desktop microphone and ``headphone`` as a phone.
    """
    tokens = set(slug.split("-"))
    for pattern in patterns:
        if not pattern:
            continue
        if pattern == slug or pattern in tokens:
            return True
        if len(pattern) > 2 and pattern in slug:
            return True
    return False


def _builtin_specs() -> dict[str, CohortSpec]:
    """Fallback hardware classes, used when cohorts.json does not exist yet."""
    specs = (
        CohortSpec("webcam-c920", "webcam Logitech C920", "webcam",
                   ("c920", "webcam", "046d")),
        CohortSpec("headset-edifier", "fone Edifier", "headset",
                   ("edifier", "headset", "headphone", "fone-de-ouvido",
                    "fone")),
        CohortSpec("celular", "celular", "phone",
                   ("celular", "telefone", "android", "phone")),
        CohortSpec("pc", "microfone do PC", "pc", ("pc", "desktop")),
        CohortSpec("default", "aparelho desconhecido", "default", ()),
    )
    return {spec.name: spec for spec in specs}


def _specs_from_data(data: Mapping[str, Any] | None) -> dict[str, CohortSpec]:
    specs = _builtin_specs()
    if not isinstance(data, Mapping):
        return specs
    cohorts = data.get("cohorts")
    if not isinstance(cohorts, Mapping) or not cohorts:
        return specs
    out: dict[str, CohortSpec] = {}
    for name, raw in cohorts.items():
        if isinstance(raw, Mapping):
            out[_slugify(name) or str(name)] = CohortSpec.from_dict(str(name), raw)
    if "default" not in out:
        out["default"] = specs["default"]
    return out or specs


def load_index(path: str | Path | None = None) -> dict[str, Any] | None:
    """Read the voice index; None when missing or unreadable. Never raises."""
    target = Path(path) if path else DEFAULT_INDEX_PATH
    try:
        with target.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _index_owner(index: Mapping[str, Any]) -> str:
    return str(index.get("owner") or "Anthero").strip() or "Anthero"


def _index_aliases(index: Mapping[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(index, Mapping):
        return out
    raw = index.get("device_aliases")
    if isinstance(raw, Mapping):
        for old, new in raw.items():
            old_slug, new_slug = _slugify(old), _slugify(new)
            if old_slug and new_slug and old_slug != new_slug:
                out[old_slug] = new_slug
    return out


def _rows_from_index(index: Mapping[str, Any] | None
                     ) -> list[tuple[str, str, list[np.ndarray]]]:
    """[(speaker, device, [unit vectors])] from an index-shaped mapping.

    Only trusted ``samples`` are read; ``auto_samples`` are deliberately
    ignored because they may have been grown from a conversational match.
    """
    if not isinstance(index, Mapping):
        return []
    speakers = index.get("speakers")
    if not isinstance(speakers, Mapping):
        return []
    rows: list[tuple[str, str, list[np.ndarray]]] = []
    for slug, rec in speakers.items():
        if not isinstance(rec, Mapping):
            continue
        speaker = str(rec.get("name") or slug)
        profiles = rec.get("profiles")
        if isinstance(profiles, Mapping) and profiles:
            for device, profile in profiles.items():
                if not isinstance(profile, Mapping):
                    continue
                vectors = [v for v in (_vector(s) for s in profile.get("samples") or [])
                           if v is not None]
                if vectors:
                    rows.append((speaker, _slugify(device), vectors))
            continue
        vectors = [v for v in (_vector(s) for s in rec.get("samples") or [])
                   if v is not None]
        if vectors:
            device = _slugify(rec.get("primary") or "pc")
            rows.append((speaker, device, vectors))
    return rows


def _intra_distribution(vectors: Sequence[np.ndarray]) -> Distribution:
    """Each sample against the centroid it will be compared with at runtime."""
    if not vectors:
        return Distribution(0.0, 0.0, 0)
    mean = np.mean(np.asarray(vectors, dtype=np.float64), axis=0)
    centroid = _vector(mean)
    if centroid is None:
        return Distribution(0.0, 0.0, 0)
    scores = [_cosine(vec, centroid) for vec in vectors]
    return Distribution.from_values(scores)


def _cross_scores(target: Sequence[np.ndarray],
                  pool: Sequence[np.ndarray]) -> list[float]:
    scores: list[float] = []
    for vec in target:
        for other in pool:
            score = _cosine(vec, other)
            # A copied/migrated vector is the same recording, not an impostor.
            if score < _DUPLICATE_COSINE:
                scores.append(score)
    return scores


def _derive_threshold(intra: Distribution, inter: Distribution, accept_z: float,
                      *, sd_intra_floor: float = INTRA_SD_FLOOR,
                      sd_inter_floor: float = INTER_SD_FLOOR,
                      floor: float = THRESHOLD_FLOOR,
                      ceiling: float = THRESHOLD_CEILING
                      ) -> tuple[float, float, float, bool]:
    """Raw limit at ``accept_z``; returns (threshold, sd_intra, sd_inter, clamped).

    Solves ``0.5*((t-mi)/si + (t-me)/se) = accept_z`` — the score whose
    standardized distance to the owner's distribution equals its standardized
    distance to the impostor distribution (plus the requested offset).
    """
    sd_intra = max(float(intra.std), float(sd_intra_floor), _EPS)
    sd_inter = max(float(inter.std), float(sd_inter_floor), _EPS)
    denominator = (1.0 / sd_intra) + (1.0 / sd_inter)
    raw = (2.0 * float(accept_z)
           + float(intra.mean) / sd_intra
           + float(inter.mean) / sd_inter) / denominator
    clamped = raw < floor or raw > ceiling
    return min(ceiling, max(floor, raw)), sd_intra, sd_inter, clamped


class CohortRegistry:
    """Hardware classes plus an optional in-memory voice index.

    Build it explicitly for tests/calibration::

        CohortRegistry.from_index(index_dict)

    or let :func:`default_registry` load ``cohorts.json`` and ``index.json``.
    """

    def __init__(
        self,
        specs: Mapping[str, CohortSpec] | None = None,
        *,
        aliases: Mapping[str, str] | None = None,
        index: Mapping[str, Any] | None = None,
        snapshot: Mapping[str, Any] | None = None,
        config: Mapping[str, Any] | None = None,
        path: str | Path | None = None,
    ) -> None:
        specs = dict(specs or _builtin_specs())
        if "default" not in specs:
            specs["default"] = _builtin_specs()["default"]
        self._specs = specs
        self._aliases = {_slugify(k): _slugify(v)
                         for k, v in dict(aliases or {}).items()
                         if _slugify(k) and _slugify(v)}
        self._index = index if isinstance(index, Mapping) else None
        self._snapshot = snapshot if isinstance(snapshot, Mapping) else None
        self._path = Path(path) if path else None
        self._config = {
            "accept_z": DEFAULT_ACCEPT_Z,
            "min_intra_samples": MIN_INTRA_SAMPLES,
            "min_inter_scores": MIN_INTER_SCORES,
            "intra_sd_floor": INTRA_SD_FLOOR,
            "inter_sd_floor": INTER_SD_FLOOR,
            "threshold_floor": THRESHOLD_FLOOR,
            "threshold_ceiling": THRESHOLD_CEILING,
            "min_separation": MIN_SEPARATION,
        }
        if isinstance(config, Mapping):
            for key in self._config:
                if key in config:
                    try:
                        self._config[key] = float(config[key])
                    except (TypeError, ValueError):
                        pass
        self._cache: dict[tuple[str, float], Threshold] = {}

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def from_index(cls, index: Mapping[str, Any], *,
                   specs: Mapping[str, CohortSpec] | None = None) -> CohortRegistry:
        """Registry calibrated live from an index-shaped dict (tests/tools)."""
        return cls(specs or _builtin_specs(), aliases=_index_aliases(index),
                   index=index)

    @classmethod
    def from_file(cls, path: str | Path | None = None, *,
                  index_path: str | Path | None = None) -> CohortRegistry:
        """Load ``cohorts.json`` plus the live index, read-only.

        The index wins over the persisted statistics: a newly enrolled clip
        must move the limit without a manual refresh. The file remains the
        fallback when the index is unavailable (packaged/test environments).
        """
        cohorts_path = Path(path) if path else DEFAULT_COHORTS_PATH
        data = _read_json(cohorts_path)
        index = load_index(index_path)
        specs = _specs_from_data(data)
        aliases = dict(_index_aliases(index))
        if isinstance(data, Mapping):
            aliases.update(_index_aliases(data))
        snapshot = data.get("cohorts") if isinstance(data, Mapping) else None
        config = data if isinstance(data, Mapping) else None
        return cls(specs, aliases=aliases, index=index,
                   snapshot=snapshot if isinstance(snapshot, Mapping) else None,
                   config=config, path=cohorts_path)

    # ── public queries ──────────────────────────────────────────────────────

    @property
    def source_path(self) -> Path | None:
        return self._path

    def resolved_device(self, device: str | None) -> str:
        slug = _slugify(device)
        seen: set[str] = set()
        while slug in self._aliases and slug not in seen:
            seen.add(slug)
            slug = self._aliases[slug]
        return slug

    def cohort_for(self, device: str | None) -> str:
        slug = self.resolved_device(device)
        if not slug:
            return "default"
        for name, spec in self._specs.items():
            if name == "default":
                continue
            if slug in spec.devices:
                return name
        for name, spec in self._specs.items():
            if name == "default":
                continue
            if _matches_pattern(slug, spec.patterns):
                return name
        return "default"

    def threshold_for(self, device: str | None = None, *,
                      accept_z: float | None = None) -> Threshold:
        try:
            z = (self._config["accept_z"] if accept_z is None
                 else float(accept_z))
        except (TypeError, ValueError):
            z = DEFAULT_ACCEPT_Z
        key = (_slugify(device), float(z))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        name = self.cohort_for(device)
        spec = self._specs.get(name) or self._specs["default"]
        if self._index is not None:
            result = self._live_threshold(device, name, spec, z)
        else:
            result = self._snapshot_threshold(device, name, spec, z)
        self._cache[key] = result
        return result

    # ── live calibration ────────────────────────────────────────────────────

    def _live_threshold(self, device: str | None, name: str, spec: CohortSpec,
                        accept_z: float) -> Threshold:
        slug = _slugify(device)
        resolved = self.resolved_device(device)
        rows = _rows_from_index(self._index)
        owner = _index_owner(self._index or {})
        owner_rows = [row for row in rows if row[0] == owner]
        all_rows = rows

        target_devices = {slug, resolved} - {""}
        target = _dedupe([vec for _speaker, dev, vectors in owner_rows
                          if dev in target_devices for vec in vectors])
        cohort_vectors = _dedupe([vec for _speaker, dev, vectors in owner_rows
                                  if self.cohort_for(dev) == name
                                  for vec in vectors])

        source = SOURCE_DEVICE
        if len(target) < self._config["min_intra_samples"]:
            if len(cohort_vectors) > len(target):
                target = cohort_vectors
                source = SOURCE_COHORT
            elif not target:
                target = cohort_vectors
                source = SOURCE_COHORT

        has_profile = (any(dev in target_devices for _s, dev, _v in all_rows)
                       if target_devices else None)
        owner_profile = bool(target) if target_devices else None
        profile_devices = tuple(sorted({
            dev for _speaker, dev, _vectors in all_rows
            if self.cohort_for(dev) == name
        } | {d for d in spec.devices if self.cohort_for(d) == name}))

        # Inter-class: other speakers first (true impostors); with a single
        # enrolled speaker, the same speaker on another capture chain is the
        # domain-mismatch proxy. Copied/migrated vectors are skipped later.
        other_speaker = [vec for speaker, _dev, vectors in all_rows
                         if speaker != owner for vec in vectors]
        other_chain = [vec for _speaker, dev, vectors in owner_rows
                       if dev not in target_devices
                       and self.cohort_for(dev) != name for vec in vectors]
        pool = _dedupe(other_speaker)
        if (len(_cross_scores(target, pool))
                < self._config["min_inter_scores"]):
            pool = _dedupe(list(pool) + other_chain)

        intra = _intra_distribution(target)
        inter = Distribution.from_values(_cross_scores(target, pool))
        return self._build_threshold(
            device or "", name, spec, accept_z, intra, inter, source,
            samples=len(target), has_profile=has_profile,
            owner_profile=owner_profile,
            profile_devices=profile_devices,
            missing_reason=self._missing_reason(
                device, name, target, pool, intra, inter))

    def _missing_reason(self, device: str | None, cohort: str,
                        target: Sequence[np.ndarray], pool: Sequence[np.ndarray],
                        intra: Distribution, inter: Distribution) -> str:
        label = device or "aparelho ativo"
        if not target:
            return (f"sem amostras do dono em {label} nem na coorte {cohort}; "
                    "default explícito")
        if intra.n < self._config["min_intra_samples"]:
            return (f"amostras intra insuficientes em {label} "
                    f"({intra.n} < {int(self._config['min_intra_samples'])}); "
                    "default explícito")
        if not pool or inter.n < self._config["min_inter_scores"]:
            return (f"escores inter-classe insuficientes em {label} "
                    f"({inter.n} < {int(self._config['min_inter_scores'])}); "
                    "default explícito")
        if intra.mean <= inter.mean or (intra.mean - inter.mean) < float(
                self._config["min_separation"]):
            return (f"distribuições não separáveis em {label} "
                    f"(dono {intra.mean:.2f}, impostor {inter.mean:.2f}, "
                    f"separação {intra.mean - inter.mean:.3f} < "
                    f"{self._config['min_separation']:.2f}); default explícito")
        return "calibração indisponível; default explícito"

    def _build_threshold(self, device: str, cohort: str, spec: CohortSpec,
                         accept_z: float, intra: Distribution,
                         inter: Distribution, source: str, *,
                         samples: int, has_profile: bool | None,
                         owner_profile: bool | None,
                         profile_devices: tuple[str, ...],
                         missing_reason: str) -> Threshold:
        min_intra = int(self._config["min_intra_samples"])
        min_inter = int(self._config["min_inter_scores"])
        calibrated = (intra.n >= min_intra and inter.n >= min_inter
                      and (intra.mean - inter.mean) >= float(
                          self._config["min_separation"]))
        if not calibrated:
            return Threshold(
                device=device, cohort=cohort, cohort_label=spec.label,
                threshold=float(spec.default_threshold),
                normalized_threshold=None, calibrated=False,
                source=SOURCE_DEFAULT,
                reason=(f"{missing_reason} {spec.default_threshold:.2f} "
                        f"(coorte {cohort}, sem AS-norm)"),
                accept_z=float(accept_z), intra=intra if intra.n else None,
                inter=inter if inter.n else None, samples=int(samples),
                has_profile=has_profile, owner_profile=owner_profile,
                profile_devices=profile_devices,
            )

        raw, sd_intra, sd_inter, clamped = _derive_threshold(
            intra, inter, accept_z,
            sd_intra_floor=self._config["intra_sd_floor"],
            sd_inter_floor=self._config["inter_sd_floor"],
            floor=self._config["threshold_floor"],
            ceiling=self._config["threshold_ceiling"])
        reason = (
            f"limiar calibrado por {source}: intra n={intra.n} "
            f"média={intra.mean:.3f} dp={intra.std:.3f}; inter n={inter.n} "
            f"média={inter.mean:.3f} dp={inter.std:.3f}; aceita_z={accept_z:.2f}")
        if clamped:
            reason += (f"; limitado à faixa "
                       f"[{self._config['threshold_floor']:.2f}, "
                       f"{self._config['threshold_ceiling']:.2f}]")
        return Threshold(
            device=device, cohort=cohort, cohort_label=spec.label,
            threshold=float(raw),
            normalized_threshold=float(accept_z), calibrated=True,
            source=source, reason=reason, accept_z=float(accept_z),
            intra=intra, inter=inter, sd_intra=sd_intra, sd_inter=sd_inter,
            samples=int(samples), has_profile=has_profile,
            owner_profile=owner_profile,
            profile_devices=profile_devices, clamped=clamped,
        )

    # ── persisted fallback ──────────────────────────────────────────────────

    def _snapshot_threshold(self, device: str | None, name: str,
                            spec: CohortSpec, accept_z: float) -> Threshold:
        slug = _slugify(device)
        resolved = self.resolved_device(device)
        raw = {}
        if isinstance(self._snapshot, Mapping):
            raw = self._snapshot.get(name) or {}
        if not isinstance(raw, Mapping):
            raw = {}
        devices = tuple(_slugify(d) for d in raw.get("devices") or [])
        has_profile = ((slug in devices or resolved in devices)
                       if (slug or resolved) and devices else None)
        stats = raw.get("stats") if isinstance(raw.get("stats"), Mapping) else {}
        per_device = stats.get("devices") if isinstance(stats, Mapping) else {}
        entry = None
        if isinstance(per_device, Mapping):
            entry = per_device.get(slug) or per_device.get(resolved)
        source = SOURCE_SNAPSHOT
        if not isinstance(entry, Mapping):
            entry = stats.get("pooled") if isinstance(stats, Mapping) else None
            source = SOURCE_COHORT
        if not isinstance(entry, Mapping):
            return Threshold(
                device=device or "", cohort=name, cohort_label=spec.label,
                threshold=float(spec.default_threshold),
                normalized_threshold=None, calibrated=False,
                source=SOURCE_DEFAULT,
                reason=(f"cohorts.json sem estatísticas para {device or name}; "
                        f"default explícito {spec.default_threshold:.2f}"),
                accept_z=float(accept_z), has_profile=has_profile,
                owner_profile=None, profile_devices=devices)

        intra = Distribution.from_dict(entry.get("intra"))
        inter = Distribution.from_dict(entry.get("inter"))
        try:
            threshold = float(entry.get("threshold") or spec.default_threshold)
        except (TypeError, ValueError):
            threshold = float(spec.default_threshold)
        calibrated = bool(entry.get("calibrated")) and intra is not None and inter is not None
        sd_intra = entry.get("sd_intra")
        sd_inter = entry.get("sd_inter")
        return Threshold(
            device=device or "", cohort=name, cohort_label=spec.label,
            threshold=threshold,
            normalized_threshold=float(accept_z) if calibrated else None,
            calibrated=calibrated, source=str(entry.get("source") or source),
            reason=str(entry.get("reason") or
                       f"estatísticas persistidas da coorte {name}"),
            accept_z=float(accept_z), intra=intra, inter=inter,
            sd_intra=None if sd_intra is None else float(sd_intra),
            sd_inter=None if sd_inter is None else float(sd_inter),
            samples=int(entry.get("samples") or (intra.n if intra else 0)),
            has_profile=has_profile, owner_profile=bool(
                entry.get("samples") or (intra.n if intra else 0)),
            profile_devices=devices,
            clamped=bool(entry.get("clamped")),
        )

    # ── snapshot / refresh ──────────────────────────────────────────────────

    def as_dict(self) -> dict[str, Any]:
        """Serializable snapshot of specs, membership and derived statistics."""
        rows = _rows_from_index(self._index)
        owner = _index_owner(self._index or {})
        all_devices: list[str] = []
        for _speaker, dev, _vectors in rows:
            if dev not in all_devices:
                all_devices.append(dev)
        for spec in self._specs.values():
            for dev in spec.devices:
                if dev not in all_devices:
                    all_devices.append(dev)

        cohorts: dict[str, Any] = {}
        for name, spec in self._specs.items():
            members = [d for d in all_devices if self.cohort_for(d) == name]
            devices_stats: dict[str, Any] = {}
            pooled_vectors = _dedupe([
                vec for _speaker, dev, vectors in rows
                if self.cohort_for(dev) == name for vec in vectors])
            for dev in members:
                target = _dedupe([
                    vec for speaker, d, vectors in rows
                    if d == dev and speaker == owner for vec in vectors])
                if not target:
                    continue
                devices_stats[dev] = self._stats_entry(
                    dev, name, target, rows, owner)
            pooled = None
            if pooled_vectors:
                pooled = self._stats_entry(
                    name, name, pooled_vectors, rows, owner)
            cohorts[name] = {
                **spec.as_dict(),
                "devices": members,
                "stats": {"devices": devices_stats,
                          "pooled": pooled},
            }

        data = {
            "version": 1,
            "updated": time.time(),
            "owner": owner,
            "accept_z": float(self._config["accept_z"]),
            "min_intra_samples": int(self._config["min_intra_samples"]),
            "min_inter_scores": int(self._config["min_inter_scores"]),
            "intra_sd_floor": float(self._config["intra_sd_floor"]),
            "inter_sd_floor": float(self._config["inter_sd_floor"]),
            "threshold_floor": float(self._config["threshold_floor"]),
            "threshold_ceiling": float(self._config["threshold_ceiling"]),
            "min_separation": float(self._config["min_separation"]),
            "device_aliases": dict(self._aliases),
            "cohorts": cohorts,
        }
        return data

    def _stats_entry(self, label: str, cohort: str,
                     target: Sequence[np.ndarray],
                     rows: Sequence[tuple[str, str, list[np.ndarray]]],
                     owner: str) -> dict[str, Any]:
        resolved = self.resolved_device(label)
        target_devices = {_slugify(label), resolved} - {""}
        other_speaker = [vec for speaker, _dev, vectors in rows
                         if speaker != owner for vec in vectors]
        other_chain = [vec for _speaker, dev, vectors in rows
                       if dev not in target_devices
                       and self.cohort_for(dev) != cohort for vec in vectors]
        pool = _dedupe(other_speaker)
        if (len(_cross_scores(target, pool))
                < self._config["min_inter_scores"]):
            pool = _dedupe(list(pool) + other_chain)
        intra = _intra_distribution(target)
        inter = Distribution.from_values(_cross_scores(target, pool))
        missing = self._missing_reason(label, cohort, target, pool, intra, inter)
        threshold = self._build_threshold(
            label, cohort, self._specs.get(cohort) or self._specs["default"],
            self._config["accept_z"], intra, inter,
            SOURCE_COHORT if label == cohort else SOURCE_DEVICE,
            samples=len(target), has_profile=None,
            owner_profile=bool(target), profile_devices=(),
            missing_reason=missing)
        return {
            "intra": intra.as_dict(),
            "inter": inter.as_dict(),
            "threshold": round(float(threshold.threshold), 6),
            "calibrated": bool(threshold.calibrated),
            "source": threshold.source,
            "reason": threshold.reason,
            "sd_intra": None if threshold.sd_intra is None
            else round(threshold.sd_intra, 6),
            "sd_inter": None if threshold.sd_inter is None
            else round(threshold.sd_inter, 6),
            "samples": int(threshold.samples),
            "clamped": bool(threshold.clamped),
        }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def refresh(store_dir: str | Path | None = None,
            cohorts_path: str | Path | None = None, *,
            persist: bool = True) -> dict[str, Any]:
    """Recompute cohort statistics from ``index.json`` and optionally persist.

    Call after an enrollment (or at startup) so ``cohorts.json`` reflects the
    profiles that actually exist. The return value is the same dict written.
    """
    store = Path(store_dir) if store_dir else DEFAULT_COHORTS_PATH.parent
    cohorts = Path(cohorts_path) if cohorts_path else store / "cohorts.json"
    index = load_index(store / "index.json")
    existing = _read_json(cohorts)
    specs = _specs_from_data(existing)
    aliases = dict(_index_aliases(index))
    if isinstance(existing, Mapping):
        aliases.update(_index_aliases(existing))
    registry = CohortRegistry(
        specs, aliases=aliases, index=index,
        snapshot=(existing or {}).get("cohorts"),
        config=existing, path=cohorts)
    data = registry.as_dict()
    if persist:
        _write_json(cohorts, data)
        clear_cache()
    return data


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, float, float], CohortRegistry] = {}


def _mtime(path: Path) -> float:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return -1.0


def default_registry(path: str | Path | None = None,
                     index_path: str | Path | None = None) -> CohortRegistry:
    """Read-only registry from ``cohorts.json`` + ``index.json`` (cached).

    The cache key includes both mtimes, so an enrollment or a refreshed
    cohort file is picked up without restarting the process.
    """
    cohorts = Path(path) if path else DEFAULT_COHORTS_PATH
    index = Path(index_path) if index_path else DEFAULT_INDEX_PATH
    key = (str(cohorts), _mtime(cohorts), _mtime(index))
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
        registry = CohortRegistry.from_file(cohorts, index_path=index)
        _CACHE.clear()
        _CACHE[key] = registry
        return registry
