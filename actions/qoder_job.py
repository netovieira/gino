"""Answer, inspect or stop the Qoder job that is running in the background.

WHY IT EXISTS
    `qoder_agent` returns immediately; the work continues on its own thread. If
    Qoder stops to ask a decision, the assistant asks the user — and the user's
    answer has to travel back to the task. This tool is that channel, plus the
    honest status ("what is it doing?") and the stop button.

IDENTIDADE (FLUXO TRABALHO, passo 5 — lei do dono)
    `approve`, `reject` and `answer` only run with a valid one-shot grant in
    core/job_grant.py, armed exclusively by the owner's voiceprint-confirmed
    consent or by the typed owner password. The model relays decisions; it
    never makes one. A typed "pode" without password and a self-approval by
    the model are refused here, with the audit line naming the factor.
"""
from core import job_grant, qoder_jobs

_DENIED = ("Não posso aprovar, recusar ou responder por você: a resposta ao "
           "plano exige a sua voz confirmada ou a senha digitada. Nada foi "
           "alterado.")


def _reply_allowed(action: str) -> bool:
    """Consume the owner's one-shot window for this job (False without it)."""
    return job_grant.consume(qoder_jobs.pending_reply_id(), action) is not None


def qoder_job(parameters: dict, player=None, speak=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "status").strip().lower()
    if action in ("answer", "responder", "resposta", "reply"):
        text = str(p.get("answer") or p.get("text") or "").strip()
        if not _reply_allowed("answer"):
            return _DENIED
        return qoder_jobs.answer(text)
    if action in ("approve", "aprovar", "aplicar", "aplica", "confirmar", "sim"):
        if not _reply_allowed("approve"):
            return _DENIED
        return qoder_jobs.approve()
    if action in ("reject", "recusar", "rejeitar", "nao", "não", "descartar"):
        if not _reply_allowed("reject"):
            return _DENIED
        return qoder_jobs.reject()
    if action in ("cancel", "cancelar", "parar", "para", "stop"):
        return qoder_jobs.cancel()
    if action in ("status", "andamento", "estado", "progresso"):
        return qoder_jobs.status()
    return ("Use action='answer' (com a resposta), 'approve', 'reject', "
            "'status' ou 'cancel'.")


TOOL = {
    "name": "qoder_job",
    "description": (
        "Talks to the Qoder task running in the background (started by "
        "qoder_agent). action='answer' sends the user's decision to Qoder when "
        "it stopped to ask something — pass their words (or the recommended "
        "option if they asked for it) in 'answer'; action='approve' applies the "
        "plan Qoder proposed for a write task (only after the user clearly said "
        "yes); action='reject' discards that plan; action='status' reports what "
        "Qoder is doing; action='cancel' stops it. Use status when the user asks "
        "'como está o Qoder?' and answer as soon as the user replies to a "
        "question Qoder asked. approve/reject/answer only go through after the "
        "owner confirmed by voice (voiceprint) or by typed password — never on "
        "your own judgment."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "'answer' | 'approve' | 'reject' | 'status' | 'cancel'"
            },
            "answer": {
                "type": "STRING",
                "description": "The user's decision, in their own words — only for "
                               "action='answer'."
            },
        },
        "required": ["action"],
    },
    "handler": qoder_job,
    "behavior": "NON_BLOCKING",
    "scheduling": "WHEN_IDLE",
}
