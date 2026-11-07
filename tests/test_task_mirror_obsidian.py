"""UP-7: o espelho de execução vira nota no Obsidian (com cópia no Nextcloud).

Cobre o passo 4 do FLUXO TRABALHO ("usuário solicita abertura na tela") e o
evento ``obsidian_opened`` no activity.log. O cofre, a pasta de sincronização
e o launcher são sempre falsos: nada aqui escreve no cofre real do dono nem
abre aplicativo de verdade.

As regras duras testadas:

* o plano vem do arquivo que o passo 2 grava em ``~/.qoder/plans`` — sem job,
  a nota não inventa plano, mostra o andamento;
* a cópia Nextcloud cai dentro de ``~/Nextcloud/Jarvis`` (a conta do dono) e
  nenhum comando de compartilhamento/link público é disparado;
* sem Obsidian, sem cofre ou sem execução: mensagem honesta, nunca exceção.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import task_mirror as mirror_action  # noqa: E402
from core import obsidian, qoder_jobs, task_mirror  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Espelho + cofre + Nextcloud + launcher, todos em tmp_path."""
    mirror_dir = tmp_path / "logs" / "mirror"
    vault = tmp_path / "Jarvis"
    plans = tmp_path / "qoder" / "plans"
    plans.mkdir(parents=True)
    activity = tmp_path / "logs" / "activity.log"
    nextcloud = tmp_path / "Nextcloud"
    nextcloud.mkdir()
    opens: list[dict] = []
    launched: list[list[str]] = []

    def fake_run(argv, **kwargs):
        launched.append(list(argv))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(task_mirror, "MIRROR_DIR", mirror_dir)
    monkeypatch.setattr(task_mirror, "_open_window",
                        lambda entry: (True, "Abri o terminal na tela."))
    monkeypatch.setattr(task_mirror, "_assistant_name", lambda: "Gino")
    monkeypatch.setattr(obsidian, "VAULT", vault)
    monkeypatch.setattr(obsidian, "PLANS_DIR", plans)
    monkeypatch.setattr(obsidian, "ACTIVITY_LOG", activity)
    monkeypatch.setattr(obsidian, "NEXTCLOUD_DIR", nextcloud)
    monkeypatch.setattr(obsidian.subprocess, "run", fake_run)
    monkeypatch.setattr(obsidian.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("Popen não é necessário"))
    monkeypatch.setattr(qoder_jobs, "_ACTIVE", None)
    # O cofre registrado é lido da config real do Obsidian; nos testes o cofre
    # é sempre o dos tmp_path, então fingimos "sem config" (=> vale o nosso).
    monkeypatch.setattr(obsidian, "registered_vault", lambda: "")
    task_mirror.reset()
    yield {
        "mirror": task_mirror,
        "obsidian": obsidian,
        "mirror_dir": mirror_dir,
        "vault": vault,
        "plans": plans,
        "activity": activity,
        "nextcloud": nextcloud,
        "opens": opens,
        "launched": launched,
    }
    task_mirror.reset()


def _note(title: str) -> Path:
    return obsidian._slug(f"Plano de ação — {title}") + ".md"


def _running_mirror(ident="tarefa-obs", title="Consertar o login",
                    lines=("$ pytest -q", "editar core/task_mirror.py")):
    task_mirror.begin(ident, title, source="queue", auto_open=False)
    for line in lines:
        task_mirror.append(ident, line)


def _activity_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


# ── abrir o plano na tela ───────────────────────────────────────────────────

def test_open_plan_writes_note_in_vault_and_opens_it(env):
    e = env
    _running_mirror()

    result = task_mirror.open_plan(ident="tarefa-obs")

    assert result["ok"] is True, result
    note = Path(result["note"])
    assert note.parent == e["vault"] / "Planos"
    assert note.name == _note("Consertar o login")
    text = note.read_text(encoding="utf-8")
    assert "# Plano de ação — Consertar o login" in text
    assert "## Andamento" in text
    assert "$ pytest -q" in text and "editar core/task_mirror.py" in text
    assert f"- Log ao vivo: {e['mirror_dir'] / 'tarefa-obs.log'}" in text
    # O launcher recebeu o URI do Obsidian com o caminho da nota.
    assert e["launched"] == [[
        "xdg-open",
        "obsidian://open?path=" + quote(str(note), safe=""),
    ]]


def test_obsidian_opened_event_lands_in_activity_log(env):
    e = env
    _running_mirror()

    result = task_mirror.open_plan(ident="tarefa-obs")

    # UP-8 põe ``event=task_progress`` no mesmo arquivo: aqui só interessa a
    # abertura do cofre.
    lines = [line for line in _activity_lines(e["activity"])
             if "event=obsidian_opened" in line]
    assert len(lines) == 1, lines
    line = lines[0]
    assert line.startswith("[") and "] event=obsidian_opened " in line
    assert f'path="{Path(result["note"])}"' in line
    assert "source=task_mirror" in line and "event=obsidian_opened" in line


def test_note_is_copied_into_the_owner_nextcloud_folder(env):
    e = env
    _running_mirror()

    result = task_mirror.open_plan(ident="tarefa-obs")

    copied = Path(result["nextcloud"])
    assert copied == (e["nextcloud"] / "Jarvis" / "Planos" / _note("Consertar o login"))
    assert copied.read_text(encoding="utf-8") == \
        Path(result["note"]).read_text(encoding="utf-8")


def test_nextcloud_sync_issues_no_share_or_public_link_command(env):
    """Contrato (skills/actions/SKILL.md): o cofre é só do dono.

    Um comando externo por abertura — o URI do Obsidian. A cópia para o
    Nextcloud é só I/O de arquivo na pasta do dono: sem share, sem link
    público, sem curl, sem occ.
    """
    e = env
    _running_mirror()

    task_mirror.open_plan(ident="tarefa-obs")

    assert len(e["launched"]) == 1, e["launched"]
    argv = e["launched"][0]
    assert argv[0] == "xdg-open" and argv[1].startswith("obsidian://open?path=")


def test_missing_nextcloud_folder_still_opens_the_note(env, monkeypatch):
    e = env
    _running_mirror()
    monkeypatch.setattr(obsidian, "NEXTCLOUD_DIR", e["nextcloud"].parent / "nope")

    result = task_mirror.open_plan(ident="tarefa-obs")

    assert result["ok"] is True and result["nextcloud"] == ""
    assert Path(result["note"]).is_file()


def test_plan_section_uses_the_qoder_plan_file(env, tmp_path, monkeypatch):
    e = env
    _running_mirror()
    plan = tmp_path / "plano.md"
    plan.write_text(
        "# Plano — Consertar o login\n\n"
        "## Passos\n\n"
        "1. Ler o espelho\n"
        "2. Corrigir a rota\n\n"
        "## Risco\n\nNada.\n",
        encoding="utf-8")
    monkeypatch.setattr(qoder_jobs, "_ACTIVE", types.SimpleNamespace(
        plan_path=str(plan), plan="resumo em RAM"))

    result = task_mirror.open_plan(ident="tarefa-obs")

    text = Path(result["note"]).read_text(encoding="utf-8")
    section = text.split("## Plano de ação", 1)[1].split("## Andamento", 1)[0]
    assert "1. Ler o espelho" in section and "2. Corrigir a rota" in section
    assert "Nada." not in section        # só a seção Pedidos, não o resto


def test_without_an_active_job_the_note_shows_progress_not_a_fake_plan(env):
    _running_mirror()

    result = task_mirror.open_plan(ident="tarefa-obs")

    text = Path(result["note"]).read_text(encoding="utf-8")
    assert "ainda não gravou um plano" in text
    assert "$ pytest -q" in text


def test_explicit_plan_text_wins_over_the_job(env, monkeypatch):
    monkeypatch.setattr(qoder_jobs, "_ACTIVE", types.SimpleNamespace(
        plan_path="/nao/existe.md", plan="plano velho"))

    _running_mirror()
    result = task_mirror.open_plan(ident="tarefa-obs",
                                   plan="1. Rodar os testes\n2. Commitar")

    text = Path(result["note"]).read_text(encoding="utf-8")
    assert "1. Rodar os testes" in text and "plano velho" not in text


def test_the_opening_is_recorded_in_the_mirror_log(env):
    e = env
    _running_mirror()

    result = task_mirror.open_plan(ident="tarefa-obs")

    log = (e["mirror_dir"] / "tarefa-obs.log").read_text(encoding="utf-8")
    assert f"▸ plano no Obsidian: {result['note']}" in log


def test_repeated_request_updates_the_same_note(env):
    _running_mirror()
    first = task_mirror.open_plan(ident="tarefa-obs")
    second = task_mirror.open_plan(ident="tarefa-obs")

    assert first["note"] == second["note"]
    opened = [line for line in _activity_lines(env["activity"])
              if "event=obsidian_opened" in line]
    assert len(opened) == 2


def test_finished_execution_is_shown_as_concluded(env):
    _running_mirror()
    task_mirror.finish("tarefa-obs", "completed", "pronto")

    result = task_mirror.open_plan(ident="tarefa-obs")

    assert result["ok"] is True
    assert "- Estado: concluída" in Path(result["note"]).read_text("utf-8")


def test_backticks_in_the_log_do_not_break_the_markdown_fence(env):
    _running_mirror(lines=("ler `core/task_mirror.py`",))

    result = task_mirror.open_plan(ident="tarefa-obs")

    text = Path(result["note"]).read_text(encoding="utf-8")
    assert "```" in text
    assert "`core/task_mirror.py`" not in text
    assert "ler 'core/task_mirror.py'" in text


def test_open_plan_resolves_the_same_execution_as_show(env, monkeypatch):
    """A mesma escolha de alvo do passo 4: fila, fala do dono ou live."""
    e = env
    monkeypatch.setattr(task_mirror, "_queue_current", lambda: None)
    monkeypatch.setattr(task_mirror, "_match_task", lambda query: {
        "id": "fila-7", "title": "Instalar o ffmpeg"})
    task_mirror.begin("fila-7", "Instalar o ffmpeg", auto_open=False)
    task_mirror.append("fila-7", "$ apt install ffmpeg")

    result = task_mirror.open_plan(query="abre o plano do instalar o ffmpeg")

    assert result["ok"] and result["id"] == "fila-7"
    assert (e["mirror_dir"] / "fila-7.log").is_file()
    assert "Instalar o ffmpeg" in Path(result["note"]).read_text("utf-8")


def test_open_plan_without_execution_is_honest(env, monkeypatch):
    monkeypatch.setattr(task_mirror, "_queue_current", lambda: None)
    monkeypatch.setattr(task_mirror, "_newest_log", lambda: ("", ""))

    result = task_mirror.open_plan()

    assert result["ok"] is False
    assert "Não há execução" in result["message"]
    assert not env["activity"].exists()


def test_obsidian_failure_returns_the_message_and_never_raises(env, monkeypatch):
    e = env
    _running_mirror()

    def broken_run(argv, **kwargs):
        raise FileNotFoundError("xdg-open")

    monkeypatch.setattr(obsidian.subprocess, "run", broken_run)
    monkeypatch.setattr(obsidian.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError("sem Obsidian")))

    result = task_mirror.open_plan(ident="tarefa-obs")

    assert result["ok"] is False
    assert "sem Obsidian" in result["message"]
    log = (e["mirror_dir"] / "tarefa-obs.log").read_text(encoding="utf-8")
    assert "· plano não abriu no Obsidian:" in log
    assert f"o arquivo está em {Path(result['note'])}" in result["message"]


def test_unavailable_vault_module_is_reported(env, monkeypatch):
    _running_mirror()
    # Instalação sem o módulo do cofre: nem o atributo no pacote nem o módulo
    # existem -> o ``from core import obsidian`` de dentro de open_plan levanta
    # ImportError, e isso não pode voar para a conversa.
    monkeypatch.delattr(sys.modules["core"], "obsidian", raising=False)
    monkeypatch.setitem(sys.modules, "core.obsidian", None)

    result = task_mirror.open_plan(ident="tarefa-obs")

    assert result["ok"] is False and result["note"] == ""
    assert "cofre" in result["message"]


# ── rota do cofre: o que o Obsidian desta máquina realmente abre ───────────

def test_registered_vault_is_read_from_the_obsidian_config(tmp_path,
                                                           monkeypatch):
    config = tmp_path / ".config" / "obsidian" / "obsidian.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        '{"vaults":{"aaa":{"path":"/home/anthero/Documentos/Obsidian Vault",'
        '"ts":1793913401686,"open":true},'
        '"bbb":{"path":"/home/anthero/Outro","ts":1700000000000}}}',
        encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert obsidian.registered_vault() == "/home/anthero/Documentos/Obsidian Vault"


def test_registered_vault_without_config_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "vazio")

    assert obsidian.registered_vault() == ""
    # Sem config não há como contradizer: nada de aviso na mensagem.
    assert obsidian._inside_registered_vault(Path("/qualquer/coisa")) is True


def test_unregistered_vault_is_warned_in_the_message_and_the_log(env, monkeypatch):
    e = env
    _running_mirror()
    monkeypatch.setattr(obsidian, "registered_vault",
                        lambda: str(e["vault"].parent / "Outro Cofre"))

    result = task_mirror.open_plan(ident="tarefa-obs")

    assert result["ok"] is True and result["registered"] is False
    assert "registrado" in result["message"]
    assert str(e["vault"]) in result["message"]
    assert "JARVIS_OBSIDIAN_VAULT" in result["message"]
    log = (e["mirror_dir"] / "tarefa-obs.log").read_text(encoding="utf-8")
    assert "cofre fora da lista do Obsidian" in log


def test_env_can_point_the_vault_without_touching_the_default(env, tmp_path,
                                                              monkeypatch):
    """A rota do cofre é decisão do dono: existe só a alavanca, não o mudar."""
    other = tmp_path / "Vault do Dono"
    monkeypatch.setenv("JARVIS_OBSIDIAN_VAULT", str(other))

    assert obsidian._vault() == other
    note = obsidian.write_note("Plano — Rota", "# plano")
    assert note.parent == other / "Planos"
    assert note.read_text(encoding="utf-8") == "# plano"


def test_default_vault_is_untouched_when_the_env_is_absent(env, monkeypatch):
    monkeypatch.delenv("JARVIS_OBSIDIAN_VAULT", raising=False)

    assert obsidian._vault() == Path(obsidian.VAULT).expanduser()


def test_note_name_keeps_portuguese_and_drops_path_separators(env):
    assert obsidian._slug("Plano de ação — con/clusão") == \
        "Plano de ação — con_clusão"
    assert obsidian._slug("   ") == "nota"
    assert "/" not in obsidian._slug("../../etc/passwd")


# ── módulo do cofre, chamado direto ─────────────────────────────────────────

def test_open_note_reports_path_nextcloud_and_message(env):
    result = obsidian.open_note("Plano — Teste", "# Plano\n\n1. nada\n")

    assert result["ok"] is True
    note = Path(result["path"])
    assert note.parent == env["vault"] / "Planos"
    assert note.read_text(encoding="utf-8") == "# Plano\n\n1. nada\n"
    assert result["nextcloud"].startswith(str(env["nextcloud"] / "Jarvis"))
    assert "Abrindo" in result["message"]


def test_open_note_without_a_writable_vault_is_error_not_exception(env, monkeypatch):
    monkeypatch.setattr(obsidian, "VAULT", Path("/proc/definitely-not-writable"))

    result = obsidian.open_note("Plano — Teste", "corpo")

    assert result["ok"] is False and "cofre" in result["message"]


def test_event_helper_quotes_paths_with_spaces(env, tmp_path):
    spaced = tmp_path / "com espaço" / "nota.md"
    spaced.mkdir(parents=True)
    spaced.joinpath("nota.md").write_text("# nota", encoding="utf-8")

    obsidian._log_event("obsidian_opened", path=spaced / "nota.md",
                        source="task_mirror", extra=None, empty="")

    line = _activity_lines(env["activity"])[0]
    assert f'path="{spaced / "nota.md"}"' in line
    assert "source=task_mirror" in line
    assert "extra" not in line and "empty" not in line


# ── ação do catálogo ────────────────────────────────────────────────────────

def test_action_plan_returns_json_with_the_note(env):
    import json

    _running_mirror()
    payload = json.loads(mirror_action.task_mirror_action(
        {"action": "plan", "task_id": "tarefa-obs"}))

    assert payload["status"] == "ok" and payload["action"] == "plan"
    assert Path(payload["note"]).is_file()
    assert Path(payload["nextcloud"]).is_file()


def test_action_status_lists_and_unknown_action_still_errors(env):
    import json

    _running_mirror()
    ok = json.loads(mirror_action.task_mirror_action({"action": "status"}))
    bad = json.loads(mirror_action.task_mirror_action({"action": "recortar"}))

    assert ok["status"] == "ok"
    assert bad["status"] == "error" and "action=plan" in bad["error"]


def test_action_contract_mentions_the_obsidian_step(env):
    assert mirror_action.TOOL["name"] == "task_mirror"
    assert mirror_action.TOOL["handler"] is mirror_action.task_mirror_action
    assert mirror_action.TOOL["parameters"]["properties"]["action"]["enum"] == \
        ["show", "plan", "status"]
    description = mirror_action.TOOL["description"]
    assert "Obsidian" in description and "plano de ação" in description
