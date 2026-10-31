"""One continuous conversation: a journal that survives trimming and restarts.

WHY THIS EXISTS
    The economy brain keeps a short local history and trims the oldest exchanges
    to stay inside the provider's token budget. Silent trimming is how an
    assistant "forgets" the subject mid-conversation and starts mixing threads.
    The Live session has its own resumption, but that dies with the process.

    This module is the assistant's own notebook for the SAME conversation:
    day rollover, closed subjects with a time, short notes for later, and a
    verbatim snippet of whatever was trimmed out. It is data, not a script: the
    prompt tells the model to treat it as memory and never invent past it.

STORAGE
    memory/continuity.json (local, git-ignored). Small, bounded lists only; it
    is meant to be read whole, not searched.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PATH = BASE_DIR / "memory" / "continuity.json"

_LOCK = threading.RLock()
_MAX_NOTES = 60
_MAX_TOPICS = 16
_MAX_ARCHIVE = 40
_MAX_SESSIONS = 6
_MAX_SUMMARY = 320
_MAX_ARCHIVE_TEXT = 2400

_WEEKDAY_PT = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
               "sexta-feira", "sábado", "domingo")


def _now() -> datetime:
    return datetime.now()


def _stamp(dt: datetime | None = None) -> str:
    return (dt or _now()).strftime("%Y-%m-%d %H:%M")


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


def _empty() -> dict:
    return {
        "day": _today(),
        "updated": _stamp(),
        "topic": "",
        "topics": [],
        "notes": [],
        "archive": [],
        "sessions": [],
    }


def _load() -> dict:
    with _LOCK:
        if not PATH.exists():
            return _empty()
        try:
            data = json.loads(PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[Continuity] load failed: {e}")
            return _empty()
        if not isinstance(data, dict):
            return _empty()
        base = _empty()
        for key in base:
            if key not in data:
                data[key] = base[key]
        return data


def _save(data: dict) -> None:
    with _LOCK:
        data["updated"] = _stamp()
        try:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(PATH)
        except Exception as e:
            print(f"[Continuity] save failed: {e}")


def _trim(data: dict) -> None:
    data["notes"] = data["notes"][-_MAX_NOTES:]
    data["archive"] = data["archive"][-_MAX_ARCHIVE:]
    data["sessions"] = data["sessions"][-_MAX_SESSIONS:]
    data["topics"] = data["topics"][-_MAX_TOPICS:]


def _note(data: dict, kind: str, text: str) -> None:
    text = " ".join(str(text or "").split()).strip()
    if not text:
        return
    data["notes"].append({"at": _stamp(), "kind": kind, "text": text[:400]})


def check_day() -> None:
    """Record a day rollover once, so a resumed conversation knows the date
    changed instead of assuming it is still yesterday evening."""
    data = _load()
    today = _today()
    if data.get("day") == today:
        return
    data["day"] = today
    _note(data, "dia", f"virou o dia — {_now():%d/%m/%Y}")
    _trim(data)
    _save(data)


def note(text: str, kind: str = "nota") -> str:
    text = str(text or "").strip()
    if not text:
        return "Nota vazia."
    data = _load()
    _note(data, kind, text)
    _trim(data)
    _save(data)
    return f"Anotado: {text[:120]}"


def open_topic(name: str) -> str:
    name = " ".join(str(name or "").split()).strip()[:90]
    if not name:
        return "Diga qual é o assunto."
    data = _load()
    for t in data["topics"]:
        if str(t.get("name", "")).lower() == name.lower():
            t["state"] = "open"
            t["closed"] = ""
            break
    else:
        data["topics"].append({"name": name, "state": "open",
                               "opened": _stamp(), "closed": ""})
    data["topic"] = name
    _note(data, "assunto", f"assunto aberto: {name}")
    _trim(data)
    _save(data)
    return f"Assunto aberto: {name}"


def close_topic(name: str = "", summary: str = "") -> str:
    data = _load()
    target = (str(name or "").strip() or str(data.get("topic") or "").strip())
    if not target:
        return "Não há um assunto aberto para encerrar."
    ended = None
    for t in data["topics"]:
        if str(t.get("name", "")).lower() == target.lower():
            t["state"] = "closed"
            t["closed"] = _stamp()
            ended = t
            break
    if ended is None:
        data["topics"].append({"name": target, "state": "closed",
                               "opened": "", "closed": _stamp()})
    if str(data.get("topic") or "").lower() == target.lower():
        data["topic"] = ""
    extra = " ".join(str(summary or "").split()).strip()
    text = f"assunto encerrado — {target} ({_now():%H:%M})"
    if extra:
        text += f": {extra[:220]}"
    _note(data, "assunto", text)
    _trim(data)
    _save(data)
    return text


def archive(messages: list, assistant_name: str = "Assistente") -> None:
    """Keep the exact words of whatever history trimming is about to discard.

    A verbatim tail is safer than an invented summary: on a later resume the
    model can read what was actually said instead of reconstructing it.
    """
    lines: list[str] = []
    for m in messages if isinstance(messages, list) else []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(str(p.get("text") or "") for p in content
                               if isinstance(p, dict))
        text = " ".join(str(content or "").split()).strip()
        if not text:
            continue
        who = "Você" if role == "user" else assistant_name
        lines.append(f"{who}: {text[:220]}")
    if not lines:
        return
    body = "\n".join(lines)[-_MAX_ARCHIVE_TEXT:]
    data = _load()
    data["archive"].append({"at": _stamp(), "text": body})
    _trim(data)
    _save(data)


def record_session(summary: str) -> None:
    """One session ended: keep its summary with the date, newest last."""
    summary = " ".join(str(summary or "").split()).strip()
    if not summary:
        return
    data = _load()
    data["sessions"].append({"date": _today(), "summary": summary[:_MAX_SUMMARY]})
    _note(data, "sessao", summary[:_MAX_SUMMARY])
    _trim(data)
    _save(data)


def set_topic(name: str) -> str:
    name = " ".join(str(name or "").split()).strip()[:90]
    data = _load()
    data["topic"] = name
    _trim(data)
    _save(data)
    return data["topic"]


def recap() -> str:
    data = _load()
    lines: list[str] = []
    if data.get("sessions"):
        s = data["sessions"][-1]
        lines.append(f"Última sessão ({s.get('date', '')}): {s.get('summary', '')}")
    if data.get("topic"):
        lines.append(f"Assunto atual: {data['topic']}")
    open_topics = [t.get("name", "") for t in data.get("topics", [])
                   if t.get("state") == "open"]
    if open_topics:
        lines.append("Assuntos abertos: " + "; ".join(open_topics))
    notes = data.get("notes", [])[-4:]
    if notes:
        lines.append("Marcos: " + " | ".join(f"{n.get('at', '')[11:]} {n.get('text', '')}"
                                             for n in notes))
    return "\n".join(lines) or "Ainda não há registros de conversa."


def prompt_block() -> str:
    """The journal as it goes into the system prompt — bounded and factual."""
    check_day()
    data = _load()
    sessions = data.get("sessions", [])
    notes = data.get("notes", [])
    topics = data.get("topics", [])
    archive = data.get("archive", [])
    if not (sessions or notes or topics or archive or data.get("topic")):
        return ""

    now = _now()
    lines = ["[CONTINUIDADE — MESMA CONVERSA]",
             f"Hoje é {now:%d/%m/%Y} ({_WEEKDAY_PT[now.weekday()]}), "
             f"{now:%H:%M}. Este é o seu próprio caderno desta conversa; "
             "trate-o como memória, não como um roteiro."]
    if data.get("topic"):
        lines.append(f"Assunto atual: {data['topic']}")
    open_topics = [str(t.get("name", "")) for t in topics if t.get("state") == "open"]
    if open_topics:
        lines.append("Assuntos abertos: " + "; ".join(open_topics[:8]) + ".")
    if sessions:
        lines.append("Resumos das últimas sessões (mais recente por último):")
        for s in sessions[-2:]:
            lines.append(f"- {s.get('date', '')}: {str(s.get('summary', ''))[:240]}")
    recent_notes = notes[-6:]
    if recent_notes:
        lines.append("Marcos recentes:")
        for n in recent_notes:
            at = str(n.get("at") or "")
            lines.append(f"- {at[11:16] if len(at) >= 16 else at}: "
                         f"{str(n.get('text', ''))[:160]}")
    if not sessions and archive:
        tail = str(archive[-1].get("text") or "")[-700:]
        lines.append("Trecho arquivado da conversa (verbatim):")
        lines.append(tail)
    elif archive:
        # A long-running day trims turns after older session summaries exist:
        # keep the newest trimmed batch visible so the thread does not vanish
        # just because yesterday already has a summary.
        tail = str(archive[-1].get("text") or "")[-500:]
        lines.append("Trecho recente arquivado da conversa (verbatim):")
        lines.append(tail)
    lines.append("Use isto para retomar o fio. Não invente detalhes que não "
                 "estejam aqui ou na memória; se faltar algo, diga que não "
                 "lembra ou consulte a memória.")
    return "\n".join(lines)
