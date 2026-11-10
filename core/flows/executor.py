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

PROVA MATERIAL — BLOQUEIO PÓS-OBSERVAÇÃO (UP-9)
    Terminado o período de observação (29/11/2026), a ação com efeito
    colateral que devolve resultado SEM recibo HMAC deixa de ser apenas
    "unverified": ela é BLOQUEADA. O bloqueio é um portão temporal
    (``_blocking_enabled``) — antes de 29/11 nada muda, o resultado continua
    sendo só marcado. Depois, o executor:
      1. marca ``result.blocked`` e devolve ``ok=False`` com erro declarado;
      2. grava o evento ``execution_blocked`` no activity log;
      3. notifica o dono (core/actiongate — o registro de execuções
         bloqueadas, de onde o app pega o aviso).
    Bloquear não desfaz o efeito que já aconteceu: bloquear é recusar o
    resultado como fato — o cérebro e a boca nunca afirmam o que não tem
    prova.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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

# ── UP-9: gate temporal do bloqueio ────────────────────────────────────────
#
# O bloqueio efetivo (recusar o resultado, gravar ``execution_blocked`` e
# avisar o dono) entra em vigor quando o período de observação termina:
# FASE3_TASK4_START + OBSERVER_MODE_WEEKS semanas = 29/11/2026. Até lá o
# comportamento é exatamente o de sempre — o código existe, o portão fica
# fechado. Um resultado bloqueado nunca é entregue como fato; desfazer o
# efeito que já aconteceu não está nas mãos do executor.

MATERIAL_PROOF_BLOCK_START = (
    FASE3_TASK4_START + timedelta(weeks=OBSERVER_MODE_WEEKS))

_ACTIVITY_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "activity.log"

# Motivo canônico do bloqueio — aparece no log e no aviso ao dono.
BLOCK_REASON = "ação sem prova material (recibo HMAC + verificação externa)"


def _as_utc(moment: datetime) -> datetime:
    """Normaliza para UTC sem fuso (datas ingênuas viram UTC)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _is_observer_mode(now: datetime | None = None) -> bool:
    """True se estamos dentro das 2 primeiras semanas (modo observador)."""
    now = _as_utc(now or datetime.now(timezone.utc))
    elapsed = (now - FASE3_TASK4_START).total_seconds()
    return 0 <= elapsed < OBSERVER_MODE_WEEKS * 7 * 86400


def _blocking_enabled(now: datetime | None = None) -> bool:
    """UP-9: True somente a partir de 29/11/2026 — portão do bloqueio.

    Antes do fim do período de observação o bloqueio não existe: nem
    resultado recusado, nem ``execution_blocked``, nem aviso ao dono. É o
    gate que permite preparar o bloqueio agora e ativá-lo na data.
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    return now >= MATERIAL_PROOF_BLOCK_START


def _append_activity(event: str, task_id: str, tool: str,
                     reason: str, origin: str = "") -> None:
    """Escreve ``[ts] EVENTO: task_id=.. tool=.. reason=.. [origin=..]``.

    UV-4: quando há origem declarada ela fecha a linha — toda ação de
    dispositivo fica rastreável no activity.log sem mudar o formato antigo
    para os leitores de sempre (o sufixo é opcional).
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {event}: task_id={task_id} tool={tool} reason={reason}"
    if origin:
        line += f" origin={origin}"
    line += "\n"
    try:
        _ACTIVITY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _ACTIVITY_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:
        log.warning("could not write %s to activity log: %s", event, exc)


def _log_unverified(tool: str, task_id: str, reason: str) -> None:
    """Grava evento TURN_UNVERIFIED no activity log."""
    _append_activity("TURN_UNVERIFIED", task_id, tool, reason)


def _log_execution_blocked(tool: str, task_id: str, reason: str) -> None:
    """UP-9: grava evento ``execution_blocked`` no activity log."""
    _append_activity("execution_blocked", task_id, tool, reason)


# ── UV-4: origem de toda ação de dispositivo ───────────────────────────────
#
# A política de origem mora em core/actiongate.py (vocabulário canônico,
# sensibilidade e o motivo do bloqueio). O executor é o portão de despacho
# dos fluxos: ele DECLARA a origem no resultado, recusa origem explícita
# ``automatica`` em dispositivo sensível sem nunca chamar o runner, e grava
# cada ação com efeito colateral no activity.log com o rótulo — rastreável
# mesmo quando o actiongate estiver indisponível (aí vale o vocabulário
# mínimo local; ausência de portão não apaga o rastro).

_FALLBACK_ORIGIN_UNDECLARED = "nao_declarada"


def _normalize_origin(value: Any) -> str:
    """Rótulo de origem -> nome canônico (actiongate; fallback conservador)."""
    try:
        from core import actiongate
        return actiongate.normalize_origin(value)
    except Exception:
        folded = " ".join(str(value or "").split()).strip().lower()
        if folded in ("fala_do_dono", "fala_visitante", "automatica",
                      "filtro_email"):
            return folded
        return folded if folded else _FALLBACK_ORIGIN_UNDECLARED


def _origin_block_reason(tool: str, origin: str,
                         args: dict[str, Any] | None = None) -> str:
    """Motivo do bloqueio UV-4 (ou ''). Sem actiongate, não se inventa
    bloqueio: a política é uma só e vive no portão de ações."""
    try:
        from core import actiongate
        return actiongate.origin_block_reason(tool, origin, args)
    except Exception:
        return ""


def _notify_owner_blocked(tool: str, task_id: str, reason: str) -> dict:
    """UP-9: notifica o dono de que uma execução foi bloqueada.

    O canal é o portão de ações (core/actiongate): o registro fica vivo até o
    app colhê-lo e é entregue a quem registrou ouvinte (a fala do dono, o
    painel, o WhatsApp — quem injeta é o processo vivo). Sem actiongate, ou
    com ele quebrado, o rastro continua no log: nada de silenciar um bloqueio.
    """
    payload = {
        "kind": "execution_blocked",
        "tool": str(tool or ""),
        "task_id": str(task_id or ""),
        "reason": str(reason or ""),
        "message": (f"A execução de {tool or 'ferramenta'} foi bloqueada: "
                    f"{reason or BLOCK_REASON}"),
    }
    try:
        from core import actiongate
        actiongate.note_blocked(payload)
    except Exception as exc:
        log.warning("could not route the blocked-execution notice: %s", exc)
    log.warning("[Executor] ACAO BLOQUEADA (UP-9): tool=%s task=%s reason=%s",
                tool, task_id, reason)
    return payload


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
    blocked: bool = False
    facts: list[dict[str, Any]] = field(default_factory=list)
    ms: float = 0.0
    error: str = ""
    # UV-4: origem canônica do despacho (fala_do_dono, fala_visitante,
    # automatica, filtro_email ou nao_declarada). Campo de rastro: nunca é
    # inferido depois — é o que quem pediu declarou no ato.
    origin: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "ok": self.ok,
            "cancelled": self.cancelled,
            "pending_confirm": self.pending_confirm,
            "unverified": self.unverified,
            "blocked": self.blocked,
            "facts": self.facts, "ms": round(self.ms, 1),
            "error": self.error,
            "origin": self.origin,
        }


def pending_phrase(tool: str, args: dict[str, Any] | None = None) -> str:
    args = args or {}
    if tool == "restart_assistant":
        # Dono, 10/11/2026: o reinício do próprio processo não tem frase
        # (poder próprio). Quando a lane de regras ainda parka um reinício,
        # a frase que falta é a do alvo realmente pedido.
        if str(args.get("action") or "").strip().lower() in (
                "pc", "computer", "computador", "machine", "restart_pc"):
            return "confirmo reiniciar o computador"
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
            now: float | None = None,
            origin: str = "") -> ExecutorResult:
        """UV-4: ``run`` é o portão de despacho dos fluxos.

        A origem declarada por quem pede viaje no resultado (rastreável até o
        activity.log), uma origem EXPLÍCITA ``automatica`` (ou rótulo
        declarado fora do vocabulário) nunca toca dispositivo sensível — o
        runner nem é chamado — e toda ação com efeito colateral fica registrada
        com seu desfecho. Origem não declarada ('') não é bloqueada: é a
        lacuna de rastreamento que a métrica de 24 h precisa mostrar, e o log
        a grava como ``nao_declarada``.
        """
        label = _normalize_origin(origin)
        block = _origin_block_reason(str(tool or ""), origin,
                                     dict(args or {}))
        if block:
            result = self._origin_blocked(task_id, tool, label, block)
        else:
            result = self._run_inner(task_id, tool, args,
                                     requires_confirm=requires_confirm,
                                     cancel_event=cancel_event,
                                     person=person, now=now)
            result.origin = label
        self._record_action(tool, str(task_id), result)
        return result

    def _origin_blocked(self, task_id: str, tool: str, label: str,
                        block: str) -> ExecutorResult:
        """Resultado de ação recusada pela política de origem (nunca roda).

        Diferente do bloqueio UP-9, aqui o efeito NÃO aconteceu: o portão
        fechou antes do runner. O dono é avisado pelo mesmo registro vivo do
        actiongate — é notícia, nunca grant.
        """
        result = ExecutorResult(task_id=str(task_id), origin=label)
        result.blocked = True
        result.error = f"bloqueado: {block}"
        result.facts = [{
            "spoken": "acao automatica bloqueada: dispositivo sensivel so "
                      "age por pedido do dono",
            "ok": False, "assertion": False, "simulated": False,
            "blocked": True, "origin_blocked": True, "detail": block,
        }]
        _append_activity("origin_blocked", str(task_id), str(tool), block,
                         origin=label)
        try:
            from core import actiongate
            actiongate.note_blocked({
                "kind": "origin_blocked",
                "tool": str(tool or ""),
                "task_id": str(task_id or ""),
                "reason": str(block),
                "origin": label,
                "message": (f"A execucao de {tool or 'ferramenta'} foi "
                            f"bloqueada: origem automatica em dispositivo "
                            f"sensivel sem pedido do dono"),
            })
        except Exception as exc:
            log.warning("could not route the origin-blocked notice: %s", exc)
        log.warning("[Executor] ACAO BLOQUEADA (UV-4): tool=%s task=%s "
                    "origin=%s", tool, task_id, label)
        return result

    @staticmethod
    def _record_action(tool: str, task_id: str,
                       result: ExecutorResult) -> None:
        """Uma linha ``device_action`` por ação com efeito colateral (UV-4).

        Ferramenta de leitura não polui o log: não é 'ação de dispositivo' no
        sentido da queixa. O desfecho usa a verdade do portão, na mesma ordem
        do resumo do orquestrador.
        """
        if not _tier_has_side_effects(_get_action_tier(str(tool))):
            return
        state = ("bloqueado" if result.blocked else
                 "cancelado" if result.cancelled else
                 "confirmacao_pendente" if result.pending_confirm else
                 "sem_prova_material" if result.unverified else
                 "ok" if result.ok else "falha")
        _append_activity("device_action", task_id, str(tool),
                         f"state={state}",
                         origin=result.origin or _normalize_origin(""))

    def _run_inner(self, task_id: str, tool: str,
                   args: dict[str, Any] | None = None,
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
                               task_id: str,
                               now: datetime | None = None) -> None:
        """FASE3-TASK4: verifica prova material para ações com efeito colateral.

        Toda ação com risk_tier >= "low" exige prova material (recibo HMAC +
        verificação externa). Sem prova material, o resultado é marcado como
        "unverified" e o guard rejeita a afirmação de resultado.

        Modo observador (primeiras 2 semanas): loga mas não bloqueia.

        UP-9: passado o período de observação (29/11/2026) o resultado sem
        prova material é BLOQUEADO — ``blocked=True``, ``ok=False``, evento
        ``execution_blocked`` no activity log e aviso ao dono.
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
        # Algum fato AFIRMAVA sucesso? Só existe resultado a bloquear quando a
        # ação se vendeu como concluída — uma ferramenta que já respondeu
        # "falhou" não ganha um bloqueio em cima da própria falha.
        claimed_success = any(
            isinstance(fact, dict) and fact.get("assertion")
            for fact in result.facts)
        # Invalida asserções (o guard rejeita afirmação sem prova).
        for fact in result.facts:
            if isinstance(fact, dict) and fact.get("assertion"):
                fact["assertion"] = False
                fact["unverified"] = True

        # Loga o evento TURN_UNVERIFIED.
        _log_unverified(tool, str(task_id), "sem prova material (recibo HMAC + verificação)")

        # Modo observador: nas primeiras 2 semanas, loga mas não bloqueia.
        # Após esse período, o resultado unverified é retornado e o guard rejeita.
        observer = _is_observer_mode(now)
        if observer:
            # Restaura asserções (modo observador não bloqueia).
            for fact in result.facts:
                if isinstance(fact, dict) and fact.get("unverified"):
                    fact["assertion"] = True
            log.info("[Executor] OBSERVER: ação sem prova material tool=%s task=%s",
                     tool, task_id)
            return

        # UP-9: o bloqueio efetivo tem portão temporal. Antes de 29/11/2026
        # nada aqui muda o que sempre aconteceu: o fato continua só marcado.
        if not _blocking_enabled(now) or not claimed_success:
            return

        self._block_without_proof(result, str(tool), str(task_id), tier)

    def _block_without_proof(self, result: ExecutorResult, tool: str,
                              task_id: str, tier: str) -> None:
        """UP-9: recusa o resultado de ação com efeito colateral sem recibo HMAC.

        O efeito pode já ter acontecido — o que se bloqueia é o resultado virar
        fato: ``ok=False``, asserções mortas e um erro declarado. O evento
        ``execution_blocked`` fica no activity log e o dono é avisado pelo
        portão de ações. Silenciar um bloqueio não é opção: se o registro do
        portão falhar, o log ainda guarda o rastro.
        """
        result.blocked = True
        result.ok = False
        result.error = f"bloqueado: {BLOCK_REASON}"
        for fact in result.facts:
            if not isinstance(fact, dict):
                continue
            fact["assertion"] = False
            fact["unverified"] = True
            fact["blocked"] = True
            fact["detail"] = (str(fact.get("detail") or "")
                              + f" | execution_blocked tier={tier}").strip(" |")
            fact["spoken"] = ("a ação rodou sem prova material; "
                              "o resultado foi bloqueado")
        _log_execution_blocked(tool, task_id, BLOCK_REASON)
        _notify_owner_blocked(tool, task_id, BLOCK_REASON)
