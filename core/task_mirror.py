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

UP-7 (FLUXO TRABALHO, passo 4)
    O dono também pode pedir o PLANO na tela, não só o terminal. Em
    ``open_plan`` o espelho vira nota Markdown do cofre (título, plano de ação
    do job corrente e o andamento já gravado) e é aberta no Obsidian por
    ``core.obsidian`` — que registra ``event=obsidian_opened`` com o caminho no
    activity.log e espelha a nota na pasta Nextcloud do dono. Sem cofre, sem
    Obsidian ou sem plano: a falha volta como mensagem honesta e uma linha no
    log; jamais quebra o turno.

NOTIFICAÇÃO DE ANDAMENTO (UP-8, FLUXO TRABALHO passos 6-7)
    Gravar no log não chega ao dono que está longe da tela: o passo 6 manda
    "notificar a boca a cada mudança". Cada linha realmente nova que entra no
    espelho passa por ``on_progress_update``, que decide (etapa nova OU janela
    mínima do mesmo andamento), grava ``event=task_progress`` no activity.log e
    chama o gancho instalado por ``set_progress_hook`` — no processo vivo, a boca
    (``core.flows.mouth.progress_notifier``). Sem gancho instalado o evento fica
    no log com ``hook=ausente``: há evidência da etapa, e ninguém inventa fala.
    Os tetos são os módulos de baixo, não deste: ``PROGRESS_GAP_S`` (mesma
    etapa) e ``PROGRESS_STAGE_GAP_S`` (piso entre etapas), com as alavancas
    ``JARVIS_MIRROR_PROGRESS_GAP_S`` / ``JARVIS_MIRROR_PROGRESS_STAGE_GAP_S``.
    Nunca levanta: notificar é consequência do log, não pré-condição dele.

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

# Nota do plano de ação (UP-7): quantas linhas do andamento cabem na nota.
_NOTE_FOLDER = "Planos"
_NOTE_PROGRESS = 80
_NOTE_LINE = 200

# ── notificação de andamento (UP-8) ─────────────────────────────────────────
ACTIVITY_LOG = BASE_DIR / "logs" / "activity.log"
_DEFAULT_MIRROR_DIR = Path(MIRROR_DIR)

PROGRESS_GAP_S = 45.0        # mesmo andamento: no máximo uma notícia por janela
PROGRESS_STAGE_GAP_S = 12.0  # etapa diferente: piso contra o gotejo do transcrito
PROGRESS_GAP_ENV = "JARVIS_MIRROR_PROGRESS_GAP_S"
PROGRESS_STAGE_GAP_ENV = "JARVIS_MIRROR_PROGRESS_STAGE_GAP_S"
_PROGRESS_GAP_MAX = 600.0
_PROGRESS_TEXT = 180         # o que vai no log de auditoria
_HOOK_TEXT = 400             # o que o gancho (a boca) recebe para frasear

# "$ pytest -q" é teste, não comando qualquer: é a etapa que o dono quer ouvir.
_TEST_RE = re.compile(
    r"\b(?:pytest|unittest|jest|vitest|npm\s+test|yarn\s+test|ctest|mvn\s+test"
    r"|cargo\s+test|go\s+test|phpunit|rspec)\b")

# Rótulos de etapa: o que o dono ouve é "mudou o andamento", não a ferramenta.
_STAGE_BY_PREFIX = (
    ("$ ", "comando"),
    ("editar ", "edicao"),
    ("ler ", "leitura"),
    ("buscar ", "busca"),
    ("listar ", "busca"),
    ("abrir ", "consulta_externa"),
    ("pesquisar ", "consulta_externa"),
    ("subagente:", "delegacao"),
    ("organizar o plano", "plano"),
    ("▸ subagente:", "inicio"),
    ("» ", "resposta"),
    ("│ ", "resultado"),
    ("… usando", "ferramenta"),
    ("… ", "resumo"),
    ("· ", "nota"),
    ("── ", "fim"),
)

_lock = threading.RLock()
_entries: dict[str, dict] = {}
_aliases: dict[str, str] = {}
_last_active: str | None = None
# Mantém as threads vivas para o collector não as destruir no meio.
_tailers: list[threading.Thread] = []
# ``on_progress_update`` de fora do módulo: a boca do processo vivo.
_progress_hook = None


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
        "notify": {"at": 0.0, "stage": "", "text": ""},   # UP-8: ritmo da voz
        "progressed": False,
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
        "notify": {"at": 0.0, "stage": "", "text": ""},
        "progressed": False,
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


def append(ident, text: str, prefix: str = "", notify: bool = True) -> None:
    """Acrescenta uma linha com horário ao log da execução (ou do alias).

    ``notify`` (UP-8): a linha gravada é o andamento, então ela também passa por
    ``on_progress_update`` — que corta repetição, respeita a janela e só então
    registra ``task_progress`` e chama a boca. Linhas de burocracia do próprio
    espelho (plano aberto, fim) nascem com ``notify=False``.
    """
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
        if notify:
            on_progress_update(key, prefix + body)
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
    append(key, f"── fim: {label} ──", prefix="", notify=False)
    if summary:
        append(key, summary, notify=False)
    if busy:
        return                      # o fim que importa é o da última execução
    try:
        _close_windows(entry)
    except Exception as exc:  # noqa: BLE001 - fechar janela não quebra o turno
        print(f"[TaskMirror] fechar janela ({key}): {exc}")
    # PORTÃO DE FECHAMENTO (ordem do dono, 07/11/2026): fechar "done" um item
    # que tem asserção cadastrada em core/dev_audit e a asserção está em
    # falha/nao_verificado gera AVISO registrado — uma linha
    # ``event=close_advisory``, nunca fala, nunca bloqueio. A tarefa terminou
    # e quem fecha continua sendo livre; o mecanismo existe para a
    # divergência "fechado no plano x vivo de verdade" ficar no log.
    if str(status or "").strip().lower() in ("done", "completed",
                                             "concluido", "concluído"):
        try:
            from core import dev_audit
            dev_audit.advise_close(key, status)
        except Exception:  # noqa: BLE001 - auditoria não fecha nem quebra nada
            pass


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


# ── notificação de andamento (UP-8) ─────────────────────────────────────────

def classify_stage(text) -> str:
    """Rótulo da etapa a partir de uma linha gravada no espelho.

    A boca não anuncia ferramenta nem caminho (lei do guard): o que sobe para
    ela é a CLASSE do que mudou ("edicao", "testes", "comando"), nunca o
    comando cru. Linha sem classe reconhecida devolve "" e vale como
    "andamento", não como etapa nova.
    """
    line = _one_line(text, _HOOK_TEXT)
    if not line:
        return ""
    low = line.lower()
    if low.startswith("$ ") and _TEST_RE.search(line):
        return "testes"
    for prefix, stage in _STAGE_BY_PREFIX:
        if low.startswith(prefix.lower()):
            return stage
    return ""


def _progress_gap(name: str, default: float) -> float:
    """Teto de notificação, com alavanca de ambiente (o dono decide o ritmo)."""
    try:
        raw = os.environ.get(name, "")
    except Exception:
        return float(default)
    if not str(raw or "").strip():
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if not value > 0:
        return float(default)
    return min(_PROGRESS_GAP_MAX, value)


def _log_event(event: str, **fields) -> None:
    """Uma linha ``event=... chave="valor"`` no activity.log. Nunca levanta.

    Mesmo molde de ``core.obsidian``: é a evidência de que a etapa aconteceu,
    lida pela auditoria (FLUXO TRABALHO passo 6).
    """
    try:
        parts = [f"event={event}"]
        for key, value in fields.items():
            if value is None or value == "":
                continue
            text = str(value).replace('"', "'").replace("\n", " ")
            parts.append(f'{key}="{text}"' if " " in text else f"{key}={text}")
        path = _activity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                     f"{' '.join(parts)}\n")
            fh.flush()
    except Exception:      # noqa: BLE001 - registrar não pode quebrar o log
        pass


def _activity_path() -> Path:
    """O activity.log onde o espelho registra a etapa.

    Produção: ``logs/activity.log`` (o mesmo arquivo do resto do sistema).
    Teste com ``MIRROR_DIR`` em ``tmp_path``: o evento cai ao lado, no tmp —
    rodar a suite não enche o log de auditoria do dono de etapas falsas.
    """
    override = os.environ.get("JARVIS_MIRROR_ACTIVITY_LOG", "").strip()
    if override:
        return Path(override).expanduser()
    try:
        mirror = Path(MIRROR_DIR)
        if mirror != _DEFAULT_MIRROR_DIR:
            return mirror.parent / "activity.log"
    except Exception:      # noqa: BLE001 - caminho ruim não bloqueia o log
        pass
    return Path(ACTIVITY_LOG).expanduser()


def set_progress_hook(handler) -> None:
    """Instala o ``on_progress_update`` de fora: ``handler(payload)``.

    No processo vivo é ``core.flows.mouth.progress_notifier(speak=...)``. Roda
    na thread que gravou a linha (tail do transcrito ou fila), então o handler
    tem de ser thread-safe e nunca pode travar o espelho — o custo de falar já
    é do próprio handler. ``None`` desliga a voz e mantém o evento no log.
    """
    global _progress_hook
    _progress_hook = handler if callable(handler) else None


def progress_hook():
    """O gancho instalado (``None`` quando a voz não está plugada)."""
    return _progress_hook


def on_progress_update(ident, text: str, *, stage: str = "",
                       force: bool = False) -> dict:
    """Decide, registra e voz: a mudança de andamento do espelho (UP-8).

    Chamar ``append`` já faz isso para a linha gravada; a função é pública para
    os eventos que não passam por uma linha de log (o progresso do sidecar com
    um resumo novo) e para os testes. Regras duras:

    * espelho encerrado ou inexistente: nada;
    * mesma linha de novo: nada (o dedupe do log já cortou, aqui é a voz);
    * mesma etapa: só depois de ``PROGRESS_GAP_S``;
    * etapa nova: só depois de ``PROGRESS_STAGE_GAP_S``;
    * a primeira notícia de uma execução sai na hora.

    Nunca levanta e devolve ``{"ok", "notified", "reason"}``.
    """
    key = _resolve(ident) or str(ident or "")
    line = _one_line(text, _HOOK_TEXT)
    if not key or not line:
        return {"ok": False, "notified": False, "reason": "sem conteudo"}
    label = (stage or classify_stage(line))[:40]
    payload: dict | None = None
    hook = None
    with _lock:
        entry = _entries.get(key)
        if entry is None:
            return {"ok": False, "notified": False, "reason": "sem espelho"}
        if entry.get("done"):
            return {"ok": False, "notified": False, "reason": "encerrado"}
        state = entry.get("notify")
        if state is None:
            state = entry["notify"] = {"at": 0.0, "stage": "", "text": ""}
        now = time.monotonic()
        gap = _progress_gap(PROGRESS_GAP_ENV, PROGRESS_GAP_S)
        floor = _progress_gap(PROGRESS_STAGE_GAP_ENV, PROGRESS_STAGE_GAP_S)
        waited = now - float(state.get("at") or 0.0)
        same_stage = bool(label) and state.get("stage") == label
        reason = ""
        if state.get("text") == line and not force:
            reason = "andamento repetido"
        elif float(state.get("at") or 0.0) and not force:
            if same_stage and waited < gap:
                reason = f"mesma etapa ha {waited:.0f}s"
            elif waited < floor:
                reason = f"janela de {floor:.0f}s"
        if reason:
            return {"ok": True, "notified": False, "reason": reason}
        state.update({"at": now, "stage": label, "text": line})
        entry["updated"] = now
        hook = _progress_hook
        payload = {
            "event": "task_progress",
            "id": key,
            "title": _one_line(entry.get("title"), 160),
            "source": str(entry.get("source") or ""),
            "log": str(entry.get("path") or ""),
            "stage": label,
            "text": line,
            "elapsed_s": round(max(0.0, now - float(
                entry.get("created") or now)), 1),
            "first": not bool(entry.get("progressed")),
        }
        entry["progressed"] = True

    spoken: bool | None = None
    sentence = ""
    reason = ""
    error = ""
    if hook is None:
        hook_state = "ausente"
    else:
        hook_state = "installado"
        try:
            result = hook(dict(payload))
            if isinstance(result, dict):
                spoken = bool(result.get("spoken"))
                sentence = _one_line(result.get("sentence") or "", 200)
                reason = _one_line(result.get("reason") or "", 160)
            else:
                spoken = bool(result)
        except Exception as exc:  # noqa: BLE001 - falar não pode quebrar o espelho
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            spoken = False
    _log_event(
        "task_progress",
        id=payload["id"],
        title=payload["title"],
        stage=payload["stage"] or "andamento",
        text=payload["text"][:_PROGRESS_TEXT],
        source=payload["source"],
        elapsed_s=payload["elapsed_s"],
        hook=hook_state,
        spoken=("" if spoken is None else str(spoken).lower()),
        voice=sentence,
        reason="" if spoken else (reason or error),
        error=error,
    )
    return {"ok": True, "notified": True, "reason": "",
            "stage": payload["stage"], "hook": hook is not None,
            "spoken": spoken, "sentence": sentence, "error": error or reason}


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


def _resolve_target(ident=None, query: str = "") -> tuple[str, str]:
    """Id e título da execução pedida: task_id, título falado, fila ou live.

    ``show`` e ``open_plan`` usam o mesmo mapa — é o que garante que "me mostra
    trabalhando" e "abre o plano na tela" apontem para o mesmo trabalho.
    """
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
    return target, title


def _entry_snapshot(ident: str, title: str = "") -> tuple[dict | None, bool]:
    """(snapshot do espelho, veio_de_disco). ``None`` quando não existe.

    Log antigo em disco é registrado como encerrado: abrir o plano de uma
    execução que já terminou é pedido explícito do dono, não um espelho vivo.
    """
    global _last_active
    with _lock:
        key = _resolve(ident) or str(ident or "")
        entry = _entries.get(key)
        from_disk = False
        if entry is None:
            path = _path_for(key)
            if path.is_file():
                entry = _attach_existing(key, title or key)
                from_disk = True
            else:
                return None, False
        _last_active = entry["id"]
        return dict(entry), from_disk


def show(ident=None, query: str = "", force: bool = False) -> dict:
    """Abre o espelho da execução pedida, da fila corrente ou do live."""
    target, title = _resolve_target(ident, query)

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


# ── plano de ação em nota (UP-7: FLUXO TRABALHO passo 4) ────────────────────

def _log_tail(path, limit: int = _NOTE_PROGRESS) -> tuple[str, list[str]]:
    """(início do cabeçalho, últimas linhas legíveis) de um log em disco."""
    started = ""
    lines: list[str] = []
    try:
        text = Path(str(path or "")).read_text(
            encoding="utf-8", errors="replace")
    except Exception:
        return "", lines
    for raw in text.splitlines():
        if raw.startswith("# início:"):
            started = raw.split(":", 1)[1].strip()
        if raw.startswith("#") or not raw.strip():
            continue
        # Cerca de `` dentro do bloco quebraria a nota Markdown.
        lines.append(_one_line(raw, _NOTE_LINE).replace("`", "'"))
    return started, lines[-limit:]


def _markdown_section(text: str, heading: str) -> str:
    """Corpo de uma seção ``## <heading>`` de um Markdown, ou ''."""
    out: list[str] = []
    inside = False
    for line in str(text or "").splitlines():
        if not inside:
            inside = line.strip().lower() == f"## {heading.lower()}"
            continue
        if line.strip().startswith("#"):
            break
        out.append(line)
    return "\n".join(out).strip()


def _active_plan_text() -> str:
    """O plano de ação do job corrente (arquivo em ``~/.qoder/plans``), ou ''.

    O passo 2 do fluxo grava o plano nesse arquivo antes da aprovação; é ele
    que a nota tem de mostrar. Sem job, ou sem arquivo, a nota não inventa
    plano nenhum — fica com o andamento do espelho.
    """
    try:
        from core import qoder_jobs
        job = getattr(qoder_jobs, "_ACTIVE", None)
        if job is None:
            return ""
        path = str(getattr(job, "plan_path", "") or "").strip()
        if path:
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except Exception:
                text = ""
            section = _markdown_section(text, "Passos")
            if section:
                return section
        return _one_line(getattr(job, "plan", "") or "", 1500)
    except Exception:      # noqa: BLE001 - sem job do Qoder, sem plano na nota
        return ""


def plan_note_body(entry: dict, plan_text: str = "") -> str:
    """A nota Markdown: plano de ação + andamento gravado no espelho."""
    title = _one_line(entry.get("title"), 160) or str(entry.get("id") or "")
    started, tail = _log_tail(entry.get("path"))
    state = "concluída" if entry.get("done") else "em andamento"
    plan = str(plan_text or "").strip() or (
        "- O Qoder ainda não gravou um plano em arquivo; o que segue é o "
        "andamento real desta execução.")
    lines = [
        f"# Plano de ação — {title}",
        "",
        f"- Execução: {entry.get('id') or ''}",
        f"- Estado: {state}",
        f"- Aberta em: {started or _stamp()}",
        f"- Log ao vivo: {entry.get('path') or ''}",
        "",
        "## Plano de ação",
        "",
        plan,
        "",
        "## Andamento",
        "",
        "```",
        *(tail or ["(o log ainda não tem linhas)"]),
        "```",
        "",
        "_Gerada pelo espelho de execução na pasta do cofre; a cópia fica na "
        "conta Nextcloud do dono._",
        "",
    ]
    return "\n".join(lines)


def open_plan(ident=None, query: str = "", plan: str = "") -> dict:
    """Abre na tela a nota do plano de ação (Obsidian + cópia Nextcloud).

    É o passo 4 do FLUXO TRABALHO: depois de aprovar, o dono pede para ver. O
    espelho vira nota, o ``core.obsidian`` abre no Obsidian e grava
    ``event=obsidian_opened`` no activity.log. Nada aqui levanta: sem cofre ou
    sem Obsidian a resposta é a mensagem honesta, e a linha fica no log do
    espelho para a auditoria ver que a tentativa aconteceu.
    """
    target, found_title = _resolve_target(ident, query)
    snapshot, _from_disk = _entry_snapshot(target, found_title)
    key = str((snapshot or {}).get("id") or _resolve(target) or target or "")
    if snapshot is None:
        return {"ok": False, "id": key,
                "message": "Não há execução para abrir o plano."}
    try:
        from core import obsidian
    except Exception as exc:  # noqa: BLE001 - cofre indisponível não quebra a fala
        message = f"O módulo do cofre não está disponível: {exc}"
        append(key, f"· plano não abriu no Obsidian: {message}", notify=False)
        return {"ok": False, "id": key, "title": snapshot.get("title", ""),
                "note": "", "nextcloud": "", "registered": False,
                "message": message}
    body = plan_note_body(snapshot, plan or _active_plan_text())
    note_title = f"Plano de ação — {_window_task_title(snapshot)}"
    try:
        result = obsidian.open_note(note_title, body, folder=_NOTE_FOLDER,
                                    source="task_mirror")
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "path": "", "nextcloud": "",
                  "registered": True, "message": str(exc)}
    opened = bool(result.get("ok"))
    registered = bool(result.get("registered", True))
    note = str(result.get("path") or "")
    message = str(result.get("message") or "")
    if opened:
        append(key, f"▸ plano no Obsidian: {note}", notify=False)
        if not registered:
            # O comando saiu mas o cofre não é o que o Obsidian tem aberto:
            # sem essa linha o dono veria "abriu" onde nada apareceu.
            append(key, "· cofre fora da lista do Obsidian — registre-o ou "
                        "ajuste JARVIS_OBSIDIAN_VAULT", notify=False)
    else:
        append(key, f"· plano não abriu no Obsidian: {message or 'erro'}",
               notify=False)
    headline = f"Abri o plano de ação na tela: {note}"
    return {"ok": opened, "id": key, "title": snapshot.get("title", ""),
            "note": note, "nextcloud": str(result.get("nextcloud") or ""),
            "registered": registered,
            "log": snapshot.get("path", ""),
            "message": (f"{headline}. {message}" if opened and not registered
                        else headline if opened
                        else message or "Não consegui abrir o plano no Obsidian.")}


def status() -> dict:
    with _lock:
        rows = [{"id": e["id"], "title": e.get("title", ""),
                 "done": bool(e.get("done")), "log": e.get("path", ""),
                 "opened": bool(e.get("open_count"))}
                for e in _entries.values()]
        active = _resolve(_last_active) if _last_active else ""
        voice = _progress_hook is not None
    current = _queue_current()
    # ``voice`` (UP-8): sem gancho o andamento é só log — a auditoria precisa
    # saber se a boca estava plugada quando a etapa passou.
    return {"ok": True, "current": current, "active": active,
            "voice": voice, "mirrors": rows}


def reset() -> None:
    """Testes: esquece o estado em memória (não apaga os logs)."""
    global _last_active, _progress_hook
    with _lock:
        _entries.clear()
        _aliases.clear()
        _tailers.clear()
        _last_active = None
        _progress_hook = None
