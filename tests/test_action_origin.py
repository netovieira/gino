"""UV-4 — rastrear origem de toda ação de dispositivo ("não mexer sem pedido").

Três portões, uma política (PLANO Fase 7 TASK4):

  1. ``core/actiongate.py`` é o dono do vocabulário: os quatro valores do
     contrato (fala_do_dono, fala_visitante, automatica, filtro_email), o
     dobrador de rótulos (o ``source`` que o app já passa vira origem
     canônica num lugar só) e a regra de bloqueio — origem EXPLÍCITA
     ``automatica`` não toca dispositivo sensível (tier medium/high do
     bridge). Origem não declarada não é bloqueada: é lacuna registrada.
  2. ``core/flows/executor.py`` é o portão dos fluxos: o resultado carrega a
     origem, a ação com efeito colateral sai no activity.log com ``origin=``
     e a recusa por origem NUNCA chega ao runner.
  3. ``core/tool_bridge.py`` é o portão HTTP: ``/call`` aceita ``origin``,
     grava no ledger JSONL, bloqueia automatica em dispositivo sensível
     antes de pending/confirm/execução, e carimba o activity.log.

O orquestrador amarra as pontas: o ``source`` do turno vira ``plan.origin``
e viaja em cada ``executor.run``.

Sem microfone, sem rede e sem o app vivo: fake runner, TestClient e
activity.log temporário. Nada aqui é inferido — ou a origem foi declarada,
ou o log diz ``nao_declarada``.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core import actiongate as ag
from core import tool_bridge as tb
from core.flows import executor as exe
from core.flows.config import FlowsConfig
from core.flows.orchestrator import Orchestrator, _execution_summary, _plan_origin

TOKEN = "test-token-uv4"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ── 1. vocabulário canônico (actiongate) ────────────────────────────────────

class TestNormalizeOrigin:
    def test_quatro_valores_do_contrato(self):
        assert ag.ACTION_ORIGINS == frozenset(
            {"fala_do_dono", "fala_visitante", "automatica", "filtro_email"})

    @pytest.mark.parametrize("value", sorted(ag.ACTION_ORIGINS))
    def test_canonico_passa_intacto(self, value):
        assert ag.normalize_origin(value) == value

    @pytest.mark.parametrize("alias,expected", [
        ("owner", "fala_do_dono"),
        ("voice", "fala_do_dono"),
        ("typed", "fala_do_dono"),
        ("dono", "fala_do_dono"),
        ("visitor", "fala_visitante"),
        ("visitante", "fala_visitante"),
        ("email", "filtro_email"),
        ("email_flow", "filtro_email"),
        ("queue", "automatica"),
        ("system", "automatica"),
        ("proativa", "automatica"),
    ])
    def test_rotulos_do_app_viram_canonico(self, alias, expected):
        assert ag.normalize_origin(alias) == expected

    def test_vazio_nao_e_presumido_e_lacuna(self):
        """Ausência é registrada como lacuna, nunca como pedido de alguém."""
        assert ag.normalize_origin("") == ag.ORIGIN_UNDECLARED
        assert ag.normalize_origin(None) == ag.ORIGIN_UNDECLARED

    def test_marcador_de_lacuna_e_idempotente(self):
        """Reler o log não transforma a lacuna em origem automática."""
        assert ag.normalize_origin("nao_declarada") == ag.ORIGIN_UNDECLARED

    def test_rotulo_desconhecido_e_conservador(self):
        """Origem declarada fora do vocabulário não pode se vender a dono."""
        assert ag.normalize_origin("sei-la-o-que-e") == "automatica"

    def test_acentos_e_caixa_nao_mudam_o_rotulo(self):
        assert ag.normalize_origin("  FALA_DO_DONO ") == "fala_do_dono"


class TestSensitiveDevice:
    def test_dispositivo_sensivel_e_o_tier_medium_high_do_bridge(self):
        assert ag.is_sensitive_device("home_control") is True
        assert ag.is_sensitive_device("whatsapp_control") is True
        assert ag.is_sensitive_device("restart_pc") is True

    def test_leitura_e_reversivel_nao_sao_sensiveis(self):
        assert ag.is_sensitive_device("web_search") is False
        assert ag.is_sensitive_device("music_control") is False
        # desdobramento por args é o outro lado da mesma régua (volume é baixo)
        assert ag.is_sensitive_device("computer_settings",
                                      {"action": "volume"}) is False

    def test_args_escalam_a_sensibilidade(self):
        """O MESMO tier do portão: setting de volume é baixo, restart não."""
        assert ag.is_sensitive_device("computer_settings",
                                      {"action": "volume"}) is False
        assert ag.is_sensitive_device("computer_settings",
                                      {"action": "shutdown"}) is True

    def test_fallback_estatico_sem_bridge_importavel(self):
        """Ambiente enxuto: o espelho dos tiers medium/high segura a política."""
        assert "home_control" in ag._SENSITIVE_ACTIONS_FALLBACK
        assert "whatsapp_control" in ag._SENSITIVE_ACTIONS_FALLBACK
        assert "music_control" not in ag._SENSITIVE_ACTIONS_FALLBACK


class TestOriginBlockReason:
    def test_automatica_em_sensivel_bloqueia(self):
        reason = ag.origin_block_reason("home_control", "automatica")
        assert reason and "automatica" in reason

    def test_automatica_em_nao_sensivel_livre(self):
        assert ag.origin_block_reason("music_control", "automatica") == ""

    @pytest.mark.parametrize("origin", [
        "fala_do_dono", "owner", "fala_visitante", "filtro_email", "email",
    ])
    def test_pedidos_humanos_nao_sao_bloqueados(self, origin):
        assert ag.origin_block_reason("home_control", origin) == ""

    def test_nao_declarada_e_lacuna_registrada_nao_acusacao(self):
        assert ag.origin_block_reason("home_control", "") == ""
        assert ag.origin_block_reason("home_control", None) == ""
        assert ag.origin_block_reason("home_control", "nao_declarada") == ""

    def test_rotulo_desconhecido_declara_automatica(self):
        assert ag.origin_block_reason("whatsapp_control", "sei-la") != ""

    def test_escalameto_por_args_vale_para_o_bloqueio(self):
        assert ag.origin_block_reason("computer_settings", "automatica",
                                      {"action": "volume"}) == ""
        assert ag.origin_block_reason("computer_settings", "automatica",
                                      {"action": "restart"}) != ""


# ── 2. executor: portão, rastro e resultado ─────────────────────────────────

@pytest.fixture
def activity_log(tmp_path):
    """Activity log temporário: teste nenhum escreve no log real."""
    path = tmp_path / "activity.log"
    with patch.object(exe, "_ACTIVITY_LOG_PATH", path):
        yield path


@pytest.fixture
def clean_actiongate():
    ag.close()
    ag._blocked_listeners.clear()
    yield ag
    ag.close()
    ag._blocked_listeners.clear()


class FakeSpeaker:
    """Runner injetado: marca o que EXECUTOU de fato (prova de que bloqueado
    nunca chega ao runner)."""

    def __init__(self, result=None):
        self.calls: list[tuple[str, dict]] = []
        self._result = result or {"ok": True, "spoken": "feito"}

    def __call__(self, tool, args):
        self.calls.append((tool, dict(args)))
        return dict(self._result)


def executor_with(runner):
    return exe.Executor(runner=runner, catalog={"home_control", "music_control",
                                               "web_search", "send_message"},
                        enabled=True)


class TestExecutorOriginGate:
    def test_resultado_carrega_origem_canonica(self, activity_log):
        runner = FakeSpeaker()
        result = executor_with(runner).run("t1", "music_control", {},
                                           origin="owner")
        assert result.origin == "fala_do_dono"
        assert result.as_dict()["origin"] == "fala_do_dono"

    def test_automatica_em_sensivel_bloqueia_sem_rodar_runner(self,
                                                              activity_log,
                                                              clean_actiongate):
        runner = FakeSpeaker()
        result = executor_with(runner).run("t2", "home_control", {},
                                           origin="automatica")
        assert result.blocked is True
        assert result.ok is False
        assert result.origin == "automatica"
        assert runner.calls == []          # o runner NUNCA foi tocado
        assert any(f.get("origin_blocked") for f in result.facts)
        log_text = activity_log.read_text(encoding="utf-8")
        assert "origin_blocked" in log_text
        assert "origin=automatica" in log_text
        # o dono é avisado pelo registro vivo do portão de ações
        kinds = [r.get("kind") for r in clean_actiongate.blocked_actions()]
        assert "origin_blocked" in kinds

    def test_automatica_em_nao_sensivel_executa(self, activity_log):
        runner = FakeSpeaker()
        result = executor_with(runner).run("t3", "music_control", {},
                                           origin="auto")
        assert runner.calls == [("music_control", {})]
        assert result.origin == "automatica"
        assert result.blocked is False

    def test_fala_do_dono_em_sensivel_executa(self, activity_log):
        runner = FakeSpeaker()
        result = executor_with(runner).run("t4", "home_control", {},
                                           origin="fala_do_dono")
        assert runner.calls == [("home_control", {})]
        assert result.blocked is False

    def test_origem_nao_declarada_e_lacuna_visivel(self, activity_log):
        """Legado sem origem roda (não quebra o vivo) mas a lacuna aparece."""
        runner = FakeSpeaker()
        result = executor_with(runner).run("t5", "home_control", {})
        assert runner.calls == [("home_control", {})]
        assert result.origin == "nao_declarada"
        line = activity_log.read_text(encoding="utf-8")
        assert "device_action" in line
        assert "origin=nao_declarada" in line

    def test_acao_com_efeito_colateral_sai_no_activity_log(self, activity_log):
        executor_with(FakeSpeaker()).run("t6", "send_message", {"to": "x"},
                                         origin="email")
        line = activity_log.read_text(encoding="utf-8")
        assert "device_action: task_id=t6 tool=send_message" in line
        assert "origin=filtro_email" in line
        assert "state=" in line

    def test_leitura_nao_polui_o_log(self, activity_log):
        exe.Executor(runner=FakeSpeaker(), catalog={"web_search"},
                     enabled=True).run("t7", "web_search", {},
                                       origin="fala_do_dono")
        # safe: nenhuma linha escrita — nem arquivo existe
        assert not activity_log.exists()

    def test_args_do_runner_nao_recebem_origin(self, activity_log):
        """O campo de rastreamento não vaza para o handler da ferramenta."""
        runner = FakeSpeaker()
        executor_with(runner).run("t8", "music_control", {"action": "play"},
                                  origin="owner")
        assert runner.calls == [("music_control", {"action": "play"})]


# ── 3. orquestrador: source do turno viaja até o despacho ──────────────────

class RecordingExecutor:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def run(self, task_id, tool, args, *, requires_confirm=False,
            cancel_event=None, origin=""):
        self.calls.append((tool, {"origin": origin,
                                  "requires_confirm": requires_confirm}))
        return exe.ExecutorResult(task_id=task_id, ok=True, origin=origin,
                                  facts=[{"spoken": "feito", "ok": True}])

    def knows(self, tool):
        return True


def flows_orchestrator(executor=None, log_sink=None, event_sink=None):
    config = FlowsConfig(enabled=True, log_events=True,
                         mouth_enabled=False, executor_enabled=True)
    orch = Orchestrator(config, executor=executor or RecordingExecutor(),
                        log=(log_sink.append if log_sink else None),
                        on_event=event_sink)
    return orch


class TestOrchestratorOrigin:
    def test_begin_mapeia_source_em_origem_canonica(self):
        orch = flows_orchestrator()
        assert orch.begin("u1", "que horas sao").origin == "fala_do_dono"
        assert orch.begin("u2", "oi", source="visitor").origin \
            == "fala_visitante"
        assert orch.begin("u3", "mail", source="email_flow").origin \
            == "filtro_email"
        assert orch.begin("u4", "t", source="queue").origin == "automatica"

    def test_source_desconhecido_e_conservador(self):
        orch = flows_orchestrator()
        assert orch.begin("u5", "t", source="fantasma").origin == "automatica"

    def test_plan_origin_vazio_para_turnos_fora_do_begin(self):
        """Planos montados à mão (testes, replay) não ganham origem inventada."""
        from core.flows.orchestrator import TurnPlan
        assert TurnPlan(turn_id="x", text="t").origin == ""

    def test_run_plan_despacha_com_a_origem_do_turno(self, activity_log):
        from core.flows.router import Action
        executor = RecordingExecutor()
        orch = flows_orchestrator(executor=executor)
        plan = orch.begin("v1", "manda mensagem pra maio")
        plan.decision.actions = [Action(tool="send_message",
                                        args={"to": "maio"})]
        orch.run_plan(plan)
        assert executor.calls and executor.calls[0][1]["origin"] \
            == "fala_do_dono"

    def test_turno_automatico_nao_toca_dispositivo_sensivel(self, activity_log):
        """Integração real: Orquestrador + Executor (sem runner falso)."""
        runner = FakeSpeaker()
        config = FlowsConfig(enabled=True, log_events=True,
                             mouth_enabled=False, executor_enabled=True)
        orch = Orchestrator(config,
                            executor=exe.Executor(runner=runner,
                                                  catalog={"home_control"},
                                                  enabled=True))
        plan = orch.begin("v2", "acende a sala", source="queue")
        from core.flows.router import Action
        plan.decision.actions = [Action(tool="home_control", args={})]
        results = orch.run_plan(plan)
        assert runner.calls == []                 # nada rodou de fato
        assert results[0].blocked is True
        assert results[0].origin == "automatica"

    def test_evento_execution_complete_carrega_origem(self):
        events: list[dict] = []
        executor = RecordingExecutor()
        orch = flows_orchestrator(executor=executor, event_sink=events.append)
        plan = orch.begin("v3", "abre a luz")
        from core.flows.router import Action
        plan.decision.actions = [Action(tool="send_message", args={})]
        orch.run_plan(plan)
        payloads = [p for p in events if p.get("type") == "execution_complete"]
        assert payloads and payloads[0]["origin"] == "fala_do_dono"
        rows = payloads[0]["results"]
        assert rows and rows[0]["origin"] == "fala_do_dono"

    def test_resumo_de_resultos_usa_origem_do_turno_como_fallback(self):
        results = [exe.ExecutorResult(task_id="k1", ok=True)]
        rows = _execution_summary(results, default_origin="owner")
        assert rows[0]["origin"] == "fala_do_dono"
        rows = _execution_summary(results)
        assert rows[0]["origin"] == "nao_declarada"


# ── 4. tool bridge: /call com origem ────────────────────────────────────────

class FakeRegistry:
    def __init__(self):
        self._decls = [{"name": "home_control", "description": "luzes",
                        "parameters": {"type": "OBJECT"}},
                       {"name": "music_control", "description": "musica",
                        "parameters": {"type": "OBJECT"}}]

    def get_tool_declarations(self):
        return [dict(d) for d in self._decls]

    def has(self, name):
        return any(d["name"] == name for d in self._decls)

    def run(self, name, parameters, ctx=None):
        return f"registry:{name}"


def make_bridge(tmp_path, **overrides):
    overrides.setdefault("audit_path", str(tmp_path / "ledger.jsonl"))
    return tb.ToolBridge(FakeRegistry(), overrides.pop("dispatch", None)
                         or (lambda name, args: f"ran:{name}"),
                         TOKEN, logger=lambda _m: None, **overrides)


def ledger_records(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    # O mesmo JSONL recebe recibos HMAC (up-9); a linha de auditoria é a que
    # tem status.
    return [r for r in records if "status" in r]


@pytest.fixture
def bridge_activity(tmp_path, monkeypatch):
    """activity.log do bridge redirecionado para o tmp (nunca o log real)."""
    target = tmp_path / "logs" / "activity.log"
    real = tb._append_activity_line

    def spy(event, fields):
        target.parent.mkdir(parents=True, exist_ok=True)
        real_ts = __import__("datetime").datetime.now().strftime("%H:%M:%S")
        with target.open("a", encoding="utf-8") as fh:
            fh.write(f"[{real_ts}] {event}: {fields}\n")

    monkeypatch.setattr(tb, "_append_activity_line", spy)
    return target


class TestBridgeOrigin:
    def test_automatica_em_sensivel_bloqueia_antes_de_executar(self, tmp_path,
                                                               bridge_activity):
        called: list[tuple[str, dict]] = []

        def dispatch(name, args):
            called.append((name, args))
            return "ran"

        bridge = make_bridge(tmp_path, dispatch=dispatch,
                             confirm=lambda title, message: True)
        client = TestClient(bridge.app)
        response = client.post("/call", headers=AUTH, json={
            "name": "home_control", "args": {}, "origin": "automatica"})
        assert response.status_code == 403
        assert response.json()["status"] == "origin_blocked"
        assert called == []                       # o dispatcher nunca viu a chamada
        records = ledger_records(tmp_path / "ledger.jsonl")
        assert records[-1]["status"] == "origin_blocked"
        assert records[-1]["origin"] == "automatica"
        line = bridge_activity.read_text(encoding="utf-8")
        assert "device_action" in line
        assert "origin=automatica" in line
        assert "status=origin_blocked" in line

    def test_automatica_em_nao_sensivel_passa(self, tmp_path, bridge_activity):
        bridge = make_bridge(tmp_path)
        client = TestClient(bridge.app)
        response = client.post("/call", headers=AUTH, json={
            "name": "music_control", "args": {"action": "play"},
            "origin": "auto"})
        assert response.status_code == 200
        assert response.json()["origin"] == "automatica"

    def test_fala_do_dono_em_sensivel_segue_o_portao_normal(self, tmp_path,
                                                           bridge_activity):
        """A origem não substitui o portão de confirmação: soma-se a ele."""
        consented: list[bool] = []

        def confirm(title, message):
            consented.append(True)
            return True

        bridge = make_bridge(tmp_path, confirm=confirm)
        client = TestClient(bridge.app)
        response = client.post("/call", headers=AUTH, json={
            "name": "home_control", "args": {}, "origin": "fala_do_dono"})
        assert response.status_code == 200
        assert consented == [True]                # passou pelo portão de voz

    def test_origem_ausente_e_lacuna_registrada(self, tmp_path,
                                                bridge_activity):
        """Legado: chamada sem origem continua e roda (confirm faz o resto)."""
        bridge = make_bridge(tmp_path,
                             confirm=lambda title, message: True)
        client = TestClient(bridge.app)
        response = client.post("/call", headers=AUTH,
                               json={"name": "home_control", "args": {}})
        assert response.status_code == 200
        assert response.json()["origin"] == "nao_declarada"
        records = ledger_records(tmp_path / "ledger.jsonl")
        assert records[-1]["origin"] == "nao_declarada"
        assert "origin=nao_declarada" in bridge_activity.read_text(
            encoding="utf-8")

    def test_rotulo_desconhecido_e_tratado_como_automatica(self, tmp_path,
                                                           bridge_activity):
        bridge = make_bridge(tmp_path,
                             confirm=lambda title, message: True)
        client = TestClient(bridge.app)
        response = client.post("/call", headers=AUTH, json={
            "name": "home_control", "args": {}, "origin": "sei-la"})
        assert response.status_code == 403
        assert response.json()["origin"] == "automatica"

    def test_filtro_email_vai_auditoria_e_ao_log(self, tmp_path,
                                                 bridge_activity):
        bridge = make_bridge(tmp_path)
        client = TestClient(bridge.app)
        response = client.post("/call", headers=AUTH, json={
            "name": "music_control", "args": {}, "origin": "email_flow"})
        assert response.status_code == 200
        assert ledger_records(tmp_path / "ledger.jsonl")[-1]["origin"] \
            == "filtro_email"
        assert "origin=filtro_email" in bridge_activity.read_text(
            encoding="utf-8")

    def test_chamada_antiga_sem_campo_origin_continua_valida(self, tmp_path,
                                                             bridge_activity):
        """CallRequest ganhou campo opcional: payloads antigos não quebram."""
        bridge = make_bridge(tmp_path)
        client = TestClient(bridge.app)
        response = client.post("/call", headers=AUTH,
                               json={"name": "music_control", "args": {}})
        assert response.status_code == 200

    def test_risk_tier_da_instancia_e_da_funcao_sao_a_mesma_regra(self):
        bridge = tb.ToolBridge(FakeRegistry(), lambda n, a: "x", TOKEN)
        for name, args in [("home_control", {}), ("music_control", {}),
                           ("computer_settings", {"action": "restart"}),
                           ("desconhecida_total", {})]:
            assert bridge.risk_tier(name, args) == tb.action_risk_tier(
                name, args)


# ── 5. _plan_origin: a ponte source -> origem ───────────────────────────────

class TestPlanOrigin:
    @pytest.mark.parametrize("source,expected", [
        ("owner", "fala_do_dono"),
        ("whatsapp", "fala_do_dono"),
        ("visitor", "fala_visitante"),
        ("email", "filtro_email"),
        ("task", "automatica"),
        ("fantasma", "automatica"),      # conservador por construção
        ("", ""),                        # lacuna: vazio atravessa vazio
    ])
    def test_mapeamento(self, source, expected):
        assert _plan_origin(source) == expected

    def test_marcador_de_lacuna_nao_reentra_como_automatica(self):
        assert _plan_origin("nao_declarada") == ag.ORIGIN_UNDECLARED
        assert ag.origin_block_reason("home_control",
                                      _plan_origin("nao_declarada")) == ""
