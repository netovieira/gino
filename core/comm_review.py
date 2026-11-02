"""Revisão periódica do Activity Log para melhorar a comunicação com o dono.

O QUE ESTA ROTINA FAZ
    A cada 6 horas lê somente o trecho novo de ``logs/activity.log`` (um
    offset em bytes fica em ``memory/comm_review_state.json``), conta o que
    aconteceu na conversa e procura sinais de atrito: pergunta do dono sem
    resposta, fala cortada por interrupção, resposta longa demais, falha de
    cérebro/conexão no meio do papo. O resumo vai em append para
    ``logs/comm-review.md``, com data e janela.

O QUE A LEITURA RESPEITA AGORA
    * Só falas do DONO no canal de voz abrem uma pergunta pendente. Linhas
      com ``[audio={"chan":"texto"}]`` (ou whatsapp/persona/tarefa) e falas
      marcadas ``[FALANTE: desconhecido]``/``[FALANTE: TV]`` são de outro
      canal/pessoa e não contam como pergunta abandonada.
    * Toda contagem bruta ganha uma taxa por hora da janela; uma janela longa
      com 5 perguntas não é comparável a uma curta com 5.
    * Tarefas concluídas em ``memory/tasks.json`` são cruzadas com as linhas
      de anúncio: conclusão sem fala em até 3 minutos conta como MUDA, com
      achado próprio e taxa por hora.
    * Cada relatório grava o início do processo e o commit/estado da árvore
      git. Os incidentes são separados em ``pré-restart`` e ``pós-restart``
      (timestamp da linha comparado ao start do processo), e a nota é dada
      para cada lado — um defeito já corrigido não assombra mais o número.
    * Latência real (pergunta → primeiro áudio) e as regras de humanização
      (>2 frases ou >35 palavras; primeira frase acima de 1,5 s) vêm dos
      campos ``latency_ms`` das linhas; sem amostras o relatório DIZ isso em
      vez de estimar.

COMO O DONO FICA SABENDO
    A análise é determinística — nenhum modelo local é chamado. Quando há
    algo útil a dizer, a rotina deixa uma pendência na fila do próprio app
    (``core.task_queue``, kind ``event``): o ``_task_watch`` do main.py já
    anuncia eventos em voz alta com ``_say_now`` e os mostra na fila. Se a
    fila não puder ser gravada agora, a pendência fica no state e é tentada
    de novo no próximo ciclo; nada é perdido e nada pede confirmação ao dono.

CADÊNCIA
    O primeiro start conta como uma janela de 6 h já vencida (lê só o fim do
    log, com teto, e filtra pelo timestamp); depois vence de 6 em 6 horas. O
    relógio fica no state, então reiniciar o app não antecipa nem atrasa a
    próxima revisão.

LIMITES
    O log nunca é apagado nem truncado. A leitura é somente leitura, com teto
    de ``MAX_READ_BYTES`` por ciclo para não varrer o mundo; se o arquivo for
    rotacionado/reduzido, o offset volta para o começo em vez de estourar.
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core import activity as _activity

BASE_DIR = Path(__file__).resolve().parent.parent
ACTIVITY_PATH = BASE_DIR / "logs" / "activity.log"
REPORT_PATH = BASE_DIR / "logs" / "comm-review.md"
STATE_PATH = BASE_DIR / "memory" / "comm_review_state.json"

INTERVAL_S = 6 * 60 * 60.0          # a cadência pedida pelo dono
POLL_S = 60.0                       # quanto o vigia dorme entre checagens
MAX_READ_BYTES = 512 * 1024         # teto de uma leitura; o resto fica para a próxima
LONG_REPLY_WORDS = 60
HUMAN_LONG_WORDS = 35
HUMAN_MAX_SENTENCES = 2
LATE_FIRST_MS = 1500
ANNOUNCE_GRACE_S = 180.0            # 3 min para a conclusão virar fala
UNANSWERED_GRACE_S = 120.0
MIN_INTERRUPTIONS = 3
_GIT_TTL_S = 120.0

_LINE_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+([^:]{1,40}):\s?(.*)$")
_SYSTEM_SOURCES = {"SYS", "YOU", "QODER"}
_FALANTE_RE = re.compile(r"^\[FALANTE:\s*([^\]]*)\]\s*(.*)$", re.S)
_NON_OWNER_NAMES = {"desconhecido", "unknown", "desconhecida", "?"}
_SERIES_NAME_RE = re.compile(r"\b(tv|s[eé]rie|s[eé]ries|v[ií]deo|filme|playback)\b")

_IMPORT_WALL = time.time()
_PROCESS_START: float | None = None
_git_cache: tuple[float, dict] | None = None

_lock = threading.RLock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_logger = None


def _always_ready() -> bool:
    return True


_ready = _always_ready          # a janela do dono (acordado e sem mudo)
_last_warning = ""


# ── caminhos e estado ───────────────────────────────────────────────────────

def _log(message: str) -> None:
    fn = _logger
    try:
        (fn or (lambda m: print(f"[CommReview] {m}")))(message)
    except Exception:
        pass


def _warn(message: str) -> None:
    """A mesma falta de arquivo não vira uma linha por minuto."""
    global _last_warning
    if message == _last_warning:
        return
    _last_warning = message
    _log(message)


def _load_state() -> dict | None:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:
        _warn(f"não consegui ler o estado da revisão: {e}")
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(STATE_PATH)
    except Exception as e:
        _warn(f"não consegui gravar o estado da revisão: {e}")


def _size() -> int:
    try:
        return int(ACTIVITY_PATH.stat().st_size)
    except FileNotFoundError:
        return 0
    except Exception as e:
        _warn(f"não consegui medir o Activity Log: {e}")
        return 0


# ── carimbo do processo e da árvore ─────────────────────────────────────────

def _detect_process_start() -> float:
    """Início real deste processo (psutil → /proc → import como último caso)."""
    try:
        import psutil
        return float(psutil.Process().create_time())
    except Exception:
        pass
    try:
        with open("/proc/self/stat", "rb") as f:
            ticks = int(f.read().split()[21])
        with open("/proc/stat", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("btime "):
                    btime = float(line.split()[1])
                    return btime + ticks / float(os.sysconf("SC_CLK_TCK"))
    except Exception:
        pass
    return _IMPORT_WALL


def _process_start() -> float:
    global _PROCESS_START
    if _PROCESS_START is None:
        _PROCESS_START = _detect_process_start()
    return _PROCESS_START


def _git_info() -> dict:
    """Commit atual e se a árvore tem mudança — tolerante a repo ausente."""
    global _git_cache
    now = time.time()
    if _git_cache is not None and now - _git_cache[0] < _GIT_TTL_S:
        return dict(_git_cache[1])
    info = {"commit": "", "branch": "", "dirty": None}
    try:
        def _git(*args: str) -> str:
            out = subprocess.run(
                ["git", *args], cwd=str(BASE_DIR), capture_output=True,
                text=True, timeout=3.0, check=False)
            return out.stdout.strip() if out.returncode == 0 else ""

        info["commit"] = _git("rev-parse", "HEAD")
        info["branch"] = _git("rev-parse", "--abbrev-ref", "HEAD")
        info["dirty"] = bool(_git("status", "--porcelain"))
    except Exception as e:
        _warn(f"não consegui ler o estado do git: {e}")
    _git_cache = (now, dict(info))
    return info


# ── leitura do log ──────────────────────────────────────────────────────────

def _read_slice(start: int) -> tuple[str, int, bool]:
    """Lê de ``start`` até o fim (com teto) e devolve texto, novo offset e
    se houve corte. Só avança até a última linha completa: uma escrita pela
    metade é relida no próximo ciclo, nunca perdida."""
    start = max(0, int(start))
    try:
        with ACTIVITY_PATH.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            cut = False
            if start > size:                 # log rotacionado/recriado
                start = 0
                cut = True
            if size - start > MAX_READ_BYTES:
                start = size - MAX_READ_BYTES
                cut = True
            if start:
                f.seek(start - 1)
                if f.read(1) != b"\n":
                    # o offset caiu no meio de uma linha; descarta o pedaço
                    line_end = f.readline()
                    start += len(line_end)
            f.seek(start)
            data = f.read()
    except FileNotFoundError:
        return "", start, False
    except Exception as e:
        _warn(f"não consegui ler o Activity Log: {e}")
        return "", start, False

    end = data.rfind(b"\n")
    if end < 0:
        return "", start, cut
    complete = data[:end + 1]
    return complete.decode("utf-8", "replace"), start + len(complete), cut


@dataclass(frozen=True)
class Row:
    ts: float
    source: str
    text: str
    meta: dict


def _parse(text: str) -> list[Row]:
    rows: list[Row] = []
    for line in text.splitlines():
        base, meta = _activity.split(line)
        m = _LINE_RE.match(base)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        rows.append(Row(ts, m.group(2).strip(), m.group(3).strip(), meta or {}))
    return rows


# ── análise sem modelo ──────────────────────────────────────────────────────

def _norm(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def _owner_text(text: str) -> str:
    m = _FALANTE_RE.match(str(text or ""))
    return (m.group(2) if m else str(text or "")).strip()


def _is_owner_voice(row: Row) -> bool:
    """Fala do dono NO CANAL DE VOZ — a única que abre pergunta pendente.

    ``chan`` presente e diferente de ``voz`` (texto/whatsapp/persona/tarefa)
    fica de fora. O prefixo ``[FALANTE:]`` é respeitado: desconhecido ou
    rótulo de série/vídeo não é o dono. Linhas antigas sem flags continuam
    contando (eram todas de voz), mas a separação pré/pós-restart mostra
    quando o número ainda é legado.
    """
    if row.source.upper() != "YOU":
        return False
    chan = _norm((row.meta or {}).get("chan"))
    if chan and chan != "voz":
        return False
    m = _FALANTE_RE.match(row.text or "")
    if m:
        name = _norm(m.group(1))
        if not name or name in _NON_OWNER_NAMES:
            return False
        if _SERIES_NAME_RE.search(name):
            return False
    return True


def _is_assistant_reply(row: Row) -> bool:
    """Fala do assistente (não evento de sistema, não WhatsApp, não YOU)."""
    src = row.source.upper()
    if src in _SYSTEM_SOURCES:
        return False
    if src.startswith("WHATSAPP"):
        return False
    return bool(row.text)


def _is_spoken_reply(row: Row) -> bool:
    """Resposta que foi para o alto-falante (para as regras de humanização)."""
    if not _is_assistant_reply(row):
        return False
    chan = _norm((row.meta or {}).get("chan"))
    if chan and chan != "voz":
        return False
    origin = _norm((row.meta or {}).get("origin"))
    return origin not in {"narração", "narracao", "pensando"}


def _words(text: str) -> int:
    return len(_owner_text(text).split())


def _sentences(text: str) -> int:
    parts = re.split(r"(?<=[.!?…])\s+", _owner_text(text))
    return len([p for p in parts if p.strip()])


def _unanswered(rows: list[Row], now: float) -> tuple[list[float], list[float]]:
    """(falas sem resposta, perguntas sem resposta) — só do dono no canal voz.

    Uma fala só conta como sem resposta quando outra fala dele veio depois, ou
    quando a janela fechou há mais de 2 minutos. Fala de série/outro canal é
    ignorada por inteiro: não abre nem fecha uma pendência do dono.
    """
    pending: tuple[float, str] | None = None
    turns: list[float] = []
    questions: list[float] = []
    for row in rows:
        if _is_owner_voice(row):
            if pending is not None:
                turns.append(pending[0])
                if "?" in pending[1]:
                    questions.append(pending[0])
            pending = (row.ts, _owner_text(row.text))
        elif _is_assistant_reply(row):
            pending = None
    if pending is not None and now - pending[0] >= UNANSWERED_GRACE_S:
        turns.append(pending[0])
        if "?" in pending[1]:
            questions.append(pending[0])
    return turns, questions


def _latency_samples(rows: list[Row]) -> list[tuple[float, int]]:
    """(timestamp da linha, latency_ms) — uma amostra por vez (campo turn)."""
    seen: set[object] = set()
    samples: list[tuple[float, int]] = []
    for row in rows:
        if not _is_spoken_reply(row):
            continue
        raw = (row.meta or {}).get("latency_ms")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        ms = int(raw)
        if ms < 0:
            continue
        key = (row.meta or {}).get("turn")
        if key is None:
            key = ("ts", round(row.ts, 3))
        if key in seen:
            continue
        seen.add(key)
        samples.append((row.ts, ms))
    return samples


def _iso_ts(value) -> float | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def _task_audit(rows: list[Row], now: float,
                window_start: float) -> tuple[list[float], list[float]]:
    """(conclusões na janela, conclusões mudas).

    Muda = tarefa ``done`` cujo ``finished`` já passou da carência de 3 min e
    nenhuma fala do assistente apareceu entre o fim e o fim+3 min. Tarefa que
    acabou de terminar ainda está dentro do prazo e não é julgada.
    """
    try:
        from core import task_queue
        tasks = task_queue.list_tasks("done")
    except Exception as e:
        _warn(f"não consegui ler as tarefas concluídas: {e}")
        return [], []
    completed: list[float] = []
    silent: list[float] = []
    for task in tasks:
        finished = _iso_ts(task.get("finished"))
        if finished is None:
            continue
        if finished < window_start - 1.0 or finished > now + 1.0:
            continue
        completed.append(finished)
        if now - finished < ANNOUNCE_GRACE_S:
            continue                         # ainda dentro dos 3 min
        announced = any(
            finished - 5.0 <= r.ts <= finished + ANNOUNCE_GRACE_S
            and _is_assistant_reply(r)
            for r in rows)
        if not announced:
            silent.append(finished)
    return completed, silent


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1,
                       math.ceil(fraction * len(ordered)) - 1))
    return int(ordered[index])


def _score(scope: dict) -> int:
    """Nota 0–100 a partir de taxas (nunca de contagem bruta)."""
    score = 100.0
    user = max(1, int(scope.get("user") or 0))
    assistant = max(1, int(scope.get("assistant") or 0))
    score -= min(30.0, 60.0 * int(scope.get("unanswered_questions") or 0) / user)
    score -= min(15.0, 20.0 * int(scope.get("interruptions") or 0) / assistant)
    score -= min(25.0, 40.0 * int(scope.get("human_long") or 0) / assistant)
    completed = int(scope.get("completed") or 0)
    if completed:
        score -= min(30.0, 40.0 * int(scope.get("silent") or 0) / completed)
    latency_n = max(1, int(scope.get("latency_samples") or 0))
    score -= min(15.0, 30.0 * int(scope.get("late_first") or 0) / latency_n)
    score -= min(20.0, 30.0 * int(scope.get("sanitized") or 0) / assistant)
    score -= min(10.0, 15.0 * int(scope.get("cut") or 0) / assistant)
    score -= min(10.0, 20.0 * int(scope.get("failures") or 0)
                 / (assistant + user))
    return int(max(0, min(100, round(score))))


def _scope_partition(owner_ts: list[float], assistant_ts: list[float],
                     spoken_ts: list[float], parts: dict[str, list[float]],
                     completed: list[float],
                     latencies: list[tuple[float, int]],
                     start: float, post: bool) -> dict:
    def pick(ts: float) -> bool:
        return ts >= start if post else ts < start

    scope = {
        "user": sum(1 for ts in owner_ts if pick(ts)),
        "assistant": sum(1 for ts in assistant_ts if pick(ts)),
        "spoken": sum(1 for ts in spoken_ts if pick(ts)),
        "completed": sum(1 for ts in completed if pick(ts)),
        "latency_samples": sum(1 for ts, _ms in latencies if pick(ts)),
    }
    for key, values in parts.items():
        scope[key] = sum(1 for ts in values if pick(ts))
    return scope


def _scope_text(values: list[float], start: float) -> str:
    pre = sum(1 for ts in values if ts < start)
    return f"pós-restart {len(values) - pre} · pré-restart {pre}"


def _rate(value: int, hours: float) -> str:
    return f"{value / max(hours, 1.0 / 60.0):.1f}/h"


def _analyse(rows: list[Row], now: float, *, window_start: float | None = None,
             process_start: float | None = None) -> dict:
    start = _process_start() if process_start is None else float(process_start)
    if window_start is None:
        window_start = rows[0].ts if rows else now
    hours = max(1.0 / 60.0, (now - window_start) / 3600.0)

    owner_rows = [r for r in rows if _is_owner_voice(r)]
    assistant_rows = [r for r in rows if _is_assistant_reply(r)]
    spoken_rows = [r for r in rows if _is_spoken_reply(r)]
    system = [r for r in rows if r.source.upper() == "SYS"]
    tools = [r for r in rows if r.source.upper() == "QODER"]

    unanswered_turns, unanswered_questions = _unanswered(rows, now)
    interruptions = [r.ts for r in system
                     if "interrompido por voz" in r.text.lower()]
    failures = [r.ts for r in rows
                if any(s in r.text for s in ("ERR:", "falhou", "indisponível",
                                             "não ficou pronto", "caiu ("))]
    long_replies = [r.ts for r in assistant_rows
                    if _words(r.text) >= LONG_REPLY_WORDS]
    human_long = [r.ts for r in spoken_rows
                  if (_words(r.text) > HUMAN_LONG_WORDS
                      or _sentences(r.text) > HUMAN_MAX_SENTENCES)]
    latencies = _latency_samples(rows)
    late_first = [ts for ts, ms in latencies if ms > LATE_FIRST_MS]
    completed, silent = _task_audit(rows, now, window_start)
    # Vazamento medido, não adivinhado: a própria esteira de fala carimba
    # `sanitized=` quando core.speech_guard troca ferramenta/caminho/JSON/
    # idioma antes do TTS, e `cut=` quando a fala morre no meio.
    sanitized_rows = [r for r in assistant_rows
                      if (r.meta or {}).get("sanitized")]
    cut_rows = [r for r in assistant_rows
                if (r.meta or {}).get("cut") is True]
    leak_kinds: dict[str, int] = {}
    for row in sanitized_rows:
        for name in str((row.meta or {}).get("sanitized") or "").split("+"):
            if name:
                leak_kinds[name] = leak_kinds.get(name, 0) + 1

    parts = {
        "unanswered_turns": unanswered_turns,
        "unanswered_questions": unanswered_questions,
        "interruptions": interruptions,
        "failures": failures,
        "long_replies": long_replies,
        "human_long": human_long,
        "late_first": late_first,
        "silent": silent,
        "sanitized": [r.ts for r in sanitized_rows],
        "cut": [r.ts for r in cut_rows],
    }
    owner_ts = [r.ts for r in owner_rows]
    assistant_ts = [r.ts for r in assistant_rows]
    spoken_ts = [r.ts for r in spoken_rows]
    full_scope = {key: len(value) for key, value in parts.items()}
    full_scope.update({
        "user": len(owner_rows),
        "assistant": len(assistant_rows),
        "spoken": len(spoken_rows),
        "completed": len(completed),
        "latency_samples": len(latencies),
    })
    pre_scope = _scope_partition(owner_ts, assistant_ts, spoken_ts, parts,
                                 completed, latencies, start, False)
    post_scope = _scope_partition(owner_ts, assistant_ts, spoken_ts, parts,
                                  completed, latencies, start, True)

    latency_values = [ms for _ts, ms in latencies]
    stats = {
        "lines": len(rows),
        "user": len(owner_rows),
        "assistant": len(assistant_rows),
        "spoken": len(spoken_rows),
        "system": len(system),
        "tools": len(tools),
        "window_hours": round(hours, 3),
        "interruptions": len(interruptions),
        "failures": len(failures),
        "long_replies": len(long_replies),
        "unanswered_turns": len(unanswered_turns),
        "unanswered_questions": len(unanswered_questions),
        "human_long_replies": len(human_long),
        "late_first_sentences": len(late_first),
        "completed_tasks": len(completed),
        "silent_completions": len(silent),
        "sanitized_speech": len(sanitized_rows),
        "cut_speech": len(cut_rows),
        "sanitized_kinds": dict(sorted(leak_kinds.items())),
        "latency_ms": {
            "samples": len(latency_values),
            "median": (int(statistics.median(latency_values))
                       if latency_values else None),
            "p90": _percentile(latency_values, 0.90),
            "max": (max(latency_values) if latency_values else None),
        },
        "per_hour": {
            "user": round(len(owner_rows) / hours, 2),
            "unanswered_questions": round(len(unanswered_questions) / hours, 2),
            "unanswered_turns": round(len(unanswered_turns) / hours, 2),
            "interruptions": round(len(interruptions) / hours, 2),
            "silent_completions": round(len(silent) / hours, 2),
            "sanitized": round(len(sanitized_rows) / hours, 2),
            "cut": round(len(cut_rows) / hours, 2),
        },
        "score": _score(full_scope),
        "score_pre": _score(pre_scope),
        "score_post": _score(post_scope),
        "restart": {"at": start, "pre": pre_scope, "post": post_scope},
    }

    findings: list[dict] = []
    if owner_rows and not assistant_rows:
        findings.append({
            "weight": 100,
            "detail": "o dono falou e nenhuma resposta do assistente foi registrada",
            "suggestion": "confirmar o recebimento ou avisar em uma frase que está "
                          "processando antes de qualquer silêncio longo",
        })
    if unanswered_questions:
        findings.append({
            "weight": 90,
            "detail": (f"{len(unanswered_questions)} pergunta(s) do dono no canal "
                       f"de voz ficaram sem resposta "
                       f"({_rate(len(unanswered_questions), hours)}; "
                       f"{_scope_text(unanswered_questions, start)})"),
            "suggestion": "responder a pergunta antes de mudar de assunto; se não "
                          "souber, dizer em uma frase que está verificando",
        })
    if silent:
        findings.append({
            "weight": 95,
            "detail": (f"{len(silent)} de {len(completed)} tarefas concluídas "
                       f"ficaram mudas — sem anúncio em até 3 min "
                       f"({_rate(len(silent), hours)}; "
                       f"{_scope_text(silent, start)})"),
            "suggestion": "anunciar a conclusão em uma frase assim que a tarefa "
                          "terminar, mesmo quando o resultado for longo; silêncio "
                          "parece falha",
        })
    if len(interruptions) >= MIN_INTERRUPTIONS:
        findings.append({
            "weight": 70,
            "detail": (f"{len(interruptions)} falas foram cortadas por interrupção "
                       f"de voz ({_rate(len(interruptions), hours)}; "
                       f"{_scope_text(interruptions, start)})"),
            "suggestion": "encurtar as respostas: uma ideia por frase, pausa no fim "
                          "e retomada do fio sem repetir o que já foi dito",
        })
    if len(human_long) >= 2:
        findings.append({
            "weight": 65,
            "detail": (f"{len(human_long)} respostas passaram de "
                       f"{HUMAN_MAX_SENTENCES} frases ou {HUMAN_LONG_WORDS} palavras "
                       f"({_rate(len(human_long), hours)}; "
                       f"{_scope_text(human_long, start)})"),
            "suggestion": "responder em uma ou duas frases e oferecer o detalhe "
                          "em vez de narrar tudo de uma vez",
        })
    if len(long_replies) >= 2:
        findings.append({
            "weight": 60,
            "detail": (f"{len(long_replies)} respostas passaram de "
                       f"{LONG_REPLY_WORDS} palavras "
                       f"({_scope_text(long_replies, start)})"),
            "suggestion": "resumir em uma a três frases e oferecer o detalhe em vez "
                          "de narrar tudo de uma vez",
        })
    if len(late_first) >= 2:
        median = stats["latency_ms"]["median"]
        findings.append({
            "weight": 60,
            "detail": (f"{len(late_first)} primeiras frases demoraram mais de "
                       f"{LATE_FIRST_MS / 1000:.1f}s"
                       + (f" (mediana {median / 1000:.1f}s)"
                          if median is not None else "")
                       + f"; {_scope_text(late_first, start)}"),
            "suggestion": "começar a falar uma frase curta de recebimento enquanto "
                          "a resposta completa é preparada",
        })
    if len(sanitized_rows) >= 2:
        kinds = ", ".join(f"{name} {count}"
                          for name, count in stats["sanitized_kinds"].items())
        findings.append({
            "weight": 75,
            "detail": (f"{len(sanitized_rows)} falas vazaram jargão de ferramenta "
                       f"e precisaram de sanitização antes do TTS "
                       f"({kinds}; {_rate(len(sanitized_rows), hours)}; "
                       f"{_scope_text(parts['sanitized'], start)})"),
            "suggestion": "dizer a intenção humana em vez do nome da ferramenta, "
                          "do caminho de arquivo, do trecho em inglês ou do JSON",
        })
    if len(cut_rows) >= 2:
        findings.append({
            "weight": 65,
            "detail": (f"{len(cut_rows)} respostas foram cortadas no meio e só "
                       f"tiveram a conclusão reanunciada no turno seguinte "
                       f"({_rate(len(cut_rows), hours)}; "
                       f"{_scope_text(parts['cut'], start)})"),
            "suggestion": "encurtar a resposta e anunciar a conclusão em uma frase "
                          "antes de continuar, para o corte não apagar o essencial",
        })
    if len(failures) >= 2:
        findings.append({
            "weight": 50,
            "detail": (f"{len(failures)} falhas de cérebro/conexão apareceram na "
                       f"conversa ({_scope_text(failures, start)})"),
            "suggestion": "anunciar a falha em uma frase e guardar a fala; não sumir "
                          "no meio do papo",
        })
    findings.sort(key=lambda f: -int(f["weight"]))
    return {"stats": stats, "findings": findings[:3], "silent_ts": silent,
            "incident_ts": parts}


# ── relatório e pendência ───────────────────────────────────────────────────

def _stamp(ts: float, seconds: bool = False) -> str:
    fmt = "%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M"
    try:
        return datetime.fromtimestamp(float(ts)).strftime(fmt)
    except (OSError, OverflowError, ValueError):
        return "?"


def _tree_state(dirty) -> str:
    if dirty is True:
        return "árvore suja"
    if dirty is False:
        return "árvore limpa"
    return "estado da árvore desconhecido"


def _incident_total(parts: dict, start: float, post: bool) -> int:
    """Incidentes distintos de um lado do restart (pergunta não conta 2x)."""
    seen: set[float] = set()
    for values in parts.values():
        for ts in values:
            if (ts >= start) if post else (ts < start):
                seen.add(round(float(ts), 3))
    return len(seen)


def _write_report(now: float, since: float, result: dict) -> bool:
    """Acrescenta a seção ao relatório; False mantém a janela para retry."""
    stats = result["stats"]
    findings = result["findings"]
    start = stats["restart"]["at"]
    git = _git_info()
    commit = str(git.get("commit") or "")[:12] or "sem commit"
    branch = str(git.get("branch") or "")
    hours = float(stats["window_hours"])
    incident_parts = result.get("incident_ts") or {}
    lines = [
        f"## {_stamp(now)} — janela {_stamp(since)}–{_stamp(now)}",
        "",
        (f"- Processo: pid {os.getpid()} iniciado {_stamp(start, True)}"
         + (f" ({branch})" if branch else "")
         + f" · commit {commit} ({_tree_state(git.get('dirty'))})"),
        (f"- Nota: {stats['score']}/100 (pós-restart {stats['score_post']}/100 · "
         f"pré-restart {stats['score_pre']}/100)"),
        (f"- Linhas: {stats['lines']} · falas do dono: {stats['user']} · "
         f"respostas do assistente: {stats['assistant']} · "
         f"eventos de sistema: {stats['system']} · ferramentas: {stats['tools']}"),
        (f"- Incidentes por processo: pós-restart "
         f"{_incident_total(incident_parts, start, True)} · pré-restart "
         f"{_incident_total(incident_parts, start, False)} "
         f"(corte em {_stamp(start, True)})"),
        (f"- Ritmo por hora: falas do dono {stats['per_hour']['user']:.1f}/h · "
         f"perguntas sem resposta "
         f"{stats['per_hour']['unanswered_questions']:.1f}/h · "
         f"interrupções {stats['per_hour']['interruptions']:.1f}/h"),
    ]
    if stats["completed_tasks"]:
        lines.append(
            f"- Tarefas concluídas: {stats['completed_tasks']} · mudas (sem "
            f"anúncio em até 3 min): {stats['silent_completions']} "
            f"({_rate(stats['silent_completions'], hours)}; "
            f"{_scope_text(result.get('silent_ts') or [], start)})")
    latency = stats["latency_ms"]
    if latency["samples"]:
        lines.append(
            f"- Latência pergunta→1º áudio: mediana "
            f"{latency['median'] / 1000:.1f}s · p90 {latency['p90'] / 1000:.1f}s · "
            f"máx {latency['max'] / 1000:.1f}s (n={latency['samples']})")
    else:
        lines.append("- Latência pergunta→1º áudio: sem amostras neste processo — "
                     "o campo latency_ms só existe nas linhas escritas após o "
                     "restart")
    if stats["interruptions"]:
        lines.append(f"- Interrupções de voz: {stats['interruptions']} "
                     f"({_rate(stats['interruptions'], hours)})")
    if stats["unanswered_questions"]:
        lines.append(f"- Perguntas sem resposta (canal de voz): "
                     f"{stats['unanswered_questions']} "
                     f"({_rate(stats['unanswered_questions'], hours)})")
    if stats["unanswered_turns"]:
        lines.append(f"- Falas do dono sem resposta: {stats['unanswered_turns']} "
                     f"({_rate(stats['unanswered_turns'], hours)})")
    if stats["long_replies"]:
        lines.append(f"- Respostas longas (>= {LONG_REPLY_WORDS} palavras): "
                     f"{stats['long_replies']}")
    if stats["human_long_replies"] or stats["late_first_sentences"]:
        lines.append(f"- Humanização: {stats['human_long_replies']} respostas acima "
                     f"de {HUMAN_MAX_SENTENCES} frases/{HUMAN_LONG_WORDS} palavras · "
                     f"{stats['late_first_sentences']} primeiras frases acima de "
                     f"{LATE_FIRST_MS / 1000:.1f}s")
    if stats["sanitized_speech"] or stats["cut_speech"]:
        kinds = ", ".join(f"{name} {count}"
                          for name, count in stats["sanitized_kinds"].items())
        lines.append(
            f"- Vazamentos na fala: {stats['sanitized_speech']} sanitizados"
            + (f" ({kinds})" if kinds else "")
            + f" · cortes no meio: {stats['cut_speech']} "
            f"({stats['per_hour']['sanitized']:.1f}/h)")
    if stats["failures"]:
        lines.append(f"- Falhas de cérebro/conexão: {stats['failures']}")
    lines.append("")
    if findings:
        lines.append("**Melhorias sugeridas**")
        for item in findings:
            lines.append(f"- {item['detail'][0].upper() + item['detail'][1:]}. "
                         f"Sugestão: {item['suggestion']}.")
    else:
        lines.append("Sem sinal claro de atrito na janela; nada a mudar por ora.")
    lines.append("")

    try:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        new = not REPORT_PATH.exists()
        with REPORT_PATH.open("a", encoding="utf-8") as f:
            if new:
                f.write("# Revisão de comunicação\n\n")
            f.write("\n".join(lines) + "\n")
        return True
    except Exception as e:
        _warn(f"não consegui escrever o relatório da revisão: {e}")
        return False


def _announcement(result: dict) -> str:
    top = result["findings"][0]
    return (f"Revisão de comunicação pronta: {top['suggestion']}; "
            f"o resumo está no relatório de comunicação")


def _enqueue_default(payload: dict) -> bool:
    """Deixa a pendência na fila do app; o _task_watch fala e risca sozinho."""
    try:
        from core import task_queue
    except Exception as e:
        _warn(f"fila de tarefas indisponível para a revisão: {e}")
        return False
    try:
        for row in task_queue.list_tasks("open"):
            if (row.get("payload") or {}).get("source") == "comm_review":
                return True          # já há uma revisão esperando a vez
        task_queue.add(
            title=payload["announcement"],
            kind="event",
            priority=0,
            payload={"source": "comm_review",
                     "report": str(REPORT_PATH),
                     "window": payload["window"],
                     "at": payload["at"]},
        )
        return True
    except Exception as e:
        _warn(f"não consegui enfileirar o anúncio da revisão: {e}")
        return False


def _try_pending(state: dict, enqueue, ready) -> None:
    payload = state.get("pending")
    if not isinstance(payload, dict) or not payload.get("announcement"):
        return
    if not _safe_ready(ready):
        return                     # segura até o dono estar na janela
    if enqueue(payload) is False:
        return
    state["pending"] = None
    _save_state(state)


def _safe_ready(ready) -> bool:
    try:
        return bool(ready())
    except Exception:
        return False


# ── ciclo público ───────────────────────────────────────────────────────────

def review_once(now: float | None = None, *, force: bool = False,
                enqueue=None, interval_s: float = INTERVAL_S,
                ready=None) -> dict | None:
    """Executa uma revisão se a janela de 6 h venceu; devolve o resultado.

    ``force`` ignora a cadência (teste/uso manual). ``enqueue`` substitui a
    fila real nos testes; devolver ``False`` mantém a pendência para a próxima
    tentativa. ``ready`` é a janela do dono: com False, o relatório é gravado
    mas o anúncio espera o próximo ciclo (acordado e sem mudo).
    """
    now = time.time() if now is None else float(now)
    interval = max(60.0, float(interval_s))
    enqueue = enqueue or _enqueue_default
    ready = ready or _ready

    with _lock:
        state = _load_state()
        if state is None:
            # Primeiro start: o estado nasce vencido, mas a leitura é limitada
            # ao teto e filtrada pela janela de 6 h — não relê o mundo.
            state = {
                "last_run": now - interval,
                "offset": max(0, _size() - MAX_READ_BYTES),
                "pending": None,
            }
            _save_state(state)

        _try_pending(state, enqueue, ready)
        try:
            last_run = float(state.get("last_run") or 0.0)
        except (TypeError, ValueError):
            last_run = now - interval
        if not force and now - last_run < interval:
            return None

        try:
            offset = int(state.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        text, new_offset, cut = _read_slice(offset)
        rows = [r for r in _parse(text) if r.ts >= last_run - 1.0]
        result = _analyse(rows, now, window_start=last_run)
        if cut and not text:
            _warn("o Activity Log encolheu; recomeçando o offset do zero.")
        if not _write_report(now, last_run, result):
            return None            # a janela não se perde; tenta no próximo poll

        state["last_run"] = now
        state["offset"] = new_offset
        state["window_start"] = last_run
        if result["findings"]:
            payload = {
                "announcement": _announcement(result),
                "window": f"{_stamp(last_run)}–{_stamp(now)}",
                "at": _stamp(now),
            }
            if not _safe_ready(ready):
                state["pending"] = payload      # espera o dono na janela
            elif enqueue(payload) is False:
                state["pending"] = payload
            else:
                state["pending"] = None
        _save_state(state)
        return result


# ── vigia em background ─────────────────────────────────────────────────────

def _loop(poll: float, interval: float, ready) -> None:
    while not _stop.is_set():
        try:
            review_once(interval_s=interval, ready=ready)
        except Exception as e:
            _warn(f"a revisão de comunicação falhou: {e}")
        _stop.wait(poll)


def start(logger=None, interval: float = INTERVAL_S,
          poll: float = POLL_S, ready=None) -> None:
    """Liga a revisão em background (uma única thread por processo)."""
    global _logger, _thread, _ready
    if logger is not None:
        _logger = logger
    if ready is not None:
        _ready = ready
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(
            target=_loop, args=(max(1.0, float(poll)),
                                max(60.0, float(interval)), _ready),
            daemon=True, name="jarvis-comm-review")
        _thread.start()


def stop() -> None:
    global _thread, _logger
    _stop.set()
    with _lock:
        thread, _thread = _thread, None
        _logger = None
    if thread is not None and thread.is_alive() \
            and thread is not threading.current_thread():
        thread.join(timeout=5)


def reset(activity_path=None, report_path=None, state_path=None,
          process_start: float | None = None) -> None:
    """Para a thread e esquece avisos; troca os caminhos quando informados.

    ``None`` mantém o caminho atual (os testes sempre passam os três).
    ``process_start`` permite fixar o corte pré/pós-restart nos testes; None
    volta a detectar o início real deste processo.
    """
    global _last_warning, _ready, _PROCESS_START, _git_cache
    stop()
    with _lock:
        if activity_path is not None:
            globals()["ACTIVITY_PATH"] = Path(activity_path)
        if report_path is not None:
            globals()["REPORT_PATH"] = Path(report_path)
        if state_path is not None:
            globals()["STATE_PATH"] = Path(state_path)
        _ready = _always_ready
        _PROCESS_START = process_start
        _git_cache = None
    _last_warning = ""
