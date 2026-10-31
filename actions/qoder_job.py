"""Answer, inspect or stop the Qoder job that is running in the background.

WHY IT EXISTS
    `qoder_agent` returns immediately; the work continues on its own thread. If
    Qoder stops to ask a decision, the assistant asks the user — and the user's
    answer has to travel back to the task. This tool is that channel, plus the
    honest status ("what is it doing?") and the stop button.
"""
from core import qoder_jobs


def qoder_job(parameters: dict, player=None, speak=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "status").strip().lower()
    if action in ("answer", "responder", "resposta", "reply"):
        text = str(p.get("answer") or p.get("text") or "").strip()
        return qoder_jobs.answer(text)
    if action in ("approve", "aprovar", "aplicar", "aplica", "confirmar", "sim"):
        return qoder_jobs.approve()
    if action in ("reject", "recusar", "rejeitar", "nao", "não", "descartar"):
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
        "question Qoder asked."
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
