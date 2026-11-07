"""UC-5 — ``owner_decision`` responde sem oferecer auditoria da fala do dono.

Aceitação da task: reclamação do dono -> defeito aberto, ZERO verificação.
Estes testes travam a superfície exposta em ``core.owner_decision`` (a porta
única de "quem falou") e a ausência de qualquer caminho de ``verificar_fala``.
"""
from __future__ import annotations

import json

import pytest

from core import owner_decision as od
from core import owner_law as law


@pytest.fixture(autouse=True)
def _defeito_em_tmp(tmp_path):
    path = tmp_path / "abertos.jsonl"
    law.configure(path=path, log=lambda _m: None)
    yield path
    law.configure(path=law.DEFAULT_DEFECT_PATH, log=lambda _m: None)


def test_owner_decision_exporta_a_regra():
    """A lei está exposta onde a decisão de "é o dono?" já vive."""
    for nome in ("record_owner_complaint", "open_defect",
                 "blocks_owner_speech_audit", "owner_speech_verdict"):
        assert hasattr(od, nome), nome
    assert od.record_owner_complaint is law.record_owner_complaint


def test_reclamacao_do_dono_abre_defeito_sem_verificacao(_defeito_em_tmp):
    rec = od.record_owner_complaint("isso não funciona desde ontem")
    assert rec is not None and rec["status"] == "aberto"
    assert rec["route"] == "abrir_defeito"
    assert _defeito_em_tmp.exists()
    assert len(_defeito_em_tmp.read_text(encoding="utf-8").splitlines()) == 1


def test_veredito_nao_menciona_verificar_fala(_defeito_em_tmp):
    v = od.owner_speech_verdict("você parou de responder")
    serializado = json.dumps(v, ensure_ascii=False)
    assert v["audit"] == "blocked"
    assert v["action"] == "abrir_defeito"
    # Prova negativa: a palavra "verificar_fala" não aparece em lugar nenhum.
    assert "verificar_fala" not in serializado


def test_caminho_de_audit_sobre_a_fala_dele_e_bloqueado():
    bloqueio = od.blocks_owner_speech_audit(
        "grep", {"pattern": "confirmar se ele disse",
                 "path": "logs/activity.log"})
    assert bloqueio and "bloqueado" in bloqueio


@pytest.mark.parametrize("tool,args", [
    ("memory_search", {"query": "música queen"}),
    ("media_control", {"action": "play", "query": "Queen"}),
    ("open_app", {"app_name": "terminal"}),
    ("action_verifier", {"receipt": "abc"}),
])
def test_nada_mais_e_bloqueado(tool, args):
    """O bloqueio é cirúrgico: não pode virar portão genérico de ferramenta."""
    assert od.blocks_owner_speech_audit(tool, args) == ""


def test_fala_normal_do_dono_nao_abre_defeito(_defeito_em_tmp):
    assert od.record_owner_complaint("põe a música do Queen") is None
    assert not _defeito_em_tmp.exists()


def test_reclamacao_de_visitante_nao_abre_defeito(_defeito_em_tmp):
    """Lei (c): o que o outro diz é dado, não ordem."""
    assert od.record_owner_complaint("isso não funciona",
                                     who="visitante") is None
    assert not _defeito_em_tmp.exists()
