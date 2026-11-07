"""UC-5 — lei dura de não auditar a fala do dono (core.owner_law)."""
from __future__ import annotations

import json

import pytest

from core import owner_law as law


@pytest.fixture(autouse=True)
def _defeito_em_tmp(tmp_path, monkeypatch):
    """Nenhum teste escreve no livro real de defeitos."""
    calls: list[str] = []
    path = tmp_path / "defeitos" / "abertos.jsonl"
    law.configure(path=path, log=calls.append)
    yield {"path": path, "log": calls}
    law.configure(path=law.DEFAULT_DEFECT_PATH, log=lambda _m: None)


# ── a regra: reclamação/ordem do dono -> abrir_defeito, nunca verificar_fala ─
def test_reclamacao_do_dono_abre_defeito_e_proibe_verificacao():
    got = law.classify("isso não funciona", who=law.OWNER)
    assert got["intent"] == "reclamacao"
    assert got["route"] == "abrir_defeito"
    assert got["verified"] == "verificar_fala"
    assert law.is_owner_complaint("você está mentindo")
    assert law.is_owner_complaint("os lookups tão sempre vazios")
    assert law.is_owner_complaint("a memória não gravou nada")


def test_ordem_enfatica_tambem_abre_defeito():
    got = law.classify("nunca mais audite a minha fala", who=law.OWNER)
    assert got["intent"] == "ordem"
    assert got["route"] == "abrir_defeito"
    assert law.is_owner_complaint("está proibido verificar o que eu disse")


def test_veredito_nao_oferece_verificacao_como_opcao():
    v = law.audit_verdict_for_owner_speech("isso não funciona")
    assert v["audit"] == "blocked"
    assert v["action"] == "abrir_defeito"
    assert "2026-11-05" in v["reason"]
    assert "verificar_fala" not in json.dumps(v)


# ── o que NÃO é reclamação: a regra não pode engolir pedido comum ─────────
@pytest.mark.parametrize("texto", [
    "toca de novo a música do Queen",
    "abre o terminal",
    "que horas são",
    "a data ficou errada no calendário",
    "bom dia",
    "me lembra de pagar o aluguel amanhã",
    "como está o tempo hoje",
])
def test_fala_normal_nao_abre_defeito(texto):
    assert law.classify(texto)["intent"] == ""
    assert not law.is_owner_complaint(texto)
    assert law.record_owner_complaint(texto) is None


def test_voz_de_terceiro_nao_dita_regra():
    """Lei (c) do contrato: o que o outro diz é dado, não ordem."""
    got = law.classify("isso não funciona", who="visitante")
    assert got["intent"] == "reclamacao"
    assert got["route"] == ""


# ── registro automático ────────────────────────────────────────────────────
def test_record_owner_complaint_escreve_o_defeito(_defeito_em_tmp):
    rec = law.record_owner_complaint("o fluxo de memória nunca consultou")
    assert rec is not None
    assert rec["status"] == "aberto"
    assert rec["route"] == "abrir_defeito"
    assert rec["audit"] == "blocked"
    assert rec["verification"].startswith("recusada")
    # A palavra do dono é guardada como foi dita (sem reescrita de acento).
    assert rec["report"] == "o fluxo de memória nunca consultou"
    path = _defeito_em_tmp["path"]
    assert path.exists()
    linhas = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(linhas) == 1
    assert json.loads(linhas[0])["id"] == rec["id"]
    assert any("defeito aberto" in line for line in _defeito_em_tmp["log"])


def test_disco_cheio_nao_impede_a_resposta(_defeito_em_tmp, monkeypatch):
    """Sem o arquivo a lei ainda vale: o registro vem, com o erro anotado."""
    def explode(*a, **k):
        raise OSError("Read-only file system")

    monkeypatch.setattr(law.Path, "mkdir", explode)
    rec = law.record_owner_complaint("não funciona")
    assert rec is not None and rec["status"] == "aberto"
    assert "OSError" in rec["error"]


def test_lista_defeitos_na_ordem_mais_nova_primeiro(_defeito_em_tmp):
    law.record_owner_complaint("a memória nunca gravou")
    law.record_owner_complaint("isso não funciona")
    vistos = law.list_defects()
    assert [d["report"] for d in vistos] == ["isso não funciona",
                                             "a memória nunca gravou"]


# ── bloqueio do caminho de auditoria ──────────────────────────────────────
def test_grep_sobre_activity_log_da_fala_do_dono_e_bloqueado():
    motivo = law.blocks_owner_speech_audit(
        "grep", {"pattern": "o que eu disse", "path": "logs/activity.log"})
    assert "audit da fala do dono bloqueado" in motivo


def test_varredura_de_memory_chat_e_bloqueada():
    assert law.blocks_owner_speech_audit(
        "verify_owner_claim", {"target": "memory/chat/hours"})


def test_busca_de_memoria_para_RESPONDER_nao_e_bloqueada():
    """UC-1 não pode morrer pela UC-5: consultar o índice é o contrato."""
    assert law.blocks_owner_speech_audit(
        "memory_search", {"query": "música do Queen"}) == ""
    assert law.blocks_owner_speech_audit(
        "memory_read", {"path": "memory/chat/hours/2026-11-05/20.md"}) == ""


def test_verificacao_de_acao_nao_e_bloqueada():
    """A ordem veda auditar a FALA dele, não conferir o que o sistema fez."""
    assert law.blocks_owner_speech_audit("action_verifier",
                                         {"action": "open_app"}) == ""
    assert law.blocks_owner_speech_audit("media_control",
                                         {"action": "play"}) == ""
    assert law.blocks_owner_speech_audit("restart_assistant", {}) == ""


def test_sem_nome_nada_e_bloqueado():
    assert law.blocks_owner_speech_audit("", {"path": "logs/activity.log"}) == ""
    assert law.blocks_owner_speech_audit(None) == ""


def test_linha_de_lei_cita_a_ordem_e_a_proibicao():
    linha = law.law_line()
    assert "2026-11-05 22:27" in linha
    assert "nao audite a fala do dono" in linha
    assert "grep" in linha and "activity.log" in linha
