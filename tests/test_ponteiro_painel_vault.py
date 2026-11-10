"""Ponteiro do painel GUIA para os documentos do vault: nome pelo padrão, não pelo literal.

Decisão 17 (D4) do ``PLANO-2026-11-09-correcoes-pendentes-de-cinco-pontas``: o
dono respondeu "renomeia" -- opção (a) da ponta 4, RENOMEAR os três nomes fora
do padrão, e NÃO a isenção no validador. O buraco que a renomeação abriria é
este: ``guide_panel`` abria cada documento pelo nome exato
(:func:`core.guide_panel.find_vault_doc`), então renomear sem tocar aqui deixa
o painel dizendo "(ausente no vault)" para sempre.

O que fica travado aqui, um caso por buraco:

 (i)   o nome canônico é calculado pelo validador de verdade (``build_name``),
       não chutado à mão -- se o validador reprovar, o ponteiro cai no alias em
       vez de inventar nome;
 (ii)  o ponteiro resolve os três documentos pelo nome NOVO;
 (iii)o ponteiro resolve os três pelo nome VELHO (alias): renomeio de arquivo e
       reescrita de citações não são atômicos para o processo vivo, e uma
       máquina onde a rodada ainda não passou não pode ficar com painel mudo;
 (iv)  o contrato do repo (``skills/fluxos/FLUXOS.md``) é a MESMA palavra com
       OUTRO alvo: não é um dos três nomes do vault, não é renomeado, e o painel
       continua abrindo cada um no seu lugar;
 (v)   nenhum outro canto do código abre um dos renomeados pelo nome de antes.

Nenhum teste daqui lê o cofre do dono: o vault é sempre ``tmp_path``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core import guide_panel  # noqa: E402
from core.obsidian import check_name  # noqa: E402

#: (legenda, TIPO, título, data do conteúdo, nome antigo) dos três documentos
#: que a decisão 17 renomeou. Se a tabela do painel mudar, este teste muda
#: junto e obriga a conferir a escolha de tipo e data.
OS_TRES = {
    "Contrato do dono no vault": ("REGRA", "fluxos", "2026-11-05", "FLUXOS.md"),
    "Ordens do dono": ("REGRA", "ordens do dono", "2026-11-05",
                       "ORDENS-DO-DONO-2026-11-05.md"),
    "Tarefas unificadas": ("TAREFA", "tarefas unificadas", "2026-11-05",
                           "TAREFAS-UNIFICADAS.md"),
}


def _nomes(label: str) -> tuple[str, ...]:
    tipo, titulo, dia, antigo = OS_TRES[label]
    return guide_panel.vault_doc_names(tipo, titulo, dia, (antigo,))


def _links_de(base: Path) -> list[dict]:
    secoes = guide_panel.sections(base)
    return next(s for s in secoes if s["title"] == "LINKS")["items"]


@pytest.fixture
def cofre(tmp_path, monkeypatch):
    """Vault falso na tmp: só existe aqui o que o teste escrever."""
    raiz = tmp_path / "Cofre"
    raiz.mkdir()
    monkeypatch.setattr(guide_panel, "_vault_dirs", lambda: [raiz])
    monkeypatch.setattr(guide_panel, "unverified_markers", lambda: [])
    return raiz


# ── (i) o nome vem do validador, não da mão de quem escreveu o ponteiro ──────

def test_nome_canonico_e_calculado_pelo_validador():
    for label, tipo, titulo, dia, antigo in guide_panel.VAULT_DOCS:
        nomes = guide_panel.vault_doc_names(tipo, titulo, dia, (antigo,))
        assert nomes, label
        assert check_name(nomes[0]) == nomes[0], (label, nomes[0])
        assert nomes[-1] == antigo, (label, nomes)
        assert nomes[0] != antigo, (label, nomes[0])


def test_tabela_do_ponteiro_bate_com_a_lista_deste_teste():
    """A tabela do painel é esta, com exatamente um alias por documento."""
    for entry in guide_panel.VAULT_DOCS:
        assert len(entry) == 5, entry
        assert len(entry[4]) == 1, entry
    tabelados = {e[0]: (e[1], e[2], e[3], e[4][0])
                 for e in guide_panel.VAULT_DOCS}
    assert tabelados == OS_TRES


def test_validador_reprovado_caí_no_alias_sem_inventar_nome(monkeypatch):
    from core import obsidian

    def reprova(tipo, titulo, day=None):
        raise ValueError("nome fora do padrão")

    monkeypatch.setattr(obsidian, "build_name", reprova)
    nomes = guide_panel.vault_doc_names("REGRA", "fluxos", "2026-11-05",
                                        ("FLUXOS.md",))
    assert nomes == ("FLUXOS.md",), nomes


# ── (ii)(iii) resolve pelo nome novo e pelo velho ────────────────────────────

@pytest.mark.parametrize("label", sorted(OS_TRES))
def test_ponteiro_resolve_pelo_nome_novo(cofre, label):
    novo = _nomes(label)[0]
    alvo = cofre / novo
    alvo.write_text("##### FLUXO TESTE\n", encoding="utf-8")
    assert guide_panel.find_vault_doc(_nomes(label)) == alvo


@pytest.mark.parametrize("label", sorted(OS_TRES))
def test_ponteiro_resolve_pelo_alias_antigo(cofre, label):
    antigo = OS_TRES[label][3]
    velho = cofre / antigo
    velho.write_text("##### FLUXO TESTE\n", encoding="utf-8")
    assert guide_panel.find_vault_doc(_nomes(label)) == velho


def test_preferencia_e_do_nome_canonico_nao_do_cofre(cofre, tmp_path,
                                                     monkeypatch):
    outro = tmp_path / "OutroCofre"
    outro.mkdir()
    nomes = _nomes("Contrato do dono no vault")
    (cofre / nomes[1]).write_text("velho\n", encoding="utf-8")
    novo = outro / nomes[0]
    novo.write_text("novo\n", encoding="utf-8")
    monkeypatch.setattr(guide_panel, "_vault_dirs", lambda: [cofre, outro])
    assert guide_panel.find_vault_doc(nomes) == novo


def test_find_vault_doc_continua_aceitando_um_nome_so(cofre):
    """API antiga (string solta) não quebra: quem já chamava assim continua."""
    alvo = cofre / "Qualquer.md"
    alvo.write_text("x\n", encoding="utf-8")
    assert guide_panel.find_vault_doc("Qualquer.md") == alvo
    assert guide_panel.find_vault_doc("Inexistente.md") is None


# ── (iv) o contrato do repo é outra coisa, com outro alvo ────────────────────

def test_contrato_do_repo_nao_e_um_dos_tres_nomes():
    contrato = REPO / "skills" / "fluxos" / "FLUXOS.md"
    assert contrato.is_file(), "o contrato do repo é lido pelo índice de skills"
    nomes = {n for entry in guide_panel.VAULT_DOCS
             for n in guide_panel.vault_doc_names(*entry[1:])}
    assert "FLUXOS.md" in nomes            # alias: alvo é a raiz do vault do dono
    assert not any("/" in n or "\\" in n for n in nomes), nomes


def test_painel_abre_as_duas_copias_cada_um_no_seu_lugar(cofre):
    nomes = _nomes("Contrato do dono no vault")
    (cofre / nomes[0]).write_text("##### FLUXO FALA\n", encoding="utf-8")
    links = _links_de(REPO)
    repo = next(i for i in links
                if i["label"] == "Contrato (cópia literal no projeto)")
    dono = next(i for i in links if i["label"] == "Contrato do dono no vault")
    assert Path(repo["path"]) == REPO / "skills" / "fluxos" / "FLUXOS.md"
    assert repo["exists"] is True
    assert Path(dono["path"]) == cofre / nomes[0]
    assert dono["exists"] is True
    assert Path(repo["path"]) != Path(dono["path"])


# ── o painel inteiro: nenhum dos três aparece como ausente ───────────────────

def test_painel_nao_diz_ausente_para_os_tres_documentos(cofre):
    for label in OS_TRES:
        (cofre / _nomes(label)[0]).write_text("qualquer coisa\n",
                                               encoding="utf-8")
    links = _links_de(REPO)
    for label in OS_TRES:
        item = next(i for i in links if i["label"] == label)
        assert item["exists"] is True, label
        assert "(ausente no vault" not in item["path"], (label, item["path"])


def test_ausencia_continua_diagnostica_pelo_nome_canonico(cofre):
    item = next(i for i in _links_de(REPO) if i["label"] == "Ordens do dono")
    assert item["exists"] is False
    procurado = item["path"].split("ausente no vault: ")[1].split(",")[0]
    assert check_name(procurado) == procurado


# ── (v) o resto do código não abre um dos renomeados pelo nome de antes ──────

@pytest.mark.parametrize("literal", ["ORDENS-DO-DONO-2026-11-05",
                                     "TAREFAS-UNIFICADAS"])
def test_nome_antigo_sobrevive_so_na_tabela_de_aliases(literal):
    """Uma linha só no código inteiro: o alias registrado no ponteiro."""
    arquivos = [REPO / "main.py", REPO / "ui.py"]
    for pasta in ("core", "actions", "plugins"):
        arquivos.extend(p for p in (REPO / pasta).rglob("*.py")
                        if "__pycache__" not in p.parts)
    achei = [(p.relative_to(REPO).as_posix(), linha.strip())
             for p in arquivos if p.is_file()
             for linha in p.read_text(errors="replace").splitlines()
             if literal in linha]
    assert len(achei) == 1, achei
    origem, linha = achei[0]
    assert origem == "core/guide_panel.py", achei
    # é a linha da tabela: o nome velho entre aspas, como alias declarado
    assert linha.strip() == f'("{literal}.md",)),', linha
