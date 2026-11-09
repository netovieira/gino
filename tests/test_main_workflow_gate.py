"""O portão de plano LIGADO no ciclo de execução (ordem do dono, 09/11/2026 16:40).

``core/workflow_gate.py`` existia com ``require_plan_approved`` inteiro e NENHUM
ponto de chamada em produção -- só os testes o importavam, e por isso o dono
reclamava que "executa sem plano". O ponto real por onde um pedido de trabalho
entra no ciclo de execução é ``main.py:_start_qoder_task``: a docstring dela já
dizia que é "the only path a live-chat execution may take", e é a única linha do
``main.py`` que chama ``core.qoder_jobs.start``. Agora ela consulta o portão
ANTES de criar o job.

Um teste por buraco previsível:

| BURACO PREVISÍVEL                                    | O QUE FORÇA ESTE ARQUIVO                       |
|-------------------------------------------------------|------------------------------------------------|
| portão ligado, pedido sem plano aprovado executa      | ``test_gate_ligado_sem_plano_recusa_e_nao_...`` |
| portão ligado com plano aprovado fica travado         | ``test_gate_ligado_com_plano_aprovado_passa``   |
| mexer na alavanca muda o caminho do trabalho         | ``test_gate_desligado_e_byte_a_byte_o_de_hoje`` |
| JSON do portão podre no meio do caminho              | ``test_json_corrompido_segura_o_ultimo_valor``  |
| erro no portão derruba quem só queria trabalhar      | ``test_erro_interno_do_portao_libera_com_rastro`` |
| decisão do portão sem rastro para a auditoria        | ``event=workflow_gate_call`` nos quatro primeiros |

Nenhum número, chave ou conversa do dono aqui: estado e config em ``tmp_path``,
e o job é um stub -- nada discaria o Qoder.
"""
from __future__ import annotations

import json

import pytest

import main
from core import workflow_gate

ACK = "JOB-CRIADO"


class _UI:
    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, text):
        self.logs.append(text)


def _app() -> main.JarvisLive:
    obj = object.__new__(main.JarvisLive)
    obj.ui = _UI()
    return obj


def _config(caminho, ativo: bool) -> None:
    caminho.write_text(json.dumps({"version": 1, "ativo": ativo}),
                       encoding="utf-8")


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    """Portão isolado: estado, config e log em tmp; o job é um stub gravado."""
    estado = tmp_path / "workflow_gate.json"
    config = tmp_path / "workflow_gate_config.json"
    atividade = tmp_path / "planos" / "trocar-a-rota-de-voz"
    atividade.mkdir(parents=True)
    plano = atividade / "plan.md"
    plano.write_text("# Plano\n\nFase: aguardando aprovacao\n", encoding="utf-8")
    monkeypatch.setattr(workflow_gate, "ACTIVITY_LOG", tmp_path / "activity.log")
    monkeypatch.setattr(workflow_gate, "_ultimo_ativo", None)
    monkeypatch.setattr(workflow_gate, "_ultimo_problema", "")
    monkeypatch.setenv("JARVIS_WORKFLOW_GATE_STATE", str(estado))
    monkeypatch.setenv("JARVIS_WORKFLOW_GATE_CONFIG", str(config))
    monkeypatch.delenv("JARVIS_WORKFLOW_GATE", raising=False)
    _config(config, True)

    calls: list[tuple[tuple, dict]] = []

    def _fake_start(task, mode="answer", model=None, workspace=None,
                    files=None, player=None, approval=None):
        calls.append((task, {"mode": mode, "player": player,
                             "approval": approval}))
        return ACK

    monkeypatch.setattr(main._qoder_jobs, "start", _fake_start)
    obj = _app()
    yield type("Gate", (object,), {"obj": obj, "config": config,
                                   "plano_arquivo": str(plano),
                                   "calls": calls, "estado": estado})


def _linhas(obj) -> str:
    return "\n".join(obj.ui.logs)


# ── 1. ligado sem plano: recusa com a frase do dono e NENHUM job ────────────

def test_gate_ligado_sem_plano_recusa_e_nao_executa(gate):
    obj = gate.obj
    recusa = obj._start_qoder_task("trocar a rota de voz agora", mode="write")

    assert recusa.startswith(workflow_gate.MENSAGEM_SEM_PLANO), recusa
    assert "Preciso de um plano pra isso" in recusa
    assert gate.calls == [], "sem plano aprovado NADA entra no ciclo de execução"
    # a decisão é registrada com event= para a sonda do core/dev_audit.py
    assert "event=workflow_gate_call" in _linhas(obj)
    assert "decisao=bloqueado" in _linhas(obj)
    assert "ponto=task_start" in _linhas(obj)
    assert "sem_referencia_e_sem_tarefa_ativa" in _linhas(obj)


# ── 2. ligado COM plano aprovado: passa e o job é criado ────────────────────

def test_gate_ligado_com_plano_aprovado_passa(gate):
    obj = gate.obj
    workflow_gate.registrar_plano("trocar-a-rota-de-voz", "Trocar a rota de voz",
                                  arquivo=gate.plano_arquivo)
    workflow_gate.registrar_tarefa("trocar-a-rota-de-voz", "tarefa-01",
                                   "Trocar a rota no brain.py")
    workflow_gate.aprovar_plano("trocar-a-rota-de-voz", quem="Anthero",
                               fonte="whatsapp")
    workflow_gate.definir_tarefa_ativa("tarefa-01")

    saida = obj._start_qoder_task("trocar a rota de voz", mode="write")

    assert saida == ACK, "plano aprovado é exatamente o que libera o trabalho"
    assert gate.calls, "o job tinha de ter sido criado"
    log = _linhas(obj)
    assert "decisao=liberado" in log
    # sem espaço não tem aspa: é assim que o _event_kv grava, e a sonda do
    # dev_audit lê essas duas chaves para dizer QUAL plano liberou o trabalho
    assert "plano_id=trocar-a-rota-de-voz" in log, log
    assert "fase=aprovado" in log, log
    assert "referencia=tarefa-01" in log, log


# ── 3. desligado: o caminho é o de hoje, byte a byte ────────────────────────

def test_gate_desligado_e_byte_a_byte_o_de_hoje(gate):
    obj = gate.obj
    _config(gate.config, False)

    saida = obj._start_qoder_task("trocar a rota de voz", mode="write")

    assert saida == ACK
    # a assinatura de chamada é a que já era: posicional na tarefa, player=self.ui,
    # approval=False para write. Nem um argumento a mais, nem um a menos.
    assert gate.calls == [("trocar a rota de voz",
                           {"mode": "write", "player": obj.ui,
                            "approval": False})]
    # e o log diz que passou porque a alavanca estava aberta, não por plano
    assert "decisao=liberado" in _linhas(obj)
    assert "gate=desligado" in _linhas(obj)
    assert "motivo=gate_desligado_por_config" in _linhas(obj)

    # modo answer com approval explícito também segue a mesma rota de sempre
    gate.calls.clear()
    obj._start_qoder_task("o que é este arquivo", mode="answer", approval=True)
    assert gate.calls == [("o que é este arquivo",
                           {"mode": "answer", "player": obj.ui,
                            "approval": True})]


# ── 4. JSON corrompido: último valor BOM, sem exceção na cara do dono ───────

@pytest.mark.parametrize("valor_bom", (True, False))
def test_json_corrompido_segura_o_ultimo_valor_bom(gate, valor_bom):
    obj = gate.obj
    _config(gate.config, valor_bom)
    assert workflow_gate.ativo() is valor_bom
    gate.calls.clear()
    obj.ui.logs.clear()

    # o arquivo pifa no meio do caminho (escrita cortada, resto de JSON)
    gate.config.write_text('{"version": 1, "ativo": tr', encoding="utf-8")

    assert workflow_gate.ativo() is valor_bom, "JSON podre nunca muda a alavanca"
    assert workflow_gate.problema_config().startswith("config_inutilizavel")

    saida = obj._start_qoder_task("trocar a rota de voz", mode="write")
    if valor_bom:
        # portão estava LIGADO: continua ligado, e continua recusando.
        assert saida.startswith(workflow_gate.MENSAGEM_SEM_PLANO)
        assert gate.calls == []
    else:
        # portão estava DESLIGADO: podre nenhum liga o portão sozinho.
        assert saida == ACK
        assert gate.calls


# ── 5. erro interno do portão: libera com rastro, nunca com traceback ───────

def test_erro_interno_do_portao_libera_com_rastro(gate, monkeypatch):
    obj = gate.obj

    def _explode(_referencia=""):
        raise RuntimeError("estado ilegível")

    monkeypatch.setattr(workflow_gate, "require_plan_approved", _explode)

    saida = obj._start_qoder_task("trocar a rota de voz", mode="write")

    assert saida == ACK, "portão quebrado não pode parar quem já estava trabalhando"
    # o job é criado MESMO: falha do portão não é pretexto para perder o pedido
    assert [t for t, _ in gate.calls] == ["trocar a rota de voz"], gate.calls
    log = _linhas(obj)
    assert "decisao=falha_aberta" in log, log
    assert "gate=erro" in log, log
    assert "motivo=RuntimeError" in log, log
    # o rastro jamais leva o texto do pedido: só o tipo do erro
    assert "trocar a rota de voz" not in log, log
