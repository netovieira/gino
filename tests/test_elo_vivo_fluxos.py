"""UP-8 e UP-10: o mecanismo existia, o que faltava era o elo vivo.

A auditoria de 07/11 achou dois buracos do mesmo tipo — código pronto, testado
e NUNCA plugado no processo real:

* UP-8: ``task_mirror`` já tinha throttle, evento ``task_progress`` e o gancho
  ``set_progress_hook``; a boca já tinha ``progress_notifier``. O gancho só era
  instalado em teste: 214 linhas ``event=task_progress ... hook=ausente`` no
  activity.log do dia. O elo foi fechado no boot da ponte
  (``main.JarvisLive._flows_get`` → ``_flows_progress_hook``, commit ``e14be4e``)
  e tem teste próprio em ``tests/test_task_mirror_notifications.py``. Aqui fica
  a invariante que faltava: o BOOT REAL (``_flows_get``), não o auxiliar
  chamado à mão, deixa o gancho presente — nada de ``hook=ausente``.
* UP-10: ``execution_complete`` só saía de ``run_plan``, que não tem chamador
  em produção (a runtime viva é ``core/flows/live.py``, que despacha no
  ``_gate_worker``). Resultado: zero eventos no activity.log inteiro. O elo é
  ``FlowsRuntime._gate_worker`` → ``Orchestrator.notify_execution_complete``.

O que se prova neste arquivo:

1. depois de ``_flows_get`` construir a ponte, ``progress_hook()`` não é
   ``None`` e a etapa gravada sai ``hook=installado``;
2. uma execução viva emite ``execution_complete`` exatamente UMA vez, com o
   que o contrato FLUXO EXECUCAO pede (ações despachadas, desfecho por ação,
   rota, origem e tempo);
3. repetir o ciclo, chamar a notificação outra vez ou relançar o mesmo turno
   não duplica o evento; erro do portão e erro da notificação não viram
   silêncio nem quebram o despacho;
4. nada de evento onde não houve despacho (turno de conversa, turno sem ação
   de confirmação) e o kill switch ``log_events`` continua valendo;
5. ``run_plan`` (caminho morto no vivo) não é invocado: não há segunda fonte
   do mesmo fato.

Nada daqui toca o ``logs/activity.log`` do dono: o espelho vai para
``tmp_path`` (``MIRROR_DIR``), o log do executor é isolado no autouse e a
configuração de fluxos do teste vem de um JSON temporário.
"""
from __future__ import annotations

import json
import threading
import time
import types
from pathlib import Path

import pytest

from core import task_mirror
from core.flows import executor as exe
from core.flows.config import FlowsConfig
from core.flows.live import FlowsRuntime


# ── isolamento ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _sem_log_real(tmp_path, monkeypatch):
    """Nenhum teste daqui escreve no activity.log do dono.

    Três escritores, três caminhos: o espelho (``MIRROR_DIR``, no fixture
    ``espelho``), o registro de ação do executor e a medição de latência da
    boca — esta última vive em ``core.flows.mouth._LOG_PATH`` e é chamada pela
    ponte com o default de produção, inclusive quando o runtime nasce do
    ``main._flows_get``.
    """
    from core.flows import mouth as mouth_mod

    monkeypatch.setattr(exe, "_ACTIVITY_LOG_PATH", tmp_path / "activity.log")
    monkeypatch.setattr(mouth_mod, "_LOG_PATH", tmp_path / "latencia.log")
    task_mirror.reset()
    yield
    task_mirror.reset()


@pytest.fixture
def sem_janela_do_dono(monkeypatch):
    """Fecha a janela de 120 s do dono: ação confirmável continua pedindo confirmação.

    Sem isto o desfecho esperado dependeria do horário real em que a suite
    roda — e a lei (b) é justamente "nada é executado aqui".
    """
    from core import actiongate
    monkeypatch.setattr(actiongate, "authorized", lambda *a, **k: False)
    yield


@pytest.fixture
def espelho(tmp_path, monkeypatch):
    """Espelho em tmp, sem terminal e sem janela de throttle."""
    directory = tmp_path / "logs" / "mirror"
    monkeypatch.setattr(task_mirror, "MIRROR_DIR", directory)
    monkeypatch.setattr(task_mirror, "_open_window",
                        lambda entry: (True, "Abri o terminal na tela."))
    monkeypatch.setattr(task_mirror, "_assistant_name", lambda: "Gino")
    monkeypatch.setattr(task_mirror, "_find_task_meta", lambda *a, **k: None)
    monkeypatch.setattr(task_mirror, "PROGRESS_GAP_S", 0.0)
    monkeypatch.setattr(task_mirror, "PROGRESS_STAGE_GAP_S", 0.0)
    yield {"activity": directory.parent / "activity.log"}


def _activity_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _runtime(**kw) -> tuple[FlowsRuntime, list[str]]:
    """Runtime viva SEM boca e SEM executor real: só o despacho do portão."""
    base = dict(enabled=True, mouth_enabled=False, executor_enabled=False,
                log_events=True)
    base.update(kw)
    events: list[str] = []
    rt = FlowsRuntime(FlowsConfig(**base), speak=events.append,
                      log=events.append, start_expiry_thread=False)
    return rt, events


def _concl(events: list[str]) -> list[str]:
    return [e for e in events if "'type': 'execution_complete'" in e]


def _espera_conclusao(events: list[str], *, segundos: float = 2.0) -> list[str]:
    """O despacho do portão roda em thread própria: espera fechar sem dormir."""
    deadline = time.monotonic() + segundos
    while not _concl(events) and time.monotonic() < deadline:
        time.sleep(0.01)
    return _concl(events)


# ── UP-10: o caminho vivo emite a conclusão ─────────────────────────────────

def test_execucao_viva_emite_execution_complete_uma_unica_vez(
        sem_janela_do_dono):
    rt, events = _runtime()
    plan = rt.start_turn("t1", "desliga o computador aí")
    assert plan is not None and plan.active

    concl = _espera_conclusao(events)
    assert len(concl) == 1, f"uma execução, uma notificação: {concl}"
    payload = concl[0]
    # Contrato FLUXO EXECUCAO passo 3: o que foi despachado, o desfecho de
    # cada ação e o tempo — exatamente os campos que o orquestrador monta.
    for campo in ("'actions'", "'dispatched'", "'results'", "'total_ms'",
                  "'route'", "'origin'", "'ok_count'", "'failures'", "facts"):
        assert campo in payload, campo
    assert "'dispatched': 1" in payload
    assert "restart_assistant" in payload
    # Nada foi executado (lei b): o desfecho honesto é confirmação pendente.
    assert "'state': 'confirmacao_pendente'" in payload
    rt.finish("t1")


def test_ciclo_repetido_do_portao_nao_duplica_a_notificacao(
        sem_janela_do_dono):
    rt, events = _runtime()
    plan = rt.start_turn("t1", "desliga o computador aí")
    assert plan is not None
    assert _espera_conclusao(events)

    # O mesmo ciclo relançado (turno ainda registrado) e a chamada direta da
    # notificação: nada se repete.
    for _ in range(3):
        rt._gate_worker(plan.turn_id)
    rt._notify_execution(rt._turns[plan.turn_id], [], [], rt.clock())
    time.sleep(0.1)
    assert len(_concl(events)) == 1
    rt.finish("t1")


def test_turno_sem_despacho_gera_nenhum_execution_complete():
    rt, events = _runtime()
    plan = rt.start_turn("t1", "que horas são?")
    assert plan is not None
    time.sleep(0.2)
    assert _concl(events) == [], "nada foi despachado: nada se conclui"
    rt.finish("t1")


def test_portao_que_levanta_notifica_como_falha(sem_janela_do_dono, monkeypatch):
    rt, events = _runtime()

    def _boom(*a, **k):
        raise RuntimeError("portão quebrado")

    # ANTES do turno: é o único jeito de o thread do portão encontrar o erro.
    monkeypatch.setattr(rt.orchestrator.executor, "run", _boom)
    plan = rt.start_turn("t1", "desliga o computador aí")
    assert plan is not None

    concl = _espera_conclusao(events)
    assert len(concl) == 1, "erro do portão é desfecho, não silêncio"
    assert "'state': 'falha'" in concl[0]
    assert "RuntimeError" in concl[0]
    assert "'dispatched': 1" in concl[0]
    rt.finish("t1")


def test_caminho_morto_run_plan_nao_participa_da_execucao_viva(
        sem_janela_do_dono):
    """Não há segunda fonte do evento: o vivo não chama ``run_plan``."""
    rt, events = _runtime()
    called: list[str] = []
    original = rt.orchestrator.run_plan

    def espiado(*a, **k):
        called.append("run_plan")
        return original(*a, **k)

    rt.orchestrator.run_plan = espiado
    plan = rt.start_turn("t1", "desliga o computador aí")
    assert plan is not None
    assert len(_espera_conclusao(events)) == 1
    assert called == [], "o vivo não passa por run_plan"
    rt.finish("t1")


def test_kill_switch_log_events_desliga_a_notificacao_viva(
        sem_janela_do_dono):
    rt, events = _runtime(log_events=False)
    plan = rt.start_turn("t1", "desliga o computador aí")
    assert plan is not None
    _espera_conclusao(events, segundos=0.3)
    assert _concl(events) == []
    rt.finish("t1")


def test_notificacao_quebra_mas_nao_derruba_o_portao(sem_janela_do_dono,
                                                     monkeypatch):
    rt, events = _runtime()

    def _explode(*a, **k):
        raise RuntimeError("alto-falante entupido")

    monkeypatch.setattr(rt.orchestrator, "notify_execution_complete", _explode)
    plan = rt.start_turn("t1", "desliga o computador aí")
    assert plan is not None
    deadline = time.monotonic() + 2.0
    while not any("execution_complete falhou" in e for e in events) \
            and time.monotonic() < deadline:
        time.sleep(0.01)

    assert any("execution_complete falhou" in e for e in events)
    # A marca fica posta mesmo com erro: não há segunda tentativa muda.
    assert rt._turns[plan.turn_id].execution_notified is True
    rt.finish("t1")


# ── UP-8: o boot real da ponte deixa o gancho presente ──────────────────────

def _jarvis_com_ponte(tmp_path, monkeypatch, spoken: list[str]):
    """``JarvisLive`` montado à mão e a ponte construída pelo caminho do vivo.

    Só o ``_flows_get`` de verdade conta: é ele que o processo chama no
    primeiro turno, e é dele que o gancho tem de sair instalado.
    """
    import main
    from core import webcam_attention as _attn

    cfg = tmp_path / "flows.json"
    cfg.write_text(json.dumps({"enabled": True,
                               "mouth": {"enabled": False},
                               "executor": {"enabled": False}}),
                   encoding="utf-8")
    monkeypatch.setenv("JARVIS_FLOWS_CONFIG", str(cfg))
    # O gate de atenção é opt-in e abre webcam: desligado por construção.
    monkeypatch.setattr(_attn.AttentionConfig, "load",
                        classmethod(lambda cls, *a, **k: cls(enabled=False)))

    obj = object.__new__(main.JarvisLive)
    obj.ui = types.SimpleNamespace(write_log=lambda _msg: None)
    obj._asst_name = "Gino"
    obj._flows_rt = None
    obj._flows_rt_lock = threading.Lock()
    obj._flows_prewarm = {}
    obj._live_progress_speech = lambda text: spoken.append(text)
    return obj, main


def test_boot_do_main_instala_o_gancho_e_a_etapa_nao_sai_ausente(espelho,
                                                                  tmp_path,
                                                                  monkeypatch):
    spoken: list[str] = []
    obj, main = _jarvis_com_ponte(tmp_path, monkeypatch, spoken)
    assert task_mirror.progress_hook() is None, "regressão: gancho do teste vizinho"

    rt = obj._flows_get()
    try:
        assert rt is not None, "ponte não montou (config desligada?)"
        assert task_mirror.progress_hook() is not None, \
            "boot da ponte sem gancho instalado é o bug do UP-8"

        task_mirror.begin("trabalho-9", "Consertar o login", source="queue",
                          auto_open=False)
        task_mirror.append("trabalho-9", "pytest passou nos testes de rede")

        linha = _activity_lines(espelho["activity"])[-1]
        assert "event=task_progress" in linha
        assert "hook=installado" in linha
        assert "hook=ausente" not in linha
    finally:
        if rt is not None:
            rt.close()


def test_sem_instalacao_a_etapa_sai_hook_ausente(espelho):
    """A outra face da invariante: sem gancho o espelho grava e o dono não ouve."""
    assert task_mirror.progress_hook() is None
    task_mirror.begin("trabalho-8", "Consertar o login", source="queue",
                      auto_open=False)
    task_mirror.append("trabalho-8", "pytest passou nos testes de rede")

    linha = _activity_lines(espelho["activity"])[-1]
    assert "hook=ausente" in linha and "spoken=" not in linha
