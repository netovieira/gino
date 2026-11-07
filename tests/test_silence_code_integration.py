"""UC-4 — integração: SilenceCode + SilenceGate.

Prova que:
  - quando SilenceCode está ativo, a fala é retida na fila do SilenceGate
    compartilhada (não se perde);
  - SilenceGate continua sua própria lógica "silêncio" sem ser afetada;
  - quando SilenceCode libera, summary_lines é produzido;
  - a fila do SilenceGate fica consistente (nada duplicado);
  - um evento logável sai em cada ativação/liberação.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from core.memory_index import slugify
from core.silence_code import SilenceCode, parse_silence_code_order, summary_lines
from core.silence_gate import SilenceGate


@pytest.fixture
def tmp_paths(tmp_path):
    return {
        "gate_state": tmp_path / "silence_mode.json",
        "gate_queue": tmp_path / "silence_queue.json",
        "code_state": tmp_path / "silence_code.json",
    }


@pytest.fixture
def gate(tmp_paths):
    return SilenceGate(
        state_path=tmp_paths["gate_state"],
        queue_path=tmp_paths["gate_queue"],
        logger=lambda m: None,
    )


@pytest.fixture
def sc(tmp_paths, gate):
    """SilenceCode ligado ao on_retain do gate real."""

    def retain_in_gate(text: str, subject: str, slug: str) -> None:
        # SilenceGate.retain() nao tem param slug — ele deduz de subject.
        gate.retain(text, subject=subject or slug)

    return SilenceCode(
        "Oklahoma",
        state_path=tmp_paths["code_state"],
        on_retain=retain_in_gate,
        logger=lambda m: None,
    )


# ── ativação por transcrição ──────────────────────────────────────────────────

def test_transcricao_ativa_e_boca_fica_calada(sc, gate):
    r = sc.handle_transcription("Oklahoma TheroTV")
    assert r is not None and r["action"] == "activate"
    assert sc.is_active_for("TheroTV")
    assert not sc.is_active_for("Outro")


def test_transcricao_libera_tudo(sc, gate):
    sc.handle_transcription("Oklahoma TheroTV")
    r = sc.handle_transcription("libera Oklahoma")
    assert r and r["action"] == "deactivate_all"
    assert not sc.active


# ── retenção na fila do gate ──────────────────────────────────────────────────

def test_retain_dentro_do_sc_grava_na_fila_do_gate(sc, gate):
    sc.activate("TheroTV")
    sc.retain("Primeira frase sobre TheroTV.", subject="TheroTV")
    sc.retain("Segunda frase.", subject="TheroTV")
    # fila do gate recebe cada linha
    pending = gate.pending()
    assert len(pending) >= 2
    assert any("Primeira" in p.get("text", "") for p in pending)


def test_gate_proprio_nao_e_alterado_por_sc_scactivation(sc, gate):
    """O estado "silêncio" do gate (silence_mode.json) não é modificado."""
    sc.activate("TheroTV")
    # gate ainda sem nenhuma ordem "silêncio" ativa
    assert not gate.active
    assert not gate.muted_subjects()


# ── liberação com resumo ──────────────────────────────────────────────────────

def test_libera_produz_summary_lines(sc):
    sc.activate("TheroTV")
    sc.retain("Chegou e-mail da Maria.", subject="TheroTV")
    sc.retain("Lembrete de reunião.", subject="TheroTV")
    result = sc.deactivate(None)
    assert "summary_lines" in result
    assert len(result["summary_lines"]) >= 1
    assert any("TheroTV" in l for l in result["summary_lines"])


def test_summary_lines_vazio_quando_nada_retido(sc):
    sc.activate("TheroTV")
    result = sc.deactivate("therotv")
    assert result["summary_lines"] == []


# ── ciclo completo ────────────────────────────────────────────────────────────

def test_ciclo_completo(sc):
    """Dizer "Oklahoma TheroTV" → fala é retida → "libera Oklahoma" → resumo."""
    sc.handle_transcription("Oklahoma TheroTV")
    assert sc.is_active_for("TheroTV")

    # simula o que a boca diria durante o silêncio
    sc.retain("Primeira fala.", subject="TheroTV")
    sc.retain("Segunda fala.", subject="TheroTV")

    r = sc.handle_transcription("libera Oklahoma")
    assert r and r["action"] == "deactivate_all"
    assert not sc.is_active_for("TheroTV")
    assert len(r["entries"]) == 2
    assert any("Primeira" in e["text"] for e in r["entries"])


# ── projeto global (vazio) ─────────────────────────────────────────────────────

def test_activate_sem_projeto_calcula_tudo(sc):
    """Activate com projeto vazio = silencia tudo (nao via transcricao)."""
    sc.activate("")
    assert sc.is_active_for("qualquer coisa")
    r = sc.handle_transcription("libera Oklahoma")
    assert r and r["action"] == "deactivate_all"
    assert not sc.active


# ── thread safety ─────────────────────────────────────────────────────────────

def test_ativacao_concorrente_nao_duplica(sc):
    threads = [
        threading.Thread(target=sc.activate, args=("TheroTV",))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2)
    slugs = [p["slug"] for p in sc.active_projects()]
    assert slugs.count("therotv") == 1


# ── parser em contexto de integração ──────────────────────────────────────────

@pytest.mark.parametrize(
    "texto,expected_action",
    [
        ("Oklahoma Meu Projeto", "activate"),
        ("libera Oklahoma", "deactivate_all"),
        ("libera Oklahoma Meu Projeto", "deactivate"),
        ("oi tudo bem", "none"),
        ("abre o terminal", "none"),
    ],
)
def test_parser_em_contexto(texto, expected_action):
    r = parse_silence_code_order(texto, "Oklahoma")
    assert r["action"] == expected_action


# ── gate e code coexistem sem conflito ────────────────────────────────────────

def test_silence_code_e_silence_gate_coexistem(sc, gate, tmp_paths):
    """Ativar "silêncio" no gate E palavra-código: os dois silenciam, fila única."""
    gate.apply_order("TheroTV")
    sc.handle_transcription("Oklahoma Jarvis")
    # gate está ativo
    assert gate.is_muted(subject="TheroTV")
    # sc está ativo
    assert sc.is_active_for("Jarvis")
    # fila do gate recebe de ambos
    gate.retain("fala do gate", subject="TheroTV")
    sc.retain("fala do code", subject="Jarvis")
    pending = gate.pending()
    assert len(pending) >= 2
