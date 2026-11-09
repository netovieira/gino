"""Uma pasta por plano (ordem do dono, 09/11/2026) e os bugs previsíveis dela.

Ordem literal: "CADA PLANO TEM QUE TER SUA PASTA, E DENTRO DELA TER o
``plan.md``, ``status.md``, ``progresso.md``, ``historico.md``, ``testes.md``,
``prompt.md``, E os DIRETORIOS ``steps`` E DENTRO DE STEPS ``tasks``, E OS
ARQUIVOS REFERENTES AOS STEPS E TASKS DEVEM SEGUIR O PADRAO/REGRA".

Este arquivo é a prova do mecanismo, não a promessa. Cada teste abaixo força
UM bug que ia acontecer de propósito e não aconteceu porque há tratamento:

* rodar duas vezes duplicando pasta ou pisando edição do dono;
* a mesma obra em duas pastas com nomes diferentes (a que existe hoje em
  ``~/Jarvis/Planos/`` foi montada à mão com o nome do arquivo);
* fonte que não existe virar ``plan.md`` vazio;
* arquivo ocupando o lugar da pasta;
* cofre sem permissão de escrita, com nome e caminho no erro;
* plano sem matéria para slug;
* as duas cópias do mesmo plano divergindo sem ninguém medir;
* esquecimento de que o Obsidian do dono abre OUTRO cofre
  (``/home/anthero/Documentos/Obsidian Vault``) e ``~/Jarvis`` é o cofre
  exclusive do assistente -- nada dos testes toca o cofre dele.

Nenhum teste escreve no cofre real: ``VAULT``, ``ACTIVITY_LOG`` e
``registered_vault`` são trocados por ``tmp_path``.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import obsidian  # noqa: E402

TODAY = dt.date.today().isoformat()
NOME_PLANO = "PLANO-2026-11-09-fluxo-agendamento-e-avisos-por-evento.md"
SLUG = "fluxo-agendamento-e-avisos-por-evento"
#: A pasta que a rodada anterior montou À MÃO, com o nome do arquivo.
PASTA_A_MAO = "PLANO-2026-11-09-fluxo-agendamento-e-avisos-por-evento"
ESQUELETO = ("plan.md", "status.md", "progresso.md", "historico.md",
             "testes.md", "prompt.md")


@pytest.fixture
def cofre(tmp_path, monkeypatch):
    """Dois cofres falsos: o meu (``~/Jarvis``) e o do dono (``Documentos``)."""
    meu = tmp_path / "Jarvis"
    dono = tmp_path / "Documentos" / "Obsidian Vault"
    dono.mkdir(parents=True)
    (dono / f"{PASTA_A_MAO}.md").write_text(
        "# Fluxo de agendamento e avisos por evento\n\noriginal do dono\n",
        encoding="utf-8")
    monkeypatch.setattr(obsidian, "VAULT", meu)
    monkeypatch.setattr(obsidian, "PLANS_DIR", tmp_path / "qoder" / "plans")
    monkeypatch.setattr(obsidian, "ACTIVITY_LOG", tmp_path / "activity.log")
    monkeypatch.setattr(obsidian, "NEXTCLOUD_DIR", tmp_path / "Nextcloud")
    monkeypatch.setattr(obsidian, "registered_vault", lambda: str(dono))
    monkeypatch.delenv("JARVIS_OBSIDIAN_VAULT", raising=False)
    monkeypatch.delenv("JARVIS_VAULT_NAME_POLICY", raising=False)
    return {"meu": meu, "dono": dono, "log": tmp_path / "activity.log",
            "tmp": tmp_path}


def _linhas(log: Path, evento: str) -> list[str]:
    if not log.is_file():
        return []
    return [ln for ln in log.read_text(encoding="utf-8").splitlines()
            if f"event={evento}" in ln]


# ── a estrutura pedida ──────────────────────────────────────────────────────

def test_a_pasta_tem_os_seis_arquivos_e_os_diretorios(cofre):
    pasta = obsidian.create_plan_folder(NOME_PLANO, dia=TODAY)
    assert pasta.name == SLUG
    for nome in ESQUELETO:
        assert (pasta / nome).is_file(), nome
    assert (pasta / "steps").is_dir()
    assert (pasta / "steps" / "tasks").is_dir()
    # nada de arquivo inventado fora do que o dono nomeou
    assert sorted(p.name for p in pasta.iterdir() if p.is_file()) == \
        sorted(ESQUELETO)


def test_o_texto_do_plano_e_co_piado_para_o_plan_md(cofre):
    original = cofre["dono"] / f"{PASTA_A_MAO}.md"
    pasta = obsidian.create_plan_folder(NOME_PLANO, fonte=original, dia=TODAY)
    corpo = (pasta / "plan.md").read_text(encoding="utf-8")
    assert "original do dono" in corpo
    assert "# Fluxo de agendamento e avisos por evento" in corpo
    assert "## Plano" in corpo or "---" in corpo
    # título sai do H1, não do nome do arquivo
    assert "agendamento" in (pasta / "status.md").read_text(
        encoding="utf-8").lower()


def test_plan_md_de_clara_o_padrao_de_steps_e_tasks(cofre):
    pasta = obsidian.create_plan_folder(NOME_PLANO, dia=TODAY)
    corpo = (pasta / "plan.md").read_text(encoding="utf-8")
    assert "TIPO-AAAA-MM-DD-slug" in corpo
    assert obsidian.STEP_TIPO in corpo and obsidian.TASK_TIPO in corpo
    # segredo não entra: o plano diz como endereçar a chave, nunca o valor
    assert "[key:" in corpo


# ── idempotência (rodar duas vezes) ────────────────────────────────────────

def test_rodar_duas_vezes_nao_duplica_nem_pisa(cofre):
    """O teste que a ordem pede: a segunda passada não cria nem reescreve."""
    original = cofre["dono"] / f"{PASTA_A_MAO}.md"
    primeira = obsidian.create_plan_folder(NOME_PLANO, fonte=original,
                                           dia=TODAY)
    passos = sorted(p.name for p in (cofre["meu"] / "Planos").iterdir())

    # o dono edita status.md na tela do Obsidian entre uma passada e outra
    (primeira / "status.md").write_text("# edição do dono, não perder\n",
                                        encoding="utf-8")
    notas = {p: p.read_bytes() for p in primeira.rglob("*.md")}
    carimbos = {p: p.stat().st_mtime_ns for p in notas}
    segunda = obsidian.create_plan_folder(NOME_PLANO, fonte=original,
                                          dia=TODAY)

    assert segunda == primeira
    assert sorted(p.name for p in (cofre["meu"] / "Planos").iterdir()) == \
        passos, "segunda pasta para o mesmo plano"
    assert (primeira / "status.md").read_text(
        encoding="utf-8") == "# edição do dono, não perder\n"
    for caminho, bytes_ in notas.items():
        assert caminho.read_bytes() == bytes_, caminho
    for caminho, ns in carimbos.items():
        assert caminho.stat().st_mtime_ns == ns, f"{caminho} foi reescrito"


def test_a_pasta_montada_a_mao_com_nome_de_arquivo_e_reutilizada(cofre):
    """Hoje o cofre real tem ``Planos/PLANO-2026-11-09-<slug>/plan.md``.

    Sem resolução de alias, a mesma obra ganharia ``Planos/<slug>/`` ao lado:
    duas pastas, duas verdades. O que existe vence, e o resto do esqueleto
    entra DENTRO da pasta que já estava lá.
    """
    mao = cofre["meu"] / "Planos" / PASTA_A_MAO
    mao.mkdir(parents=True)
    (mao / "plan.md").write_text("# plano escrito à mão\n", encoding="utf-8")
    pasta = obsidian.create_plan_folder(NOME_PLANO, dia=TODAY)
    assert pasta == mao
    assert [p.name for p in (cofre["meu"] / "Planos").iterdir()] == \
        [PASTA_A_MAO], "criou pasta gêmea"
    assert (mao / "plan.md").read_text(encoding="utf-8") == \
        "# plano escrito à mão\n"
    for nome in ESQUELETO[1:]:
        assert (mao / nome).is_file(), nome
    assert any("alias=true" in ln for ln in
               _linhas(cofre["log"], "vault_pasta_de_plano"))


def test_os_tres_nomes_do_mesmo_plano_caem_na_mesma_pasta(cofre):
    """Arquivo, pasta à mão e slug: um plano, uma casa.

    O título livre NOT é uma dessas formas: "fluxo de agendamento e avisos por
    evento" normaliza em outro slug e ganharia outra pasta -- por isso o
    chamador passa o nome do arquivo ou o id, nunca uma frase. O teste grava
    essa limitação para ninguém "consertar" o que é decisão.
    """
    obsidian.create_plan_folder(NOME_PLANO, dia=TODAY)
    caminhos = {obsidian.plan_folder(NOME_PLANO),
                obsidian.plan_folder(PASTA_A_MAO),
                obsidian.plan_folder(f"{SLUG}.md"),
                obsidian.plan_folder(SLUG)}
    assert len(caminhos) == 1, caminhos
    assert obsidian.plan_folder("fluxo de agendamento e avisos") == \
        cofre["meu"] / "Planos" / "fluxo-de-agendamento-e-avisos"


def test_slug_de_titulo_livre_e_ascii_e_estavel(cofre):
    """Acento não entra em caminho: o mesmo título sempre dá o mesmo slug."""
    primeiro = obsidian.plan_folder_slug("Plano de Ação & Cadastro (2026)")
    assert primeiro.isascii() and " " not in primeiro
    assert primeiro == obsidian.plan_folder_slug("Plano de Ação & Cadastro (2026)")


# ── o que a função RECUSA em vez de improvisar ─────────────────────────────

def test_fonte_inexistente_e_recusada_sem_meia_nota(cofre):
    sumido = cofre["dono"] / "PLANO-2026-11-09-ninguem-escreveu-este.md"
    with pytest.raises(ValueError) as erro:
        obsidian.create_plan_folder(sumido.name, fonte=sumido)
    assert "inexistente" in str(erro.value)
    assert not (cofre["meu"] / "Planos" / obsidian.plan_folder_slug(
        sumido.name)).exists(), "criou pasta com plano vazio"


def test_fonte_que_e_pasta_e_recusada(cofre):
    with pytest.raises(ValueError) as erro:
        obsidian.create_plan_folder(NOME_PLANO, fonte=cofre["dono"])
    assert "pasta" in str(erro.value)


def test_arquivo_no_lugar_da_pasta_e_colisao_de_nome(cofre):
    ocupado = cofre["meu"] / "Planos" / SLUG
    obsidian.ensure_vault()
    ocupado.write_text("eu sou um arquivo, não uma pasta\n", encoding="utf-8")
    with pytest.raises(ValueError) as erro:
        obsidian.create_plan_folder(NOME_PLANO)
    assert "colisão de nome" in str(erro.value)
    assert ocupado.read_text(encoding="utf-8").startswith("eu sou um arquivo")
    assert _linhas(cofre["log"], "vault_pasta_colisao")


def test_cofre_sem_permisso_e_OSError_com_caminho_e_nome(cofre):
    """Permissão negada no cofre: erro legível, nunca traceback mudo."""
    obsidian.ensure_vault()
    planos = cofre["meu"] / "Planos"
    planos.chmod(0o500)
    try:
        with pytest.raises(OSError) as erro:
            obsidian.create_plan_folder("PLANO-2026-11-09-outro-plano-de-teste.md")
    finally:
        planos.chmod(0o700)
    mensagem = str(erro.value)
    assert "sem permissão" in mensagem and str(planos) in mensagem
    assert _linhas(cofre["log"], "vault_pasta_sem_permicao")


def test_escrita_do_esqueleto_sem_permisso_tambem_e_nomeada(cofre):
    """A pasta existe, o arquivo não entra: o mesmo diagnóstico na escrita."""
    pasta = obsidian.create_plan_folder(NOME_PLANO, dia=TODAY)
    (pasta / "status.md").unlink()
    pasta.chmod(0o500)
    try:
        with pytest.raises(OSError) as erro:
            obsidian.create_plan_folder(NOME_PLANO, dia=TODAY)
    finally:
        pasta.chmod(0o700)
    assert "sem permissão para escrever" in str(erro.value)
    assert str(pasta / "status.md") in str(erro.value)


@pytest.mark.parametrize("sem_slug", ["", "   ", "2026-11-09", "---",
                                      "2026-11-09.md", "…"])
def test_plano_sem_materia_para_slug_e_recusado(cofre, sem_slug):
    """Plano sem slug = pasta sem nome = lixo no cofre. Recusa antes."""
    with pytest.raises(ValueError):
        obsidian.create_plan_folder(sem_slug)
    with pytest.raises(ValueError):
        obsidian.plan_folder(sem_slug)


def test_data_fora_do_formato_e_recusada(cofre):
    with pytest.raises(ValueError):
        obsidian.create_plan_folder(NOME_PLANO, dia="09-11-2026")


# ── as duas cópias: medida, não juramento ──────────────────────────────────

def test_copias_divergindo_sao_medidas_e_nao_sao_sobre_postas(cofre):
    original = cofre["dono"] / f"{PASTA_A_MAO}.md"
    pasta = obsidian.create_plan_folder(NOME_PLANO, fonte=original, dia=TODAY)
    plano_md = pasta / "plan.md"
    antes = plano_md.read_bytes()
    veredito = obsidian.divergem_copias_do_plano(NOME_PLANO)
    assert veredito["iguais"] is False or veredito["copias"], veredito

    original.write_text("# mesmo título\n\ntexto MUDADO na cópia do dono\n",
                        encoding="utf-8")
    veredito = obsidian.divergem_copias_do_plano(NOME_PLANO)
    assert veredito["motivo"] == "copias_divergindo"
    assert not veredito["iguais"]
    hashes = {c["sha"] for c in veredito["copias"]}
    assert len(hashes) > 1, veredito["copias"]

    # o portão de materialização não pisa a cópia nem apaga o original
    assert obsidian.create_plan_folder(NOME_PLANO, fonte=original,
                                       dia=TODAY) == pasta
    assert plano_md.read_bytes() == antes
    assert "texto MUDADO na cópia do dono" not in plano_md.read_text(
        encoding="utf-8")
    assert original.is_file() and "MUDADO" in original.read_text(
        encoding="utf-8")
    assert _linhas(cofre["log"], "vault_copias_divergindo")


def test_nada_do_teste_sai_do_cofre_do_assistente(cofre):
    """O cofre do dono (``Documentos``) não recebe nada por engano.

    Fato medido: o Obsidian desta máquina tem registrado
    ``/home/anthero/Documentos/Obsidian Vault`` -- que NÃO é onde o código
    escreve (``~/Jarvis`` é o cofre exclusive do assistente, decisão 2 do
    plano de fluxo de 07/11). O teste prova os dois lados: a pasta nasceu no
    meu cofre, e o cofre dele continua com os arquivos dele, intactos.
    """
    obsidian.create_plan_folder(NOME_PLANO, dia=TODAY)
    assert (cofre["meu"] / "Planos" / SLUG).is_dir()
    assert not (cofre["dono"] / "Planos" / SLUG).exists()
    assert not (cofre["dono"] / "Memoria").exists()
    assert obsidian._vault() == cofre["meu"]
    assert obsidian.cofre_do_dono() == cofre["dono"]
    assert (cofre["dono"] / f"{PASTA_A_MAO}.md").is_file(), "original sumiu"


# ── REGRA de nomes: esqueleto é exceção declarada; steps/tasks NÃO são ─────

def test_esqueleto_e_isento_mas_continua_fora_do_padrao(cofre):
    pasta = obsidian.create_plan_folder(NOME_PLANO, dia=TODAY)
    for nome in ESQUELETO:
        alvo = pasta / nome
        assert obsidian.is_exempt_name(alvo) is True, nome
        # isento != dentro da REGRA: o validador ainda chama o nome de fora
        assert obsidian.name_errors(alvo.name), nome
        assert alvo.name == nome, "o portão renomeou o que o dono nomeou"
        assert alvo.name not in obsidian.CODE_OWNED_NAMES


def test_steps_e_tasks_seguem_a_REGRA_e_a_pasta_ja_existente(cofre):
    pasta = obsidian.create_plan_folder(NOME_PLANO, dia=TODAY)
    passo = obsidian.create_step(NOME_PLANO, 1, "montar o esqueleto",
                                 dia=TODAY)
    tarefa = obsidian.create_task(NOME_PLANO, 1, "criar o portão",
                                  passo="montar o esqueleto", dia=TODAY)
    assert passo.parent == pasta / "steps"
    assert tarefa.parent == pasta / "steps" / "tasks"
    for caminho in (passo, tarefa):
        assert obsidian.name_errors(caminho.name) == [], caminho.name
        assert obsidian.is_exempt_name(caminho) is False
        assert obsidian.check_name(caminho.name) == caminho.name
    assert f"{TODAY}" in passo.name
    # idempotência também aqui: mesmo passo de novo não duplica
    assert obsidian.create_step(NOME_PLANO, 1, "montar o esqueleto",
                                dia=TODAY) == passo
    assert len(list((pasta / "steps").glob("*.md"))) == 1


def test_step_sem_titulo_continha_dentro_da_REGRA(cofre):
    """Título vazio não produz nome-sujo: a sequência segura o nome.

    O que se exige aqui é o padrão, não a criatividade: ``build_name`` monta,
    ``check_name`` valida, e o portão não deixa passar nome fora da REGRA.
    """
    pasta = obsidian.create_plan_folder(NOME_PLANO, dia=TODAY)
    passo = obsidian.create_step(NOME_PLANO, 3, "   ", dia=TODAY)
    assert passo.parent == pasta / "steps"
    assert obsidian.name_errors(passo.name) == [], passo.name
    assert "step-03" in passo.name
    with pytest.raises(ValueError):
        obsidian.create_step(NOME_PLANO, 4, "  ", dia="13/11/2026")


def test_step_de_plano_novo_usa_a_mesma_resolucao_de_nome(cofre):
    """Passo e esqueleto têm de cair na MESMA pasta, sempre."""
    pasta = obsidian.create_plan_folder(PASTA_A_MAO, dia=TODAY)
    assert pasta.name == SLUG, "nome canónico quando ainda não há pasta"
    passo = obsidian.create_step(PASTA_A_MAO, 2, "portão de aprovação",
                                 dia=TODAY)
    assert passo.parent == pasta / "steps"
    assert obsidian.plan_folder(NOME_PLANO) == pasta
    assert len(list((cofre["meu"] / "Planos").iterdir())) == 1


def test_log_de_pasta_de_plano_registra_se_esta_no_cofre_aberto(cofre):
    """``registradas=false`` é o honesto: o dono não vê isto no Obsidian dele."""
    obsidian.create_plan_folder(NOME_PLANO, dia=TODAY)
    linhas = _linhas(cofre["log"], "vault_pasta_de_plano")
    assert linhas and "registradas=false" in linhas[-1], linhas
    assert f"plano={SLUG}" in linhas[-1], linhas[-1]
