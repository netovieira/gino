"""UC-5 — fluxo completo: dono reclama -> defeito aberto no mesmo turno.

Prova negativa pedida pela task: nenhuma linha de "verificando se é verdade"
no caminho. Aqui o log do orquestrador é o log vivo: ele só pode registrar
``owner_defect_opened``, nunca um passo de verificação. E o portão de <1 s da
boca (FALA passo 2) continua de pé: a gravação do defeito é fio aparte.
"""
from __future__ import annotations

import ast
import threading
import time

import pytest

from core import owner_law as law
from core.flows.config import FlowsConfig
from core.flows.orchestrator import Orchestrator
from core.flows.router import Router


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _defeito_em_tmp(tmp_path):
    path = tmp_path / "abertos.jsonl"
    law.configure(path=path, log=lambda _m: None)
    yield path
    law.configure(path=law.DEFAULT_DEFECT_PATH, log=lambda _m: None)


def _orchestrator(events):
    cfg = FlowsConfig(enabled=True, mouth_enabled=False,
                      partial_prewarm=False, executor_enabled=False,
                      memory_enabled=False)
    return Orchestrator(cfg, clock=Clock(), log=events.append)


def _payloads(events):
    out = []
    for line in events:
        if line.startswith("[Fluxos] "):
            try:
                out.append(ast.literal_eval(line[len("[Fluxos] "):]))
            except (ValueError, SyntaxError):
                continue
    return out


# ── roteador: marcador, sem mudar a rota ───────────────────────────────────
def test_rota_continua_a_mesma_o_que_muda_e_o_tratamento():
    """Mudar a rota mexeria no portão de <1 s da boca (FALA passo 2)."""
    d = Router().parse("isso não funciona")
    assert d.owner_complaint is True
    assert d.route == "livre"
    assert d.actions == [] and d.lookups == []


def test_pedido_normal_nao_marca_nada():
    assert Router().parse("abre o terminal").owner_complaint is False


def test_texto_de_terceiro_nao_chega_a_marcar():
    """untrusted morre antes do parse: o roteador nem olha a intenção."""
    d = Router().route("isso não funciona", trust="untrusted")
    assert d.blocked and d.owner_complaint is False


# ── orquestrador: defeito aberto + lei para o cérebro ──────────────────────
def test_turno_de_reclamacao_abre_defeito_e_loga_abertura(_defeito_em_tmp):
    events: list[str] = []
    orch = _orchestrator(events)
    plan = orch.begin("t1", "os lookups tão sempre vazios")

    assert plan.defect_wait.wait(timeout=2.0)
    assert plan.defect["status"] == "aberto"
    assert plan.defect["audit"] == "blocked"
    assert "vazios" in plan.defect["report"]
    assert _defeito_em_tmp.exists()
    assert "owner_defect_opened" in [p["type"] for p in _payloads(events)]
    colhido = orch.owner_defect("t1")
    assert colhido["id"] == plan.defect["id"]


def test_nenhuma_linha_de_verificacao_no_log(_defeito_em_tmp):
    """Prova negativa: zero trilha de auditoria da fala do dono."""
    events: list[str] = []
    orch = _orchestrator(events)
    plan = orch.begin("t1", "você está mentindo sobre o que eu pedi")
    assert plan.defect_wait.wait(timeout=2.0)
    texto = " ".join(events).lower()
    for vedado in ("verificando se", "verificando se é verdade",
                   "confirmar se o dono", "auditar a fala", "grep",
                   "verificar_fala"):
        assert vedado not in texto, vedado
    payload = [p for p in _payloads(events)
               if p["type"] == "owner_defect_opened"][-1]
    assert payload["audit"] == "blocked"


def test_lei_chega_ao_celebro_do_turno(_defeito_em_tmp):
    events: list[str] = []
    orch = _orchestrator(events)
    plan = orch.begin("t1", "não funciona")
    assert "2026-11-05 22:27" in plan.owner_law
    assert orch.owner_law_context("t1") == plan.owner_law
    assert orch.owner_law_context("turno-que-nao-existe") == ""


def test_turno_normal_nao_carrega_lei_nem_defeito(_defeito_em_tmp):
    events: list[str] = []
    orch = _orchestrator(events)
    plan = orch.begin("t1", "bom dia")
    assert plan.defect == {}
    assert plan.owner_law == ""
    assert orch.owner_law_context("t1") == ""
    assert orch.owner_defect("t1") == {}
    assert not _defeito_em_tmp.exists()


def test_gravacao_lenta_nao_segura_o_begin(_defeito_em_tmp, monkeypatch):
    """O disco não pode entrar no portão de 1 s da boca (FALA passo 2)."""
    gate = threading.Event()
    real = law.record_owner_complaint

    def devagar(texto, *, who=law.OWNER):
        gate.wait(timeout=5.0)
        return real(texto, who=who)

    monkeypatch.setattr(law, "record_owner_complaint", devagar)
    orch = _orchestrator([])
    t0 = time.monotonic()
    plan = orch.begin("t1", "isso não funciona")
    esperado = time.monotonic() - t0
    assert esperado < 1.0, f"begin esperou o disco: {esperado:.3f}s"
    # A lei já está no plano (é string pronta); o registro ainda não.
    assert plan.owner_law
    assert plan.defect == {}
    gate.set()
    assert plan.defect_wait.wait(timeout=2.0)
    assert plan.defect["status"] == "aberto"


def test_defeito_que_nao_grava_nao_derruba_o_turno(_defeito_em_tmp,
                                                   monkeypatch):
    events: list[str] = []

    def explode(texto, *, who=law.OWNER):
        raise OSError("disco fora do ar")

    monkeypatch.setattr(law, "record_owner_complaint", explode)
    orch = _orchestrator(events)
    plan = orch.begin("t1", "isso não funciona")
    assert plan.defect_wait.wait(timeout=2.0)
    assert plan.defect == {}
    assert plan.active and not plan.blocked
    tipos = [p["type"] for p in _payloads(events)]
    assert "owner_defect_error" in tipos
