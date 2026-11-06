"""Dono-only grant para responder a um job do Qoder (approve/reject/answer).

POR QUE EXISTE
    FLUXO TRABALHO, passo 5: o plano só é aplicado depois de "CONFIRMAÇÃO DE
    IDENTIDADE VIA SENHA NO TEXTO OU CONFIRMAÇÃO DE VOZ". O modelo ao vivo
    pode RELAY a resposta do dono, mas não pode fabricá-la. Este módulo é a
    única porta de entrada dessas três ações: uma janela one-shot, de 120 s
    (a mesma do actiongate), aberta apenas por dois caminhos que verificam
    identidade antes de chamar ``arm``:

    * ``main._try_action_consent`` — consentimento falado com VOZ DO DONO
      confirmada por voiceprint (texto digitado comum nunca chega aqui:
      sem áudio a recusa é imediata, lei do dono 01/11);
    * ``main._owner_text_gate`` — SENHA DIGITADA correta (PBKDF2 em
      core/owner_auth.py).

    O modelo que chamar approve sem janela armada recebe recusa; "pode"
    digitado sem senha não abre nada; uma aprovação não vale para outro job
    (a janela carrega o id do job que esperava).

AUDITORIA
    O consumo grava uma linha auditável: QUEM aprovou (dono — os dois
    fatores são, por construção, do dono) e POR QUAL FATOR. A linha nunca
    nomeia canal nem número.
"""
from __future__ import annotations

import threading
import time

from core.owner_auth import FACTORS  # veredito único: voz do dono OU senha digitada

# Fatores aceitos — os únicos que a lei do dono prevê: as MESMAS constantes
# que core/actiongate.py e core/owner_auth.py usam, para que todas as
# janelas privilegiadas falem do mesmo veredito de fator (decisão do dono,
# voz, 06/11/2026: "eu posso liberar QUALQUER coisa com a senha ou com a
# minha voz"). Não há classe em que a senha seja banida — job, ação, envio,
# sudo, reinício, energia e e-mail aceitam os dois fatores; o que continua
# valendo é que só o DONO os fornece (voiceprint ou PBKDF2), nunca o modelo.

# Mesma duração da janela de ação do actiongate: quem abriu uma abriu a outra.
WINDOW_S = 120.0

_LOCK = threading.RLock()
_GRANT: dict | None = None
_AUDIT = None


def set_audit(fn) -> None:
    """Registra o canal de auditoria (main liga em ui.write_log)."""
    global _AUDIT
    _AUDIT = fn


def _audit(line: str, *, log: bool = True) -> None:
    print(f"[JobGrant] {line}")
    if not log:
        return
    fn = _AUDIT
    if callable(fn):
        try:
            fn(line)
        except Exception as e:
            print(f"[JobGrant] auditoria: {e}")


def arm(job_id: str, factor: str, ttl: float = WINDOW_S) -> float:
    """Abre a janela one-shot de resposta ao job que espera o dono.

    Retorna os segundos concedidos (0.0 quando recusado: sem job, fator
    desconhecido ou ttl inválido). Quem chama já verificou a identidade.
    """
    global _GRANT
    key = str(job_id or "").strip()
    fac = str(factor or "").strip()
    try:
        secs = float(ttl)
    except (TypeError, ValueError):
        secs = 0.0
    if not key or fac not in FACTORS or secs <= 0.0:
        return 0.0
    with _LOCK:
        _GRANT = {"job_id": key, "factor": fac,
                  "expires": time.monotonic() + secs}
    return secs


def has_valid(job_id: str = "") -> bool:
    """Existe janela válida para o job que espera resposta agora?"""
    with _LOCK:
        if _GRANT is None:
            return False
        if time.monotonic() >= float(_GRANT["expires"]):
            return False
        if job_id and str(_GRANT["job_id"]) != str(job_id):
            return False
        return True


def consume(job_id: str = "", action: str = "") -> dict | None:
    """Reclama a janela exatamente uma vez. None sem grant válido.

    Sem grant a resposta é recusada — é o caminho do modelo tentando
    aprovar o próprio plano ou de um "pode" digitado sem senha.
    """
    global _GRANT
    with _LOCK:
        g = _GRANT
        if g is None or time.monotonic() >= float(g["expires"]):
            _GRANT = None
            _audit(f"resposta ao job recusada: sem confirmação do dono "
                   f"(ação: {str(action or '?')[:30]})", log=False)
            return None
        if job_id and str(g["job_id"]) != str(job_id):
            _GRANT = None
            _audit("resposta ao job recusada: a janela aberta não era deste "
                   f"job (ação: {str(action or '?')[:30]})", log=False)
            return None
        _GRANT = None
    _audit(f"SYS: resposta ao job aceita — quem: dono; fator: {g['factor']} "
           f"(ação: {str(action or '?')[:30]}).")
    return dict(g)


def reset() -> None:
    """Esquece a janela (testes / novo boot)."""
    global _GRANT
    with _LOCK:
        _GRANT = None
