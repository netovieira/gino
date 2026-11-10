"""Obsidian vault mirror: folders, plan copies, index files and open calls."""
from __future__ import annotations

import json
import os
import sys
import types
import datetime as dt
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import obsidian  # noqa: E402


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "Jarvis"
    plans = tmp_path / "qoder" / "plans"
    plans.mkdir(parents=True)
    monkeypatch.setattr(obsidian, "VAULT", root)
    monkeypatch.setattr(obsidian, "PLANS_DIR", plans)
    monkeypatch.setattr(obsidian, "OBSIDIAN_BIN", "/snap/bin/obsidian")
    # UP-7: abrir nota grava event=obsidian_opened; o activity.log do dono
    # não pode virar alvo de teste.
    monkeypatch.setattr(obsidian, "ACTIVITY_LOG", tmp_path / "activity.log")
    monkeypatch.setattr(obsidian, "NEXTCLOUD_DIR", tmp_path / "Nextcloud")
    return root, plans


# ── vault layout ────────────────────────────────────────────────────────────

def test_ensure_vault_creates_layout_and_is_idempotent(vault):
    root, _ = vault
    assert obsidian.ensure_vault() == root
    for name in ("Planos", "Projetos", "Personas", "Indices", "Topicos"):
        assert (root / name).is_dir()
    app = root / ".obsidian" / "app.json"
    assert json.loads(app.read_text("utf-8")) == {}

    marker = root / "Planos" / "keep.md"
    marker.write_text("keep", "utf-8")
    assert obsidian.ensure_vault() == root
    assert marker.read_text("utf-8") == "keep"


# ── plans ───────────────────────────────────────────────────────────────────

def test_mirror_plans_copies_only_when_content_differs(vault):
    root, plans = vault
    first = plans / "alfa.md"
    first.write_text("# alfa\n", "utf-8")
    (plans / "beta.md").write_text("# beta\n", "utf-8")

    destinations = obsidian.mirror_plans()
    assert [p.name for p in destinations] == ["alfa.md", "beta.md"]
    assert all(p.parent == root / "Planos" for p in destinations)
    assert (root / "Planos" / "alfa.md").read_text("utf-8") == "# alfa\n"

    # Freeze the mtime; an unchanged plan must not be rewritten.
    frozen = 1_000_000_000
    os.utime(destinations[0], ns=(frozen, frozen))
    assert obsidian.mirror_plans() == destinations
    assert destinations[0].stat().st_mtime_ns == frozen

    first.write_text("# alfa 2\n", "utf-8")
    obsidian.mirror_plans()
    assert destinations[0].read_text("utf-8") == "# alfa 2\n"
    assert destinations[0].stat().st_mtime_ns != frozen


def test_mirror_plans_missing_source_is_empty(vault, tmp_path):
    assert obsidian.mirror_plans(tmp_path / "nope") == []


def test_mirror_plan_missing_file_raises(vault):
    with pytest.raises(ValueError):
        obsidian.mirror_plan(Path("/definitely/not/here.md"))


def test_mirror_plan_refuses_files_outside_plans_and_vault(vault, tmp_path):
    stray = tmp_path / "stray.md"
    stray.write_text("# stray", "utf-8")
    with pytest.raises(ValueError):
        obsidian.mirror_plan(stray)


def test_mirror_plan_accepts_file_already_in_vault(vault):
    root, _ = vault
    inside = root / "Topicos" / "topico.md"
    inside.parent.mkdir(parents=True)
    inside.write_text("# topico", "utf-8")
    destination = obsidian.mirror_plan(inside)
    assert destination == root / "Planos" / "topico.md"
    assert destination.read_text("utf-8") == "# topico"


# ── index mirror ────────────────────────────────────────────────────────────

def test_mirror_index_writes_teia_and_grafo(vault):
    root, _ = vault
    graph = {"version": 1, "events": {}, "nodes": {}, "edges": []}
    path = obsidian.mirror_index("# Teia\n\n[[subject:saude]]", graph)
    assert path == root / "Indices" / "Teia.md"
    assert path.read_text("utf-8") == "# Teia\n\n[[subject:saude]]"
    stored = json.loads((root / "Indices" / "Grafo.json").read_text("utf-8"))
    assert stored == graph


# ── opening ─────────────────────────────────────────────────────────────────

def test_open_path_mirrors_plan_and_calls_xdg_open(vault, monkeypatch):
    root, plans = vault
    plan = plans / "plano.md"
    plan.write_text("# plano", "utf-8")
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(obsidian.subprocess, "run", fake_run)
    monkeypatch.setattr(obsidian.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("Popen must not be used"))

    message = obsidian.open_path(plan)
    target = root / "Planos" / "plano.md"
    assert target.is_file()
    assert "Abrindo" in message and "Obsidian" in message
    assert calls == [(
        ["xdg-open", "obsidian://open?path=" + quote(str(target), safe="")],
        {"timeout": 5},
    )]


def test_open_path_copies_outside_markdown_into_planos(vault, tmp_path, monkeypatch):
    root, _ = vault
    outside = tmp_path / "fora.md"
    outside.write_text("# fora", "utf-8")
    monkeypatch.setattr(obsidian.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0))
    obsidian.open_path(outside)
    copied = root / "Planos" / "fora.md"
    assert copied.read_text("utf-8") == "# fora"


def test_open_path_falls_back_to_obsidian_binary(vault, tmp_path, monkeypatch):
    root, _ = vault
    outside = tmp_path / "nota.md"
    outside.write_text("# nota", "utf-8")
    popped = []

    def broken_run(*args, **kwargs):
        raise FileNotFoundError("xdg-open")

    monkeypatch.setattr(obsidian.subprocess, "run", broken_run)
    monkeypatch.setattr(obsidian.subprocess, "Popen",
                        lambda args, **kwargs: popped.append((args, kwargs)))

    message = obsidian.open_path(outside)
    target = root / "Planos" / "nota.md"
    assert "Abrindo" in message
    assert popped and popped[0][0] == ["/snap/bin/obsidian", str(target)]


def test_open_path_missing_file_does_not_call_subprocess(vault, monkeypatch):
    monkeypatch.setattr(obsidian.subprocess, "run",
                        lambda *a, **k: pytest.fail("no subprocess for missing"))
    message = obsidian.open_path("/definitely/not/here.md")
    assert "não encontrado" in message


# ── pasta por plano (ordem do dono, 09/11/2026) ─────────────────────────────

def test_create_plan_folder_monta_os_seis_arquivos_e_duas_pastas(vault):
    root, _ = vault
    pasta = obsidian.create_plan_folder(
        "PLANO-2026-11-07-personas-de-trabalho-e-caminhos-de-uso.md")
    assert pasta == root / "Planos" / "personas-de-trabalho-e-caminhos-de-uso"
    for nome in obsidian.PLAN_FOLDER_FILES:
        assert (pasta / nome).is_file(), nome
    assert (pasta / "steps").is_dir()
    assert (pasta / "steps" / "tasks").is_dir()
    assert obsidian.PLAN_FOLDER_FILES == (
        "plan.md", "status.md", "progresso.md", "historico.md", "testes.md",
        "prompt.md")


def test_create_plan_folder_e_idempotente_e_nao_amassa_edicao_do_dono(vault):
    pasta = obsidian.create_plan_folder("plano-exemplo")
    (pasta / "status.md").write_text("# editado pelo dono\n", "utf-8")
    (pasta / "plan.md").write_text("# plano do dono\n", "utf-8")
    outra = obsidian.create_plan_folder("plano-exemplo")
    assert outra == pasta
    assert (pasta / "status.md").read_text("utf-8") == "# editado pelo dono\n"
    assert (pasta / "plan.md").read_text("utf-8") == "# plano do dono\n"


def test_plan_md_carrega_o_texto_original_do_plano(vault, tmp_path):
    original = tmp_path / "PLANO-2026-11-09-original.md"
    original.write_text("# Título original\n\ncorpo do plano\n", "utf-8")
    pasta = obsidian.create_plan_folder(original, fonte=original)
    texto = (pasta / "plan.md").read_text("utf-8")
    assert "# Título original" in texto and "corpo do plano" in texto
    # o rastreio entre arquivos: a pasta aponta para o documento do cofre
    assert "[[PLANO-2026-11-09-original]]" in texto


def test_nomes_do_esqueleto_passam_pelo_portao_sem_infraquear_a_regra(vault,
                                                                      monkeypatch):
    root, _ = vault
    monkeypatch.delenv("JARVIS_VAULT_NAME_POLICY", raising=False)
    pasta = obsidian.create_plan_folder("plano-exemplo")
    assert obsidian.is_exempt_name(pasta / "plan.md")
    assert obsidian.is_exempt_name(root / "Memoria" / "topico-vault.md")
    # fora do esqueleto, a REGRA continua valendo: nota solta namedurada é
    # logada como infração, não virou passe livre.
    assert not obsidian.is_exempt_name(root / "Topicos" / "nota solta.md")
    assert obsidian.name_errors("plan.md"), "plan.md não deve validar como nota"


def test_create_step_e_create_task_seguem_a_regra_de_nomes(vault):
    pasta = obsidian.create_plan_folder("plano-exemplo")
    passo = obsidian.create_step("plano-exemplo", 1, "Montar o esqueleto",
                                 dia="2026-11-09")
    tarefa = obsidian.create_task("plano-exemplo", 7, "Escrever o gate",
                                  passo=passo.name, dia="2026-11-09")
    assert passo.parent == pasta / "steps"
    assert tarefa.parent == pasta / "steps" / "tasks"
    assert passo.name == "NOTA-2026-11-09-step-01-montar-o-esqueleto.md"
    assert tarefa.name == "TAREFA-2026-11-09-task-07-escrever-o-gate.md"
    # o nome sai validado: reescrever o mesmo passo não inventa arquivo novo
    assert obsidian.name_errors(passo.name) == []
    assert obsidian.name_errors(tarefa.name) == []
    corpo = tarefa.read_text("utf-8")
    assert f"[[{passo.name[:-3]}]]" in corpo          # tarefa -> passo
    assert "[[plan]]" in corpo                          # passo/tarefa -> plano
    assert "valor nunca entra aqui" in corpo            # portão de chave


def test_create_step_recusa_data_inventada(vault):
    obsidian.create_plan_folder("plano-exemplo")
    with pytest.raises(ValueError):
        obsidian.create_step("plano-exemplo", 1, "passo", dia="2026-13-45")


# ── fluxo de memória com wikilink (ordem do dono, 09/11/2026) ───────────────

def test_ensure_vault_cria_a_pasta_memoria(vault):
    root, _ = vault
    assert obsidian.ensure_vault() == root
    assert (root / "Memoria").is_dir()


def test_write_memory_note_costura_wikilinks_e_mantem_o_nome_estavel(vault):
    root, _ = vault
    primeira = obsidian.write_memory_note(
        "vault obsidian", "o dono quer a memória no Obsidian",
        quando="2026-11-09 15:00", plano="plano-exemplo")
    assert primeira == root / "Memoria" / "topico-vault-obsidian.md"
    segunda = obsidian.write_memory_note(
        "Vault Obsidian", "segunda linha no mesmo tópico",
        quando="2026-11-09 15:01")
    assert segunda == primeira, "tópico não pode ganhar arquivo novo"
    texto = primeira.read_text("utf-8")
    assert "[[plano-exemplo]]" in texto
    assert "[[Indice-da-memoria]]" in texto
    assert "[2026-11-09 15:00]" in texto and "[2026-11-09 15:01]" in texto


def test_a_memoria_nao_grava_valor_de_chave(vault):
    segredo = "Sup3rS3Credealha01"
    caminho = obsidian.write_memory_note(
        "chaves", f"configurar groq_api_key: {segredo} para o stt")
    texto = caminho.read_text("utf-8")
    assert segredo not in texto
    assert "[key:groq_api_key, tipo:api_key]" in texto


def test_espelhar_memoria_escreve_indice_e_nota_do_dia(vault):
    root, _ = vault
    graph = {"events": {
        "e1": {"id": "e1", "text": "falou da vault",
               "at": "2026-11-09T14:00:00", "subject": "vault",
               "persona": "gino", "project": "jarvis", "kind": "note",
               "source": "voice", "refs": []},
        "e2": {"id": "e2", "text": "outra coisa de vault",
               "at": "2026-11-09T15:00:00", "subject": "vault",
               "persona": None, "project": None, "kind": "note",
               "source": "voice", "refs": []},
    }}
    # O dia vem EXPLÍCITO: "nota do dia" é o dia do relógio de parede do
    # processo, e um teste que comparava o nome gravado na noite de 09/11
    # quebrou sozinho à meia-noite de 10/11 (mesma família do estado local de
    # F3/F4). A data de hoje é provada no teste logo abaixo.
    caminhos = obsidian.espelhar_memoria(graph, dia="2026-11-09")
    assert [c.parent for c in caminhos] == [root / "Memoria"] * 2
    indice = (root / "Memoria" / "Indice-da-memoria.md").read_text("utf-8")
    assert "[[topico-vault|vault]]" in indice
    assert "[[topico-gino|gino]]" in indice
    assert "[[dia-2026-11-09]]" in indice
    dia = (root / "Memoria" / "dia-2026-11-09.md").read_text("utf-8")
    assert "[15:00]" in dia and "[14:00]" in dia
    assert "[[topico-vault|vault]]" in dia


def test_espelhar_memoria_sem_dia_usa_o_hoje_do_processo(vault):
    """Sem parâmetro, "nota do dia" é o dia do relógio de parede.

    A data esperada é derivada do MESMO relógio que o código usa (e aceita a
    virada de meia-noite no meio da chamada: o nome tem de estar entre o dia
    lido antes e o lido depois).
    """
    root, _ = vault
    antes = dt.date.today().isoformat()
    caminhos = obsidian.espelhar_memoria({"events": {}})
    depois = dt.date.today().isoformat()
    indice = caminhos[0].read_text("utf-8")
    dias_ok = {f"[[dia-{d}]]" for d in {antes, depois}}
    assert any(d in indice for d in dias_ok), indice[:200]


def test_mirror_index_tambem_espeha_a_memoria_e_nao_quebra_sem_ela(vault,
                                                                  monkeypatch):
    root, _ = vault
    graph = {"events": {"e1": {"id": "e1", "text": "x", "at":
                               "2026-11-09T14:00:00", "subject": "vault",
                               "persona": None, "project": None,
                               "kind": "note", "source": "voice", "refs": []}}}
    teia = obsidian.mirror_index("# Teia\n", graph)
    assert teia == root / "Indices" / "Teia.md"
    assert (root / "Memoria" / "Indice-da-memoria.md").is_file()
    # memória podre não derruba a teia: o contrato antigo continua de pé
    monkeypatch.setattr(obsidian, "espelhar_memoria",
                        lambda _g: (_ for _ in ()).throw(RuntimeError("caiu")))
    assert obsidian.mirror_index("# Teia 2\n", graph) == teia
    assert (root / "Indices" / "Teia.md").read_text("utf-8") == "# Teia 2\n"
