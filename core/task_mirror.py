"""Espelho visível de uma execução pesada: log em arquivo + terminal tailing.

WHY THIS EXISTS
    O trabalho pesado roda em um subagente em segundo plano. Até agora ele era
    invisível: o dono só via o resultado final. Este módulo grava o que o
    executor faz em ``logs/mirror/<id>.log`` e abre um terminal visível com
    ``tail -F`` nesse arquivo, para o trabalho acontecer na frente dele.

DE ONDE VEM O TEXTO
    * Fila de tarefas: ``core.task_queue`` dá título e id da tarefa corrente.
    * Subagente: o Qoder grava o transcrito do executor em JSONL, ao vivo, em
      ``~/.qoder/projects/<projeto>/<sessão>/subagents/agent-*.jsonl``. O
      metadado ``task-<id>.json`` ao lado traz ``transcriptPath``. O sidecar
      anuncia ``task_started`` com o id; uma thread daqui segue esse JSONL e
      traduz cada ``tool_use``/resultado em uma linha legível (comando, arquivo,
      teste).
    * Fallback: os eventos ``task`` do sidecar (progresso/última ferramenta e
      fim) também entram no mesmo log, então o espelho continua útil se o
      formato interno do transcrito mudar.

LIMITES
    O caminho do transcrito é um detalhe interno do SDK. Quando ele não é
    encontrado, o espelho mostra só as linhas de progresso do sidecar. Nada aqui
    levanta para o turno de voz: falhar ao abrir a janela vira uma linha no log.

CICLO DE VIDA DA JANELA
    A janela nasce com a execução e morre com ela: o pid do terminal é anotado
    no nascimento e, em ``finish`` (sucesso, erro ou cancelamento), é terminado
    — o que leva o ``tail`` junto. O terminal é aberto com ``hold=False``, de
    modo que a aba fecha sozinha quando o tail sai. Se o dono fechou a janela
    antes, o pid já não existe e o fim não é erro. Um espelho compartilhado
    (live) só fecha quando a última execução que o usa termina.

STYLE
    No Qt, no main.py e sem threads de UI. Só arquivo, lock e ``Popen`` do
    terminal (via ``actions.terminal``, que já sabe abrir ptyxis).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MIRROR_DIR = BASE_DIR / "logs" / "mirror"

# Um único espelho para execuções nascidas na conversa; a fila tem um por
# tarefa (a fila serializa, então nunca há dois ao mesmo tempo).
LIVE_ID = "live"
LIVE_TITLE = "Execuções em segundo plano"

# Título da janela: "<nome configurado> — <tarefa daquela janela>". O limite é
# o mesmo que ``actions.terminal`` aplica ao argumento --title.
_WINDOW_TITLE_MAX = 80
_WINDOW_TITLE_MIN_TASK = 16    # a tarefa nunca some atrás de um nome comprido
_TASK_TITLE_FALLBACK = "execução em segundo plano"

_OPEN_COOLDOWN = 10.0          # evita uma janela por evento da mesma execução
_META_WAIT = 20.0              # espera o SDK criar o metadado do subagente
_TAIL_IDLE_STOP = 30 * 60.0    # uma thread órfã morre sozinha
_TAIL_MAX_LIFE = 6 * 60 * 60.0
_MAX_LINE = 900
_MAX_RESULT_LINES = 4
_RECENT_DEDUP = 6.0
_MAX_WINDOWS = 3               # pids lembrados por espelho (reaberturas)
_CLOSE_TERM_WAIT = 1.0         # ptyxis sai em milissegundos; isto é só o teto
_CLOSE_KILL_WAIT = 0.5

_lock = threading.RLock()
_entries: dict[str, dict] = {}
_aliases: dict[str, str] = {}
_last_active: str | None = None
# Mantém as threads vivas para o collector não as destruir no meio.
_tailers: list[threading.Thread] = []


# ── helpers ─────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _stamp() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M")


def _safe(value) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._-")
    return text[:80] or "execucao"


def _one_line(value, limit: int = _MAX_LINE) -> str:
    """Texto de log em uma linha: sem ANSI, sem NUL e truncado."""
    text = str(value or "").replace("\x1b", "")
    text = "".join(ch if ch in "\t\n" or ord(ch) >= 32 else " " for ch in text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    text = " · ".join(line for line in lines if line)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _path_for(ident: str) -> Path:
    return Path(MIRROR_DIR) / f"{_safe(ident)}.log"


def _resolve(ident) -> str:
    key = str(ident or "").strip()
    return _aliases.get(key, key)


def _queue_current() -> dict | None:
    try:
        from core import task_queue
        row = (task_queue.status() or {}).get("current")
        return row if isinstance(row, dict) and row else None
    except Exception:
        return None


def _current_target() -> tuple[str, str]:
    """A fila manda: se há tarefa rodando, o espelho é dela; senão, o live."""
    row = _queue_current()
    if row is not None:
        title = " ".join(str(row.get("title") or "").split()) or "tarefa da fila"
        return str(row.get("id") or LIVE_ID), title
    return LIVE_ID, LIVE_TITLE


def _assistant_name() -> str:
    """Nome configurado do assistente; renomear no painel renomeia a janela."""
    try:
        from memory.config_manager import get_assistant_name
        name = _one_line(get_assistant_name(), 60)
    except Exception:
        name = ""
    return name or "JARVIS"


def _is_generic_title(value, ident) -> bool:
    """Rótulo do espelho agregado ou o próprio id não identificam tarefa."""
    text = _one_line(value, 160)
    return not text or text == LIVE_TITLE or text == str(ident or "")


def _log_task_title(path) -> str:
    """Tarefa de um log em disco: cabeçalho ou último subagente anotado.

    Espelhos antigos do modo live têm o cabeçalho genérico; a linha
    ``▸ subagente: <título>`` é o que ainda nomeia o trabalho daquela janela.
    """
    try:
        lines = Path(str(path or "")).read_text(
            encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    for line in lines[:6]:
        if line.startswith("# tarefa:"):
            title = _one_line(line.split(":", 1)[1], 160)
            if title and title != LIVE_TITLE:
                return title
            break
    for line in reversed(lines[-400:]):
        marker = "▸ subagente:"
        if marker in line:
            title = line.split(marker, 1)[1]
            title = re.sub(r"\s*\([^()]*\)\s*$", "", title).strip()
            if title:
                return _one_line(title, 160)
    return ""


def _window_task_title(entry: dict) -> str:
    """Nome da tarefa daquela janela, nunca o rótulo genérico do live."""
    ident = str(entry.get("id") or "")
    title = _one_line(entry.get("title"), 160)
    if not _is_generic_title(title, ident):
        return title
    # O espelho live é a janela de um trabalho específico: o subagente
    # anotado no próprio log manda mais que a fila (que tem janela própria).
    from_log = _log_task_title(entry.get("path"))
    if ident == LIVE_ID and from_log:
        return from_log
    # Sem task_id: a tarefa da fila que está rodando é a dona da janela.
    row = _queue_current()
    if row is not None:
        queued = _one_line(row.get("title"), 160)
        if queued:
            return queued
    # Senão, o trabalho mais recente registrado no log (ou o último espelho
    # em disco ainda sem fim).
    if from_log:
        return from_log
    newest_id, newest_title = _newest_log()
    if newest_id and not _is_generic_title(newest_title, newest_id):
        return _one_line(newest_title, 160)
    return _TASK_TITLE_FALLBACK


def _clip_title(text, limit: int) -> str:
    """Uma linha com reticências, preservando o começo (o nome da tarefa)."""
    value = _one_line(text, 400)
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if limit == 1:
        return "…"
    return value[: limit - 1].rstrip() + "…"


def _window_title(entry: dict) -> str:
    """``"<nome configurado> — <tarefa>"`` dentro do limite do terminal."""
    name = _clip_title(_assistant_name(), 40) or "JARVIS"
    task = _window_task_title(entry)
    budget = _WINDOW_TITLE_MAX - len(name) - 3          # " — "
    if budget < _WINDOW_TITLE_MIN_TASK:
        # Nome configurado absurdamente longo: encurta o nome, não a tarefa.
        name = _clip_title(
            name, max(8, _WINDOW_TITLE_MAX - _WINDOW_TITLE_MIN_TASK - 3))
        budget = _WINDOW_TITLE_MAX - len(name) - 3
    return f"{name} — {_clip_title(task, max(1, budget))}"


# ── terminal ────────────────────────────────────────────────────────────────

def _open_window(entry: dict) -> tuple[bool, str]:
    """Abre um ptyxis com ``tail -F`` no log. Nunca levanta."""
    try:
        from actions import terminal
        command = f"tail -n +1 -F -- {_shell_quote(str(entry['path']))}"
        title = _window_title(entry)
        result = terminal.open_terminal({
            "command": command,
            "title": title,
            # hold=False: quando o tail morre, o terminal fecha a aba/janela
            # sozinho — a janela do espelho não sobrevive à tarefa.
            "hold": False,
        })
        ok = bool(result.get("ok"))
        if ok:
            # O comando é a marca desta janela: no fechamento, o /proc confirma
            # que o pid ainda é o ptyxis deste espelho (e não um pid reciclado).
            _remember_window(entry, result.get("pid"), title, command)
        return ok, str(result.get("message") or result.get("error") or "")
    except Exception as exc:  # noqa: BLE001 - a falha vira linha do log
        return False, str(exc)


def _shell_quote(text: str) -> str:
    try:
        import shlex
        return shlex.quote(text)
    except Exception:
        return "'" + text.replace("'", "'\\''") + "'"


def _should_open(entry: dict) -> bool:
    return (time.monotonic() - float(entry.get("opened_at") or 0.0)) > _OPEN_COOLDOWN


# ── fechar a janela do espelho ──────────────────────────────────────────────

def _remember_window(entry: dict, pid, title: str, marker: str = "") -> None:
    """Anota o pid da janela recém-aberta (a lista é compartilhada no snapshot)."""
    windows = entry.get("windows")
    if windows is None:
        windows = entry["windows"] = []
    try:
        pid = int(pid or 0)
    except Exception:
        pid = 0
    if pid and any(int(w.get("pid") or 0) == pid for w in windows):
        return
    windows.append({"pid": pid, "title": str(title or ""),
                    "marker": str(marker or "")})
    del windows[:-_MAX_WINDOWS]


def _pid_alive(pid: int) -> bool:
    """Vivo de verdade: zumbi (janela já morta, app ainda não colheu) não conta."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_bytes().split()
        if len(fields) > 2 and fields[2] in (b"Z", b"X"):
            return False
        return True
    except FileNotFoundError:
        return False
    except Exception:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except Exception:
        return True
    return True


def _pid_is_this_mirror(pid: int, marker: str) -> bool:
    """O pid ainda é a janela deste espelho? Protege contra pid reciclado."""
    needle = str(marker or "")
    if not needle:
        return True
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return False
    except Exception:
        return True
    if not raw:
        return True
    return needle in raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def _terminate_pid(pid, marker: str = "") -> bool:
    """Mata o processo da janela (e o ``tail`` junto). Já fechada = sucesso."""
    try:
        pid = int(pid or 0)
    except Exception:
        return False
    if pid <= 0 or not _pid_alive(pid):
        return True
    if not _pid_is_this_mirror(pid, marker):
        return True               # pid reciclado: aquela janela já morreu
    for sig, wait in ((signal.SIGTERM, _CLOSE_TERM_WAIT),
                      (signal.SIGKILL, _CLOSE_KILL_WAIT)):
        try:
            # O terminal é aberto com start_new_session: o pid é o grupo da
            # janela, então o sinal leva o agente do ptyxis e o tail embora.
            os.killpg(pid, sig)
        except ProcessLookupError:
            return True
        except Exception:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return True
            except Exception:
                return False
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                return True
            time.sleep(0.05)
    return not _pid_alive(pid)


def _close_by_title(title: str) -> bool:
    """Último recurso: fecha pelo título anotado no nascimento da janela.

    Nesta máquina não há wmctrl nem xdotool (Wayland); o caminho normal é o
    pid. Não existindo a ferramenta, devolve False sem fazer barulho.
    """
    text = str(title or "").strip()
    if not text:
        return False
    tools = (
        ("wmctrl", ["wmctrl", "-c", text]),
        ("xdotool", ["xdotool", "search", "--name", f"^{re.escape(text)}$",
                     "windowclose"]),
    )
    for binary, argv in tools:
        if not shutil.which(binary):
            continue
        try:
            done = subprocess.run(argv, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            continue
        if done.returncode == 0:
            return True
    return False


def _close_windows(entry: dict) -> int:
    """Fecha todas as janelas anotadas daquele espelho. Nunca levanta."""
    with _lock:
        # Limpa a lista NO LUGAR: um snapshot tirado no begin/show compartilha
        # este mesmo objeto e precisa enxergar a limpeza.
        windows = entry.get("windows")
        pending = list(windows or ())
        if windows is not None:
            windows.clear()
        log_path = str(entry.get("path") or "")
    closed = 0
    for window in pending:
        pid = int(window.get("pid") or 0)
        title = str(window.get("title") or "")
        marker = str(window.get("marker") or "") or log_path
        try:
            ok = _terminate_pid(pid, marker) if pid else False
        except Exception as exc:  # noqa: BLE001
            print(f"[TaskMirror] fechar pid {pid}: {exc}")
            ok = False
        if not ok:
            try:
                ok = _close_by_title(title)
            except Exception as exc:  # noqa: BLE001
                print(f"[TaskMirror] fechar \"{title}\": {exc}")
                ok = False
        if ok:
            closed += 1
    return closed


def _close_if_finished(key: str) -> None:
    """A tarefa terminou enquanto a janela nascia: a janela morre agora."""
    with _lock:
        entry = _entries.get(key)
        done = bool(entry and entry.get("done"))
    if not done or entry is None:
        return
    try:
        _close_windows(entry)
    except Exception as exc:  # noqa: BLE001
        print(f"[TaskMirror] fechar janela tardia ({key}): {exc}")


# ── entries ─────────────────────────────────────────────────────────────────

def _new_entry(ident: str, title: str, source: str) -> dict:
    path = _path_for(ident)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("# JARVIS · espelho de execução\n")
            fh.write(f"# tarefa: {_one_line(title, 160)}\n")
            fh.write(f"# id: {ident}\n")
            fh.write(f"# início: {_stamp()}\n\n")
    except Exception as exc:  # noqa: BLE001
        print(f"[TaskMirror] não criei {path}: {exc}")
    return {
        "id": ident,
        "title": _one_line(title, 160) or ident,
        "path": str(path),
        "source": source,
        "created": time.monotonic(),
        "updated": time.monotonic(),
        "opened_at": 0.0,
        "open_count": 0,
        "open_error": "",
        "done": False,
        "recent": deque(maxlen=80),
        "stop": threading.Event(),
        "windows": [],       # pids das janelas abertas para este espelho
        "active": set(),     # subagentes que ainda usam esta janela
    }


def _ensure(ident: str, title: str = "", source: str = "task") -> dict:
    """Chamar com o lock. Cria o log quando o id ainda não foi visto."""
    key = _resolve(ident) or ident
    entry = _entries.get(key)
    if entry is None:
        entry = _new_entry(key, title or key, source)
        _entries[key] = entry
    elif (key == LIVE_ID and source == "subagent" and entry.get("done")
          and title and _one_line(title, 160) != LIVE_TITLE):
        # Nova execução no espelho agregado: janela nova, nomeada pela tarefa
        # nova. Sem isso o live ficava "concluído" até reiniciar o app.
        entry = _new_entry(key, title, source)
        _entries[key] = entry
    elif title and _one_line(title, 160) and entry.get("done") is False:
        entry["title"] = _one_line(title, 160)
    return entry


def _attach_existing(ident: str, title: str = "", source: str = "disk") -> dict:
    """Registra um log que já existe em disco, sem reescrever o cabeçalho."""
    entry = {
        "id": ident,
        "title": _one_line(title, 160) or ident,
        "path": str(_path_for(ident)),
        "source": source,
        "created": 0.0,
        "updated": time.monotonic(),
        "opened_at": 0.0,
        "open_count": 0,
        "open_error": "",
        "done": True,
        "recent": deque(maxlen=80),
        "stop": threading.Event(),
        "windows": [],
        "active": set(),
    }
    entry["stop"].set()
    _entries[ident] = entry
    return entry


def begin(ident, title: str = "", source: str = "task",
          auto_open: bool = True) -> dict:
    """Registra a execução e, na primeira vez, abre o terminal visível."""
    global _last_active
    key = _resolve(ident) or str(ident or "")
    if not key:
        return {"ok": False, "message": "Sem id de execução para espelhar."}
    with _lock:
        entry = _ensure(key, title, source)
        _last_active = key
        should_open = bool(auto_open) and not entry["done"] and _should_open(entry)
        if should_open:
            # Marca antes de abrir: dois eventos simultâneos não abrem duas janelas.
            entry["opened_at"] = time.monotonic()
            entry["open_count"] += 1
        snapshot = dict(entry)
    if should_open:
        ok, message = _open_window(snapshot)
        with _lock:
            alive = _entries.get(key)
            if alive is not None:
                alive["open_error"] = "" if ok else message
                if ok:
                    alive["opened_at"] = time.monotonic()
                else:
                    # Falhou: um "show" explícito pode tentar de novo já.
                    alive["opened_at"] = 0.0
        if ok:
            # A tarefa pode ter terminado enquanto a janela nascia.
            _close_if_finished(key)
    return {"ok": True, "id": key, "title": snapshot.get("title", ""),
            "log": snapshot.get("path", ""), "opened": should_open}


def append(ident, text: str, prefix: str = "") -> None:
    """Acrescenta uma linha com horário ao log da execução (ou do alias)."""
    try:
        body = _one_line(text)
        if not body:
            return
        key = _resolve(ident)
        with _lock:
            entry = _entries.get(key)
            if entry is None:
                entry = _ensure(key, key, "task")
            key = entry["id"]
            line = f"[{_now()}] {prefix}{body}" if prefix else f"[{_now()}] {body}"
            recent = entry.get("recent")
            if recent is not None:
                now = time.monotonic()
                content = prefix + body
                if any(content == seen and now - when <= _RECENT_DEDUP
                       for seen, when in recent):
                    return
                recent.append((content, now))
            entry["updated"] = time.monotonic()
            path = Path(entry["path"])
            _last_active = key
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
    except Exception as exc:  # noqa: BLE001 - log não pode quebrar o turno
        print(f"[TaskMirror] append falhou ({ident}): {exc}")


def finish(ident, status: str = "done", summary: str = "") -> None:
    """Encerra o espelho: escreve o fim no log e fecha a janela visível.

    A janela morre com a tarefa (sucesso, erro ou cancelamento): o pid anotado
    no nascimento da janela é terminado, o que leva o ``tail`` junto e deixa o
    terminal fechar a aba sozinho (foi aberto com ``hold=False``).
    """
    key = _resolve(ident)
    with _lock:
        entry = _entries.get(key)
        if entry is None:
            return
        # Um espelho compartilhado (live) pode já servir outra execução: o fim
        # tardio de uma tarefa antiga não derruba a janela da tarefa nova.
        busy = bool(entry.get("active"))
        if not busy:
            entry["done"] = True
            entry["stop"].set()
    label = {"completed": "concluído", "done": "concluído",
             "failed": "falhou", "error": "falhou",
             "cancelled": "cancelado", "stopped": "parado"}.get(
                 str(status or "").strip().lower(), str(status or "fim"))
    append(key, f"── fim: {label} ──", prefix="")
    if summary:
        append(key, summary)
    if busy:
        return                      # o fim que importa é o da última execução
    try:
        _close_windows(entry)
    except Exception as exc:  # noqa: BLE001 - fechar janela não quebra o turno
        print(f"[TaskMirror] fechar janela ({key}): {exc}")


def link(ident, target) -> None:
    """Aponta um subagente para o log da execução que o contém."""
    source = str(ident or "").strip()
    dest = _resolve(target) or str(target or "").strip()
    if not source or not dest:
        return
    with _lock:
        _aliases[source] = dest


def _find_task_meta(task_id: str, session_id: str = "",
                    cwd: str | None = None,
                    stop: threading.Event | None = None) -> dict | None:
    """Localiza o ``task-<id>.json`` do SDK (metadado com transcriptPath)."""
    root = Path.home() / ".qoder" / "projects"
    candidates: list[Path] = []
    if session_id:
        slug = str(cwd or BASE_DIR).replace(os.sep, "-")
        candidates.append(root / slug / session_id / "subagents"
                          / f"task-{task_id}.json")
    deadline = time.monotonic() + _META_WAIT
    while time.monotonic() < deadline:
        for path in candidates:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
        # Sem sessão conhecida (ou caminho mudou): procura pelo id exato.
        try:
            for path in root.glob(f"*/*/subagents/task-{task_id}.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        return data
                except Exception:
                    continue
        except Exception:
            pass
        if stop is not None and stop.is_set():
            return None
        time.sleep(0.5)
    return None


# ── formatting ──────────────────────────────────────────────────────────────

def format_tool(name, args) -> str:
    """Uma linha legível para a ferramenta do executor."""
    tool = str(name or "").strip()
    data = args if isinstance(args, dict) else {}
    low = tool.lower()

    def pick(*keys):
        for key in keys:
            value = data.get(key)
            if value not in (None, "", [], {}):
                if isinstance(value, str):
                    return value
                try:
                    return json.dumps(value, ensure_ascii=False)
                except Exception:
                    return str(value)
        return ""

    if low in ("bash", "run", "shell", "execute"):
        return "$ " + (pick("command", "cmd", "script") or "comando")
    if low in ("edit", "multiedit", "write", "notebookedit", "applypatch",
               "apply_patch"):
        target = pick("file_path", "path", "file", "notebook_path")
        return f"editar {target}" if target else "editar arquivo"
    if low in ("read", "view", "notebookread"):
        target = pick("file_path", "path", "file", "notebook_path")
        extra = pick("offset")
        return f"ler {target}" + (f" (a partir de {extra})" if extra else "") \
            if target else "ler arquivo"
    if low in ("grep", "rg", "search"):
        pattern = pick("pattern", "query")
        target = pick("path", "glob")
        line = f"buscar {pattern}" if pattern else "procurar no projeto"
        return line + (f" em {target}" if target else "")
    if low == "glob":
        return "listar " + (pick("pattern", "glob") or "arquivos")
    if low in ("webfetch", "web_fetch"):
        return "abrir " + (pick("url") or "página")
    if low in ("websearch", "web_search"):
        return "pesquisar " + (pick("query") or "na web")
    if low in ("agent", "task"):
        return "subagente: " + (pick("description", "prompt") or "delegado")
    if low in ("todowrite", "todo", "todoread"):
        return "organizar o plano de trabalho"
    detail = pick("description", "command", "path", "file_path", "query")
    return f"{tool or 'ferramenta'}" + (f": {detail}" if detail else "")


def _result_lines(text) -> list[str]:
    value = str(text or "").strip()
    if not value:
        return []
    lines = [line for line in value.splitlines() if line.strip()]
    clipped = lines[:_MAX_RESULT_LINES]
    out = [f"  │ {_one_line(line, 240)}" for line in clipped]
    if len(lines) > len(clipped):
        out.append(f"  │ … ({len(lines) - len(clipped)} linha(s))")
    return out


def _format_transcript_line(raw: str) -> list[str]:
    """Traduz uma linha do JSONL do subagente em linhas legíveis."""
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    kind = str(data.get("type") or "")
    message = data.get("message") if isinstance(data.get("message"), dict) else {}

    if kind == "assistant":
        lines: list[str] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                lines.append(format_tool(block.get("name"), block.get("input")))
            elif block.get("type") == "text":
                text = _one_line(block.get("text"), 500)
                if text:
                    lines.append("» " + text)
        return lines

    if kind == "user":
        content = message.get("content")
        blocks = content if isinstance(content, list) else [content]
        lines = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") not in ("tool_result", "text"):
                continue
            result = block.get("content")
            if isinstance(result, list):
                result = " ".join(
                    str(part.get("text") or "") for part in result
                    if isinstance(part, dict))
            if block.get("type") == "tool_result" or (
                    isinstance(result, str) and result.strip()):
                lines.extend(_result_lines(result))
        return lines

    return []


def _tail_transcript(key: str, task_id: str, session_id: str,
                     cwd: str | None, stop: threading.Event) -> None:
    meta = _find_task_meta(task_id, session_id=session_id, cwd=cwd, stop=stop)
    if not meta:
        return
    transcript = str(meta.get("transcriptPath") or "").strip()
    if not transcript:
        output = str(meta.get("outputPath") or "").strip()
        transcript = output
    path = Path(transcript)
    deadline = time.monotonic() + _TAIL_MAX_LIFE
    idle_since = time.monotonic()
    buffer = ""
    try:
        handle = None
        while not stop.is_set() and time.monotonic() < deadline:
            if handle is None:
                try:
                    handle = path.open("r", encoding="utf-8", errors="replace")
                except Exception:
                    if time.monotonic() - idle_since > _META_WAIT:
                        return
                    time.sleep(0.5)
                    continue
            chunk = handle.readline()
            if chunk:
                idle_since = time.monotonic()
                buffer += chunk
                if not buffer.endswith("\n"):
                    continue
                raw, buffer = buffer, ""
                for line in _format_transcript_line(raw):
                    append(key, line)
                continue
            # EOF agora; o arquivo cresce enquanto o executor trabalha.
            if stop.is_set():
                break
            with _lock:
                entry = _entries.get(key)
                done = bool(entry and entry.get("done"))
            if done and time.monotonic() - idle_since > 1.0:
                break
            if time.monotonic() - idle_since > _TAIL_IDLE_STOP:
                break
            time.sleep(0.25)
    except Exception as exc:  # noqa: BLE001
        print(f"[TaskMirror] tail {path}: {exc}")
    finally:
        try:
            if handle is not None:
                handle.close()
        except Exception:
            pass


def _start_tailer(key: str, task_id: str, session_id: str,
                  cwd: str | None) -> None:
    with _lock:
        entry = _entries.get(key)
        if entry is None or entry["done"]:
            return
        stop = entry["stop"]
    thread = threading.Thread(
        target=_tail_transcript,
        args=(key, task_id, session_id, cwd, stop),
        daemon=True,
        name=f"task-mirror-{_safe(task_id)[:8]}",
    )
    thread.start()
    with _lock:
        _tailers[:] = [t for t in _tailers if t.is_alive()]
        _tailers.append(thread)


# ── subagent / queue hooks ──────────────────────────────────────────────────

def start_subagent(task_id, title: str = "", session_id: str = "",
                   cwd: str | None = None, auto_open: bool = True) -> dict:
    """Começa o espelho de um subagente, agregado à fila quando houver uma."""
    sub_id = str(task_id or "").strip()
    if not sub_id:
        return {"ok": False, "message": "Subagente sem id."}
    target, target_title = _current_target()
    linked = target != sub_id
    if linked:
        link(sub_id, target)
    # No espelho agregado (sem tarefa na fila), a janela é nomeada pelo
    # subagente que está rodando — não pelo rótulo genérico do live.
    if target == LIVE_ID and title:
        shown_title = title
    elif linked:
        shown_title = target_title
    else:
        shown_title = title or target
    result = begin(target, shown_title, source="subagent", auto_open=auto_open)
    with _lock:
        entry = _entries.get(target)
        if entry is not None:
            active = entry.get("active")
            if active is None:
                active = entry["active"] = set()
            active.add(sub_id)
    append(target, f"▸ subagente: {_one_line(title, 160) or sub_id} ({sub_id})")
    _start_tailer(target, sub_id, str(session_id or ""), cwd)
    return result


def subagent_event(event: dict) -> None:
    """Linha de ferramenta/texto vinda do lado do sidecar (fallback)."""
    if not isinstance(event, dict):
        return
    task_id = str(event.get("task_id") or event.get("tool_use_id") or "").strip()
    if not task_id:
        return
    if str(event.get("kind") or "") == "text":
        text = _one_line(event.get("text"), 500)
        if text:
            append(task_id, "» " + text)
        return
    append(task_id, format_tool(event.get("name"), event.get("input")))


def progress(task_id, event: dict) -> None:
    """Progresso do sidecar: última ferramenta e resumo, sem repetir demais."""
    if not isinstance(event, dict):
        return
    key = str(task_id or "").strip()
    if not key:
        return
    name = str(event.get("last_tool_name") or "").strip()
    summary = _one_line(event.get("summary"), 300)
    if name and summary:
        append(key, f"… {name}: {summary}")
    elif name:
        append(key, f"… usando {name}")
    elif summary:
        append(key, f"… {summary}")


def finish_subagent(task_id, status: str = "completed", summary: str = "") -> None:
    key = str(task_id or "").strip()
    if not key:
        return
    target = _resolve(key)
    with _lock:
        entry = _entries.get(target)
        active = set(entry.get("active") or ()) if entry is not None else set()
        was_active = key in active
        active.discard(key)
        if entry is not None:
            entry["active"] = active
    if not was_active and active:
        # Fim tardio de uma execução que já não é dona desta janela: a tarefa
        # nova que reaproveitou o espelho continua com a janela dela.
        return
    if active:
        append(target, f"· {key} terminou; o espelho segue com "
                       f"{len(active)} execução(ões) em andamento")
        return
    finish(target, status, summary)
    if target != key:
        link(key, target)


# ── show / status ───────────────────────────────────────────────────────────

def _match_task(query: str) -> dict | None:
    """Escolhe uma tarefa da fila cujo título aparece na fala do dono."""
    text = " ".join(str(query or "").lower().split())
    if not text:
        return None
    try:
        from core import task_queue
        rows = task_queue.list_tasks("open")
    except Exception:
        return None
    for row in rows:
        title = " ".join(str(row.get("title") or "").lower().split())
        if title and title in text:
            return row
    return None


def _log_finished(path: Path) -> bool:
    """Um log já encerrado não deve ser mostrado como se estivesse rodando."""
    try:
        return "── fim:" in path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False


def _newest_log() -> tuple[str, str]:
    """Último espelho em disco ainda sem fim: (id, título do cabeçalho)."""
    try:
        files = sorted(Path(MIRROR_DIR).glob("*.log"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return "", ""
    for path in files:
        if _log_finished(path):
            continue
        title = ""
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for _ in range(4):
                    line = fh.readline()
                    if line.startswith("# tarefa:"):
                        title = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass
        return path.stem, title
    return "", ""


def show(ident=None, query: str = "", force: bool = False) -> dict:
    """Abre o espelho da execução pedida, da fila corrente ou do live."""
    target = str(ident or "").strip()
    title = ""
    if not target:
        row = _match_task(query)
        if row is not None:
            target = str(row.get("id") or "")
            title = str(row.get("title") or "")
        else:
            target, title = _current_target()
            if target == LIVE_ID:
                found, found_title = _newest_log()
                if found and found != LIVE_ID:
                    target, title = found, found_title or title

    with _lock:
        key = _resolve(target) or target
        entry = _entries.get(key)
        if entry is None:
            # Processo novo/espelho antigo: tenta o log em disco.
            path = _path_for(key)
            if path.is_file():
                entry = _attach_existing(key, title or key)
            elif _queue_current() is not None:
                entry = _ensure(key, title, "show")
            else:
                return {"ok": False, "id": key,
                        "message": "Não há execução em andamento para mostrar."}
        _last_active = entry["id"]
        snapshot = dict(entry)
        may_open = bool(force or _should_open(entry))
        if may_open:
            entry["opened_at"] = time.monotonic()
            entry["open_count"] += 1

    if not may_open:
        return {"ok": True, "id": snapshot["id"],
                "title": snapshot.get("title", ""),
                "log": snapshot.get("path", ""), "opened": False,
                "message": "O espelho dessa execução já está aberto."}

    was_done = bool(snapshot.get("done"))
    ok, message = _open_window(snapshot)
    with _lock:
        alive = _entries.get(snapshot["id"])
        if alive is not None:
            alive["open_error"] = "" if ok else message
            if not ok:
                alive["opened_at"] = 0.0
    if ok and not was_done:
        # Terminou enquanto a janela nascia. Reabrir um log já encerrado é
        # pedido explícito do dono: essa janela fica aberta.
        _close_if_finished(snapshot["id"])
    return {"ok": bool(ok), "id": snapshot["id"],
            "title": snapshot.get("title", ""),
            "log": snapshot.get("path", ""), "opened": bool(ok),
            "message": ("Abri o espelho da execução na tela."
                        if ok else f"Não consegui abrir o terminal: {message}")}


def status() -> dict:
    with _lock:
        rows = [{"id": e["id"], "title": e.get("title", ""),
                 "done": bool(e.get("done")), "log": e.get("path", ""),
                 "opened": bool(e.get("open_count"))}
                for e in _entries.values()]
        active = _resolve(_last_active) if _last_active else ""
    current = _queue_current()
    return {"ok": True, "current": current, "active": active,
            "mirrors": rows}


def reset() -> None:
    """Testes: esquece o estado em memória (não apaga os logs)."""
    global _last_active
    with _lock:
        _entries.clear()
        _aliases.clear()
        _tailers.clear()
        _last_active = None
