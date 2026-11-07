"""O registro de obras: a vault do dono visível, o módulo e o portão do status.

Ordem do dono, 07/11/2026: pasta por projeto, pasta por implementação, e dentro
``status.md``/``prompt.md``/``historico.md``/``testes.md``; na raiz, os gerais
``status.md`` ``progresso.md`` ``historico.md`` ``testes.md``. Estes testes
provam o que virou código:

  * a estrutura é criada com OS NOMES QUE O DONO DEU (nada traduzido);
  * o quadro de arquivos do status.md traz data de adição, início e conclusão;
  * segredo em prompt sai redigido — nenhum .md da vault fica com a senha;
  * status é conjunto fechado e "concluída" exige commit e teste;
  * a raiz é gerada: edição na mão é sobrescrita na regeneração;
  * a vault não é git e nada dela entra em git nenhum.

Nada aqui escreve na vault real do dono: o autouse troca ``obras.OBRAS_DIR``
por um diretório temporário e confere que o caminho ativo é o temporário.
"""
from __future__ import annotations

import datetime as dt
import importlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import obras  # noqa: E402

HORA_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
SENHA = "Sup3rS3Credealha01"
GIT = "/usr/bin/git"


@pytest.fixture(autouse=True)
def vault_falsa(tmp_path, monkeypatch):
    """A vault dos testes é sempre temporária; a do dono fica intocada."""
    raiz = tmp_path / "Gino-Obras"
    monkeypatch.setattr(obras, "OBRAS_DIR", raiz)
    monkeypatch.delenv("JARVIS_OBRAS_DIR", raising=False)
    assert obras.obras_dir() == raiz
    yield raiz


def _impl(projeto="interface-e-voz", titulo="mute da interface so microfone", **kw):
    kw.setdefault("inicio", "2026-11-07 18:27")
    return obras.registrar_implementacao(projeto, titulo, **kw)


def _fechar(pasta):
    """Evidência mínima para o portão deixar marcar como concluída."""
    obras.registrar_commits(pasta, ["4de3a5f"])
    obras.registrar_testes(pasta, ["tests/test_obras.py::test_x"])


# ── a estrutura que o dono desenhou ─────────────────────────────────────────

def test_pasta_do_projeto_e_da_implementacao_com_os_quatro_arquivos():
    pasta = _impl()
    assert pasta.parent.name == "interface-e-voz"
    assert pasta.name == "mute-da-interface-so-microfone"
    assert sorted(p.name for p in pasta.iterdir()) == [
        "historico.md", "prompt.md", "status.md", "testes.md"]


def test_nomes_dos_arquivos_sao_literalmente_os_do_dono():
    assert obras.NOMES_IMPLEMENTACAO == (
        "status.md", "prompt.md", "historico.md", "testes.md")
    assert obras.NOMES_RAIZ == (
        "status.md", "progresso.md", "historico.md", "testes.md")


def test_raiz_tem_os_quatro_gerais_e_o_readme_como_unico_extra(vault_falsa):
    _impl()
    assert sorted(p.name for p in vault_falsa.iterdir()) == [
        "README.md", "historico.md", "interface-e-voz", "progresso.md",
        "status.md", "testes.md"]


def test_cabeecalho_de_status_tem_projeto_status_inicio_e_conclusao():
    pasta = _impl(conclusao="2026-11-07 18:50")
    texto = (pasta / "status.md").read_text(encoding="utf-8")
    assert "- **projeto:** interface-e-voz" in texto
    assert "- **status:** pendente de validacao ao vivo" in texto
    assert "- **início:** 2026-11-07 18:27" in texto
    assert "- **conclusão:** 2026-11-07 18:50" in texto


def test_quadro_de_arquivos_lista_tudo_com_data_de_adicao():
    pasta = _impl(conclusao="2026-11-07 18:50")
    obras.gravar_prompt(pasta, "prompt verdadeiro da execução")
    linhas = [lin for lin in (pasta / "status.md").read_text(encoding="utf-8").splitlines()
              if lin.startswith("| ") and "---" not in lin and "arquivo |" not in lin]
    assert len(linhas) == 4
    for lin in linhas:
        colunas = [c.strip() for c in lin.strip("|").split("|")]
        assert colunas[0] in obras.NOMES_IMPLEMENTACAO
        assert HORA_RE.match(colunas[1]), lin
        assert colunas[2] == "2026-11-07 18:27", lin
        assert colunas[3] == "2026-11-07 18:50", lin


def test_data_e_hora_sao_reais_do_sistema_e_formato_visivel():
    pasta = _impl(inicio=None)
    texto = (pasta / "status.md").read_text(encoding="utf-8")
    registrado = re.search(r"- \*\*registrado:\*\* (.+)$", texto, re.M).group(1)
    assert HORA_RE.match(registrado), registrado
    agora = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    minuto_anterior = (dt.datetime.now() - dt.timedelta(minutes=1)).strftime(
        "%Y-%m-%d %H:%M")
    assert registrado >= minuto_anterior and registrado <= agora


# ── segurança dos dados ─────────────────────────────────────────────────────

@pytest.mark.parametrize("trecho,alvo", [
    (f"senha: {SENHA}", SENHA),
    (f"a api_key = '{SENHA}' está guardada", SENHA),
    ('{"gemini_api_key": "%s"}' % SENHA, SENHA),
    ("chave de API AIzaSyDUMMYdummy0123456789abcdefg", "AIzaSyDUMMYdummy0123456789abcdefg"),
    ("token ghp_" + "A" * 36, "ghp_" + "A" * 36),
    ("Authorization: Bearer " + "a1b2" * 8, "a1b2" * 8),
    ("-----BEGIN RSA PRIVATE KEY-----\nTUACABECA123\n-----END RSA PRIVATE KEY-----",
     "TUACABECA123"),
    ("ler o arquivo config/api_keys.json e aplicar", "config/api_keys.json"),
])
def test_prompt_com_segredo_sai_redigido_da_vault(vault_falsa, trecho, alvo):
    pasta = _impl()
    obras.gravar_prompt(pasta, f"executa a correção com {trecho} no fim")
    texto = (pasta / "prompt.md").read_text(encoding="utf-8")
    assert alvo not in texto, alvo
    assert obras.REDACTED in texto
    for arquivo in sorted(vault_falsa.rglob("*.md")):
        assert alvo not in arquivo.read_text(encoding="utf-8"), arquivo


def test_o_conteudo_do_arquivo_de_segredo_todo_sai_redigido():
    pasta = _impl()
    corpo = (
        '{\n'
        '  "gemini_api_key": "AIzaSyDUMMYdummy0123456789abcdefg",\n'
        '  "groq_api_key": "gsk_AAAAAAAAAAAAAAAAAAAA",\n'
        '  "email_password": "Sup3rS3Credealha01",\n'
        '  "linkedin_token": "' + "B" * 40 + '"\n'
        '}')
    obras.gravar_prompt(pasta, "cola o config inteiro: " + corpo)
    texto = (pasta / "prompt.md").read_text(encoding="utf-8")
    for segredo in ("AIzaSyDUMMYdummy0123456789abcdefg", "gsk_AAAAAAAAAAAAAAAAAAAA",
                    "Sup3rS3Credealha01", "B" * 40):
        assert segredo not in texto
    assert not obras.contem_segredo(texto)


def test_prompt_gravado_por_extenso_sem_corte_de_capricho():
    pasta = _impl()
    longo = "linha de instrução " * 400
    obras.gravar_prompt(pasta, longo)
    texto = (pasta / "prompt.md").read_text(encoding="utf-8")
    assert texto.count("linha de instrução") == 400
    assert "[cortado" not in texto


def test_hash_de_commit_nao_e_redigido_no_historico():
    pasta = _impl()
    hash_ = "4de3a5f1c2b3a4d5e6f7089a1b2c3d4e5f607182"
    obras.registrar_commits(pasta, [(hash_, "fix(ui): mute da interface")])
    texto = (pasta / "historico.md").read_text(encoding="utf-8")
    assert f"`{hash_}`" in texto
    assert obras.REDACTED not in texto


def test_redigir_detecta_e_contem_segredo_confirma():
    assert obras.contem_segredo(f"senha: {SENHA}")
    limpo = obras.redigir(f"senha: {SENHA}")
    assert SENHA not in limpo and not obras.contem_segredo(limpo)


# ── status: conjunto fechado e o portão da conclusão ────────────────────────

@pytest.mark.parametrize("status", obras.STATUS_PERMITIDOS)
def test_todo_status_do_conjunto_fechado_e_aceito(status):
    pasta = _impl(status="em aberto")
    _fechar(pasta)          # "concluída" é o único que exige evidência
    obras.definir_status(pasta, status)
    assert f"- **status:** {status}" in (pasta / "status.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("status", ["feito", "concluido", "OK", "pronto",
                                    "em conclusão", "cancelada", "concluída com ressalvas"])
def test_status_inventado_e_rejeito_sem_escrever(status):
    pasta = _impl(status="em aberto")
    antes = (pasta / "status.md").read_text(encoding="utf-8")
    with pytest.raises(ValueError) as erro:
        obras.definir_status(pasta, status)
    assert "conjunto fechado" in str(erro.value)
    assert (pasta / "status.md").read_text(encoding="utf-8") == antes


def test_concluida_exige_commit_no_historico_e_teste_registrado():
    pasta = _impl(status="em execução")
    with pytest.raises(ValueError) as erro:
        obras.definir_status(pasta, "concluída")
    saida = str(erro.value)
    assert "historico.md sem commit" in saida and "testes.md sem teste" in saida
    obras.registrar_commits(pasta, ["4de3a5f"])
    with pytest.raises(ValueError, match="testes.md sem teste"):
        obras.definir_status(pasta, "concluída")
    obras.registrar_testes(pasta, ["tests/test_obras.py::test_x"])
    obras.definir_status(pasta, "concluída")
    texto = (pasta / "status.md").read_text(encoding="utf-8")
    assert "- **status:** concluída" in texto
    assert re.search(r"- \*\*conclusão:\*\* \d{4}-\d{2}-\d{2} \d{2}:\d{2}", texto)


def test_pendente_de_validacao_ao_vivo_existe_porque_o_dono_mandou_escrever_assim():
    assert obras.STATUS_PENDENTE_VIVO == "pendente de validacao ao vivo"
    pasta = _impl(status=obras.STATUS_PENDENTE_VIVO,
                  conclusao=obras.STATUS_PENDENTE_VIVO)
    assert obras.STATUS_CONCLUIDA not in (pasta / "status.md").read_text(
        encoding="utf-8")


# ── histórico e testes ──────────────────────────────────────────────────────

def test_commits_entram_com_data_e_nao_se_repetem():
    pasta = _impl()
    obras.registrar_commits(
        pasta, [("4de3a5f", "fix(ui): mute da interface", "2026-11-07 18:50")])
    obras.registrar_commits(pasta, ["4de3a5f", "0ae8943"])
    linhas = [lin for lin in (pasta / "historico.md").read_text(
        encoding="utf-8").splitlines() if lin.startswith("- [")]
    assert len(linhas) == 2
    carimbo = linhas[0][3:linhas[0].index("]")]
    assert HORA_RE.match(carimbo), linhas[0]
    assert "(2026-11-07 18:50)" in linhas[0]


def test_hash_fora_do_formato_git_e_rejeitado():
    pasta = _impl()
    with pytest.raises(ValueError, match="commit inválido"):
        obras.registrar_commits(pasta, ["ganhado"])
    with pytest.raises(ValueError, match="commit inválido"):
        obras.registrar_commits(pasta, ["abc"])


def test_testes_entram_com_o_que_cada_um_cobre():
    pasta = _impl()
    obras.registrar_testes(pasta, [
        ("tests/test_obras.py::test_x", "criação da estrutura"),
        {"nome": "tests/test_obras.py::test_y", "cobre": "regeneração da raiz"},
        "tests/test_obras.py::test_z"])
    texto = (pasta / "testes.md").read_text(encoding="utf-8")
    assert "cobre: criação da estrutura" in texto
    assert "cobre: regeneração da raiz" in texto
    assert "`tests/test_obras.py::test_z`" in texto
    for lin in [l for l in texto.splitlines() if l.startswith("- [")]:
        assert HORA_RE.match(lin[3:lin.index("]")]), lin


# ── a raiz é gerada, nunca editada ──────────────────────────────────────────

def test_regeneracao_da_raiz_reflete_o_que_existe_por_baixo(vault_falsa):
    a = _impl("interface-e-voz", "mute da interface so microfone")
    b = _impl("email-e-linkedin", "rota unica de aviso")
    obras.registrar_commits(a, ["4de3a5f"])
    obras.registrar_commits(b, ["0ae8943", "a6a02fb"])
    obras.registrar_testes(a, ["tests/test_mute_somente_microfone.py"])
    contagens = obras.atualizar_raiz()
    assert contagens == {"projetos": 2, "implementacoes": 2, "commits": 3, "testes": 1}
    geral = (vault_falsa / "status.md").read_text(encoding="utf-8")
    assert "### interface-e-voz" in geral and "### email-e-linkedin" in geral
    assert "mute-da-interface-so-microfone" in geral and "rota-unica-de-aviso" in geral
    historico = (vault_falsa / "historico.md").read_text(encoding="utf-8")
    assert historico.count("commit `") == 3
    for hash_ in ("4de3a5f", "0ae8943", "a6a02fb"):
        assert f"`{hash_}`" in historico
    assert "test_mute_somente_microfone" in (vault_falsa / "testes.md").read_text(
        encoding="utf-8")
    progresso = (vault_falsa / "progresso.md").read_text(encoding="utf-8")
    assert "- **implementações:** 2" in progresso
    assert "pendente de validacao ao vivo: 2" in progresso


def test_edicao_manual_da_raiz_e_sobrescrita_pela_regeneracao(vault_falsa):
    _impl()
    for nome in obras.NOMES_RAIZ:
        (vault_falsa / nome).write_text("editado na mão pelo capricho\n",
                                        encoding="utf-8")
    obras.atualizar_raiz()
    for nome in obras.NOMES_RAIZ:
        texto = (vault_falsa / nome).read_text(encoding="utf-8")
        assert "editado na mão pelo capricho" not in texto, nome
        assert "atualizar_raiz()" in texto


def test_readme_da_raiz_explica_que_os_gerais_sao_regenerados(vault_falsa):
    _impl()
    README = (vault_falsa / "README.md").read_text(encoding="utf-8")
    assert "atualizar_raiz()" in README
    for nome in (*obras.NOMES_IMPLEMENTACAO, "progresso.md"):
        assert nome in README


def test_readme_nao_e_sobreposto_se_o_dono_mexeu_nele(vault_falsa):
    _impl()
    README = vault_falsa / "README.md"
    README.write_text("nota minha do dono\n", encoding="utf-8")
    obras.atualizar_raiz()
    assert "nota minha do dono" in README.read_text(encoding="utf-8")


# ── referências e recusa de vazio ───────────────────────────────────────────

def test_implementacao_aceita_caminho_projeto_titulo_e_titulo_unico():
    pasta = _impl()
    assert obras._implementacao(pasta) == pasta
    assert obras._implementacao(
        "interface-e-voz/mute da interface so microfone") == pasta
    assert obras._implementacao("mute da interface so microfone") == pasta
    with pytest.raises(ValueError, match="não encontrada"):
        obras._implementacao("implementação que não existe")


def test_titulo_repetido_em_dois_projetos_e_ambiguo_sem_o_projeto():
    obras.registrar_implementacao("projeto-um", "mesmo titulo")
    obras.registrar_implementacao("projeto-dois", "mesmo titulo")
    with pytest.raises(ValueError, match="ambíguo"):
        obras._implementacao("mesmo titulo")


def test_projeto_ou_titulo_vazio_nao_criam_pasta(vault_falsa):
    with pytest.raises(ValueError):
        obras.abrir_projeto("   ")
    with pytest.raises(ValueError):
        obras.registrar_implementacao("projeto-x", "   ")
    assert not (vault_falsa / "projeto-x").exists()


# ── a vault nunca é git, nem dentro de git ──────────────────────────────────

def test_o_caminho_padrao_da_vault_mora_fora_do_repositorio():
    puro = importlib.reload(obras)
    try:
        assert puro.BASE_DIR not in puro.OBRAS_DIR.parents
        assert puro.OBRAS_DIR == Path.home() / "Gino-Obras"
        assert not (puro.BASE_DIR / "Gino-Obras").exists()
    finally:
        importlib.reload(obras)


def test_caminho_dentro_do_repositorio_e_recusado(monkeypatch):
    monkeypatch.setenv("JARVIS_OBRAS_DIR", str(obras.BASE_DIR / "Gino-Obras"))
    with pytest.raises(ValueError, match="dentro do repositório"):
        obras.obras_dir()


@pytest.mark.skipif(not Path(GIT).exists(), reason="sem git na máquina")
def test_nada_da_vault_aparece_no_git_do_projeto(vault_falsa):
    _impl()
    saida = subprocess.run(
        [GIT, "-C", str(obras.BASE_DIR), "status", "--porcelain",
         "--untracked-files=all"],
        capture_output=True, text=True, timeout=120).stdout
    assert "Gino-Obras" not in saida
    assert str(vault_falsa) not in saida


@pytest.mark.skipif(not Path(GIT).exists(), reason="sem git na máquina")
def test_a_vault_em_si_nao_e_repositorio_nem_filial_de_um(vault_falsa):
    _impl()
    dentro = subprocess.run([GIT, "-C", str(vault_falsa), "rev-parse",
                             "--show-toplevel"],
                            capture_output=True, text=True, timeout=60)
    assert dentro.returncode != 0, dentro.stdout
    fora = subprocess.run([GIT, "-C", str(obras.BASE_DIR), "check-ignore", "-v",
                          str(vault_falsa)],
                          capture_output=True, text=True, timeout=60)
    assert "outside repository" in (fora.stderr + fora.stdout).lower()
