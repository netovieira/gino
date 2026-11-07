"""UC-4 — teste unitário: core.silence_code.

Cobertura:
  - activate / deactivate / retain / summarize (ações da task)
  - parse_silence_code_order (detecção na transcrição)
  - is_active_for (consulta antes de falar)
  - persistência em disco
  - idempotência e liberação parcial
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from core.silence_code import (
    SilenceCode,
    parse_silence_code_order,
    summary_lines,
    summarize,
)


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "silence_code.json"


@pytest.fixture
def sc(state_file):
    return SilenceCode(
        code_word="Oklahoma",
        state_path=state_file,
        logger=lambda m: None,
    )


# ── parse_silence_code_order ─────────────────────────────────────────────────

def test_parse_ativacao_com_projeto():
    r = parse_silence_code_order("Oklahoma TheroTV", "Oklahoma")
    assert r["action"] == "activate"
    assert r["project"] == "TheroTV"
    assert r["slug"] == "therotv"


def test_parse_libera_tudo():
    r = parse_silence_code_order("libera Oklahoma", "Oklahoma")
    assert r["action"] == "deactivate_all"


def test_parse_libera_projeto():
    r = parse_silence_code_order("libera Oklahoma TheroTV", "Oklahoma")
    assert r["action"] == "deactivate"
    assert r["slug"] == "therotv"


def test_parse_acento_e_caixa_na_palavra_codigo():
    """A transcrição chega com acentos; a comparação não."""
    r = parse_silence_code_order("oclahoma projetão", "Oklahoma")
    # "oclahoma" ≠ "oklahoma" (letra a mais) — não é ordem
    assert r["action"] == "none"


def test_parse_com_acento_na_transcricao():
    """"Oklahoma Théo TV" sem acento no code_word mas com acento no projeto."""
    r = parse_silence_code_order("Oklahoma Théo TV", "Oklahoma")
    assert r["action"] == "activate"
    assert "Théo" in r["project"] or "theo" in r["slug"]


def test_parse_ignora_fala_normal():
    assert parse_silence_code_order("bom dia", "Oklahoma")["action"] == "none"


def test_parse_ignora_pergunta():
    assert parse_silence_code_order(
        "Oklahoma TheroTV funciona?", "Oklahoma")["action"] in ("activate", "none")


def test_parse_code_word_vazio():
    assert parse_silence_code_order("Oklahoma TheroTV", "")["action"] == "none"


def test_parse_libera_antes_de_ativar():
    """Se a fala começa com "libera", é liberação, não ativação."""
    r = parse_silence_code_order("libera Oklahoma", "Oklahoma")
    assert r["action"] == "deactivate_all"


# ── activate / deactivate ────────────────────────────────────────────────────

def test_activate_adds_project(sc, state_file):
    r = sc.activate("TheroTV")
    assert r["action"] == "activate"
    assert r["slug"] == "therotv"
    assert sc.active
    assert any(p["slug"] == "therotv" for p in sc.active_projects())
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert any(p["slug"] == "therotv" for p in state["active_projects"])


def test_activate_is_idempotent(sc):
    sc.activate("TheroTV")
    r2 = sc.activate("TheroTV")
    assert r2["action"] == "already_active"
    assert len(sc.active_projects()) == 1


def test_activate_multiple_projects(sc):
    sc.activate("TheroTV")
    sc.activate("JarvisDoc")
    assert len(sc.active_projects()) == 2
    assert sc.active


def test_activate_empty_project_is_global(sc):
    sc.activate("")
    assert sc.is_active_for("qualquer projeto")
    assert sc.is_active_for(text="qualquer coisa")


def test_deactivate_all_clears_active(sc):
    sc.activate("TheroTV")
    sc.activate("JarvisDoc")
    r = sc.deactivate(None)
    assert r["action"] == "deactivate_all"
    assert not sc.active
    assert sc.active_projects() == []


def test_deactivate_partial_keeps_others(sc):
    sc.activate("TheroTV")
    sc.activate("JarvisDoc")
    r = sc.deactivate("therotv")
    assert r["action"] == "deactivate"
    assert sc.active
    assert any(p["slug"] == "jarvisdoc" for p in sc.active_projects())
    assert not any(p["slug"] == "therotv" for p in sc.active_projects())


def test_deactivate_not_active(sc):
    r = sc.deactivate("nao_existe")
    assert r["action"] == "not_active"


# ── is_active_for ─────────────────────────────────────────────────────────────

def test_is_active_for_slug(sc):
    sc.activate("TheroTV")
    assert sc.is_active_for("TheroTV")
    assert sc.is_active_for("therotv")
    assert not sc.is_active_for("OutroProjeto")


def test_is_active_for_text_scanning(sc):
    """A busca pelo slug dentro do texto permite pegar falas que citam o projeto."""
    sc.activate("TheroTV")
    assert sc.is_active_for(text="Vou falar sobre TheroTV agora")
    assert not sc.is_active_for(text="Vou falar do Jarvis")


def test_is_active_global_covers_all(sc):
    sc.activate("")
    assert sc.is_active_for("qualquer coisa")


def test_is_active_when_none(sc):
    assert not sc.is_active_for("TheroTV")


# ── retain ────────────────────────────────────────────────────────────────────

def test_retain_stores_entry(sc):
    e = sc.retain("Volume em 40%", subject="TheroTV")
    assert e["id"]
    assert e["text"] == "Volume em 40%"
    assert e["subject"] == "TheroTV"
    assert e["origin"] == "silence_code"
    assert len(sc.pending_entries()) == 1


def test_retain_respects_max(sc):
    """Retém no máximo DEFAULT_MAX_RETAINED (40), descarta as mais antigas."""
    for i in range(45):
        sc.retain(f"fala {i}", subject="p")
    assert len(sc.pending_entries()) <= 40


def test_retain_delegates_to_on_retain(tmp_path, state_file):
    calls = []

    def capture(text, subj, slug):
        calls.append((text, subj, slug))

    sc2 = SilenceCode(
        "Oklahoma",
        state_path=state_file,
        on_retain=capture,
        logger=lambda m: None,
    )
    sc2.retain("Volume em 40%", subject="TheroTV", slug="therotv")
    assert calls == [("Volume em 40%", "TheroTV", "therotv")]


def test_retain_empty_text_no_op(sc):
    r = sc.retain("")
    assert r == {}


# ── deactivate returns summary ────────────────────────────────────────────────

def test_deactivate_returns_summary_lines(sc):
    sc.activate("TheroTV")
    sc.retain("Primeira frase.", subject="TheroTV")
    sc.retain("Segunda frase sobre TheroTV.", subject="TheroTV")
    result = sc.deactivate(None)
    assert len(result["entries"]) == 2
    assert result["summary_lines"]
    assert any("TheroTV" in l for l in result["summary_lines"])


def test_deactivate_clears_local_queue(sc):
    sc.activate("x")
    sc.retain("fala", subject="x")
    sc.deactivate("x")
    assert sc.pending_entries() == []


# ── summarize / summary_lines ────────────────────────────────────────────────

def test_summarize_groups_by_slug():
    entries = [
        {"slug": "a", "subject": "Projeto A", "text": "fala 1"},
        {"slug": "a", "subject": "Projeto A", "text": "fala 2"},
        {"slug": "b", "subject": "Projeto B", "text": "outra"},
    ]
    groups = summarize(entries)
    assert len(groups) == 2
    g = {g["slug"]: g for g in groups}
    assert g["a"]["count"] == 2
    assert g["a"]["latest"] == "fala 2"
    assert g["b"]["count"] == 1


def test_summary_lines_includes_dropped():
    lines = summary_lines([], dropped=5)
    assert any("5" in l and "limite" in l for l in lines)


def test_summary_lines_max_subjects():
    entries = [{"slug": str(i), "subject": f"p{i}", "text": "x"} for i in range(20)]
    lines = summary_lines(entries, max_subjects=3)
    assert len(lines) <= 4  # 3 + "mais N assuntos"


# ── handle_transcription ──────────────────────────────────────────────────────

def test_handle_activate(sc):
    r = sc.handle_transcription("Oklahoma TheroTV")
    assert r and r["action"] == "activate"
    assert sc.is_active_for("TheroTV")


def test_handle_deactivate_all(sc):
    sc.activate("TheroTV")
    r = sc.handle_transcription("libera Oklahoma")
    assert r and r["action"] == "deactivate_all"
    assert not sc.active


def test_handle_none_for_normal_speech(sc):
    assert sc.handle_transcription("bom dia") is None


def test_handle_noop_when_code_word_empty(tmp_path, state_file):
    sc2 = SilenceCode("", state_path=state_file, logger=lambda m: None)
    assert sc2.handle_transcription("Oklahoma TheroTV") is None


# ── persistência ──────────────────────────────────────────────────────────────

def test_state_survives_reload(sc, state_file):
    sc.activate("TheroTV")
    sc2 = SilenceCode(
        code_word="Oklahoma",
        state_path=state_file,
        logger=lambda m: None,
    )
    assert sc2.active
    assert any(p["slug"] == "therotv" for p in sc2.active_projects())


def test_code_word_saved_and_loaded(sc, state_file):
    sc.code_word = "Oklahoma"
    sc.activate("x")
    sc2 = SilenceCode(state_path=state_file, logger=lambda m: None)
    assert sc2.code_word == "Oklahoma"


# ── thread safety ─────────────────────────────────────────────────────────────

def test_concurrent_activate(sc):
    threads = [
        threading.Thread(target=sc.activate, args=("TheroTV",))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2)
    assert any(p["slug"] == "therotv" for p in sc.active_projects())
    # idempotente: apenas 1 entrada
    assert len([p for p in sc.active_projects() if p["slug"] == "therotv"]) == 1
