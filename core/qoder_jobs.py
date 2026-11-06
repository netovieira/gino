"""Qoder jobs in the background — the assistant keeps listening while it works.

WHY THIS EXISTS
    A delegated task takes tens of seconds to minutes. Waiting for it inside the
    conversation turn froze the assistant: no answer, no updates, and at the end
    a bare "o Qoder não devolveu nada". Here the job runs on its own thread and
    the conversation stays alive. The job reports:

        started   it began (the assistant has already said it would ask)
        progress  one short line about what it is doing, at most every 20 s
        question  it needs a decision; the assistant asks the user
        done      final summary
        error     the real reason (timeout, stderr, CLI missing)
        cancelled the user stopped it

    The events are delivered through a sink registered by main.py; this module
    never talks to a session or to the UI directly, so it can be tested headless.

ONE JOB AT A TIME
    Qoder is a heavy local process; two at once would race over the same machine
    and confuse the conversation. A second request is queued and the ack says so.
    A job waiting for a user decision holds the slot (its process is gone).
"""
from __future__ import annotations

import pathlib
import threading
import time
import uuid

from core import qoder

_LOCK = threading.RLock()
_SINK = None
_ACTIVE = None
_QUEUE: list["Job"] = []
_JOBS: dict[str, "Job"] = {}
_PROGRESS_GAP = 40.0        # seconds between spoken progress lines
_FINISHED_KEEP = 8          # remembered jobs for status/answer lookups


def _default_plans_dir() -> pathlib.Path:
    """Where Qoder keeps plan Markdown — the folder obsidian mirrors."""
    return pathlib.Path.home() / ".qoder" / "plans"


class Job:
    def __init__(self, task: str, mode: str, model: str, workspace,
                 files, player, requires_approval: bool = False) -> None:
        self.id = uuid.uuid4().hex
        self.task = task
        self.mode = mode
        self.model = model
        self.workspace = workspace
        self.files = files or []
        self.player = player
        self.state = "queued"          # queued|running|waiting_user|waiting_approval|done|error|cancelled
        self.requires_approval = bool(requires_approval)
        self.approved: bool | None = None
        self.plan = ""
        self.plan_path = ""
        self.created_at = time.monotonic()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.progress = ""
        self.progress_log: list[str] = []
        self.last_progress_at = 0.0
        self.question: dict | None = None
        self.decisions: list[tuple[str, str]] = []
        self.result = ""
        self.error = ""
        self.cancel_event = threading.Event()

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "task": self.task,
            "mode": self.mode,
            "model": self.model,
            "state": self.state,
            "progress": self.progress,
            "question": dict(self.question) if self.question else None,
            "plan": self.plan,
            "plan_path": self.plan_path,
            "result": self.result,
            "error": self.error,
        }


def set_sink(fn) -> None:
    """Register the callback that receives job events (main.py routes them to
    the conversation). Passing None silences the sink (headless tests)."""
    global _SINK
    _SINK = fn


def _emit(kind: str, job: Job, **extra) -> None:
    ev = {"kind": kind, "id": job.id, "task": job.task,
          "mode": job.mode, "model": job.model}
    ev.update(extra)
    sink = _SINK
    if sink is not None:
        try:
            sink(ev)
        except Exception as e:
            print(f"[QoderJob] sink failed: {e}")


def _remember(job: Job) -> None:
    _JOBS[job.id] = job
    if len(_JOBS) > _FINISHED_KEEP:
        for jid in list(_JOBS):
            j = _JOBS[jid]
            if j.state in ("done", "error", "cancelled"):
                del _JOBS[jid]
                break


def _next_in_queue() -> None:
    """Hand the slot to the next queued job (caller holds the lock)."""
    global _ACTIVE
    _ACTIVE = _QUEUE.pop(0) if _QUEUE else None
    nxt = _ACTIVE
    if nxt is not None:
        threading.Thread(target=_worker, args=(nxt,), daemon=True,
                         name=f"qoder-job-{nxt.id[:6]}").start()


def start(task: str, mode: str = "answer", model: str | None = None,
          workspace: str | None = None, files=None, player=None,
          approval: bool | None = None) -> str:
    """Queue a job and return the ack the assistant should say right away.

    A write job is a proposal by default: Qoder first returns a plan, nothing on
    disk changes, and the job holds the slot in `waiting_approval` until the
    user says yes (approve) or no (reject). That is the same mechanic as any
    create/execute request: plan first, act only after consent.
    """
    global _ACTIVE
    task = str(task or "").strip()
    if not task:
        return "Diga o que você quer que o Qoder faça."
    mode = str(mode or "answer").strip().lower()
    if mode not in ("answer", "plan", "write"):
        mode = "answer"
    requires_approval = (mode == "write") if approval is None else bool(approval)
    if mode != "write":
        requires_approval = False
    model = qoder.free_model(model or qoder.settings()["model"])
    job = Job(task, mode, model, workspace, files, player,
              requires_approval=requires_approval)

    with _LOCK:
        _remember(job)
        busy = _ACTIVE is not None
        if busy:
            _QUEUE.append(job)
        else:
            # start under the lock so two starts cannot both claim the slot
            _ACTIVE = job
            threading.Thread(target=_worker, args=(job,), daemon=True,
                             name=f"qoder-job-{job.id[:6]}").start()

    if busy:
        ahead = _ACTIVE.task if _ACTIVE is not None else "outra tarefa"
        _emit("queued", job, text=task[:160], ahead=str(ahead)[:120])
        return (f"O Qoder está ocupado com '{str(ahead)[:100]}'. Coloquei "
                f"'{task[:120]}' na fila e começo assim que ele terminar.")
    if requires_approval:
        return (f"Pedi ao Qoder um plano para: {task[:140]}. Nada será criado "
                "ou alterado até você aprovar; eu te mostro o plano assim que "
                "ele terminar.")
    return (f"Pedi ao Qoder: {task[:160]}. Ele está trabalhando em segundo plano e "
            "eu te atualizo conforme avançar. Se ele precisar de uma decisão, "
            "eu pergunto.")


def answer(text: str) -> str:
    """Send the user's decision to the job that is waiting for one."""
    text = str(text or "").strip()
    if not text:
        return "Qual é a resposta para a pergunta do Qoder?"
    with _LOCK:
        job = _ACTIVE
        if job is None or job.state != "waiting_user":
            return ("Não há pergunta do Qoder esperando resposta. " + status())
        q = (job.question or {}).get("question", "")
        job.decisions.append((q, text))
        job.question = None
        job.state = "running"
        job.last_progress_at = 0.0
        threading.Thread(target=_worker, args=(job,), daemon=True,
                         name=f"qoder-job-{job.id[:6]}").start()
    _emit("progress", job, text="decisão recebida — continuando a tarefa")
    return f"Anotado: {text[:120]}. O Qoder está continuando a tarefa."


def approve() -> str:
    """The user accepted the plan: let the write pass run."""
    with _LOCK:
        job = _ACTIVE
        if job is None or job.state != "waiting_approval":
            return ("Não há plano do Qoder esperando aprovação. " + status())
        job.approved = True
        job.state = "running"
        job.last_progress_at = 0.0
        task = job.task
        threading.Thread(target=_worker, args=(job,), daemon=True,
                         name=f"qoder-job-{job.id[:6]}").start()
    _emit("progress", job, text="plano aprovado — aplicando as mudanças")
    return (f"Plano aprovado. O Qoder está aplicando as mudanças de "
            f"'{task[:100]}' e eu te aviso quando terminar.")


def reject() -> str:
    """The user refused the plan: nothing is written."""
    with _LOCK:
        job = _ACTIVE
        if job is None or job.state != "waiting_approval":
            return ("Não há plano do Qoder esperando aprovação. " + status())
        job.approved = False
        job.state = "cancelled"
        job.finished_at = time.monotonic()
        task = job.task
        _next_in_queue()
    _emit("cancelled", job, text=f"plano recusado: {task[:120]}")
    return f"Plano recusado. Nada foi alterado em '{task[:100]}'."


def approval() -> dict | None:
    """The plan waiting for a yes/no — re-injected into the prompt each turn."""
    with _LOCK:
        if (_ACTIVE is not None and _ACTIVE.state == "waiting_approval"
                and _ACTIVE.plan):
            return {"task": _ACTIVE.task, "plan": _ACTIVE.plan}
        return None


def has_approval() -> bool:
    with _LOCK:
        return (_ACTIVE is not None and _ACTIVE.state == "waiting_approval"
                and bool(_ACTIVE.plan))


def pending_reply_id() -> str:
    """Id do job que pode receber a resposta do dono agora ('' se não há).

    É a chave da janela em core/job_grant.py: uma aprovação aberta para um
    plano não vale para outro.
    """
    with _LOCK:
        if (_ACTIVE is not None
                and _ACTIVE.state in ("waiting_approval", "waiting_user")):
            return _ACTIVE.id
        return ""


def plan_markdown(job: "Job", plan: str = "") -> str:
    """O plano no formato do contrato: título, passos, mudança, risco, veto."""
    title = str(job.task or "tarefa").strip() or "tarefa"
    body = str(plan or job.plan or "").strip() or "(o Qoder não descreveu passos)"
    return (
        f"# Plano — {title}\n\n"
        f"- Job: {job.id}\n"
        f"- Modo: {job.mode} | Modelo: {job.model}\n\n"
        "## Passos\n\n"
        f"{body}\n\n"
        "## O que vai mudar\n\n"
        "Somente o que está nos Passos acima, e apenas depois da sua "
        "aprovação (voz do dono ou senha digitada).\n\n"
        "## Risco\n\n"
        "Nada foi alterado ainda. A escrita acontece numa passagem separada, "
        "só após a aprovação.\n\n"
        "## O que NÃO será feito\n\n"
        "- Nada antes da sua aprovação.\n"
        "- Nada fora do escopo dos Passos acima.\n"
        "- Nenhum push, nenhum reinício, nenhum envio a terceiros.\n"
    )


def save_plan_file(job: "Job", plan: str = "", plans_dir=None) -> str:
    """Grava o plano em ``~/.qoder/plans/<id>.md`` ANTES de pedir aprovação.

    O diretório é o mesmo que ``core.obsidian.mirror_plans`` já espelha para
    o cofre ``~/Jarvis/Planos``; a ação ``open_in`` existente abre o arquivo
    na tela sem nenhum código novo de abertura ou espelhamento. Falha de
    escrita nunca derruba o job — devolve '' e o pedido de aprovação segue.
    """
    try:
        base = (pathlib.Path(plans_dir).expanduser() if plans_dir
                else _default_plans_dir())
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{job.id}.md"
        tmp = path.with_name(f"{path.name}.part")
        tmp.write_text(plan_markdown(job, plan), encoding="utf-8")
        tmp.replace(path)
        try:
            # Rastro de sucesso no mesmo formato do marcador de falha: o
            # passo existe no log quando aconteceu (FLUXO TRABALHO, passo 2).
            print(f"[QoderJob] plano em arquivo gravado: {path.name}")
        except Exception:
            pass
        return str(path)
    except Exception as e:
        print(f"[QoderJob] plano em arquivo falhou: {e}")
        return ""


def cancel() -> str:
    """Stop the running job; its process is killed by the transport."""
    with _LOCK:
        job = _ACTIVE
        if job is None:
            return "O Qoder está livre — não há nada para cancelar."
        if job.state in ("done", "error", "cancelled"):
            return f"A tarefa '{job.task[:100]}' já terminou."
        job.cancel_event.set()
        was_waiting = job.state in ("waiting_user", "waiting_approval")
        job.state = "cancelled"
        job.finished_at = time.monotonic()
        if was_waiting:
            _next_in_queue()
        task = job.task
    _emit("cancelled", job, text=task[:160])
    return f"Cancelei a tarefa do Qoder: {task[:120]}."


def status() -> str:
    """A few honest sentences about the job slot — for voice and for the log."""
    with _LOCK:
        job = _ACTIVE
        queued = len(_QUEUE)
        last = next((j for j in _JOBS.values()
                     if j.state in ("done", "error", "cancelled")), None)
    if job is None:
        line = "O Qoder está livre."
        if last is not None:
            if last.state == "done":
                line += f" Última tarefa concluída: {last.task[:100]}."
            elif last.state == "error":
                line += f" Última tarefa falhou: {last.error[:120]}."
        return line

    if job.state == "running":
        elapsed = int(time.monotonic() - (job.started_at or job.created_at))
        line = (f"O Qoder está trabalhando em '{job.task[:100]}' há {elapsed}s "
                f"({job.mode}, {job.model}).")
        if job.progress:
            line += f" Último andamento: {job.progress}."
        if queued:
            line += f" Há {queued} tarefa(s) na fila."
        return line
    if job.state == "waiting_user":
        q = job.question or {}
        line = f"O Qoder parou e espera uma decisão: {q.get('question', '')}"
        opts = q.get("options") or []
        if opts:
            line += " Opções: " + "; ".join(str(o) for o in opts) + "."
        rec = q.get("recommended") or ""
        if rec:
            line += f" A recomendação dele é: {rec}."
        return line
    if job.state == "waiting_approval":
        return (f"O plano para '{job.task[:100]}' está pronto e espera seu ok. "
                f"Resumo: {job.plan[:400]}")
    if job.state == "queued":
        return f"A tarefa '{job.task[:100]}' está na fila; o Qoder ainda não começou."
    if job.state == "done":
        return f"O Qoder concluiu '{job.task[:100]}'. Resumo: {job.result[:300]}"
    if job.state == "error":
        return f"A tarefa '{job.task[:100]}' falhou: {job.error[:200]}"
    return f"A tarefa '{job.task[:100]}' foi cancelada."


def is_busy() -> bool:
    with _LOCK:
        return _ACTIVE is not None


def has_question() -> bool:
    """Is a decision waiting for the user right now?"""
    with _LOCK:
        return (_ACTIVE is not None and _ACTIVE.state == "waiting_user"
                and _ACTIVE.question is not None)


def question() -> dict | None:
    """The pending decision, if any — re-injected into the prompt each turn so
    a trimmed history cannot hide the fact that Qoder is waiting."""
    with _LOCK:
        if (_ACTIVE is not None and _ACTIVE.state == "waiting_user"
                and _ACTIVE.question):
            return dict(_ACTIVE.question)
        return None


def phase() -> str:
    """Short state for the heartbeat log: idle|queued|running|waiting_user…"""
    with _LOCK:
        return _ACTIVE.state if _ACTIVE is not None else "idle"


def _worker(job: Job) -> None:
    """Run one job, then hand the slot to the next queued one."""
    while True:
        _run_once(job)
        if job.state in ("waiting_user", "waiting_approval"):
            # Its process is gone; the slot stays with it until the user answers
            # (or the job is cancelled).
            return
        with _LOCK:
            global _ACTIVE
            if _ACTIVE is job:
                _ACTIVE = None
            if _QUEUE:
                _next_in_queue()
                return
        return


def _progress(job: Job, text: str) -> None:
    text = str(text or "").strip()
    if not text or job.state != "running":
        return
    now = time.monotonic()
    if now - job.last_progress_at < _PROGRESS_GAP:
        return
    job.last_progress_at = now
    job.progress = text[:160]
    job.progress_log.append(job.progress)
    _emit("progress", job, text=job.progress)


def _ask_qoder(job: Job, prepared: dict, on_progress) -> tuple[str, bool]:
    """One Qoder pass; returns (reply, used_proxy).

    The CLI is the normal path. When it is not installed, the proxy answers
    text-only — the caller applies the JSON itself in write mode.
    """
    st = prepared["st"]
    try:
        reply = qoder.ask_stream(
            prepared["prompt"], st=st, model=prepared["model"],
            system=prepared["system"], cwd=prepared["root"],
            cancel_event=job.cancel_event, on_progress=on_progress)
        return reply, False
    except RuntimeError as e:
        if job.cancel_event.is_set():
            raise
        if "não encontrado" in str(e):
            reply = qoder.ask(prepared["prompt"], st=st,
                              model=prepared["model"],
                              system=prepared["proxy_system"],
                              max_tokens=8192)
            return reply, True
        raise


def _run_once(job: Job) -> None:
    if job.cancel_event.is_set():
        return
    first = job.started_at is None
    with _LOCK:
        job.state = "running"
        job.started_at = job.started_at or time.monotonic()
    if first:
        _emit("started", job, text=job.task[:160])

    try:
        from actions import qoder_agent as qa

        on_progress = lambda ev: _progress(job, qa.describe_event(ev))  # noqa: E731

        # Stage 1 — proposal. The plan pass runs without tools, so it cannot
        # touch the disk even if the model tries; nothing changes until the
        # user approves it. This is the same "plan first, act after consent"
        # mechanic as any create/execute request.
        if job.requires_approval and job.approved is None:
            prepared = qa.prepare_job(job, mode="plan", readonly=True)
            reply, _ = _ask_qoder(job, prepared, on_progress)

            question = qa.parse_question(reply)
            if question:
                with _LOCK:
                    job.question = question
                    job.state = "waiting_user"
                _emit("question", job, question=question)
                return

            plan = str(reply or "").strip()
            if not plan:
                raise RuntimeError("o Qoder não devolveu um plano")
            # FLUXO TRABALHO, passos 3-4: o plano vira arquivo ANTES do
            # pedido de aprovação, para a ação `open_in` existente abri-lo
            # no Obsidian sem código novo de abertura nem segundo espelho.
            plan_path = save_plan_file(job, plan)
            with _LOCK:
                job.plan = plan
                job.plan_path = plan_path
                job.state = "waiting_approval"
            _emit("approval", job, plan=plan[:6000], text=plan[:800],
                  plan_path=plan_path)
            try:
                # Rastro do passo 3 do dono: o plano foi informado à boca.
                print(f"[QoderJob] plano informado à boca: {job.id[:6]}")
            except Exception:
                pass
            return

        prepared = qa.prepare_job(job)
        reply, used_proxy = _ask_qoder(job, prepared, on_progress)

        applied = None
        if used_proxy:
            # No CLI: the proxy answers text-only; in write mode we apply the
            # JSON ourselves, exactly like before.
            applied = qa.apply_proxy_reply(job, reply, prepared["root"], job.mode)

        question = qa.parse_question(reply)
        if question:
            with _LOCK:
                job.question = question
                job.state = "waiting_user"
            _emit("question", job, question=question)
            return

        job.result = str(applied if applied is not None else reply).strip()
        with _LOCK:
            job.state = "done"
            job.finished_at = time.monotonic()
        _emit("done", job, text=job.result[:800], result=job.result[:4000],
              files=(applied is not None))
    except Exception as e:
        if job.state == "cancelled" or job.cancel_event.is_set():
            already = job.state == "cancelled"
            with _LOCK:
                job.state = "cancelled"
                job.finished_at = job.finished_at or time.monotonic()
            if not already:
                _emit("cancelled", job, text=job.task[:160])
            return
        job.error = str(e)[:400] or e.__class__.__name__
        with _LOCK:
            job.state = "error"
            job.finished_at = time.monotonic()
        _emit("error", job, text=job.error)
