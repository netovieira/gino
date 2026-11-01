"""Project stack detection and the path -> persona pin board.

WHY THIS EXISTS
    A persona is chosen by what the project *is*, not by what the owner
    remembered to say. ``detect()`` reads a bounded set of manifests under a
    directory (depth 2, at most 200 files) and returns normalized stack tags:
    ``react``, ``nextjs``, ``typescript``, ``python``, ``fastapi``, ``rust``...
    ``resolve()`` scores the personas' ``stacks`` against those tags, and an
    explicit pin always wins over detection.

STORAGE
    memory/projects.json (local, git-ignored), written atomically::

        {"projects": {"/abs/path": {"name": ..., "stacks": [...],
                                    "persona": "slug", "source": "pin|auto",
                                    "detected_at": "..."}}}

    Detection is cached per absolute path by manifest mtime plus a 10-minute
    TTL: a directory being edited all day is not re-read on every turn, but a
    new dependency invalidates the old answer immediately.

NO UI, NO MODEL
    This module never imports Qt, main.py or the brain. It reads files and JSON;
    the caller decides what to say.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECTS_PATH = BASE_DIR / "memory" / "projects.json"

# Detection limits. A monorepo can be huge; the point is to recognise the
# project, not to index it.
MAX_DEPTH = 2
MAX_FILES = 200
MAX_MANIFEST_BYTES = 512 * 1024
DETECT_TTL_SECONDS = 600.0

# Two matching tags are a confident hit when the project has that many tags.
# A single-tag project (e.g. bare ``python``) only requires one.
MIN_SCORE = 2

IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode",
    "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "target", ".next", ".nuxt", ".svelte-kit",
    "coverage", "vendor", "logs",
}

_MANIFEST_NAMES = {
    "package.json", "requirements.txt", "pyproject.toml", "go.mod",
    "Cargo.toml", "composer.json", "Gemfile", "pom.xml", "Dockerfile",
}

# Aliases people actually say, mapped to the canonical tag.
STACK_ALIASES = {
    "next.js": "nextjs",
    "nextjs": "nextjs",
    "next": "nextjs",
    "reactjs": "react",
    "react.js": "react",
    "react js": "react",
    "ts": "typescript",
    "tsx": "typescript",
    "js": "node",
    "javascript": "node",
    "nodejs": "node",
    "node.js": "node",
    "node js": "node",
    "vuejs": "vue",
    "vue.js": "vue",
    "vue js": "vue",
    "angularjs": "angular",
    "angular.js": "angular",
    "angular js": "angular",
    "next js": "nextjs",
    "sveltekit": "svelte",
    "py": "python",
    "python3": "python",
    "golang": "go",
    "rs": "rust",
    "cargo": "rust",
    "php8": "php",
    "rb": "ruby",
    "ror": "rails",
    "ruby on rails": "rails",
    "ruby-on-rails": "rails",
    "spring boot": "spring",
    "spring-boot": "spring",
    "springboot": "spring",
    "csharp": "dotnet",
    "c#": "dotnet",
    ".net": "dotnet",
    "dot-net": "dotnet",
    "dockerfile": "docker",
    "container": "docker",
}

_LOCK = threading.RLock()
_CACHE: dict[str, dict] = {}


# ── small helpers ───────────────────────────────────────────────────────────

def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _abs(path) -> str:
    """Absolute, symlink-resolved path; works for paths that do not exist."""
    try:
        p = os.path.realpath(os.path.expanduser(str(path)))
    except Exception:
        p = os.path.abspath(os.path.expanduser(str(path)))
    return p


def _project_root(path) -> str:
    """The directory a path belongs to (a file path resolves to its parent)."""
    root = _abs(path)
    try:
        if os.path.isfile(root):
            root = os.path.dirname(root)
    except OSError:
        pass
    return root


def _base_name(path: str) -> str:
    return os.path.basename(path.rstrip(os.sep)) or path


# ── stack normalization ─────────────────────────────────────────────────────

def normalize_stack(tag) -> str:
    """One alias-normalized stack tag, or '' when nothing sensible is left."""
    raw = str(tag or "").strip().lower()
    if not raw:
        return ""
    raw = raw.replace("_", "-")
    # Collapse whitespace inside names such as "ruby on rails".
    compact = re.sub(r"\s+", " ", raw).strip()
    if compact in STACK_ALIASES:
        return STACK_ALIASES[compact]
    word = re.sub(r"[^a-z0-9+#.]+", "-", compact).strip("-.")
    return STACK_ALIASES.get(word, word)


def normalize_stacks(values) -> list[str]:
    """Normalize a string ('react, ts') or a list into unique canonical tags."""
    if values is None:
        return []
    if isinstance(values, str):
        parts = re.split(r"[,;]", values)
    elif isinstance(values, (list, tuple, set)):
        parts = list(values)
    else:
        parts = [values]

    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if part is None:
            continue
        text = str(part).strip()
        if not text:
            continue
        # A single item may still carry several tags ("react typescript") and
        # a multi-word alias ("ruby on rails").
        items = [text]
        if " " in text and text.lower() not in STACK_ALIASES:
            items = text.split()
        for item in items:
            tag = normalize_stack(item)
            if tag and tag not in seen:
                seen.add(tag)
                out.append(tag)
    return out


# ── detection ───────────────────────────────────────────────────────────────

def _is_manifest(name: str) -> bool:
    return name in _MANIFEST_NAMES or name.endswith(".csproj")


def _scan_manifests(root: str) -> list[tuple[str, str]]:
    """(absolute path, path relative to root) for bounded manifests."""
    if not os.path.isdir(root):
        return []
    found: list[tuple[str, str]] = []
    inspected = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= MAX_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in IGNORED_DIRS and not d.startswith(".")
            )
        for name in sorted(filenames):
            if inspected >= MAX_FILES:
                return found
            inspected += 1
            if _is_manifest(name):
                found.append((os.path.join(dirpath, name),
                              os.path.normpath(os.path.join(rel, name))))
    return found


def _read_manifest(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_MANIFEST_BYTES)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _json_obj(text: str) -> dict:
    try:
        value = json.loads(text)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _package_tags(text: str) -> set[str]:
    tags = {"node"}
    data = _json_obj(text)
    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies",
                "optionalDependencies"):
        block = data.get(key)
        if isinstance(block, dict):
            deps.update(str(name).lower() for name in block)

    haystack = " ".join(sorted(deps)) if deps else text.lower()

    def has(name: str) -> bool:
        return name in haystack

    if has("next"):
        tags.add("nextjs")
    if has("react"):
        tags.add("react")
    if has("vue") or has("@vue/"):
        tags.add("vue")
    if has("@angular/") or has("angular"):
        tags.add("angular")
    if has("svelte"):
        tags.add("svelte")
    if has("typescript"):
        tags.add("typescript")
    return tags


def _framework_tags(filename: str, text: str) -> set[str]:
    """Tags for one manifest, without normalization (callers normalize)."""
    low = text.lower()
    if filename == "package.json":
        return _package_tags(text)
    if filename in ("requirements.txt", "pyproject.toml"):
        tags = {"python"}
        if re.search(r"\bdjango\b", low):
            tags.add("django")
        if re.search(r"\bflask\b", low):
            tags.add("flask")
        if re.search(r"\bfastapi\b", low):
            tags.add("fastapi")
        return tags
    if filename == "go.mod":
        return {"go"}
    if filename == "Cargo.toml":
        return {"rust"}
    if filename == "composer.json":
        tags = {"php"}
        if "laravel" in low:
            tags.add("laravel")
        return tags
    if filename == "Gemfile":
        tags = {"ruby"}
        if re.search(r"\brails\b", low):
            tags.add("rails")
        return tags
    if filename == "pom.xml":
        tags = {"java"}
        if "spring" in low:
            tags.add("spring")
        return tags
    if filename.endswith(".csproj"):
        return {"dotnet"}
    if filename == "Dockerfile":
        return {"docker"}
    return set()


def _signature(manifests: list[tuple[str, str]]) -> tuple:
    sig = []
    for path, rel in manifests:
        try:
            st = os.stat(path)
            sig.append((rel, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((rel, 0, 0))
    return tuple(sig)


def detect(path) -> list[str]:
    """Normalized stack tags detected under ``path`` (bounded and cached)."""
    root = _project_root(path)
    manifests = _scan_manifests(root)
    if not manifests:
        return []

    signature = _signature(manifests)
    now = time.monotonic()
    with _LOCK:
        cached = _CACHE.get(root)
        if (cached and cached.get("signature") == signature
                and now - float(cached.get("at") or 0.0) < DETECT_TTL_SECONDS):
            return list(cached.get("stacks") or [])

    tags: set[str] = set()
    for manifest_path, rel in manifests:
        filename = os.path.basename(rel)
        text = _read_manifest(manifest_path)
        tags.update(_framework_tags(filename, text))
    result = sorted(normalize_stacks(sorted(tags)))

    with _LOCK:
        _CACHE[root] = {"signature": signature, "stacks": result, "at": now}
    return list(result)


def clear_cache(path=None) -> None:
    """Drop cached detection for one path (or everything when omitted)."""
    with _LOCK:
        if path is None:
            _CACHE.clear()
            return
        _CACHE.pop(_project_root(path), None)


# ── project board ───────────────────────────────────────────────────────────

def _empty() -> dict:
    return {"projects": {}}


def load() -> dict:
    """The stored board; malformed or missing files yield an empty board."""
    with _LOCK:
        path = Path(PROJECTS_PATH)
        if not path.exists():
            return _empty()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return _empty()
        if not isinstance(data, dict):
            return _empty()
        projects = data.get("projects")
        if not isinstance(projects, dict):
            return _empty()
        clean = {str(k): v for k, v in projects.items() if isinstance(v, dict)}
        return {"projects": clean}


def save(data: dict) -> None:
    """Write atomically via tmp + os.replace; never touches another file."""
    projects = data.get("projects") if isinstance(data, dict) else None
    payload = {"projects": projects if isinstance(projects, dict) else {}}
    path = Path(PROJECTS_PATH)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                   dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _find_persona(personas, key):
    wanted = str(key or "").strip().casefold()
    if not wanted:
        return None
    items = personas if isinstance(personas, (list, tuple)) else (
        list(personas.values()) if isinstance(personas, dict) else [])
    for persona in items:
        if not isinstance(persona, dict):
            continue
        pid = str(persona.get("id") or "").strip().casefold()
        name = str(persona.get("name") or "").strip().casefold()
        if wanted in (pid, name):
            return persona
    return None


def resolve(path, personas) -> dict:
    """Pick a persona for a project: pin first, then stack scoring."""
    root = _project_root(path)
    record = load()["projects"].get(root) or {}
    detected = detect(root)
    if not detected and isinstance(record.get("stacks"), list):
        detected = normalize_stacks(record.get("stacks"))

    if record.get("source") == "pin":
        pinned = _find_persona(personas, record.get("persona"))
        if pinned:
            return {"status": "match", "persona": pinned,
                    "stacks": detected, "source": "pin",
                    "path": root}

    tags = set(normalize_stacks(detected))
    scored: list[tuple[int, dict]] = []
    if tags:
        items = personas if isinstance(personas, (list, tuple)) else (
            list(personas.values()) if isinstance(personas, dict) else [])
        for persona in items:
            if not isinstance(persona, dict):
                continue
            p_tags = set(normalize_stacks(persona.get("stacks") or []))
            score = len(tags & p_tags)
            if score > 0:
                scored.append((score, persona))

    if not scored:
        return {"status": "none", "stacks": detected, "path": root}

    scored.sort(key=lambda pair: (-pair[0],
                                  str(pair[1].get("name") or "").casefold()))
    best = scored[0][0]
    tied = [persona for score, persona in scored if score == best]
    required = min(MIN_SCORE, len(tags)) if tags else 1
    if best >= required and len(tied) == 1:
        return {"status": "match", "persona": tied[0], "stacks": detected,
                "score": best, "path": root}

    return {"status": "ambiguous", "candidates": tied,
            "stacks": detected, "score": best, "path": root}


def link(path, persona_id) -> dict:
    """Pin ``path`` to a persona (a pin outranks automatic detection)."""
    pid = str(persona_id or "").strip()
    if not pid:
        raise ValueError("persona obrigatória para vincular o projeto")
    root = _project_root(path)
    record = {
        "name": _base_name(root),
        "stacks": detect(root),
        "persona": pid,
        "source": "pin",
        "detected_at": _stamp(),
    }
    data = load()
    data["projects"][root] = record
    save(data)
    return {"path": root, **record}
