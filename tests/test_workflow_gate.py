"""core/workflow_gate.py — o portão que devolve "Preciso de um plano pra isso".

Ordem do dono, 09/11/2026: "O GATE DEVOLVE ALGO COMO 'PRECISO DE UM PLANO PRA
ISSO' FORCANDO O GINO A LEMBRAR QUE ELE TEM QUE CRIAR O PLANO E NÃO EXECUTAR
DIRETAMENTE". Estes testes são a prova do contrário: cada vez que um pedido sem
plano aprovado atravessa o portão, ele NÃO vira execução e a linha de evento
fica no log.

Nada aqui escreve no cofre, no repo nem no activity.log do dono: estado e log
são ``tmp_path`` pelo env ``JARVIS_WORKFLOW_GATE_STATE`` e pelo atributo
``workflow_gate.ACTIVITY_LOG``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import workflow_gate  # noqa: E402


@pytest.fixture
def porta(tmp_path, monkeypatch):
    """Estado, config e log isolados; um "plano" real no disco para a aprovação apontar."""
    estado = tmp_path / "workflow_gate.json"
    log = tmp_path / "activity.log"
    config = tmp_path / "workflow_gate_config.json"
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
    plano = tmp_path / "PLANO-2026-11-09-exemplo.md"
    plano.write_text("# Plano de exemplo\n", encoding="utf-8")
    return {"estado": estado, "log": log, "plano": plano, "config": config}


def _executar(resultado) -> int:
    """Contador de execução: só executa quem passou pelo portão."""
    return 1 if bool(resultado) else 0


def _linhas(log: Path) -> list[str]:
    if not log.is_file():
        return []
    return [ln for ln in log.read_text(encoding="utf-8").splitlines()
            if "event=workflow_gate" in ln]


# ── o veredito que o dono pediu ─────────────────────────────────────────────

def test_sem_plano_a_frase_e_a_do_dono(porta):
    resultado = workflow_gate.require_plan_approved("ajustar-o-botao-de-mute")
    assert not resultado
    assert resultado.ok is False
    assert resultado.texto().startswith(workflow_gate.MENSAGEM_SEM_PLANO)
    assert "Preciso de um plano pra isso" in resultado.texto()
    assert resultado.motivo == "nenhum_plano_para_a_referencia"


def test_o_gate_nao_executa_nada_e_deixa_rastro_no_log(porta):
    """O travão efectivo do plano de gates: 10 pedidos sem plano, 0 execuções."""
    execucoes = 0
    for n in range(10):
        resultado = workflow_gate.require_plan_approved(f"pedido-{n:02d}")
        execucoes += _executar(resultado)
    assert execucoes == 0
    linhas = _linhas(porta["log"])
    assert len(linhas) == 10
    assert all("decisao=bloqueado" in ln for ln in linhas)


def test_exigir_plano_aprovado_levanta_com_a_frase(porta):
    with pytest.raises(workflow_gate.SemPlanoAprovado) as erro:
        workflow_gate.exigir_plano_aprovado("trocar-a-rota-de-voz")
    assert "Preciso de um plano pra isso" in str(erro.value)


def test_referencia_vazia_sem_tarefa_ativa_bloqueia(porta):
    resultado = workflow_gate.require_plan_approved("")
    assert not resultado
    assert resultado.motivo == "sem_referencia_e_sem_tarefa_ativa"


# ── registrar não é aprovar ─────────────────────────────────────────────────

def test_registrar_plano_nasce_aguardando_aprovacao(porta):
    registro = workflow_gate.registrar_plano(
        "plano-exemplo", "Plano de exemplo", arquivo=porta["plano"])
    assert registro["fase"] == "aguardando_aprovacao"
    assert not workflow_gate.require_plan_approved("plano-exemplo")
    assert "plano_na_fase_aguardando_aprovacao" == \
        workflow_gate.require_plan_approved("plano-exemplo").motivo


@pytest.mark.parametrize("fase", workflow_gate.FASES_APROVADAS)
def test_nao_se_registra_plano_ja_aprovado(porta, fase):
    with pytest.raises(ValueError) as erro:
        workflow_gate.registrar_plano("plano-exemplo", fase=fase)
    assert "não aprova" in str(erro.value)


def test_aprovacao_exige_quem_aprovou(porta):
    workflow_gate.registrar_plano("plano-exemplo", arquivo=porta["plano"])
    with pytest.raises(ValueError):
        workflow_gate.aprovar_plano("plano-exemplo", quem="")


@pytest.mark.parametrize("quem", ["gino", "Jarvis", "assistente", "modelo"])
def test_assistente_nao_se_autoaprova(porta, quem):
    workflow_gate.registrar_plano("plano-exemplo", arquivo=porta["plano"])
    with pytest.raises(ValueError) as erro:
        workflow_gate.aprovar_plano("plano-exemplo", quem=quem)
    assert "quem aprova é o dono" in str(erro.value)


def test_definir_fase_tambem_nao_aprova(porta):
    workflow_gate.registrar_plano("plano-exemplo", arquivo=porta["plano"])
    with pytest.raises(ValueError):
        workflow_gate.definir_fase("plano-exemplo", "aprovado")


# ── o caminho feliz: plano + dono + arquivo real ────────────────────────────

def test_plano_aprovado_passa_e_diz_quem_aprovou(porta):
    workflow_gate.registrar_plano("plano-exemplo", "Plano de exemplo",
                                  arquivo=porta["plano"])
    workflow_gate.aprovar_plano("plano-exemplo", quem="Anthero",
                                quando="2026-11-09 15:00", fonte="WhatsApp")
    resultado = workflow_gate.require_plan_approved("plano-exemplo")
    assert resultado
    assert resultado.plano_id == "plano-exemplo"
    assert resultado.fase == "aprovado"
    assert "Anthero" in resultado.texto()
    assert "decisao=liberado" in " ".join(_linhas(porta["log"]))


def test_aprovacao_de_plano_sem_arquivo_e_recusada(porta):
    workflow_gate.registrar_plano("plano-exemplo", "sem documento")
    with pytest.raises(ValueError) as erro:
        workflow_gate.aprovar_plano("plano-exemplo", quem="Anthero")
    assert "documento" in str(erro.value)


def test_aprovacao_aponta_para_arquivo_que_nao_existe_bloqueia(porta):
    workflow_gate.registrar_plano("plano-exemplo", arquivo=porta["plano"])
    workflow_gate.aprovar_plano("plano-exemplo", quem="Anthero")
    porta["plano"].unlink()
    resultado = workflow_gate.require_plan_approved("plano-exemplo")
    assert not resultado
    assert resultado.motivo == "arquivo_de_plano_inexistente"


def test_a_tarefa_responde_pelo_plano_do_dono(porta):
    workflow_gate.registrar_plano("plano-exemplo", arquivo=porta["plano"],
                                  tarefas=[("tarefa-01", "montar esqueleto")])
    antes = workflow_gate.require_plan_approved("tarefa-01")
    assert not antes and antes.gate == "plano_aprovado"
    workflow_gate.aprovar_plano("plano-exemplo", quem="Anthero")
    depois = workflow_gate.require_plan_approved("tarefa-01")
    assert depois and depois.plano_id == "plano-exemplo"


def test_o_arquivo_do_cofre_identifica_o_mesmo_plano(porta):
    """Aprova-se pelo documento; executa-se pelo caminho da pasta do plano."""
    pasta = porta["plano"].parent / "planos-de-teste"
    pasta.mkdir()
    nota = pasta / "plan.md"
    nota.write_text("# Plano de exemplo\n", encoding="utf-8")
    workflow_gate.registrar_plano("plano-exemplo", arquivo=nota)
    workflow_gate.aprovar_plano("plano-exemplo", quem="Anthero")
    for referencia in (str(nota), f"{pasta}/plan.md", "plano-exemplo",
                       "PLANO-EXEMPLO"):
        assert workflow_gate.require_plan_approved(referencia), referencia


def test_tarefa_ativa_servi_de_referencia_quando_ninguem_diz(porta):
    workflow_gate.registrar_plano("plano-exemplo", arquivo=porta["plano"],
                                  tarefas=["tarefa-da-vez"])
    workflow_gate.aprovar_plano("plano-exemplo", quem="Anthero")
    workflow_gate.definir_tarefa_ativa("tarefa-da-vez")
    assert workflow_gate.require_plan_approved("")
    assert workflow_gate.resumo()["tarefa_ativa"] == "tarefa-da-vez"


# ── estado que sobrevive (a causa raiz que o plano de fluxo diagnosticou) ───

def test_o_estado_vive_em_disco_e_a_proxima_chamada_ouve(porta, monkeypatch):
    workflow_gate.registrar_plano("plano-exemplo", arquivo=porta["plano"])
    workflow_gate.aprovar_plano("plano-exemplo", quem="Anthero")
    bruto = json.loads(porta["estado"].read_text(encoding="utf-8"))
    assert bruto["planos"]["plano-exemplo"]["fase"] == "aprovado"
    assert bruto["planos"]["plano-exemplo"]["aprovado_por"] == "Anthero"
    # "reinício": o módulo reexecuta do zero; o que fica é o arquivo.
    import importlib
    antes_log = workflow_gate.ACTIVITY_LOG
    antes_estado = workflow_gate.DEFAULT_STATE
    recarregado = importlib.reload(workflow_gate)
    recarregado.ACTIVITY_LOG = antes_log
    recarregado.DEFAULT_STATE = antes_estado
    monkeypatch.setattr(workflow_gate, "ACTIVITY_LOG", porta["log"])
    assert recarregado.require_plan_approved("plano-exemplo")


def test_estado_corrompido_nao_derruba_o_portao(porta):
    porta["estado"].write_text("{ isto não é json", encoding="utf-8")
    resultado = workflow_gate.require_plan_approved("qualquer-coisa")
    assert not resultado
    assert resultado.motivo == "nenhum_plano_para_a_referencia"


def test_resumo_conta_as_fases(porta):
    workflow_gate.registrar_plano("um", arquivo=porta["plano"])
    workflow_gate.registrar_plano("dois", arquivo=porta["plano"])
    workflow_gate.aprovar_plano("dois", quem="Anthero")
    saida = workflow_gate.resumo()
    assert saida["planos"] == 2
    assert saida["por_fase"]["aguardando_aprovacao"] == 1
    assert saida["por_fase"]["aprovado"] == 1


# ── segredo não entra no estado nem no log ──────────────────────────────────

def test_titulo_com_chave_entra_por_endereco_nao_por_valor(porta):
    segredo = "Sup3rS3Credealha01"
    workflow_gate.registrar_plano(
        "plano-exemplo", f"plano que usa api_key: {segredo}",
        arquivo=porta["plano"])
    workflow_gate.aprovar_plano("plano-exemplo", quem="Anthero",
                                fonte="whatsapp")
    bruto = porta["estado"].read_text(encoding="utf-8")
    assert segredo not in bruto
    assert "[key:api_key, tipo:api_key]" in bruto
    assert segredo not in " ".join(_linhas(porta["log"]))


def test_o_estado_do_portao_nao_guarda_recado_do_dono(porta):
    """Canal de aprovação é vocabulário fechado: texto livre seria fala/segredo."""
    workflow_gate.registrar_plano("plano-exemplo", arquivo=porta["plano"])
    with pytest.raises(ValueError) as erro:
        workflow_gate.aprovar_plano("plano-exemplo", quem="Anthero",
                                    fonte="ele mandou usar a chave ai...")
    assert "vocabulário fechado" in str(erro.value)


@pytest.mark.parametrize("invalido", ["", "   ", "a/b", "/x", "plano/2026"])
def test_ids_inutilizaveis_sao_recusados(porta, invalido):
    with pytest.raises(ValueError):
        workflow_gate.registrar_plano(invalido, arquivo=porta["plano"])


def test_id_com_espaco_vira_hifen_e_leva_ao_mesmo_plano(porta):
    workflow_gate.registrar_plano("montar o esqueleto", arquivo=porta["plano"])
    workflow_gate.aprovar_plano("montar-o-esqueleto", quem="Anthero")
    assert workflow_gate.require_plan_approved("montar o esqueleto")


# ── A ALAVANCA: gate desligado = hoje; gate ligado não quebra nada ─────────
#
# Ordem do dono, 09/11/2026. Cada teste abaixo força UM ponto do mecanismo:
# ler o JSON a cada chamada (sem reiniciar), não desligar por engano quando o
# config apodrece no meio do caminho, e nunca derrubar quem chama se o próprio
# portão falhar. Não é prosa: é o que impede o portão de virar o defeito que a
# auditoria de 07/11 apontou no UP-8/UP-10.

def _liga(porta, valor) -> None:
    """Reescreve o config no disco. Ninguém reimporta módulo nem reinicia."""
    porta["config"].write_text(json.dumps({"version": 1, "ativo": valor}),
                               encoding="utf-8")


def test_alavanca_desligada_devolve_o_comportamento_de_hoje(porta):
    """Sem plano aprovado: ligado bloqueia, desligado deixa passar."""
    antes = workflow_gate.require_plan_approved("construir-o-botao-de-mute")
    assert not antes
    assert antes.texto().startswith(workflow_gate.MENSAGEM_SEM_PLANO)
    _liga(porta, False)
    depois = workflow_gate.require_plan_approved("construir-o-botao-de-mute")
    assert depois, depois.texto()
    assert depois.gate == "desligado"
    assert workflow_gate.MENSAGEM_SEM_PLANO not in depois.texto()
    linhas = _linhas(porta["log"])
    assert any("decisao=liberado" in ln and "gate=desligado" in ln
               for ln in linhas), linhas


def test_a_alavanca_e_lida_a_cada_chamada_sem_reiniciar(porta):
    """O precedente de core/flows/config.py: editar JSON vale na hora."""
    _liga(porta, True)
    assert workflow_gate.ativo() is True
    assert not workflow_gate.require_plan_approved("qualquer-coisa")
    _liga(porta, False)
    assert workflow_gate.ativo() is False
    assert workflow_gate.require_plan_approved("qualquer-coisa")
    _liga(porta, True)
    assert workflow_gate.ativo() is True
    assert not workflow_gate.require_plan_approved("qualquer-coisa")


def test_config_ausente_nao_desliga_a_lei(porta):
    """Arquivo sumido != portão fora: o padrão declarado é ligado."""
    porta["config"].unlink()
    assert workflow_gate.ativo() is True
    assert not workflow_gate.require_plan_approved("pedido-sem-plano")


@pytest.mark.parametrize("corrompido", [
    "{ isto não é json", '[1, 2, 3]', '{"ativo": "mais ou menos"}', "",
    '{"ativo": null}'])
def test_config_corrompido_mantem_o_ultimo_valor_e_nao_desliga_escondido(
        porta, corrompido):
    """O bug previsível: JSON podre no meio do turno. Desligar sozinho era a
    porta de saída da lei -- então fica o último valor bom e o log avisa."""
    _liga(porta, True)
    assert not workflow_gate.require_plan_approved("pedido-sem-plano")
    porta["config"].write_text(corrompido, encoding="utf-8")
    assert workflow_gate.ativo() is True, corrompido
    assert not workflow_gate.require_plan_approved("pedido-sem-plano")
    assert workflow_gate.problema_config().startswith("config_inutilizavel")
    assert any("decisao=config_recusada" in ln for ln in _linhas(porta["log"]))


def test_config_podre_depois_de_uma_ligada_falsa_nao_religa_nada(porta):
    """Mesma regra no sentido contrário: quem desligou não religa por azar."""
    _liga(porta, False)
    assert workflow_gate.ativo() is False
    porta["config"].write_text("}{ podre", encoding="utf-8")
    assert workflow_gate.ativo() is False
    assert workflow_gate.require_plan_approved("pedido-sem-plano")


def test_env_de_emergencia_desliga_sem_tocar_em_disco(porta, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKFLOW_GATE", "0")
    assert workflow_gate.ativo() is False
    assert workflow_gate.require_plan_approved("pedido-sem-plano")
    monkeypatch.setenv("JARVIS_WORKFLOW_GATE", "1")
    assert workflow_gate.ativo() is True
    assert not workflow_gate.require_plan_approved("pedido-sem-plano")


def test_gate_ligado_com_erro_interno_nao_derruba_quem_chama(porta,
                                                             monkeypatch):
    """"Gate ligado nunca pode quebrar o que já funciona", à letra.

    Se o portão explodir por dentro (bug meu, disco sumindo, estado absurdo),
    o chamador recebe liberação com o motivo no log -- nunca um traceback no
    meio da fala do dono. O bloqueio continua enquanto o portão está são.
    """
    def _explode(_referencia):
        raise RuntimeError("estado que ninguém previu")

    monkeypatch.setattr(workflow_gate, "_decidir", _explode)
    resultado = workflow_gate.require_plan_approved("qualquer-coisa")
    assert resultado, "portão com erro interno não pode travar a app"
    assert resultado.gate == "desligado"
    assert "erro_interno" in resultado.motivo
    assert any("decisao=erro_interno" in ln for ln in _linhas(porta["log"]))


def test_erro_em_exigir_plano_aprovado_sobe_so_quando_o_portao_esta_sao(
        porta, monkeypatch):
    """A versão que levanta continua levantando COM plano ausente."""
    with pytest.raises(workflow_gate.SemPlanoAprovado):
        workflow_gate.exigir_plano_aprovado("sem-plano-aqui")
    _liga(porta, False)
    assert workflow_gate.exigir_plano_aprovado("sem-plano-aqui") == {}


def test_resumo_declara_a_alavanca_e_o_config_lido(porta):
    _liga(porta, False)
    saida = workflow_gate.resumo()
    assert saida["ativo"] is False
    assert saida["arquivo_config"] == str(porta["config"])
    _liga(porta, True)
    assert workflow_gate.resumo()["ativo"] is True


def test_o_config_do_repo_vem_ligado_e_explica_a_alavanca():
    """O arquivo que vai no commit: ligado, com o motivo escrito dentro."""
    bruto = json.loads((Path(workflow_gate.__file__).resolve().parent.parent
                        / "config" / "workflow_gate.json")
                       .read_text(encoding="utf-8"))
    assert bruto["ativo"] is True
    assert "CADA chamada" in bruto["_comentario"]
