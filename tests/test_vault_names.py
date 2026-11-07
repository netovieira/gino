"""REGRA de nomes do cofre: TIPO-AAAA-MM-DD-slug.md (dono, 2026-11-07).

Texto da regra: ``~/Jarvis/REGRA-2026-11-07-padro-de-nomes-de-arquivo.md``.
O que isto prova é o lado que virou código: o verificador (``name_errors``,
``check_name``, ``build_name``, ``normalize_name``) e o portão no caminho real
de escrita (``write_note``/``_gate_name``) com as três políticas do env
``JARVIS_VAULT_NAME_POLICY``.

Nenhum teste deste arquivo escreve no cofre do dono: o cofre é sempre
``tmp_path`` (``obsidian.VAULT`` trocado) e o activity.log é um arquivo falso.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import obsidian  # noqa: E402

TODAY = dt.date.today().isoformat()


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "Jarvis"
    monkeypatch.setattr(obsidian, "VAULT", root)
    monkeypatch.setattr(obsidian, "PLANS_DIR", tmp_path / "qoder" / "plans")
    monkeypatch.setattr(obsidian, "ACTIVITY_LOG", tmp_path / "activity.log")
    monkeypatch.setattr(obsidian, "NEXTCLOUD_DIR", tmp_path / "Nextcloud")
    monkeypatch.delenv("JARVIS_VAULT_NAME_POLICY", raising=False)
    return root


# ── o verificador ───────────────────────────────────────────────────────────

def test_nome_conforme_nao_reporta_nada():
    for name in ("PLANO-2026-11-07-vault-jarvis-espelho-nodum.md",
                 "REGRA-2026-11-07-padro-de-nomes-de-arquivo.md",
                 "NOTA-2028-02-29-slug-valido.md"):     # ano bissexto real
        assert obsidian.name_errors(name) == [], name
        assert obsidian.check_name(name) == name


@pytest.mark.parametrize("name,marca", [
    ("MINUTA-2026-11-07-foo.md", "fora da lista fechada"),
    ("plano-2026-11-07-foo.md", "caixa-alta"),
])
def test_tipo_fora_da_lista_e_rejeitado(name, marca):
    errors = obsidian.name_errors(name)
    assert any(marca in error for error in errors), errors
    with pytest.raises(ValueError) as caught:
        obsidian.check_name(name)
    assert marca in str(caught.value)


def test_data_malformada_e_diz_o_que_esta_ruim():
    assert any("calendário" in e
               for e in obsidian.name_errors("PLANO-2026-13-45-foo.md"))
    assert any("calendário" in e
               for e in obsidian.name_errors("PLANO-2026-02-30-foo.md"))
    assert any("falta a data" in e
               for e in obsidian.name_errors("PLANO-setembro-foo.md"))
    assert any("falta o slug" in e
               for e in obsidian.name_errors("PLANO-2026-11-07.md"))


@pytest.mark.parametrize("name,marca", [
    ("NOTA-2026-11-07-manutenção.md", "acento"),
    ("NOTA-2026-11-07-Manutencao.md", "minúsculo"),
    ("NOTA-2026-11-07-minha nota.md", "espaço"),
    ("NOTA-2026-11-07-minha--nota.md", "hífen duplo"),
    ("NOTA-2026-11-07-reuniao.md.md", "duplicada"),
    ("Planos/NOTA-2026-11-07-reuniao.md", "caminho"),
])
def test_slug_fora_do_padrao_tem_mensagem_util(name, marca):
    errors = obsidian.name_errors(name)
    assert any(marca in error for error in errors), errors


def test_slug_e_nome_tem_teto_de_tamanho():
    assert any("máximo 50" in e
               for e in obsidian.name_errors(f"NOTA-2026-11-07-{'x' * 60}.md"))
    assert len(obsidian.ascii_slug("Karaokê LiveKit — celular como "
                                  "microfone, TV como palco")) \
        <= obsidian.SLUG_MAX_CHARS


def test_nomes_de_codigo_estao_fora_da_regra():
    # Teia.md e Grafo.json são escritos pela aranha de memória com nome fixo.
    assert obsidian.name_errors("Teia.md") == []
    assert obsidian.name_errors("Grafo.json") == []


# ── montar e normalizar ─────────────────────────────────────────────────────

def test_build_name_monta_a_partir_do_titulo():
    assert obsidian.build_name("plano", "Vault Jarvis + espelho Nodum",
                               "2026-11-07") == \
        "PLANO-2026-11-07-vault-jarvis-espelho-nodum.md"
    with pytest.raises(ValueError, match="fora da lista fechada"):
        obsidian.build_name("MINUTA", "qualquer coisa")
    with pytest.raises(ValueError, match="não produz slug"):
        obsidian.build_name("NOTA", "— — —")


def test_normalize_usa_a_pasta_para_escolher_o_tipo():
    assert obsidian.normalize_name("azure-glade-mullet.md", folder="Planos",
                                   day="2026-10-31") == \
        "PLANO-2026-10-31-azure-glade-mullet.md"
    assert obsidian.normalize_name(
        "Plano de ação — Verificação UP-7 (material).md", folder="Planos",
        day="2026-11-07") == \
        "PLANO-2026-11-07-plano-de-acao-verificacao-up-7-material.md"


def test_normalize_prefere_a_data_que_o_nome_declara():
    assert obsidian.normalize_name("PLANO-2026-11-07-foo.md",
                                   day="2026-01-01") == \
        "PLANO-2026-11-07-foo.md"


def test_normalize_recusa_quando_a_normalizacao_e_ambigua():
    with pytest.raises(ValueError, match="sem tipo"):
        obsidian.normalize_name("coisa.md", folder="Indices")
    with pytest.raises(ValueError, match="fora da lista fechada"):
        obsidian.normalize_name("coisa.md", tipo="MINUTA")


# ── o portão no caminho de escrita ──────────────────────────────────────────

def test_write_note_com_tipo_escreve_no_padrao(vault):
    note = obsidian.write_note("Auditoria do gate", "# corpo",
                               folder="Planos", tipo="auditoria")
    assert note.name == f"AUDITORIA-{TODAY}-auditoria-do-gate.md"
    assert note.is_file() and note.read_text("utf-8") == "# corpo"


def test_write_note_com_tipo_fora_da_lista_nao_escreve_nada(vault):
    with pytest.raises(ValueError, match="fora da lista fechada"):
        obsidian.write_note("Alguma coisa", "# corpo", tipo="MINUTA")
    assert not (vault / "Planos" / f"MINUTA-{TODAY}-alguma-coisa.md").exists()
    assert not vault.exists()      # rejeição acontece antes de tocar no disco


def test_politica_registrar_e_default_e_nao_renomeia_as_escondidas(vault):
    assert obsidian.name_policy() == "registrar"
    note = obsidian.write_note("Plano de ação — Consertar o login", "# p")
    assert note.name == \
        obsidian._slug("Plano de ação — Consertar o login") + ".md"
    log = Path(obsidian.ACTIVITY_LOG).read_text(encoding="utf-8")
    assert "event=vault_name_off_pattern" in log
    assert "TIPO-AAAA-MM-DD" in log          # o diagnóstico vem junto


def test_politica_normalizar_troca_o_nome_pelo_padrao(vault, monkeypatch):
    monkeypatch.setenv("JARVIS_VAULT_NAME_POLICY", "normalizar")
    note = obsidian.write_note("Plano — Rota de teste", "# p")
    assert note.name == f"PLANO-{TODAY}-plano-rota-de-teste.md"
    assert note.is_file()
    log = Path(obsidian.ACTIVITY_LOG).read_text(encoding="utf-8")
    assert "event=vault_name_normalized" in log


def test_politica_rejeitar_levanta_sem_escrever(vault, monkeypatch):
    monkeypatch.setenv("JARVIS_VAULT_NAME_POLICY", "rejeitar")
    with pytest.raises(ValueError, match="fora do padrão do cofre"):
        obsidian.write_note("Plano — Rota de teste", "# p")
    assert list((vault / "Planos").glob("*.md")) == []


def test_mirror_plan_tambem_passa_pelo_portao(vault, monkeypatch, tmp_path):
    plans = tmp_path / "qoder" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    source = plans / "azure-glade-mullet.md"
    source.write_text("# Plano — Gino Live", encoding="utf-8")
    stamp = dt.datetime(2026, 10, 31, 12, 0).timestamp()
    os.utime(source, (stamp, stamp))                 # data do conteúdo
    monkeypatch.setenv("JARVIS_VAULT_NAME_POLICY", "normalizar")

    destination = obsidian.mirror_plan(source)
    assert destination.name == "PLANO-2026-10-31-azure-glade-mullet.md"
    assert destination.read_text("utf-8") == "# Plano — Gino Live"


def test_politica_desconhecida_volta_para_registrar(vault, monkeypatch):
    monkeypatch.setenv("JARVIS_VAULT_NAME_POLICY", "gritar")
    assert obsidian.name_policy() == "registrar"
