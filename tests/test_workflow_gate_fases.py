"""Fases e tarefas DENTRO do plano — a lei que faltava no portão.

Defeito ``DEF-20261110-104454``, ordem do dono por WhatsApp em 10/11/2026:

    "Junta o plano PLANO-2026-11-09-migrar-memoria-para-vault-obsidian com o
     PLANO-2026-11-10-indexar-projetos-no-vault-com-rastreio-de-pedido. E
     jogue como próximo da fila pra quebrar em fases e tasks e executar."

e a cobrança dele, na letra:

    "o estranho é que nao tem FASES E TASKS de NADA QUE FEZ HOJE.. GINO EU DEI
     A ORDEM DIRETA DE CRIAR FASES E TAREFAS E VOCE DISSE QUE IRIA FAZER
     ISSO.. CADE!???"

Raiz medida no código (não na conversa): ``core/workflow_gate.py`` tinha
``fase`` SÓ no ciclo de vida do plano (:data:`FASES`, :func:`definir_fase`), e
a tarefa era ``(id, título, desde)`` -- sem fase de obra, sem status, sem
commit, sem evidência. Resultado: nada entregue podia virar
"fase 2, tarefa 2.1, concluída no commit X".

Um teste por regra nova:

1. **fase obrigatória** -- plano já quebrado não ganha tarefa fora de fase;
2. **status fechado** -- fora de :data:`STATUS_DE_TAREFA` é recusa;
3. **concluir exige quem, quando e prova** -- commit validado como hash;
4. **idempotência do :func:`quebrar_plano`** -- não duplica, não pisa o que
   está concluído, não apaga o que ficou fora do mapa;
5. **leitura do estado antigo** -- versão 1 (tarefa plana, sem ``fases`` nem
   ``fila``) não explode e não perde chave;
6. **fila** -- o plano fundido entra como próximo da fila, e a ordem é dita,
   nunca inventada.

Hermetismo: estado, config e log em ``tmp_path``; o fixture ELEVA erro se
qualquer um dos três cair fora do diretório temporário, e o md5 do estado REAL
do repo é conferido no fim de cada teste (o portão do dono é um ponto de
escrita compartilhado -- teste não escreve nele).
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


def _aprovado(porta, pid="plano-exemplo"):
    """O caminho feliz uma vez só: plano registrado, aprovado, com arquivo."""
    workflow_gate.registrar_plano(pid, "Plano de exemplo",
                                  arquivo=porta["plano"])
    workflow_gate.aprovar_plano(pid, quem="Anthero", fonte="whatsapp")
    return pid


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

# ── 1. fase obrigatória ─────────────────────────────────────────────────────


def test_quebrar_plano_grava_fases_status_commit_e_evidencia(porta):
    pid = _aprovado(porta)
    relatorio = workflow_gate.quebrar_plano(pid, MAPA)
    assert relatorio["fases"] == ["F1 — núcleo do agendador", "F2 — portões"]
    assert relatorio["novas"] == ["gatilho", "barreira", "laco-vivo"]
    bruto = json.loads(porta["estado"].read_text(encoding="utf-8"))
    tarefa = bruto["planos"][pid]["tarefas"]["gatilho"]
    assert tarefa["fase"] == "F1 — núcleo do agendador"
    assert tarefa["status"] == "concluida"
    assert tarefa["commit"] == "cb49789"
    assert tarefa["evidencia"] == "core/scheduler.py:1"
    assert tarefa["concluida_por"] == "Gino (executor)"
    assert tarefa["concluida_em"] == "2026-11-10 09:00:00"


def test_plano_que_ja_tem_fases_recusa_tarefa_fora_de_fase(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    with pytest.raises(ValueError) as erro:
        workflow_gate.registrar_tarefa(pid, "soltinha", "sem fase nenhuma")
    mensagem = str(erro.value)
    assert "sem fase" in mensagem and "F1 — núcleo do agendador" in mensagem
    assert "DEF-20261110-104454" in mensagem, "a recusa nomeia o defeito"


def test_fase_de_fora_do_mapa_e_recusada_nomeando_as_do_mapa(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    with pytest.raises(ValueError) as erro:
        workflow_gate.registrar_tarefa(pid, "extra", "x", fase="F9 — inventada")
    assert "não está nas fases declaradas" in str(erro.value)


def test_rostulo_do_ciclo_de_vida_nao_vira_fase_de_obra(porta):
    """A palavra "fase" serve às duas coisas; o portão separa as duas."""
    pid = _aprovado(porta)
    with pytest.raises(ValueError) as erro:
        workflow_gate.quebrar_plano(pid, [{"fase": "aprovado",
                                          "tarefas": ["x"]}])
    assert "CICLO DO PLANO" in str(erro.value)
    with pytest.raises(ValueError):
        workflow_gate.quebrar_plano(pid, [{"fase": "   ", "tarefas": ["x"]}])


# ── 2. status fechado ───────────────────────────────────────────────────────


@pytest.mark.parametrize("status", workflow_gate.STATUS_DE_TAREFA)
def test_todo_status_do_conjunto_fechado_e_aceito(porta, status):
    pid = _aprovado(porta)
    mapa = [{"fase": "F1 — uma",
             "tarefas": [{"id": "t1", "titulo": "t", "status": status,
                          "commit": "cb49789", "quem": "Gino (executor)"}]}]
    workflow_gate.quebrar_plano(pid, mapa)
    dado = workflow_gate.quadro(pid)["fases"][0]["tarefas"][0]
    assert dado["status"] == status


@pytest.mark.parametrize("status", ["feita", "concluída", "wip", "quase", ""])
def test_status_fora_do_conjunto_fechado_e_recusado(porta, status):
    pid = _aprovado(porta)
    with pytest.raises(ValueError) as erro:
        workflow_gate.quebrar_plano(pid, [{"fase": "F1 — uma",
                                          "tarefas": [{"id": "t1",
                                                       "status": status}]}])
    assert "conjunto fechado" in str(erro.value) or "sem rótulo" in str(erro.value)


def test_quebrar_plano_nao_aprova_nem_derrota_a_fase_do_plano(porta):
    """Ter o mapa não é executar: status de entrega só com plano aprovado."""
    workflow_gate.registrar_plano("plano-gelado", arquivo=porta["plano"])
    with pytest.raises(ValueError) as erro:
        workflow_gate.quebrar_plano("plano-gelado", MAPA)
    assert "aguardando_aprovacao" in str(erro.value)
    bruto = json.loads(porta["estado"].read_text(encoding="utf-8"))
    assert bruto["planos"]["plano-gelado"]["fase"] == "aguardando_aprovacao"
    assert "gatilho" not in (bruto["planos"]["plano-gelado"].get("tarefas")
                             or {})


# ── 3. concluir exige quem, quando e prova ──────────────────────────────────


def test_concluir_sem_quem_e_recusado(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    with pytest.raises(ValueError) as erro:
        workflow_gate.concluir_tarefa("barreira", commit="2f0100a")
    assert "quem" in str(erro.value)


@pytest.mark.parametrize("campos", [
    {},                                   # nada
    {"commit": "não-hash"},               # hash que não é hash
    {"commit": "abc"},                    # curto demais
])
def test_concluir_sem_prova_ou_com_hash_inutilizavel_e_recusado(porta, campos):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    with pytest.raises(ValueError):
        workflow_gate.concluir_tarefa("barreira", quem="Gino (executor)",
                                      **campos)


def test_concluir_grava_quem_quando_commit_e_evidencia(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    tarefa = workflow_gate.concluir_tarefa("barreira", quem="Gino (executor)",
                                          commit="2f0100a",
                                          evidencia="core/home_aliases.py:1",
                                          quando="2026-11-10 11:00:00")
    assert tarefa["status"] == "concluida"
    assert tarefa["commit"] == "2f0100a"
    assert tarefa["concluida_por"] == "Gino (executor)"
    assert tarefa["concluida_em"] == "2026-11-10 11:00:00"
    linhas = porta["log"].read_text(encoding="utf-8")
    assert "decisao=concluir" in linhas and "commit=2f0100a" in linhas


def test_concluir_com_evidencia_mas_sem_commit_passa(porta):
    """Papel também fecha tarefa: a nota do vault é evidência legítima."""
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    tarefa = workflow_gate.concluir_tarefa("laco-vivo", quem="Gino (executor)",
                                          evidencia="NOTA-2026-11-10-proposta-"
                                                    "de-apelidos-da-casa")
    assert tarefa["status"] == "concluida" and tarefa["commit"] == ""


def test_iniciar_exige_plano_aprovado_e_aponta_a_tarefa_ativa(porta):
    workflow_gate.registrar_plano("gelado", arquivo=porta["plano"])
    workflow_gate.quebrar_plano("gelado", [{"fase": "F1 — uma",
                                            "tarefas": ["t1"]}])
    with pytest.raises(ValueError) as erro:
        workflow_gate.iniciar_tarefa("t1")
    assert "antes da aprovação" in str(erro.value)
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    workflow_gate.iniciar_tarefa("laco-vivo", quando="2026-11-10 11:30:00")
    bruto = json.loads(porta["estado"].read_text(encoding="utf-8"))
    assert bruto["tarefa_ativa"] == f"{pid}:laco-vivo"
    dado = workflow_gate.quadro(pid)["fases"][1]["tarefas"][1]
    assert dado["status"] == "em_progresso"
    assert dado["em_progresso_desde"] == "2026-11-10 11:30:00"


def test_mudar_status_nao_alcansa_concluida_nem_em_progresso(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    for status in ("concluida", "em_progresso"):
        with pytest.raises(ValueError) as erro:
            workflow_gate.mudar_status("laco-vivo", status)
        assert "mudar_status" in str(erro.value)
    tarefa = workflow_gate.mudar_status("laco-vivo", "bloqueada",
                                        nota="falta autorização dele p/ main.py",
                                        quem="Anthero")
    assert tarefa["status"] == "bloqueada"
    assert "main.py" in tarefa["nota"]


def test_reabrir_concluida_exige_nota_e_perde_a_prova_antiga(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    with pytest.raises(ValueError) as erro:
        workflow_gate.mudar_status("gatilho", "pendente")
    assert "reabrir sem nota" in str(erro.value)
    tarefa = workflow_gate.mudar_status("gatilho", "pendente",
                                       nota="commit errado, refeito em X")
    assert tarefa["status"] == "pendente" and tarefa["commit"] == ""
    historico = json.loads(porta["estado"].read_text(encoding="utf-8"))["historico"]
    assert any(item.get("evento") == "tarefa_status" for item in historico)


# ── 4. idempotência do quebrar_plano ────────────────────────────────────────


def test_quebrar_duas_vezes_nao_duplica_nem_pisa_concluida(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    relatorio = workflow_gate.quebrar_plano(pid, MAPA)
    assert relatorio["novas"] == [] and relatorio["preservadas"] == ["gatilho"]
    bruto = json.loads(porta["estado"].read_text(encoding="utf-8"))
    plano = bruto["planos"][pid]
    assert sorted(plano["tarefas"]) == ["barreira", "gatilho", "laco-vivo"]
    assert plano["fases"] == ["F1 — núcleo do agendador", "F2 — portões"]


def test_reinscricao_com_status_pendente_nao_derrota_a_concluida(porta):
    """A reinscrição pode estar desatualizada: o que está concluído fica."""
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    workflow_gate.quebrar_plano(pid, [{"fase": "F1 — núcleo do agendador",
                                       "tarefas": [{"id": "gatilho",
                                                    "titulo": "OUTRO TÍTULO",
                                                    "status": "pendente",
                                                    "commit": "aaaaaaa"}]}])
    dado = workflow_gate.quadro(pid)["fases"][0]["tarefas"][0]
    assert dado["status"] == "concluida"
    assert dado["commit"] == "cb49789"
    assert dado["titulo"] == "gatilho chamável"


def test_quebrar_nao_apaga_tarefa_que_ficou_fora_do_mapa(porta):
    pid = _aprovado(porta)
    workflow_gate.registrar_tarefa(pid, "antiga", "tarefa de antes")
    workflow_gate.quebrar_plano(pid, MAPA)
    quadro = workflow_gate.quadro(pid)
    cesto = [galho for galho in quadro["fases"]
             if galho["fase"] == workflow_gate.SEM_FASE]
    assert len(cesto) == 1, quadro["fases"]
    assert sorted(t["id"] for t in cesto[0]["tarefas"]) == ["antiga"]
    assert "antiga" in json.loads(
        porta["estado"].read_text(encoding="utf-8"))["planos"][pid]["tarefas"]


def test_quebrar_exige_fase_com_id_de_tarefa_unico(porta):
    pid = _aprovado(porta)
    with pytest.raises(ValueError) as erro:
        workflow_gate.quebrar_plano(pid, [
            {"fase": "F1 — uma", "tarefas": ["repetida"]},
            {"fase": "F2 — outra", "tarefas": ["repetida"]}])
    assert "duas fases" in str(erro.value)
    with pytest.raises(ValueError):
        workflow_gate.quebrar_plano(pid, [])


# ── 5. leitura do estado antigo ─────────────────────────────────────────────


def test_estado_versao_1_e_lido_normalizado_sem_explodir(porta):
    """O estado REAL do dono é deste formato: 5 planos, tarefas planas."""
    velho = {"versao": 1, "tarefa_ativa": "t-velha",
             "planos": {"plano-velho": {
                 "id": "plano-velho", "titulo": "Plano velho",
                 "fase": "aprovado", "arquivo": str(porta["plano"]),
                 "aprovado_por": "Anthero", "aprovado_em": "2026-11-09 22:00",
                 "fonte_aprovacao": "whatsapp",
                 "tarefas": {"t-velha": {"id": "t-velha",
                                         "titulo": "tarefa plana",
                                         "desde": "2026-11-09 23:56:45"}}}},
             "historico": [{"evento": "plano_aprovado",
                            "plano_id": "plano-velho"}],
             "atualizado": "2026-11-10 00:00:00"}
    porta["estado"].write_text(json.dumps(velho, ensure_ascii=False),
                               encoding="utf-8")
    assert workflow_gate.require_plan_approved("plano-velho")
    quadro = workflow_gate.quadro("plano-velho")
    assert [galho["fase"] for galho in quadro["fases"]] == [
        workflow_gate.SEM_FASE]
    tarefa = quadro["fases"][0]["tarefas"][0]
    assert tarefa["status"] == "pendente"
    assert tarefa["fase"] == "" and tarefa["commit"] == ""
    assert tarefa["desde"] == "2026-11-09 23:56:45"
    saida = workflow_gate.resumo()
    assert saida["tarefas"] == 1 and saida["tarefas_sem_fase"] == 1
    assert saida["por_status"]["pendente"] == 1
    assert saida["por_status"]["concluida"] == 0, "nada vira feito por suposição"


def test_leitura_nao_perde_chave_que_nao_conheco(porta):
    velho = {"versao": 1, "planos": {"p": {"id": "p", "fase": "rascunho",
                                          "meu_campo": "valor",
                                          "tarefas": {"t": {"id": "t",
                                                            "extra": 1}}}},
             "campo_desconhecido": {"x": 1}}
    porta["estado"].write_text(json.dumps(velho, ensure_ascii=False),
                               encoding="utf-8")
    workflow_gate.quebrar_plano("p", [{"fase": "F1 — uma", "tarefas": ["t"]}])
    bruto = json.loads(porta["estado"].read_text(encoding="utf-8"))
    assert bruto["planos"]["p"]["meu_campo"] == "valor"
    assert bruto["planos"]["p"]["tarefas"]["t"]["extra"] == 1
    assert bruto["campo_desconhecido"] == {"x": 1}


def test_tarefas_em_lista_ou_com_valores_estranhos_nao_derrubam_o_portao(porta):
    """Lista em vez de dicionário, status em texto livre, commit em prosa."""
    quebrado = {"versao": 1, "planos": {"p": {
        "id": "p", "fase": "aprovado", "arquivo": str(porta["plano"]),
        "tarefas": [{"id": "t1", "status": "FEITA", "commit": "concluída ontem"},
                    {"id": "t2"}]}}, "historico": ["linha que não é objeto"]}
    porta["estado"].write_text(json.dumps(quebrado, ensure_ascii=False),
                               encoding="utf-8")
    quadro = workflow_gate.quadro("p")
    linhas = quadro["fases"][0]["tarefas"]
    assert [linha["id"] for linha in linhas] == ["t1", "t2"]
    assert all(linha["status"] == "pendente" for linha in linhas)
    assert all(linha["commit"] == "" for linha in linhas)
    assert workflow_gate.require_plan_approved("p")
    saida = workflow_gate.resumo()
    assert saida["tarefas"] == 2


# ── 6. a fila ───────────────────────────────────────────────────────────────


def test_o_plano_fundido_e_o_proximo_da_fila(porta):
    """Ordem dele, à letra: "jogue como próximo da fila"."""
    workflow_gate.registrar_plano("outro-plano", arquivo=porta["plano"])
    pid = "memoria-e-indice-no-vault-um-plano-so"
    workflow_gate.registrar_plano(pid, "Memória e índice no vault",
                                  arquivo=porta["plano"])
    workflow_gate.empurrar_para_fila("outro-plano")
    workflow_gate.empurrar_para_fila(pid, 0)
    assert workflow_gate.proxima_da_fila() == pid
    assert workflow_gate.fila() == [pid, "outro-plano"]
    assert workflow_gate.resumo()["proxima_da_fila"] == pid


def test_a_ordem_da_fila_e_a_dita_nao_inventada(porta):
    pid = _aprovado(porta)
    workflow_gate.registrar_plano("segundo", arquivo=porta["plano"])
    workflow_gate.empurrar_para_fila(pid, 0)
    workflow_gate.empurrar_para_fila("segundo", 1)
    assert workflow_gate.fila() == [pid, "segundo"]
    workflow_gate.empurrar_para_fila("segundo", 0)
    assert workflow_gate.fila() == ["segundo", pid]
    assert workflow_gate.proxima_da_fila() == "segundo"


@pytest.mark.parametrize("posicao", [-1, 5, "último", None])
def test_posicao_fora_da_fila_e_recusada_nomeando_a_fila(porta, posicao):
    pid = _aprovado(porta)
    workflow_gate.empurrar_para_fila(pid)
    with pytest.raises(ValueError):
        workflow_gate.empurrar_para_fila(pid, posicao)
    assert workflow_gate.fila() == [pid]


def test_empurrar_plano_inexistente_recusa_e_tirar_da_fila_nao_apaga_o_plano(
        porta):
    pid = _aprovado(porta)
    with pytest.raises(ValueError) as erro:
        workflow_gate.empurrar_para_fila("nao-cadastrado")
    assert workflow_gate.MENSAGEM_SEM_PLANO in str(erro.value)
    workflow_gate.empurrar_para_fila(pid)
    assert workflow_gate.tirar_da_fila(pid) == []
    assert pid in workflow_gate._carregar()["planos"]
    assert workflow_gate.proxima_da_fila() == ""


def test_fila_com_id_fantasma_aponta_o_que_existe(porta):
    pid = _aprovado(porta)
    bruto = json.loads(porta["estado"].read_text(encoding="utf-8"))
    bruto["fila"] = ["plano-que-ningem-registrou", pid]
    porta["estado"].write_text(json.dumps(bruto, ensure_ascii=False),
                               encoding="utf-8")
    assert workflow_gate.proxima_da_fila() == pid
    assert "fila_fantasma" in porta["log"].read_text(encoding="utf-8")


# ── o quadro e o resumo que respondem "o que foi feito hoje?" ───────────────


def test_quadro_devolve_a_arvore_e_a_contagem_por_status(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    workflow_gate.concluir_tarefa("barreira", quem="Gino (executor)",
                                  commit="2f0100a")
    quadro = workflow_gate.quadro(pid)
    assert [galho["fase"] for galho in quadro["fases"]] == [
        "F1 — núcleo do agendador", "F2 — portões"]
    assert quadro["fases"][0]["concluidas"] == 1
    assert [linha["status"] for linha in quadro["fases"][1]["tarefas"]] == [
        "concluida", "pendente"]
    assert quadro["por_status"] == {"pendente": 1, "em_progresso": 0,
                                   "concluida": 2, "bloqueada": 0}
    assert quadro["arquivo"] == str(porta["plano"])
    assert quadro["aprovado_por"] == "Anthero"
    assert quadro["na_fila"] is False


def test_resumo_conta_planos_fases_tarefas_e_status(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, MAPA)
    saida = workflow_gate.resumo()
    assert saida["planos"] == 1
    assert saida["fases"] == 2
    assert saida["tarefas"] == 3
    assert saida["por_status"]["concluida"] == 1
    assert saida["por_status"]["pendente"] == 2
    assert saida["por_fase"]["aprovado"] == 1


def test_o_plano_aprovado_sem_mapa_ainda_passa_mas_o_portao_diz_o_que_falta(
        porta):
    """A lei nova não derruba o fluxo: plano aprovado continua liberando.

    O que muda é o aviso -- o portão passa a dizer em voz alta que o plano
    está sem fases, que é exatamente o defeito que ele cobrou.
    """
    pid = _aprovado(porta)
    resultado = workflow_gate.require_plan_approved(pid)
    assert resultado
    assert "não tem fases" in resultado.texto()
    workflow_gate.quebrar_plano(pid, MAPA)
    assert "não tem fases" not in workflow_gate.require_plan_approved(
        pid).texto()


def test_o_estado_nao_guarda_valor_de_segredo_na_evidencia(porta):
    segredo = "Sup3rS3Credealha01"
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, [{"fase": "F1 — uma",
                                       "tarefas": [{"id": "t1"}]}])
    workflow_gate.concluir_tarefa("t1", quem="Gino (executor)",
                                 evidencia=f"config api_key: {segredo}")
    bruto = porta["estado"].read_text(encoding="utf-8")
    assert segredo not in bruto
    assert "api_key" in bruto and "[key:" in bruto


def test_referencia_ambigua_entre_planos_e_dita_antes_de_escolher(porta):
    pid = _aprovado(porta)
    workflow_gate.quebrar_plano(pid, [{"fase": "F1 — uma", "tarefas": ["t"]},
                                      ])
    outro = _aprovado(porta, "outro-plano")
    workflow_gate.quebrar_plano(outro, [{"fase": "F1 — uma", "tarefas": ["t"]}])
    with pytest.raises(ValueError) as erro:
        workflow_gate.concluir_tarefa("t", quem="Gino (executor)",
                                      commit="cb49789")
    assert "forma qualificada" in str(erro.value)
    assert workflow_gate.concluir_tarefa(f"{pid}:t", quem="Gino (executor)",
                                        commit="cb49789")["commit"] == "cb49789"
