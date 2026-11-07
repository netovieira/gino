"""UC-1 — o orquestrador consulta a memória de verdade antes do cérebro.

O defeito original: ``lookups`` saía sempre vazio no ``[Fluxos]`` porque a
consulta nunca era executada — o contexto só *nomeava* o lookup. Estes testes
travam o contrato do FALA passo 3 / RESPOSTA passo 1: existe um provedor, ele
é chamado, o resultado vira contexto do cérebro e fica registrado como
``memory_loaded``. E o portão da boca (passo 2, <1 s) continua sem esperar
por essa busca.
"""
from __future__ import annotations

import ast
import threading
import time

from core.flows.config import FlowsConfig
from core.flows.orchestrator import Orchestrator


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _config(**kw):
    base = dict(enabled=True, mouth_enabled=False, partial_prewarm=False,
                executor_enabled=False, log_events=True)
    base.update(kw)
    return FlowsConfig(**base)


class Provider:
    """Provedor fake com a mesma assinatura injetada no vivo (main.py)."""

    def __init__(self, payload=None, gate=None):
        self.calls: list[dict] = []
        self.payload = payload if payload is not None else {
            "context": "- o dono pediu a musica do TheroTV ontem",
            "topics": ["therotv", "musica"],
            "hits": 1,
        }
        self.gate = gate

    def __call__(self, query, *, hours=6.0, limit=8):
        self.calls.append({"query": str(query), "hours": hours,
                           "limit": limit})
        if self.gate is not None:
            self.gate.wait(timeout=5.0)
        return dict(self.payload)


def _orchestrator(config=None, memory=None, events=None):
    return Orchestrator(config or _config(), clock=Clock(), memory=memory,
                        log=(events.append if events is not None
                             else (lambda _m: None)))


def _events(lines: list[str]) -> list[dict]:
    out = []
    for line in lines:
        if not line.startswith("[Fluxos] "):
            continue
        try:
            out.append(ast.literal_eval(line[len("[Fluxos] "):]))
        except (ValueError, SyntaxError):
            continue
    return out


def _one(lines: list[str], kind: str) -> dict:
    found = [e for e in _events(lines) if e.get("type") == kind]
    assert found, f"sem evento {kind} em {lines}"
    return found[-1]


# ── o lookup é executado, não só nomeado ───────────────────────────────────
def test_turno_de_passado_dispara_a_busca_do_indice():
    prov = Provider()
    orch = _orchestrator(memory=prov)
    plan = orch.begin("t1", "o que eu pedi ontem sobre a musica do thero")

    assert plan.memory_wait.wait(timeout=2.0)
    assert prov.calls, "o cerebro nao consultou o indice (defeito da UC-1)"
    assert plan.lookups_run == ["memory"]
    # A pergunta chega ao índice: o lookup não é uma promessa vazia.
    assert "musica" in prov.calls[0]["query"]
    assert plan.memory_hits == 1
    assert plan.memory_topics == ["therotv", "musica"]
    assert "musica do TheroTV" in plan.memory_context


def test_rota_livre_tambem_carrega_memoria_automatica():
    """Passo 1 do RESPOSTA: conversa sem regra de consulta também consulta."""
    prov = Provider()
    orch = _orchestrator(memory=prov)
    plan = orch.begin("t1", "como ficou aquela historia do projeto?")
    assert plan.memory_wait.wait(timeout=2.0)
    assert plan.lookups_run == ["memory(auto)"]
    assert len(prov.calls) == 1
    assert plan.memory_context


def test_evento_memory_loaded_registra_o_que_foi_carregado():
    lines: list[str] = []
    prov = Provider()
    orch = _orchestrator(memory=prov, events=lines)
    orch.begin("t1", "o que eu pedi ontem sobre a musica do thero")
    assert prov.calls and prov.calls[0]["limit"] == orch.config.memory_limit
    assert prov.calls[0]["hours"] == orch.config.memory_window_hours
    payload = _one(lines, "memory_loaded")
    assert payload["args"] == ["t1"]
    assert "memory" in payload["lookups"]
    assert payload["topics"] == ["therotv", "musica"]
    assert payload["hits"] == 1
    assert payload["chars"] == len(prov.payload["context"])
    assert payload["ms"] >= 0.0


# ── o portão da boca (<1 s) não pode pagar a consulta ──────────────────────
def test_boca_nao_espera_a_memoria():
    """FALA passo 2: ``begin`` volta com a busca ainda no ar."""
    gate = threading.Event()
    prov = Provider(gate=gate)
    orch = _orchestrator(memory=prov)
    started = time.monotonic()
    plan = orch.begin("t1", "o que voce lembra da musica do thero")
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"begin esperou a memoria: {elapsed:.3f}s"
    assert prov.calls, "a busca comecou"
    assert not plan.memory_wait.is_set(), "a busca ainda esta travada"
    gate.set()
    assert plan.memory_wait.wait(timeout=2.0)


def test_celebro_colhe_o_contexto():
    prov = Provider(payload={"context": "- lembrete de ontem",
                             "topics": ["geral"], "hits": 1})
    orch = _orchestrator(memory=prov)
    plan = orch.begin("t1", "o que voce lembra da musica do thero")
    assert plan.memory_wait.wait(timeout=2.0)
    assert orch.memory_context("t1") == "- lembrete de ontem"
    assert orch.memory_context("turno-inexistente") == ""


# ── sem prova material, nada é fingido ────────────────────────────────────
def test_sem_provedor_nada_e_fingido():
    lines: list[str] = []
    orch = _orchestrator(memory=None, events=lines)
    plan = orch.begin("t1", "o que voce lembra da musica do thero")
    assert plan.memory_wait.wait(timeout=2.0)
    assert plan.memory_context == ""
    assert plan.memory_hits == 0
    payload = _one(lines, "memory_loaded")
    assert payload.get("skipped") == "sem provedor de memoria"
    assert payload["lookups"] == []


def test_provedor_vazio_nao_inventa_contexto():
    prov = Provider(payload={})
    orch = _orchestrator(memory=prov)
    plan = orch.begin("t1", "o que voce lembra da musica do thero")
    assert plan.memory_wait.wait(timeout=2.0)
    assert prov.calls
    assert plan.memory_context == ""
    assert plan.memory_hits == 0


def test_provedor_que_quebra_nao_derruba_o_turno():
    def explode(query, *, hours=6.0, limit=8):
        raise RuntimeError("indice corrompido")

    lines: list[str] = []
    orch = _orchestrator(memory=explode, events=lines)
    plan = orch.begin("t1", "o que voce lembra da musica do thero")
    assert plan.memory_wait.wait(timeout=2.0)
    assert plan.memory_context == ""
    assert "RuntimeError" in plan.memory_failed
    payload = _one(lines, "memory_loaded")
    assert "indice corrompido" in payload["error"]
    # E o turno continua vivo: a busca não pode matar o roteamento.
    assert plan.active and not plan.blocked


def test_lookup_de_hora_deixa_de_ser_so_nome():
    """``Lookup("time")`` existia no roteador e nunca foi executado."""
    prov = Provider()
    orch = _orchestrator(memory=prov)
    plan = orch.begin("t1", "que horas sao")
    assert plan.memory_wait.wait(timeout=2.0)
    assert "time" in plan.lookups_run
    assert "[hora agora]" in plan.memory_context


def test_memoria_desligada_nao_consulta():
    prov = Provider()
    orch = _orchestrator(config=_config(memory_enabled=False), memory=prov)
    plan = orch.begin("t1", "o que voce lembra da musica do thero")
    assert plan.memory_wait.is_set()
    assert prov.calls == []
    assert orch.memory_context("t1") == ""


def test_turno_bloqueado_nao_consulta_memoria():
    prov = Provider()
    orch = _orchestrator(memory=prov)
    plan = orch.begin("t1", "o que voce lembra da musica do thero",
                      trust="untrusted")
    assert plan.blocked
    assert prov.calls == []
