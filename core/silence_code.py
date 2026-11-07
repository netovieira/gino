"""Palavra-código de silêncio — UC-4 (ordem do dono, 2026-11-02 19:26-19:31).

O que faz
    Palavra-código (ex.: "Oklahoma") + nome de projeto silencia a boca para
    aquele projeto. O microfone continua aberto, a memória continua gravando
    e o que a boca diria é retido em fila. Ao liberar ("libera Oklahoma"),
    a boca entrega um resumo de tudo que ficou calado.

Diferença para SilenceGate (core/silence_gate.py)
    SilenceGate: palavra fixa "silêncio", alternância (toggle), descarta
    a fila ao liberar sem produzir resumo falado.
    SilenceCode: palavra configurável, ativa/desativa por projeto, retém
    e entrega resumo falado de tudo que ficou calado.

    Ambos usam a MESMA fila de retenção (memory/silence_queue.json) para
    que nada se perca: quando SilenceCode está ativo, o retain() grava
    na fila do gate; quando o gate está ativo, o retain() dele continua
    valendo. Os dois nunca silenciam a mesma fala duas vezes.

Estado persistido (memory/silence_code.json)
    {
      "version": 1,
      "code_word": "Oklahoma",
      "active_projects": [{"slug": "therotv", "label": "TheroTV"}],
      "started_at": "2026-11-06T18:00:00",
      "updated_at": "2026-11-06T18:00:00"
    }

Lei (c) do contrato (FLUXOS.md): só a voz do dono autoriza ativação/liberação.
O main.py trata disso; este módulo não faz verificação de voz — expõe o hook.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from core.memory_index import slugify

BASE = Path(__file__).resolve().parents[1] / "memory"
DEFAULT_STATE_PATH = BASE / "silence_code.json"
DEFAULT_MAX_RETAINED = 40
_MAX_SUMMARY_SUBJECTS = 8
_MAX_SNIPPET = 160

# Padrões de ativação e liberação (aplicados à transcrição em minúsculas
# sem acentos). O dono define a palavra-código em configure(); esta é a
# forma genérica de ler a ordem.
_ACTIVATE_RE_TEMPLATE = r"^(?:{cw})\s+(.+?)$"
_DEACTIVATE_RE_TEMPLATE = r"^libera\s+(?:{cw})\b\s*(.*)$"
_DEACTIVATE_ALL_RE_TEMPLATE = r"^libera\s+(?:{cw})\s*$"


def _fold(text: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "")).lower()
    return "".join(c for c in raw if not unicodedata.combining(c))


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


# ── parser de ordem ──────────────────────────────────────────────────────────

def parse_silence_code_order(
    text: str,
    code_word: str,
) -> dict:
    """Lê a transcrição e decide se ativa ou libera o silêncio por palavra-código.

    Retorna ``{"action": "activate"|"deactivate"|"deactivate_all"|"none",
    "project": str, "slug": str}``. Nunca lança.

    A palavra-código é comparada em lowercase sem acentos. O projeto é o
    que vem depois dela na ativação; na liberação pode vir vazio (todos)
    ou um projeto específico.
    """
    raw = " ".join(str(text or "").split()).strip()
    out = {"action": "none", "project": "", "slug": ""}
    if not raw or not code_word:
        return out
    cw_fold = _fold(code_word).strip()
    n = _fold(raw)
    # Liberação total: "libera Oklahoma"
    try:
        if re.match(_DEACTIVATE_ALL_RE_TEMPLATE.format(cw=re.escape(cw_fold)), n):
            out.update({"action": "deactivate_all", "project": "", "slug": ""})
            return out
    except re.error:
        pass
    # Liberação de projeto: "libera Oklahoma TheroTV"
    try:
        m = re.match(_DEACTIVATE_RE_TEMPLATE.format(cw=re.escape(cw_fold)), n)
        if m:
            proj = " ".join(raw.split())  # preserve original case
            rest_raw = m.group(1).strip()
            if rest_raw:
                label = rest_raw[:60]
                out.update({"action": "deactivate", "project": label,
                            "slug": slugify(label, "")})
            else:
                out.update({"action": "deactivate_all", "project": "", "slug": ""})
            return out
    except re.error:
        pass
    # Ativação: "Oklahoma TheroTV"
    try:
        m = re.match(_ACTIVATE_RE_TEMPLATE.format(cw=re.escape(cw_fold)), n)
        if m:
            rest_folded = m.group(1).strip()
            # Recupera o rótulo com o caso original
            idx = raw.lower().find(cw_fold)
            if idx >= 0:
                label = raw[idx + len(cw_fold):].strip()[:60]
            else:
                label = rest_folded[:60]
            if label:
                out.update({"action": "activate", "project": label,
                            "slug": slugify(label, "")})
            return out
    except re.error:
        pass
    return out


# ── resumo ───────────────────────────────────────────────────────────────────

def _first_sentence(text: str) -> str:
    raw = " ".join(str(text or "").split())
    if not raw:
        return ""
    m = re.match(r"(.+?[.!?…])(?:\s|$)", raw)
    snippet = m.group(1) if m else raw
    if len(snippet) > _MAX_SNIPPET:
        snippet = snippet[:_MAX_SNIPPET].rstrip() + "…"
    return snippet


def summarize(entries: list[dict]) -> list[dict]:
    """Agrupa linhas retidas por assunto: contagem + última frase.

    Retorna uma lista de grupos ``{"subject", "count", "latest"}``.
    """
    groups: dict[str, dict] = {}
    order: list[str] = []
    for entry in entries or []:
        slug = str(entry.get("slug") or "")
        key = slug or "-"
        g = groups.get(key)
        if g is None:
            g = {
                "slug": slug,
                "subject": str(
                    entry.get("subject") or entry.get("label")
                    or "sem assunto"),
                "count": 0,
                "latest": "",
            }
            groups[key] = g
            order.append(key)
        g["count"] += 1
        g["latest"] = str(entry.get("text") or g["latest"])
    return [groups[k] for k in order]


def summary_lines(entries: list[dict], dropped: int = 0,
                  max_subjects: int = _MAX_SUMMARY_SUBJECTS) -> list[str]:
    """Linhas curtas de resumo para a boca falar na liberação."""
    groups = summarize(entries)
    lines: list[str] = []
    for g in groups[:max(1, int(max_subjects))]:
        count = int(g.get("count") or 0)
        plural = "s" if count > 1 else ""
        line = f"Sobre {g['subject']}: {count} fala{plural} guardada{plural}."
        latest = _first_sentence(g.get("latest") or "")
        if latest:
            line += f" A última: {latest}"
        lines.append(line)
    extra = len(groups) - max(1, int(max_subjects))
    if extra > 0:
        lines.append(
            f"Mais {extra} assunto{'s' if extra > 1 else ''} "
            "também teve fala guardada.")
    if int(dropped or 0) > 0:
        lines.append(
            f"Descartei {int(dropped)} fala"
            f"{'s' if int(dropped) > 1 else ''} antiga"
            f"{'s' if int(dropped) > 1 else ''} por limite de espaço.")
    return lines


# ── módulo principal ─────────────────────────────────────────────────────────

class SilenceCode:
    """Palavra-código de silêncio com retenção e resumo.

    ``code_word``  : a palavra definida pelo dono (ex.: "Oklahoma").
    ``on_retain``  : callback para reter fala no SilenceGate (fila compartilhada).
    ``logger``     : console/output; nunca obrigatório.

    Estado ativo (memória + disco):
        ``active_projects`` : lista de {"slug": str, "label": str}.
    """

    def __init__(
        self,
        code_word: str = "",
        *,
        state_path: str | Path | None = None,
        on_retain: Callable[[str, str, str], None] | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._code_word: str = code_word
        self._state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self._on_retain = on_retain or (lambda t, s, sl: None)
        self._log_fn = logger or (lambda _m: None)
        self._active_projects: list[dict] = []
        self._started_at: str = ""
        self._dropped: int = 0
        self._local_entries: list[dict] = []  # when no on_retain wired
        self._load()

    # ── propriedade ─────────────────────────────────────────────────────────

    @property
    def code_word(self) -> str:
        with self._lock:
            return self._code_word

    @code_word.setter
    def code_word(self, value: str) -> None:
        with self._lock:
            self._code_word = " ".join(str(value or "").split())[:40]

    @property
    def active(self) -> bool:
        """True quando pelo menos um projeto está sob palavra-código."""
        with self._lock:
            return bool(self._active_projects)

    # ── consultas ───────────────────────────────────────────────────────────

    def is_active_for(self, project: str = "", text: str = "") -> bool:
        """A boca deve ficar calada para este projeto/texto agora?

        Retorna True se o projeto está na lista de ativos. Se ``project`` vazio,
        tenta inferir de ``text`` pelos slugs ativos.
        """
        with self._lock:
            if not self._active_projects:
                return False
            subj_slug = slugify(str(project or ""), "") if project else ""
            for item in self._active_projects:
                if subj_slug and subj_slug == item["slug"]:
                    return True
                if text and item["slug"] and item["slug"] in _fold(text):
                    return True
            # Se "todos" está ativo (slug vazio), qualquer fala é retida.
            for item in self._active_projects:
                if not item["slug"]:
                    return True
            return False

    def active_projects(self) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self._active_projects]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "code_word": self._code_word,
                "active_projects": [dict(i) for i in self._active_projects],
                "started_at": self._started_at,
            }

    # ── ativação / desativação ───────────────────────────────────────────────

    def activate(self, project: str = "") -> dict:
        """Silencia a boca para o projeto. Retorna o registro do projeto.

        ``project`` vazio = silencia tudo (slug "" = projeto global).
        """
        label = " ".join(str(project or "").split())[:60]
        slug = slugify(label, "") if label else ""
        with self._lock:
            if slug and any(i["slug"] == slug for i in self._active_projects):
                # já ativo: idempotente
                return {"action": "already_active", "project": label, "slug": slug}
            if not slug and any(not i["slug"] for i in self._active_projects):
                return {"action": "already_active", "project": "tudo", "slug": ""}
            if not self._active_projects:
                self._started_at = _now_iso()
            item = {"slug": slug, "label": label or "tudo"}
            self._active_projects.append(item)
            self._save_locked()
        self._log(f"[PalavraCodigo] silêncio ativado: {label or 'tudo'}")
        return {"action": "activate", "project": label, "slug": slug}

    def deactivate(self, project_slug: str | None = None) -> dict:
        """Libera o silêncio (um projeto, ou todos se slug=None).

        Retorna {"action": ..., "entries": [...], "dropped": int,
        "summary_lines": [str]}.  A fila local é zerada ao liberar.
        """
        with self._lock:
            if project_slug is None or project_slug == "":
                # libera tudo
                self._active_projects = []
                action = "deactivate_all"
            else:
                before = len(self._active_projects)
                self._active_projects = [
                    i for i in self._active_projects if i["slug"] != project_slug
                ]
                action = ("deactivate"
                          if len(self._active_projects) < before
                          else "not_active")
            entries = list(self._local_entries)
            self._local_entries = []
            dropped = self._dropped
            self._dropped = 0
            if not self._active_projects:
                self._started_at = ""
            self._save_locked()
        lines = summary_lines(entries, dropped)
        self._log(f"[PalavraCodigo] silêncio liberado ({action}): "
                  f"{len(entries)} linhas retidas")
        return {"action": action, "entries": entries,
                "dropped": dropped, "summary_lines": lines}

    # ── retenção ─────────────────────────────────────────────────────────────

    def retain(self, text: str, *, subject: str = "",
               slug: str = "") -> dict:
        """Retém o que a boca diria. Delega ao on_retain se configurado.

        Retorna o registro da entrada local (com id e timestamp).
        """
        text_clean = str(text or "").strip()
        if not text_clean:
            return {}
        subj = " ".join(str(subject or "").split())[:60]
        entry_slug = slug or (slugify(subj, "") if subj else "")
        entry: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "text": text_clean,
            "subject": subj or "sem assunto",
            "slug": entry_slug,
            "at": _now_iso(),
            "origin": "silence_code",
        }
        with self._lock:
            self._local_entries.append(entry)
            while len(self._local_entries) > DEFAULT_MAX_RETAINED:
                self._local_entries.pop(0)
                self._dropped += 1
        # Delega ao callback compartilhado (fila do SilenceGate)
        try:
            self._on_retain(text_clean, subj, entry_slug)
        except Exception as exc:
            self._log(f"[PalavraCodigo] on_retain falhou: {exc}")
        return dict(entry)

    def pending_entries(self) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self._local_entries]

    # ── processar fala do dono (ativação/liberação por transcrição) ──────────

    def handle_transcription(self, text: str) -> dict | None:
        """Detecta ordem de palavra-código na transcrição e age.

        Retorna o resultado de activate/deactivate, ou None se a fala
        não é ordem de palavra-código.
        """
        if not self._code_word:
            return None
        order = parse_silence_code_order(text, self._code_word)
        action = order.get("action", "none")
        if action == "none":
            return None
        if action == "activate":
            return self.activate(order.get("project", ""))
        if action == "deactivate":
            return self.deactivate(order.get("slug", ""))
        if action == "deactivate_all":
            return self.deactivate(None)
        return None

    # ── persistência ─────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        with self._lock:
            cw = str(data.get("code_word") or "")
            if cw:
                self._code_word = cw
            items = data.get("active_projects") or []
            self._active_projects = [
                {"slug": str(i.get("slug") or ""),
                 "label": str(i.get("label") or i.get("slug") or "tudo")}
                for i in items if isinstance(i, dict)
            ]
            self._started_at = str(data.get("started_at") or "")

    def _save_locked(self) -> None:
        payload = json.dumps({
            "version": 1,
            "code_word": self._code_word,
            "active_projects": list(self._active_projects),
            "started_at": self._started_at,
            "updated_at": _now_iso(),
        }, ensure_ascii=False)
        try:
            _atomic_write(self._state_path, payload)
        except Exception as exc:
            self._log(f"[PalavraCodigo] nao consegui salvar estado: {exc}")

    def _log(self, msg: str) -> None:
        try:
            self._log_fn(msg)
        except Exception:
            pass
