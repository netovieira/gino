"""Executor de ferramentas — sem modelo próprio (topologia do dono).

Recebe do orquestrador ferramenta + argumentos PRONTOS, verifica se a
ferramenta existe no catálogo, executa no processo dele e devolve um fato. Ele
não conversa, não escolhe ferramenta e não confirma nada:

* ``requires_owner_confirm`` nunca vira execução sem consulta a grants: antes
  de devolver ``pending_confirm``, consulta voice_grants para a pessoa
  identificada — se houver grant ativo cobrindo o escopo, executa;
* ferramenta fora do catálogo é recusada antes de qualquer efeito;
* o runner é injetado (o app decide como executa). Sem runner, o executor é
  um stub declarado e devolve erro — jamais finge sucesso.

O fato só vira asserção (``assertion=True``) quando o runner devolve sucesso
real, não simulado, sem confirmação pendente e sem cancelamento. É esse campo
que o guard da boca consulta.

INTEGRAÇÃO COM VOICE_GRANTS (FASE1-TASK3)
    Antes de exigir confirmação do dono, o executor consulta os grants
    persistentes por pessoa (core/voice_grants). Se a pessoa identificada
    (rótulo nominal do speaker) tem grant ativo cobrindo o escopo da ação,
    a execução prossegue sem pedir confirmação. O padrão permanece NEGAÇÃO:
    sem grant ativo, pede confirmação. A integração com actiongate (janelas
    de 120s do dono) é: se a janela do actiongate está aberta, executa
    (dono presente); se expirou, consulta grants.

EXECUÇÃO CONCRETA OBRIGATÓRIA (FASE3-TASK4)
    Toda ação com efeito colateral (risk_tier >= "low") exige prova material:
    recibo HMAC + verificação externa. Sem prova material, o fato é marcado
    como "unverified" e o guard rejeita a afirmação de resultado. Modo
    observador (primeiras 2 semanas): loga mas não bloqueia.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Callable

log = logging.getLogger(__name__)

Runner = Callable[[str, dict[str, Any]], Any]

# ── FASE3-TASK4: execução concreta obrigatória ─────────────────────────────
#
# Toda ação com efeito colateral (risk_tier >= "low") exige prova material:
# recibo HMAC + verificação externa. Sem prova material, o fato é marcado
# como "unverified" e o guard rejeita a afirmação de resultado.
#
# Modo observador (primeiras 2 semanas a partir de FASE3_TASK4_START):
# loga mas não bloqueia, para evitar falso-positivos em produção.

FASE3_TASK4_START = datetime(2026, 11, 15, tzinfo=timezone.utc)
OBSERVER_MODE_WEEKS = 2

_ACTIVITY_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "activity.log"


def _is_observer_mode(now: datetime | None = None) -> bool:
    """True se estamos dentro das 2 primeiras semanas (modo observador)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    elapsed = (now - FASE3_TASK4_START).total_seconds()
    return 0 <= elapsed < OBSERVER_MODE_WEEKS * 7 * 86400


def _log_unverified(tool: str, task_id: str, reason: str) -> None:
    """Grava evento TURN_UNVERIFIED no activity log."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] TURN_UNVERIFIED: task_id={task_id} tool={tool} reason={reason}\n"
    try:
        _ACTIVITY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _ACTIVITY_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:
        log.warning("could not write TURN_UNVERIFIED to activity log: %s", exc)


def _has_material_proof(payload: Any) -> bool:
    """Verifica se o payload carrega prova material (recibo HMAC + verificação).

    A prova material é composta por:
      1. ``call_id`` — identificador do recibo HMAC
      2. ``receipt_hmac`` — o HMAC do recibo (prova de integridade)
      3. ``verified`` — resultado da verificação externa (True)

    Quando o payload é dict, verifica no próprio dict. Quando é lista,
    verifica se todos os fatos carregam prova.
    """
    if isinstance(payload, dict):
        return _fact_has_proof(payload)
    if isinstance(payload, (list, tuple)):
        facts = [f for f in payload if isinstance(f, dict)]
        if not facts:
            return False
        return all(_fact_has_proof(f) for f in facts)
    return False


def _fact_has_proof(fact: dict) -> bool:
    """Um fato individual tem prova material quando carrega call_id + receipt_hmac + verified."""
    call_id = fact.get("call_id")
    receipt_hmac = fact.get("receipt_hmac")
    verified = fact.get("verified")
    return bool(call_id and receipt_hmac and verified is True)


def _tier_has_side_effects(tier: str) -> bool:
    """True se o risk_tier indica ação com efeito colateral (>= 'low')."""
    return tier in ("low", "medium", "high")


def _get_action_tier(tool: str) -> str:
    """Retorna o risk_tier da ferramenta. Desconhecido = 'high' (conservador).
    
    Ferramentas readonly (hora, runtime_status) são tratadas como 'safe'
    porque não têm efeito colateral.
    """
    tool_str = str(tool or "").strip()
    # Ferramentas readonly não têm efeito colateral.
    if tool_str in ("hora", "runtime_status"):
        return "safe"
    try:
        from core.tool_bridge import ACTION_TIERS
        return ACTION_TIERS.get(tool_str, "high")
    except Exception:
        return "high"


def _tool_to_scope(tool: str) -> Any:
    """Mapeia nome de ferramenta para escopo de grant (Scope ou None).
    
    Retorna None se a ferramenta não tem escopo de grant associado.
    Importa voice_grants lazy para evitar ciclo de dependência.
    """
    from core import voice_grants as vg
    
    tool_lower = str(tool or "").lower()
    if "whatsapp" in tool_lower:
        return vg.Scope(vg.SCOPE_WHATSAPP)
    if "email" in tool_lower or "mail" in tool_lower:
        return vg.Scope(vg.SCOPE_EMAIL)
    if tool_lower in ("send_message",):
        return vg.Scope(vg.SCOPE_WHATSAPP)
    # Projeto: precisa de args com project/project_path
    return None


def readonly_runner(tool: str, args: dict[str, Any] | None = None) -> Any:
    """Runner real de leitura: não muda estado, não pede rede, não mente.

    Existe para o portão de ferramenta ter pelo menos uma ferramenta REAL
    atrás dele (``hora`` e ``runtime_status``). Qualquer outra ferramenta é
    recusada aqui — o portão e o catálogo já recusariam antes, mas o runner
    também não finge. Ferramentas que mudam estado continuam no app/Qoder.
    """
    tool = str(tool or "")
    if tool == "hora":
        import datetime as _dt
        now = _dt.datetime.now()
        return {"ok": True, "spoken": f"são {now.strftime('%H:%M')}",
                "detail": "relógio local"}
    if tool == "runtime_status":
        import os as _os
        return {"ok": True,
                "spoken": f"o assistente está de pé (pid {_os.getpid()})",
                "detail": f"pid={_os.getpid()}"}
    return {"ok": False, "spoken": "essa ferramenta não é de leitura",
            "detail": f"readonly_runner recusou {tool}"}


def default_catalog() -> set[str]:
    """Catálogo de ferramentas conhecidas (sem importar módulos de ação)."""
    names: set[str] = set()
    try:
        from core.tool_bridge import ACTION_TIERS  # import barato, sem Qt
        names.update(str(k) for k in ACTION_TIERS)
    except Exception:
        pass
    names.update({
        "hora", "media_control", "open_app", "reminder", "restart_assistant",
        "open_in", "memory_index", "tasks", "runtime_status", "system_monitor",
        "web_search", "weather_report", "send_message", "email_control",
        "whatsapp_control", "home_control", "computer_settings",
    })
    return names


@dataclass
class ExecutorResult:
    task_id: str
    ok: bool = False
    cancelled: bool = False
    pending_confirm: bool = False
    unverified: bool = False
    facts: list[dict[str, Any]] = field(default_factory=list)
    ms: float = 0.0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "ok": self.ok,
            "cancelled": self.cancelled,
            "pending_confirm": self.pending_confirm,
            "unverified": self.unverified,
            "facts": self.facts, "ms": round(self.ms, 1),
            "error": self.error,
        }


def pending_phrase(tool: str, args: dict[str, Any] | None = None) -> str:
    args = args or {}
    if tool == "restart_assistant":
        return "confirmo reiniciar o assistente"
    if tool in ("send_message", "whatsapp_control", "email_control",
                "mail_control"):
        target = str(args.get("to") or args.get("recipient")
                     or args.get("contact") or "").strip()
        return f"confirmo enviar mensagem para {target}".strip()
    if tool.startswith("restart") or tool in ("power", "shutdown"):
        return "confirmo reiniciar o computador"
    return f"confirmo executar {tool}".strip()


class Executor:
    def __init__(self, runner: Runner | None = None,
                 catalog: set[str] | None = None,
                 timeout_ms: int = 4000,
                 enabled: bool = True):
        self.runner = runner
        self.catalog = set(catalog) if catalog is not None else default_catalog()
        self.timeout_ms = max(100, int(timeout_ms))
        self.enabled = bool(enabled)

    def knows(self, tool: str) -> bool:
        return str(tool) in self.catalog

    def _actiongate_or_grant_allows(self, tool: str, args: dict[str, Any],
                                     *, person: str = "",
                                     now: float | None = None) -> bool:
        """True se actiongate (janela do dono) OU voice_grants permite a ação.
        
        Ordem de consulta:
          1. actiongate.authorized(): janela de 120s do dono aberta -> True
          2. voice_grants.allows(person, scope): grant ativo para a pessoa -> True
          3. Senão -> False (pede confirmação)
        """
        # 1) actiongate: janela do dono aberta
        try:
            from core import actiongate
            if actiongate.authorized():
                return True
        except Exception:
            pass  # actiongate indisponível: segue para grants
        
        # 2) voice_grants: grant persistente por pessoa
        person = str(person or "").strip()
        if not person:
            return False  # sem pessoa identificada: não tem grant
        
        try:
            from core import voice_grants as vg
            scope = _tool_to_scope(tool)
            if scope is None:
                return False  # ferramenta sem escopo de grant
            moment = time.monotonic() if now is None else float(now)
            return vg.allows(person, scope, now=moment)
        except Exception:
            return False  # grants indisponível: nega

    def run(self, task_id: str, tool: str, args: dict[str, Any] | None = None,
            *, requires_confirm: bool = False,
            cancel_event: Event | None = None,
            person: str = "",
            now: float | None = None) -> ExecutorResult:
        started = time.monotonic()
        args = dict(args or {})
        result = ExecutorResult(task_id=str(task_id))

        def finish() -> ExecutorResult:
            result.ms = (time.monotonic() - started) * 1000.0
            return result

        if cancel_event is not None and cancel_event.is_set():
            result.cancelled = True
            result.facts = [{
                "spoken": "nada foi executado; o pedido foi cancelado",
                "ok": False, "assertion": False, "simulated": False,
                "cancelled": True, "detail": "cancel_event",
            }]
            return finish()

        if not self.knows(tool):
            result.error = "ferramenta fora do catálogo"
            result.facts = [{
                "spoken": "a ferramenta não existe; nada foi executado",
                "ok": False, "assertion": False, "simulated": False,
                "detail": result.error,
            }]
            return finish()

        if requires_confirm:
            # FASE1-TASK3: consulta grants ANTES de exigir confirmação.
            # 1) actiongate (janela de 120s do dono): se aberta, executa.
            # 2) voice_grants (grant persistente por pessoa): se ativo, executa.
            # 3) Senão, pede confirmação (pending_confirm).
            if self._actiongate_or_grant_allows(tool, args, person=person,
                                                 now=now):
                # Janela do dono ou grant ativo: prossegue para execução.
                pass
            else:
                # Lei (b): não executa, só prepara e pede a frase falada.
                result.ok = True
                result.pending_confirm = True
                result.facts = [{
                    "spoken": "só falta a sua confirmação falada",
                    "ok": True, "assertion": False, "simulated": False,
                    "pending_confirm": pending_phrase(tool, args),
                    "detail": "pending_confirm; nada foi executado",
                }]
                return finish()

        if not self.enabled:
            result.error = "executor desligado por config"
            result.facts = [{
                "spoken": "o executor está desligado; nada foi executado",
                "ok": False, "assertion": False, "simulated": False,
                "detail": result.error,
            }]
            return finish()

        if self.runner is None:
            result.error = "sem runner (stub declarado)"
            result.facts = [{
                "spoken": "não há executor ligado; nada foi executado",
                "ok": False, "assertion": False, "simulated": False,
                "detail": result.error,
            }]
            return finish()

        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="flows-exec")
        try:
            future = pool.submit(self.runner, str(tool), args)
            try:
                payload = future.result(timeout=self.timeout_ms / 1000.0)
            finally:
                # wait=False: um runner travado não segura a resposta ao dono.
                pool.shutdown(wait=False, cancel_futures=True)
        except concurrent.futures.TimeoutError:
            result.error = "timeout do executor"
            result.facts = [{
                "spoken": "a ferramenta demorou demais; não sei o resultado",
                "ok": False, "assertion": False, "simulated": False,
                "detail": result.error,
            }]
            return finish()
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.facts = [{
                "spoken": "a ferramenta falhou; nada foi confirmado",
                "ok": False, "assertion": False, "simulated": False,
                "detail": result.error,
            }]
            return finish()

        if cancel_event is not None and cancel_event.is_set():
            result.cancelled = True
            result.facts = [{
                "spoken": "pedido cancelado; não sei se algo chegou a rodar",
                "ok": False, "assertion": False, "simulated": False,
                "cancelled": True, "detail": "cancel após a chamada",
            }]
            return finish()

        if isinstance(payload, dict):
            ok = bool(payload.get("ok", True))
            simulated = bool(payload.get("simulated", False))
            spoken = str(payload.get("spoken") or "").strip()
            detail = str(payload.get("detail") or "")
            fact = {
                "spoken": spoken or ("ação concluída" if ok else "ação falhou"),
                "ok": ok,
                # Simulação nunca autoriza a boca a afirmar resultado.
                "assertion": bool(ok and not simulated
                                  and not payload.get("pending_confirm")),
                "simulated": simulated,
                "detail": detail,
            }
            # FASE3-TASK4: carrega campos de prova material do payload.
            for proof_key in ("call_id", "receipt_hmac", "verified"):
                if proof_key in payload:
                    fact[proof_key] = payload[proof_key]
            result.ok = ok
            result.facts = [fact]
            # FASE3-TASK4: verifica prova material para ações com efeito colateral.
            self._check_material_proof(result, str(tool), task_id)
            return finish()
        if isinstance(payload, (list, tuple)):
            facts = [f for f in payload if isinstance(f, dict)]
            for fact in facts:
                fact["assertion"] = bool(
                    fact.get("ok") and not fact.get("simulated")
                    and not fact.get("pending_confirm"))
            result.ok = all(bool(f.get("ok")) for f in facts)
            result.facts = facts
            # FASE3-TASK4: verifica prova material para ações com efeito colateral.
            self._check_material_proof(result, str(tool), task_id)
            return finish()

        result.error = "runner devolveu formato inesperado"
        result.facts = [{
            "spoken": "a ferramenta respondeu num formato que não entendo",
            "ok": False, "assertion": False, "simulated": False,
            "detail": result.error,
        }]
        return finish()

    def _check_material_proof(self, result: ExecutorResult, tool: str,
                               task_id: str) -> None:
        """FASE3-TASK4: verifica prova material para ações com efeito colateral.

        Toda ação com risk_tier >= "low" exige prova material (recibo HMAC +
        verificação externa). Sem prova material, o resultado é marcado como
        "unverified" e o guard rejeita a afirmação de resultado.

        Modo observador (primeiras 2 semanas): loga mas não bloqueia.
        """
        tier = _get_action_tier(tool)
        if not _tier_has_side_effects(tier):
            return  # safe: sem efeito colateral, não exige prova

        # Verifica se algum fato carrega prova material.
        has_proof = any(_fact_has_proof(f) for f in result.facts if isinstance(f, dict))
        if has_proof:
            return  # prova material presente: prossegue normalmente

        # Sem prova material: marca como unverified.
        result.unverified = True
        # Invalida asserções (o guard rejeita afirmação sem prova).
        for fact in result.facts:
            if isinstance(fact, dict) and fact.get("assertion"):
                fact["assertion"] = False
                fact["unverified"] = True

        # Loga o evento TURN_UNVERIFIED.
        _log_unverified(tool, str(task_id), "sem prova material (recibo HMAC + verificação)")

        # Modo observador: nas primeiras 2 semanas, loga mas não bloqueia.
        # Após esse período, o resultado unverified é retornado e o guard rejeita.
        observer = _is_observer_mode()
        if observer:
            # Restaura asserções (modo observador não bloqueia).
            for fact in result.facts:
                if isinstance(fact, dict) and fact.get("unverified"):
                    fact["assertion"] = True
            log.info("[Executor] OBSERVER: ação sem prova material tool=%s task=%s",
                     tool, task_id)
