"""A branch de trabalho dentro do plano -- a ordem do dono de 10/11/2026.

WhatsApp dele, à letra: "ja implementa no fluxo de trabalho para as fases e
tasks criadas ter la qual branch elas devem trabalhar... voce define a branch
daquele plano ou daquela fase.. fica mais facil gerir a worktree". Antes disto
o portão sabia fase, status, commit e evidência (commit ``910e568``) e nada
sobre ONDE o trabalho acontece: duas perguntas iguais ("o que está em que
branch?" e "de onde veio este commit?") não tinham resposta no disco.

Vocabulário fechado, como o do status: ``plano/<slug-do-plano>`` (a convenção,
derivada do id, com o mesmo tratamento ASCII de ``core/obsidian.py``),
``main`` (fato histórico) e ``""`` (não declarado). Nome torto é recusa que
diz o vocabulário, nunca interpretação.

Um teste por regra nova:

1. **nome validado** -- fora do vocabulário é recusa, fail-closed;
2. **padrão automático** -- ``registrar_plano`` já nasce com ``plano/<slug>``;
3. **herança por fase e tarefa** -- quem não fala herda, quem fala declara;
4. **conclusão expõe o ramo** -- e concluir sem declarar vira TRILHA
   ("branch não declarada"), não exceção: precedente
   ``actiongate.ORIGIN_UNDECLARED``;
5. **idempotência com branch** -- reinscrever não duplica, não pisa
   ``concluida``, não apaga ramo já registrado;
6. **estado antigo sem o campo** -- lido como "não declarado", sem perder
   chave;
7. **quadro e resumo contam por branch** -- "o que está em que branch".

Hermetismo igual ao de ``tests/test_workflow_gate_fases.py``: estado, config e
log em ``tmp_path``, a fixture ELEVA erro se qualquer um dos três cair fora do
diretório temporário, e o md5 do estado REAL é conferido no fim de cada teste.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import workflow_gate  # noqa: E402

#: O arquivo de estado do dono, que nenhum teste pode tocar.
ESTADO_REAL = Path(workflow_gate.__file__).resolve().parent.parent / "memory" / "workflow_gate.json"


def _md5(caminho: Path) -> str:
    if not caminho.is_file():
        return "ausente"
    return hashlib.md5(caminho.read_bytes()).hexdigest()


@pytest.fixture
def porta(tmp_path, monkeypatch):
    """Estado/config/log isolados, com guarda contra o caminho real."""
    estado = tmp_path / "workflow_gate.json"
    config = tmp_path / "workflow_gate_config.json"
    log = tmp_path / "activity.log"
    config.write_text(json.dumps({"version": 1, "ativo": True}),
                      encoding="utf-8")
    monkeypatch.setenv("JARVIS_WORKFLOW_GATE_STATE", str(estado))
    monkeypatch.setenv("JARVIS_WORKFLOW_GATE_CONFIG", str(config))
    monkeypatch.delenv("JARVIS_WORKFLOW_GATE", raising=False)
    monkeypatch.setattr(workflow_gate, "ACTIVITY_LOG", log)
    monkeypatch.setattr(workflow_gate, "DEFAULT_STATE", estado)
    monkeypatch.setattr(workflow_gate, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(workflow_gate, "_ultimo_ativo", None)
    monkeypatch.setattr(workflow_gate, "_ultimo_problema", "")
    # Escrever no caminho real tem que ELEVAR erro, não "passar por fora".
    for nome, caminho in (("estado", workflow_gate.state_path()),
                          ("config", workflow_gate.config_path()),
                          ("log", workflow_gate.ACTIVITY_LOG)):
        if not Path(caminho).is_relative_to(tmp_path):
            raise AssertionError(
                f"fixture furada: o {nome} do portão caiu fora do tmp_path "
                f"({caminho}) -- teste escreveria no estado real do dono")
    plano = tmp_path / "PLANO-2026-11-10-exemplo.md"
    plano.write_text("# Plano de exemplo\n", encoding="utf-8")
    antes = _md5(ESTADO_REAL)
    yield {"estado": estado, "config": config, "log": log, "plano": plano,
           "real_antes": antes}
    depois = _md5(ESTADO_REAL)
    if depois != antes:
        raise AssertionError(
            f"teste escreveu no estado REAL do portão ({ESTADO_REAL}): md5 "
            f"mudou de {antes} para {depois}")


def _aprovado(porta, pid="plano-exemplo", **campos):
    """O caminho feliz uma vez só: plano registrado, aprovado, com arquivo."""
    workflow_gate.registrar_plano(pid, "Plano de exemplo",
                                  arquivo=porta["plano"], **campos)
    workflow_gate.aprovar_plano(pid, quem="Anthero", fonte="whatsapp")
    return pid


def _estado(porta):
    return json.loads(porta["estado"].read_text(encoding="utf-8"))


MAPA = [
    {"fase": "F1 — núcleo do agendador",
     "tarefas": [{"id": "gatilho", "titulo": "gatilho chamável",
                  "status": "concluida", "commit": "cb49789",
                  "evidencia": "core/scheduler.py:1",
                  "quem": "Gino (executor)", "quando": "2026-11-10 09:00:00"}]},
    {"fase": "F2 — portões",
     "tarefas": [("barreira", "barreira da persona antes da escrita"),
                 "laco-vivo"]},
]

# ── 1. nome validado: vocabulário fechado, fail-closed ─────────────────────


@pytest.mark.parametrize("ramo", [
    "feature/login", "hotfix-urgente", "develop", "release/1", "main2",
    "plano/Com Espaco", "plano/Maiuscula", "plano/", "plano/-torto",
])
def test_branch_fora_do_vocabulario_e_recusada_nomeando_o_vocabulario(
        porta, ramo):
    with pytest.raises(ValueError) as erro:
        workflow_gate.registrar_plano("plano-torto", arquivo=porta["plano"],
                                      branch=ramo)
    mensagem = str(erro.value)
    assert "vocabulário fechado" in mensagem, mensagem
    assert "plano/<slug-do-plano>" in mensagem and "main" in mensagem
    assert "plano-torto" not in workflow_gate._carregar()["planos"], \
        "recusa não escreve: fail-closed"


@pytest.mark.parametrize("ramo", ["main", "plano/memoria-e-indice"])
def test_o_vocabulario_aceita_a_convencao_e_o_ramo_historico(porta, ramo):
    registro = workflow_gate.registrar_plano("plano-limpo",
                                             arquivo=porta["plano"],
                                             branch=ramo)
    assert registro["branch"] == ramo


def test_espaco_sozinho_nao_e_dito_a_favor_de_nada(porta):
    """``branch="   "`` é "não disse", não "apague o ramo": entra a convenção."""
    registro = workflow_gate.registrar_plano("plano-sem-ramo",
                                             arquivo=porta["plano"],
                                             branch="   ")
    assert registro["branch"] == "plano/plano-sem-ramo"


def test_o_acento_nao_entra_porque_a_validacao_recusa_e_o_slug_so_faz_ascii(
        porta):
    """Acento é recusa na boca de quem declara; só a convenção dobra o nome."""
    with pytest.raises(ValueError):
        workflow_gate.validar_branch("plano/automacao-de-reinício", "teste")
    assert workflow_gate._slug_de_branch(
        "Migração de Memória") == "migracao-de-memoria"
    assert workflow_gate.branch_do_plano(
        "tela-de-apelidos-dos-aparelhos-da-casa") == \
        "plano/tela-de-apelidos-dos-aparelhos-da-casa"
    # título com espaço vira o id, e o id é o que pareia com o estado
    assert workflow_gate.branch_do_plano(
        "Tela de Apelidos") == "plano/tela-de-apelidos"


def test_slug_de_branch_respeita_o_limite_sem_partir_palavra(porta):
    nome = "-".join(["parte"] * 40)
    ramo = workflow_gate.branch_do_plano(nome)
    assert ramo.startswith(workflow_gate.BRANCH_PADRAO_PREFIXO)
    slug = ramo[len(workflow_gate.BRANCH_PADRAO_PREFIXO):]
    assert len(slug) <= workflow_gate._BRANCH_SLUG_MAX_CHARS
    assert not slug.endswith("-") and "--" not in slug
    assert workflow_gate.validar_branch(ramo, "teste") == ramo


# ── 2. padrão automático e herança por fase/tarefa ─────────────────────────


def test_registrar_plano_define_a_branch_de_convencao_sem_ninguem_dizer(
        porta):
    pid = _aprovado(porta)
    assert _estado(porta)["planos"][pid]["branch"] == "plano/plano-exemplo"
    assert _estado(porta)["versao"] == workflow_gate.VERSAO_ESTADO


def test_quebrar_plano_grava_a_branch_em_cada_tarefa(porta):
    pid = _aprovado(porta)
    relatorio = workflow_gate.quebrar_plano(pid, MAPA)
    assert relatorio["branch"] == "plano/plano-exemplo"
    assert relatorio["ramos"] == ["plano/plano-exemplo"]
    plano = _estado(porta)["planos"][pid]
    assert plano["tarefas"]["gatilho"]["branch"] == "plano/plano-exemplo"
    assert plano["tarefas"]["laco-vivo"]["branch"] == "plano/plano-exemplo"


def test_a_fase_pode_declara_um_ramo_e_a_tarefa_vence_a_fase(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, [
        {"fase": "F1 — isolateiro", "branch": "plano/fase-isolateiro",
         "tarefas": [{"id": "t1"}, {"id": "t2", "branch": "main"},
                     {"id": "t3", "branch": ""}]},
        {"fase": "F2 — herdada", "tarefas": ["t4"]}])
    quadro = workflow_gate.quadro(pid)
    assert [galho["branch"] for galho in quadro["fases"]] == [
        "plano/fase-isolateiro", "plano/plano-exemplo"]
    linhas = {linha["id"]: linha["branch"]
              for galho in quadro["fases"] for linha in galho["tarefas"]}
    assert linhas == {"t1": "plano/fase-isolateiro", "t2": "main",
                      "t3": "", "t4": "plano/plano-exemplo"}, \
        "chave branch ausente é herança; "" dito é não declarado"
    assert quadro["por_branch"][
        workflow_gate.RAMO_SEM_DECLARACAO]["tarefas"] == 1


def test_quebrar_pode_declarar_o_ramo_de_um_plano_que_nao_tinha(porta):
    """Caminho da retro-aplicação: estado velho sem ramo ganha um por mapa."""
    pid = _aprovado(porta)
    workflow_gate.definir_branch(pid, "")
    workflow_gate.quebrar_plano(pid, [{"fase": "F1 — uma",
                                      "tarefas": ["t1"]}])
    assert _estado(porta)["planos"][pid]["tarefas"]["t1"]["branch"] == ""
    relatorio = workflow_gate.quebrar_plano(
        pid, [{"fase": "F1 — uma", "tarefas": ["t1"]}], branch="plano/obra")
    assert relatorio["branch"] == "plano/obra"
    assert _estado(porta)["planos"][pid]["tarefas"]["t1"]["branch"] == \
        "plano/obra"
    # terceira passada sem falar de ramo: o registrado permanece
    workflow_gate.quebrar_plano(pid, [{"fase": "F1 — uma",
                                      "tarefas": ["t1"]}])
    assert _estado(porta)["planos"][pid]["tarefas"]["t1"]["branch"] == \
        "plano/obra"


def test_trocar_a_branch_do_plano_por_quebrar_plano_e_recusado(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    with pytest.raises(ValueError) as erro:
        workflow_gate.quebrar_plano(pid, MAPA, branch="plano/segunda")
    assert "reescreveria o endereço" in str(erro.value)


def test_registrar_tarefa_herde_o_ramo_do_plano(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, [{"fase": "F1 — uma", "tarefas": ["t"]}])
    registro = workflow_gate.registrar_tarefa(pid, "solta", "tarefa nova",
                                              fase="F1 — uma")
    assert registro["tarefas"]["solta"]["branch"] == "plano/plano-exemplo"
    dita = workflow_gate.registrar_tarefa(pid, {"id": "propria",
                                                "fase": "F1 — uma",
                                                "branch": "main"}, "x")
    assert dita["tarefas"]["propria"]["branch"] == "main"


# ── 3./4. concluir: expõe o ramo, e o silêncio vira trilha não exceção ─────


def test_concluir_sem_branch_nao_e_excecao_e_vira_trilha(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, [{"fase": "F1 — uma", "branch": "",
                                       "tarefas": [{"id": "t1"}]}])
    tarefa = workflow_gate.concluir_tarefa("t1", quem="Gino (executor)",
                                          commit="2f0100a")
    assert tarefa["branch"] == "", "normaliza, nunca inventa origem de commit"
    linha = [item for item in _estado(porta)["historico"]
             if item.get("evento") == "tarefa_concluida"][-1]
    assert linha["branch"] == workflow_gate.TRILHA_SEM_RAMO
    assert workflow_gate.TRILHA_SEM_RAMO in porta["log"].read_text(
        encoding="utf-8")


def test_concluir_expoe_a_branch_de_onde_o_commit_veio(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    tarefa = workflow_gate.concluir_tarefa("barreira", quem="Gino (executor)",
                                          commit="2f0100a", branch="main")
    assert tarefa["commit"] == "2f0100a" and tarefa["branch"] == "main"
    linhas = porta["log"].read_text(encoding="utf-8")
    assert "decisao=concluir" in linhas and "branch=main" in linhas
    linha = [item for item in _estado(porta)["historico"]
             if item.get("evento") == "tarefa_concluida"][-1]
    assert linha["branch"] == "main"


@pytest.mark.parametrize("ramo", ["feature/x", "plano/Com Espaço", "release"])
def test_concluir_com_branch_fora_do_vocabulario_recusa_antes_de_escrever(
        porta, ramo):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    antes = porta["estado"].read_text(encoding="utf-8")
    with pytest.raises(ValueError) as erro:
        workflow_gate.concluir_tarefa("barreira", quem="Gino (executor)",
                                      commit="2f0100a", branch=ramo)
    assert "vocabulário fechado" in str(erro.value)
    assert porta["estado"].read_text(encoding="utf-8") == antes


def test_acrescentar_branch_a_quem_ja_concluiu_nao_reescreve_a_conclusao(
        porta):
    """É este o braço da retro-aplicação no estado do dono."""
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, [{"fase": "F1 — uma", "branch": "",
                                       "tarefas": [{"id": "gatilho",
                                                    "status": "concluida",
                                                    "commit": "cb49789",
                                                    "quem": "Gino (executor)",
                                                    "quando":
                                                    "2026-11-10 09:00:00"}]}])
    tarefa = workflow_gate.concluir_tarefa("gatilho", quem="Gino (executor)",
                                          commit="cb49789", branch="main")
    assert tarefa["status"] == "concluida" and tarefa["commit"] == "cb49789"
    assert tarefa["branch"] == "main"
    assert tarefa["concluida_em"] == "2026-11-10 09:00:00"
    assert tarefa["concluida_por"] == "Gino (executor)"
    assert any(item.get("evento") == "tarefa_ramo_acrescentado"
               for item in _estado(porta)["historico"])


def test_reafirmar_conclusao_apontando_outro_ramo_e_recusada(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, [{"fase": "F1 — uma",
                                       "tarefas": [{"id": "t1",
                                                    "branch": "main"}]}])
    workflow_gate.concluir_tarefa("t1", quem="Gino (executor)",
                                  commit="2f0100a")
    with pytest.raises(ValueError) as erro:
        workflow_gate.concluir_tarefa("t1", quem="Gino (executor)",
                                      commit="2f0100a", branch="plano/outra")
    assert "não se reescreve em silêncio" in str(erro.value)


# ── 5. definir_branch: corrigir endereço é ato com trilha ──────────────────


def test_definir_branch_por_plano_fase_e_tarefa(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    saida = workflow_gate.definir_branch(pid, "plano/outro-ramo",
                                         nota="dono definiu outra worktree")
    assert saida["nivel"] == "plano" and saida["antes"] == "plano/plano-exemplo"
    assert workflow_gate.definir_branch(
        pid, "main", fase="F2 — portões")["nivel"] == "fase"
    assert workflow_gate.definir_branch(
        pid, "main", tarefa="laco-vivo",
        nota="commit histórico entrou na main")["nivel"] == "tarefa"
    quadro = workflow_gate.quadro(pid)
    assert quadro["branch"] == "plano/outro-ramo"
    assert quadro["branch_por_fase"]["F2 — portões"] == "main"
    linhas = {linha["id"]: linha["branch"]
              for galho in quadro["fases"] for linha in galho["tarefas"]}
    assert linhas["laco-vivo"] == "main"
    assert any(item.get("evento") == "branch_definida"
               for item in _estado(porta)["historico"])


def test_corrigir_ramo_ja_registrado_exige_nota(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    with pytest.raises(ValueError) as erro:
        workflow_gate.definir_branch(pid, "main")
    assert "nota=<motivo>" in str(erro.value)
    saida = workflow_gate.definir_branch(pid, "main",
                                         nota="dono mandou mergiar local")
    assert saida["antes"] == "plano/plano-exemplo"


def test_definir_branch_em_fase_fora_do_mapa_recusa_nomeando_as_fases(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    with pytest.raises(ValueError) as erro:
        workflow_gate.definir_branch(pid, "main", fase="F9 — inventada")
    assert "não está nas fases declaradas" in str(erro.value)
    with pytest.raises(ValueError) as erro:
        workflow_gate.definir_branch(pid, "main", tarefa="ninguem")
    assert "não está no plano" in str(erro.value)
    with pytest.raises(ValueError) as erro:
        workflow_gate.definir_branch("plano-que-ningem-registrou", "main")
    assert workflow_gate.MENSAGEM_SEM_PLANO in str(erro.value)


# ── 6. estado antigo, sem o campo ───────────────────────────────────────────


def test_estado_sem_campo_branch_e_lido_como_nao_declarado(porta):
    """O estado REAL do dono é deste jeito hoje: 76 tarefas sem branch."""
    velho = {"versao": 2, "tarefa_ativa": "", "fila": [],
             "planos": {"p": {"id": "p", "titulo": "Plano p",
                              "fase": "aprovado",
                              "arquivo": str(porta["plano"]),
                              "fases": ["F1 — uma"],
                              "meu_campo": "valor",
                              "tarefas": {"t1": {"id": "t1",
                                                 "titulo": "velha",
                                                 "fase": "F1 — uma",
                                                 "status": "concluida",
                                                 "commit": "cb49789",
                                                 "desde": "2026-11-10 08:00",
                                                 "concluida_em":
                                                 "2026-11-10 09:00:00",
                                                 "concluida_por":
                                                 "Gino (executor)"}}}},
             "historico": []}
    porta["estado"].write_text(json.dumps(velho, ensure_ascii=False),
                                encoding="utf-8")
    quadro = workflow_gate.quadro("p")
    assert quadro["fases"][0]["tarefas"][0]["branch"] == ""
    assert quadro["por_branch"][workflow_gate.RAMO_SEM_DECLARACAO] == {
        "pendente": 0, "em_progresso": 0, "concluida": 1, "bloqueada": 0,
        "tarefas": 1}
    assert workflow_gate.resumo()["tarefas_sem_branch"] == 1
    # ler não poda: a chave que eu não conheço continua lá
    assert workflow_gate._carregar()["planos"]["p"]["meu_campo"] == "valor"


def test_ramo_colado_a_mao_fora_do_vocabulario_volta_ao_cesto(porta):
    quebrado = {"versao": 2, "planos": {"p": {
        "id": "p", "fase": "aprovado", "arquivo": str(porta["plano"]),
        "branch": "tudo-em-mim",
        "branch_por_fase": {"F1": "feature/x"},
        "tarefas": {"t1": {"id": "t1", "branch": "feature/x"}}}}}
    porta["estado"].write_text(json.dumps(quebrado, ensure_ascii=False),
                               encoding="utf-8")
    bruto = workflow_gate._carregar()["planos"]["p"]
    assert bruto["branch"] == "" and bruto["tarefas"]["t1"]["branch"] == ""
    assert bruto["branch_por_fase"] == {}
    assert workflow_gate.quadro("p")["por_branch"][
        workflow_gate.RAMO_SEM_DECLARACAO]["tarefas"] == 1
    assert workflow_gate.require_plan_approved("p"), "leitura não derruba"


# ── 7. quadro e resumo por branch ───────────────────────────────────────────


def test_quadro_e_resumo_respondem_o_que_esta_em_que_branch(porta):
    a = _aprovado(porta, "plano-a")
    b = _aprovado(porta, "plano-b", branch="main")
    workflow_gate.quebrar_plano(a, [{"fase": "F1 — uma",
                                     "tarefas": [{"id": "t1"},
                                                 {"id": "t2"}]}])
    workflow_gate.quebrar_plano(b, [{"fase": "F1 — outra",
                                     "tarefas": [{"id": "t3"}]}])
    workflow_gate.concluir_tarefa(f"{a}:t1", quem="Gino (executor)",
                                  commit="2f0100a", branch="main")
    quadro_a = workflow_gate.quadro(a)
    assert quadro_a["por_branch"]["main"]["concluida"] == 1
    assert quadro_a["por_branch"]["plano/plano-a"]["pendente"] == 1
    assert quadro_a["ramos"] == ["main", "plano/plano-a"]
    saida = workflow_gate.resumo()
    assert saida["por_branch"]["main"] == {"pendente": 1, "em_progresso": 0,
                                           "concluida": 1, "bloqueada": 0,
                                           "tarefas": 2}
    assert saida["por_branch"]["plano/plano-a"]["tarefas"] == 1
    assert saida["por_branch"]["main"]["tarefas"] == 2
    assert saida["ramos"] == ["main", "plano/plano-a"]
    assert saida["planos_por_branch"] == {"main": ["plano-b"],
                                          "plano/plano-a": ["plano-a"]}
    assert saida["tarefas"] == 3


def test_iniciar_tarefa_registra_o_ramo_na_trilha(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    workflow_gate.iniciar_tarefa("laco-vivo")
    linha = [item for item in _estado(porta)["historico"]
             if item.get("evento") == "tarefa_iniciada"]
    assert linha[-1]["branch"] == "plano/plano-exemplo"
