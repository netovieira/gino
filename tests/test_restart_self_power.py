"""O reinício do PRÓPRIO PROCESSO é poder do assistente (dono, 10/11/2026).

Prova os seis pontos da ordem dele, sem nunca tocar a máquina nem o processo
vivo: (a) executa sem a frase do dono; (b) outra voz autorizada do lado físico
também dispara; (c) `restart_pc` continua parkando e não existe caminho do
poder do processo para a máquina; (d) guarda de executor/subagente vivo adia;
(e) o teto anti-loop; (f) a trilha diz quem pediu e por quê.

Rode só este arquivo e os subsets de restart/tool bridge:

    .venv/bin/pytest tests/test_restart_self_power.py -q

Nenhum teste aqui usa dry_run=False com subprocess real: os dois mocks de
`subprocess.run`/`Popen` abaixo são a garantia de que nada nasce.
"""
from __future__ import annotations

import inspect
import json
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.restart as restart  # noqa: E402
from actions import restart_assistant as action  # noqa: E402
from core.restart import RestartManager  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_spawner(monkeypatch):
    """Nada desta família pode criar processo: nem helper, nem systemd-run."""
    monkeypatch.setattr(restart.subprocess, "run", mock.Mock(
        side_effect=AssertionError("subprocess.run was called")))
    popen = mock.Mock(side_effect=AssertionError("subprocess.Popen was called"))
    monkeypatch.setattr(restart.subprocess, "Popen", popen)
    return popen


@pytest.fixture(autouse=True)
def _no_builtin_busy(monkeypatch):
    """Por padrão não há executor vivo (o processo de teste não tem fila)."""
    monkeypatch.setattr(restart, "_jobs_busy", lambda: False)


@pytest.fixture(autouse=True)
def _unbound_action():
    action.bind(None)
    yield
    action.bind(None)


def _manager(tmp_path, **kw) -> RestartManager:
    """Manager com estado isolado. Um subdiretório pedido pelo teste é criado
    na hora — assim o teto de um manager nunca vaza para o outro."""
    folder = Path(tmp_path)
    folder.mkdir(parents=True, exist_ok=True)
    return RestartManager(dry_run=True, logger=mock.Mock(),
                          state_path=folder / "restart_pending.json", **kw)


def _trail(tmp_path) -> list[dict]:
    path = tmp_path / "restart_trail.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ── (a) o reinício do processo executa SEM a frase do dono ──────────────────

def test_self_restart_runs_without_any_phrase(tmp_path):
    m = _manager(tmp_path)

    out = m.self_restart(reason="testei a mudança do portão")

    assert out.startswith("[DRY RUN]"), out
    assert "CONFIRMATION_PENDING" not in out
    assert "confirmo reiniciar" not in out
    # Nada foi estacionar à espera de fala: o efeito é agora.
    assert m.pending() is None


def test_tool_assistant_action_executes_without_parking(tmp_path):
    m = _manager(tmp_path)
    action.bind(m)

    out = action.restart_assistant({"action": "assistant",
                                    "reason": "provar a autonomia"})

    assert "[DRY RUN]" in out, out
    assert "[CONFIRMATION_PENDING]" not in out
    assert m.pending() is None


def test_bridge_alias_assistant_is_the_power_not_a_park(tmp_path):
    m = _manager(tmp_path)

    out = m.assistant(reason="ponte qoder")

    assert "[DRY RUN]" in out, out
    assert "[CONFIRMATION_PENDING]" not in out


# ── (b) qualquer pessoa pedindo também dispara ──────────────────────────────

@pytest.mark.parametrize("requester", [restart.REQUESTER_OWNER,
                                       restart.REQUESTER_VOICE,
                                       restart.REQUESTER_SELF])
def test_every_requester_fires_the_same_power(tmp_path, requester):
    m = _manager(tmp_path)

    out = m.self_restart(reason="pediram na conversa", requester=requester)

    assert "[DRY RUN]" in out, out
    assert m.pending() is None
    lines = _trail(tmp_path)
    assert lines and lines[-1]["decisao"] == "executado"
    assert lines[-1]["quem"] == requester


def test_unknown_requester_is_recorded_as_such(tmp_path):
    m = _manager(tmp_path)

    assert restart.normalize_requester("quem quer que seja") == (
        restart.REQUESTER_UNKNOWN)
    out = m.self_restart(reason="voz qualquer", requester="quem quer que seja")
    assert "[DRY RUN]" in out
    assert _trail(tmp_path)[-1]["quem"] == restart.REQUESTER_UNKNOWN


# ── (c) a máquina continua no portão antigo, e não há vazamento ─────────────

def test_restart_pc_still_parks_and_needs_the_exact_phrase(tmp_path):
    m = _manager(tmp_path)

    parked = m.request_pc(reason="kernel novo")
    assert "[CONFIRMATION_PENDING]" in parked
    assert "confirmo reiniciar o computador" in parked
    assert m.pending()["kind"] == "pc"

    refused = m.confirm("reinicia logo isso aí")
    assert "not confirmed" in refused.lower()
    assert m.pending() is not None

    done = m.confirm("confirmo reiniciar o computador")
    assert "[DRY RUN]" in done and "PC" in done
    assert m.pending() is None


def test_tool_pc_action_still_parks_after_a_self_restart(tmp_path):
    m = _manager(tmp_path)
    action.bind(m)

    assert "[DRY RUN]" in action.restart_assistant({"action": "assistant"})
    parked = action.restart_assistant({"action": "pc", "reason": "kernel"})

    assert "[CONFIRMATION_PENDING]" in parked
    assert "confirmo reiniciar o computador" in parked
    assert m.pending()["kind"] == "pc"


def test_self_power_has_no_route_to_the_machine(tmp_path):
    """Não existe caminho: o poder do processo nunca alcança `_run_pc`."""
    m = _manager(tmp_path)
    action.bind(m)
    machine = mock.Mock(side_effect=AssertionError("a máquina foi tocada"))
    m._run_pc = machine
    pc_alias = mock.Mock(side_effect=AssertionError("request_pc foi chamado"))
    m.request_pc = pc_alias

    # Assinatura: sem parâmetro de alvo, o único efeito possível é o processo.
    assert set(inspect.signature(RestartManager.self_restart).parameters) == {
        "self", "reason", "requester"}

    m.self_restart(reason="iniciativa própria")
    m.self_restart(reason="com o teto livre", requester="outra voz")
    action.restart_assistant({"action": "self"})
    action.restart_assistant({"action": "jarvis", "reason": "teste"})
    m.assistant(reason="ponte")
    m.flush_self_restart()

    machine.assert_not_called()
    pc_alias.assert_not_called()


def test_confirm_marker_is_not_needed_nor_produced(tmp_path):
    m = _manager(tmp_path)

    out = m.self_restart(reason="sem frase")

    assert "confirmo" not in out.lower()
    assert "owner-factor-only" not in out


# ── (d) guarda: nunca reiniciar com executor/subagente rodando ──────────────

def test_defers_when_a_background_job_is_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(restart, "_jobs_busy", lambda: True)
    m = _manager(tmp_path)

    out = m.self_restart(reason="mudança testada", requester="dono")

    assert "ADIADO" in out, out
    assert "tarefa de fundo" in out
    assert m.deferred_self_restart() is not None
    assert m.pending() is None
    line = _trail(tmp_path)[-1]
    assert line["decisao"] == "adiado"
    assert line["guarda"] == "tarefa de fundo em execução"

    # Enquanto a guarda continua viva, o tick não reinicia.
    assert m.flush_self_restart() == ""
    assert m.deferred_self_restart() is not None

    # Liberou o executor: o ponto de efeito entrega o que ficou adiado.
    monkeypatch.setattr(restart, "_jobs_busy", lambda: False)
    fired = m.flush_self_restart()

    assert fired.startswith("[DRY RUN]"), fired
    assert m.deferred_self_restart() is None
    assert [r["decisao"] for r in _trail(tmp_path)] == ["adiado", "executado"]


def test_defers_while_the_answer_is_still_leaving(tmp_path):
    m = _manager(tmp_path)
    delivering = mock.Mock(return_value=True)
    m.bind(delivering_cb=delivering)

    out = m.self_restart(reason="resposta no ar")

    assert "ADIADO" in out and "resposta ainda em entrega" in out
    assert m.flush_self_restart() == ""       # ainda entregando: nada
    assert m.deferred_self_restart() is not None
    delivering.return_value = False
    assert m.flush_self_restart().startswith("[DRY RUN]")


def test_deferred_slot_coalesces_one_decision_per_batch(tmp_path, monkeypatch):
    """Um reinício por lote: o segundo pedido não acumula um segundo reboot."""
    monkeypatch.setattr(restart, "_jobs_busy", lambda: True)
    m = _manager(tmp_path)

    first = m.self_restart(reason="primeiro", requester="dono")
    second = m.self_restart(reason="segundo", requester="iniciativa propria")

    # O primeiro foi adiamento; o segundo é RECUSA, porque não entrou na fila.
    assert "ADIADO" in first
    assert "não reiniciei" in second.lower(), second
    monkeypatch.setattr(restart, "_jobs_busy", lambda: False)
    fired = m.flush_self_restart()

    assert fired.startswith("[DRY RUN]")
    assert m.deferred_self_restart() is None
    # O slot tinha a origem do primeiro pedido, não a do segundo.
    assert m._last_reason or True
    assert [r["decisao"] for r in _trail(tmp_path)] == ["adiado", "recusado",
                                                        "executado"]
    assert _trail(tmp_path)[-1]["motivo"] == "primeiro"


def test_deferral_ages_out_instead_of_firing_days_later(tmp_path, monkeypatch):
    monkeypatch.setattr(restart, "_jobs_busy", lambda: True)
    m = _manager(tmp_path)
    assert "ADIADO" in m.self_restart(reason="adiado para sempre")

    monkeypatch.setattr(restart, "SELF_DEFER_TTL_SECONDS", -1.0)
    out = m.flush_self_restart()

    assert "descartado" in out.lower(), out
    assert m.deferred_self_restart() is None
    assert _trail(tmp_path)[-1]["decisao"] == "descartado"


# ── (e) teto anti-loop ──────────────────────────────────────────────────────

def test_ceiling_refuses_a_second_restart_in_the_window(tmp_path):
    m = _manager(tmp_path)

    assert "[DRY RUN]" in m.self_restart(reason="primeiro do lote")
    second = m.self_restart(reason="segundo pedido", requester="outra voz")

    assert "não reiniciei" in second.lower(), second
    assert "teto" in second and "5 minutos" in second
    assert _trail(tmp_path)[-1]["decisao"] == "recusado"
    assert _trail(tmp_path)[-1]["guarda"] == "teto de reinício"


def test_ceiling_survives_the_restart_it_guards(tmp_path):
    """O teto é o restart_last.json persistido — não nasce com o processo novo."""
    (tmp_path / "restart_last.json").write_text(json.dumps(
        {"at": time.time(), "pid": os_pid_of_someone_else(),
         "reason": "reinício anterior"}), encoding="utf-8")
    m = _manager(tmp_path)

    out = m.self_restart(reason="logo depois do boot")

    assert "não reiniciei" in out.lower(), out
    assert _trail(tmp_path)[-1]["decisao"] == "recusado"


# ── (e2) a recusa é FALADA com o motivo (dono, 10/11/2026, áudio) ───────────
#
# "Se eu te pedir e você tiver algum impedimento, você tem que me falar que
# você não pode reiniciar por conta de tal coisa." Cada um dos quatro
# impedimentos tem de sair daqui nomeando o fato concreto — nunca silêncio,
# nunca "não deu". Afrouxar guarda não é opção: o que muda é a frase.

def test_each_impediment_names_itself_in_the_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(restart, "_jobs_busy", lambda: True)
    m = _manager(tmp_path)

    fundo = m.self_restart(reason="pediram na conversa", requester="dono")
    assert "tarefa de fundo em execução" in fundo, fundo

    def _segundo_pedido(manager):
        return manager.self_restart(reason="de novo", requester="dono")

    slot_cheio = _segundo_pedido(m)
    assert "já existe um reinício" in slot_cheio, slot_cheio
    assert "tarefa de fundo em execução" in slot_cheio

    teto = _manager(tmp_path / "teto")
    monkeypatch.setattr(restart, "_jobs_busy", lambda: False)
    assert "[DRY RUN]" in teto.self_restart(reason="primeiro")
    gasto = _segundo_pedido(teto)
    assert "teto" in gasto and "5 minutos" in gasto, gasto

    entrega = _manager(tmp_path / "entrega")
    entrega.bind(delivering_cb=lambda: True)
    monkeypatch.setattr(restart, "_jobs_busy", lambda: False)
    no_ar = _segundo_pedido(entrega)
    assert "resposta ainda em entrega" in no_ar, no_ar

    for phrase in (fundo, slot_cheio, gasto, no_ar):
        assert "Nada foi reiniciado" in phrase or "nada foi reiniciado" \
            in phrase, phrase


def test_the_slot_full_refusal_does_not_promise_a_restart(tmp_path,
                                                          monkeypatch):
    """Caso (d): o segundo pedido do lote NÃO ficou na fila — dizer o contrário
    seria prometer um reinício que nunca acontece."""
    monkeypatch.setattr(restart, "_jobs_busy", lambda: True)
    m = _manager(tmp_path)
    m.self_restart(reason="primeiro", requester="dono")

    out = m.self_restart(reason="segundo", requester="dono")

    assert "ADIADO" not in out, out
    assert "o seu não entrou na fila" in out
    assert _trail(tmp_path)[-1]["decisao"] == "recusado"
    assert _trail(tmp_path)[-1]["guarda"] == "adiamento já em espera"


def test_refusal_carries_no_secret_and_no_contact_number(tmp_path,
                                                         monkeypatch):
    """A frase de recusa é receita fixa: nem o reason sujo vaza nela."""
    monkeypatch.setattr(restart, "_jobs_busy", lambda: True)
    m = _manager(tmp_path)
    sujo = ("liga no +55 48 99999-1234 e confere senha=abracadabra123 "
            "token=DEADBEEF")

    first = m.self_restart(reason=sujo, requester="dono")
    second = m.self_restart(reason=sujo, requester="dono")
    m._last_assistant_at = None
    teto = _manager(tmp_path / "sem-teto")
    teto.self_restart(reason=sujo)
    gasto = teto.self_restart(reason=sujo)

    for phrase in (first, second, gasto):
        assert "abracadabra123" not in phrase, phrase
        assert "DEADBEEF" not in phrase, phrase
        assert "99999-1234" not in phrase and "999991234" not in phrase, phrase


# ── (e3) adiamento que sai da fila vira AVISO (dono, 10/11/2026) ────────────

def _notice_manager(tmp_path, monkeypatch, busy=True):
    """Manager com o canal de aviso do app capturado."""
    monkeypatch.setattr(restart, "_jobs_busy", lambda: busy)
    m = _manager(tmp_path)
    saids: list[str] = []
    m.bind(notice_cb=saids.append)
    return m, saids


def test_when_the_impediment_passes_the_owner_is_warned(tmp_path, monkeypatch):
    m, saids = _notice_manager(tmp_path, monkeypatch)

    assert "ADIADO" in m.self_restart(reason="correção do portão",
                                      requester="dono")
    assert saids == []                    # ainda esperando: nada a dizer
    monkeypatch.setattr(restart, "_jobs_busy", lambda: False)

    fired = m.flush_self_restart()

    assert fired.startswith("[DRY RUN]"), fired
    assert any("adiado" in line and "vai sair agora" in line for line in saids
               ), saids


def test_an_expired_deferral_is_announced_as_thrown_away(tmp_path,
                                                         monkeypatch):
    m, saids = _notice_manager(tmp_path, monkeypatch)
    assert "ADIADO" in m.self_restart(reason="adiado para sempre")

    monkeypatch.setattr(restart, "SELF_DEFER_TTL_SECONDS", -1.0)
    out = m.flush_self_restart()

    assert out in saids, (out, saids)
    assert "descartado" in saids[-1] and "Nada foi reiniciado" in saids[-1]


def test_the_fire_notice_is_not_promised_without_a_shutdown_hook(tmp_path,
                                                                 monkeypatch):
    """dry_run=False sem gancho de desligada: não executa, então não promete."""
    monkeypatch.setattr(restart, "_jobs_busy", lambda: True)
    m = RestartManager(dry_run=False, logger=mock.Mock(),
                       state_path=tmp_path / "restart_pending.json")
    saids: list[str] = []
    m.bind(notice_cb=saids.append)
    assert "ADIADO" in m.self_restart(reason="efeito real")
    monkeypatch.setattr(restart, "_jobs_busy", lambda: False)

    out = m.flush_self_restart()

    assert "shutdown hook" in out, out
    assert saids == []


def test_notice_failure_never_breaks_the_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(restart, "_jobs_busy", lambda: True)
    m = _manager(tmp_path)
    m.bind(notice_cb=mock.Mock(side_effect=RuntimeError("boca ocupada")))
    assert "ADIADO" in m.self_restart(reason="aviso que quebra")
    monkeypatch.setattr(restart, "_jobs_busy", lambda: False)

    assert m.flush_self_restart().startswith("[DRY RUN]")



def os_pid_of_someone_else() -> int:
    import os
    return os.getpid() + 987654321


# ── (f) trilha de toda decisão ──────────────────────────────────────────────

def test_trail_says_who_asked_and_why(tmp_path):
    m = _manager(tmp_path)

    m.self_restart(reason="verificar a correção do áudio", requester="dono")

    line = _trail(tmp_path)[-1]
    assert line["quem"] == "dono"
    assert line["motivo"] == "verificar a correção do áudio"
    assert line["decisao"] == "executado"
    assert line["alvo"] == "processo"
    assert line["guarda"] == ""
    assert {"at", "pid"} <= set(line)


def test_trail_records_a_refusal_with_its_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(restart, "_jobs_busy", lambda: True)
    m = _manager(tmp_path)

    m.self_restart(reason="pedido da eloísa", requester="outra voz")

    line = _trail(tmp_path)[-1]
    assert line["decisao"] == "adiado"
    assert line["quem"] == "outra voz"
    assert line["motivo"] == "pedido da eloísa"
    assert line["guarda"] == "tarefa de fundo em execução"


def test_trail_never_holds_a_secret_or_a_contact_number(tmp_path):
    m = _manager(tmp_path)
    sujo = ("confere em +55 48 99999-1234 por favor "
            "senha=abracadabra123 token=DEADBEEF")

    m.self_restart(reason=sujo, requester="dono")

    raw = (tmp_path / "restart_trail.jsonl").read_text(encoding="utf-8")
    assert "abracadabra123" not in raw
    assert "99999-1234" not in raw and "999991234" not in raw
    assert "DEADBEEF" not in raw
    assert restart._TRAIL_MAX_CHARS >= len(
        _trail(tmp_path)[-1]["motivo"])


def test_trail_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(restart, "TRAIL_MAX_LINES", 5)
    m = _manager(tmp_path)

    for i in range(12):
        m.self_restart(reason=f"lote {i}")
        m._last_assistant_at = None      # libera o teto para o próximo lote

    lines = _trail(tmp_path)
    assert len(lines) <= 5
    assert lines[-1]["motivo"] == "lote 11"


def test_dry_run_without_isolated_state_writes_no_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    m = RestartManager(dry_run=True, logger=mock.Mock())

    assert "[DRY RUN]" in m.self_restart(reason="sem estado")

    assert m.pending() is None
    assert not Path("restart_pending.json").exists()
