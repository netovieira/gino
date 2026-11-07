"""UP-8: o espelho de execução avisa a boca quando o andamento muda.

O passo 6 do FLUXO TRABALHO manda "notificar a boca a cada mudança". Antes
disso o ``core.task_mirror`` só gravava no log e abria terminal: execução longa
era silenciosa para o dono que está longe da tela.

Aqui se testa o fio inteiro, sem rede, sem Ollama e sem aplicativo:

* cada linha nova do espelho passa por ``on_progress_update``, que decide se a
  mudança merece fala (etapa nova OU janela mínima do mesmo andamento) e grava
  ``event=task_progress`` no activity.log;
* o gancho do processo é ``core.flows.mouth.progress_notifier``: ele fraseia
  pela boca e devolve o resultado para o evento registrar ``spoken=``;
* a lei da boca vale integralmente no andamento: etapa nenhuma autoriza
  "terminou/pronto/aberto", anúncio de ação é cortado e caminho de arquivo
  nunca vira fala.

Nada deste arquivo toca o ``logs/activity.log`` do dono: com ``MIRROR_DIR`` em
``tmp_path`` o evento cai ao lado, no tmp (``_activity_path``).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import task_mirror  # noqa: E402
from core.flows import mouth as mouth_mod  # noqa: E402
from core.flows.config import FlowsConfig  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def espelho(tmp_path, monkeypatch):
    """Espelho em tmp, sem terminal, com janela de notificação zerada."""
    directory = tmp_path / "logs" / "mirror"
    calls: list[dict] = []

    def hook(payload):
        calls.append(dict(payload))
        return True

    monkeypatch.setattr(task_mirror, "MIRROR_DIR", directory)
    monkeypatch.setattr(task_mirror, "_open_window",
                        lambda entry: (True, "Abri o terminal na tela."))
    monkeypatch.setattr(task_mirror, "_assistant_name", lambda: "Gino")
    monkeypatch.setattr(task_mirror, "_find_task_meta", lambda *a, **k: None)
    monkeypatch.setattr(task_mirror, "PROGRESS_GAP_S", 0.0)
    monkeypatch.setattr(task_mirror, "PROGRESS_STAGE_GAP_S", 0.0)
    task_mirror.reset()
    yield {"dir": directory, "activity": directory.parent / "activity.log",
           "calls": calls, "hook": hook}
    task_mirror.reset()


def _config(**kw) -> FlowsConfig:
    base = dict(enabled=True, mouth_enabled=True)
    base.update(kw)
    return FlowsConfig(**base)


def _caller(text, *, error=None):
    """Um `stream_ollama` falso: devolve exatamente a frase pedida."""
    def caller(messages, max_tokens, **kw):
        return {"text": "" if error else text, "ttft_ms": 5.0,
                "first_sentence_ms": 6.0, "end_ms": 7.0, "error": error,
                "aborted": False}
    return caller


def _activity_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _run(ident="trabalho-1", title="Consertar o login", lines=()):
    task_mirror.begin(ident, title, source="queue", auto_open=False)
    for line in lines:
        task_mirror.append(ident, line)


# ── o espelho chama o gancho ────────────────────────────────────────────────

def test_sem_gancho_o_espelho_grava_e_calado_manda_embora(espelho):
    e = espelho
    _run(lines=("$ pytest -q tests",))

    out = task_mirror.on_progress_update("trabalho-1", "editar core/io.py")

    assert task_mirror.progress_hook() is None
    assert out["notified"] is True and out["hook"] is False
    assert out["spoken"] is None
    line = _activity_lines(e["activity"])[-1]
    assert "event=task_progress" in line and "hook=ausente" in line
    assert "spoken=" not in line        # ninguém finge que falou


def test_gancho_recebe_a_etapa_e_o_andamento(espelho):
    e = espelho
    task_mirror.set_progress_hook(e["hook"])
    _run(lines=("$ pytest -q tests",))

    assert e["calls"], "a etapa gravada precisa chegar ao gancho"
    first = e["calls"][0]
    assert first["id"] == "trabalho-1" and first["title"] == "Consertar o login"
    assert first["stage"] == "testes" and "pytest" in first["text"]
    assert first["source"] == "queue"
    assert first["log"].endswith("trabalho-1.log")
    assert first["elapsed_s"] >= 0.0


def test_etapa_nova_notifica_e_andamento_repetido_nao(espelho):
    e = espelho
    task_mirror.set_progress_hook(e["hook"])
    monkey = e  # legibilidade
    _run(ident="trabalho-2", lines=("editar core/io.py",
                                    "buscar token em core",
                                    "editar core/io.py"))

    stages = [c["stage"] for c in e["calls"]]
    assert stages == ["edicao", "busca"], monkey
    # a terceira linha é idêntica à primeira: o dedupe do log a corta antes.
    assert len(_activity_lines(e["activity"])) == 2


def test_mesma_etapa_sobe_depois_do_teto(espelho, monkeypatch):
    e = espelho
    monkeypatch.setattr(task_mirror, "PROGRESS_GAP_S", 0.05)
    monkeypatch.setattr(task_mirror, "PROGRESS_STAGE_GAP_S", 0.0)
    task_mirror.set_progress_hook(e["hook"])
    _run(ident="trabalho-3", lines=("editar core/io.py", "editar core/ua.py"))

    assert len(e["calls"]) == 1, "edição atrás de edição cabe antes do teto"
    time.sleep(0.08)
    task_mirror.append("trabalho-3", "editar core/db.py")
    assert len(e["calls"]) == 2
    assert [c["stage"] for c in e["calls"]] == ["edicao", "edicao"]


def test_janela_de_etapa_corta_o_gotejo_do_transcrito(espelho, monkeypatch):
    e = espelho
    monkeypatch.setattr(task_mirror, "PROGRESS_GAP_S", 5.0)
    monkeypatch.setattr(task_mirror, "PROGRESS_STAGE_GAP_S", 5.0)
    task_mirror.set_progress_hook(e["hook"])
    _run(ident="trabalho-4", lines=("editar core/io.py",))
    before = len(e["calls"])
    task_mirror.append("trabalho-4", "buscar token em core")

    assert before == 1 and len(e["calls"]) == 1
    assert e["calls"][0]["stage"] == "edicao"


def test_alavanca_de_ambiente_muda_o_ritmo(espelho, monkeypatch):
    """O teto é decisão do dono: existe a alavanca, não o arbitrar."""
    e = espelho
    monkeypatch.setenv(task_mirror.PROGRESS_GAP_ENV, "7")
    monkeypatch.setenv(task_mirror.PROGRESS_STAGE_GAP_ENV, "3")

    assert task_mirror._progress_gap(task_mirror.PROGRESS_GAP_ENV, 45.0) == 7.0
    assert task_mirror._progress_gap(
        task_mirror.PROGRESS_STAGE_GAP_ENV, 12.0) == 3.0
    monkeypatch.setenv(task_mirror.PROGRESS_GAP_ENV, "não-número")
    assert task_mirror._progress_gap(
        task_mirror.PROGRESS_GAP_ENV, 45.0) == 45.0
    monkeypatch.setenv(task_mirror.PROGRESS_GAP_ENV, "-2")
    assert task_mirror._progress_gap(
        task_mirror.PROGRESS_GAP_ENV, 45.0) == 45.0
    monkeypatch.setenv(task_mirror.PROGRESS_GAP_ENV, "99999")
    assert task_mirror._progress_gap(
        task_mirror.PROGRESS_GAP_ENV, 45.0) == task_mirror._PROGRESS_GAP_MAX
    task_mirror.set_progress_hook(e["hook"])


def test_espelho_encerrado_para_de_anunciar(espelho):
    e = espelho
    task_mirror.set_progress_hook(e["hook"])
    _run(ident="trabalho-5", lines=("editar core/io.py",))
    n = len(e["calls"])
    task_mirror.finish("trabalho-5", "completed", "pronto")
    task_mirror.append("trabalho-5", "editar core/outra.py")

    assert len(e["calls"]) == n, "o fim não é andamento"
    assert not task_mirror.on_progress_update(
        "trabalho-5", "qualquer coisa")["notified"]


def test_fim_nao_gera_evento_de_andamento(espelho):
    e = espelho
    task_mirror.set_progress_hook(e["hook"])
    _run(ident="trabalho-6", lines=("editar core/io.py",))
    before = len(_activity_lines(e["activity"]))
    task_mirror.finish("trabalho-6", "completed", "resumo")

    assert len(_activity_lines(e["activity"])) == before
    log = (e["dir"] / "trabalho-6.log").read_text(encoding="utf-8")
    assert "── fim: concluído ──" in log


def test_erro_do_gancho_nao_quebra_o_espelho(espelho):
    e = espelho

    def broken(payload):
        raise RuntimeError("alto-falante ocupado")

    task_mirror.set_progress_hook(broken)
    _run(ident="trabalho-7", lines=("editar core/io.py",))

    lines = _activity_lines(e["activity"])
    assert "event=task_progress" in lines[0]
    assert "spoken=false" in lines[0] and "alto-falante ocupado" in lines[0]
    assert (e["dir"] / "trabalho-7.log").is_file()


def test_progresso_do_sidecar_tambem_notifica(espelho):
    """O fallback do sidecar (sem transcrito) é andamento real."""
    e = espelho
    task_mirror.set_progress_hook(e["hook"])
    task_mirror.begin("sub-9", "Revisar o checkout", source="subagent",
                      auto_open=False)
    task_mirror.link("sub-9", "sub-9")
    task_mirror.progress("sub-9", {"last_tool_name": "Bash",
                                   "summary": "rodando a suíte"})

    assert len(e["calls"]) == 1
    assert e["calls"][0]["stage"] == "resumo"
    assert "Bash" in e["calls"][0]["text"]


def test_classifica_etapa_sem_confundir_teste_com_comando(espelho):
    assert task_mirror.classify_stage("$ pytest -q tests") == "testes"
    assert task_mirror.classify_stage("$ npm test") == "testes"
    assert task_mirror.classify_stage("$ apt install ffmpeg") == "comando"
    assert task_mirror.classify_stage("editar core/task_mirror.py") == "edicao"
    assert task_mirror.classify_stage("ler core/io.py") == "leitura"
    assert task_mirror.classify_stage("buscar token em core") == "busca"
    assert task_mirror.classify_stage("organizar o plano de trabalho") == "plano"
    assert task_mirror.classify_stage("▸ subagente: X (t-1)") == "inicio"
    assert task_mirror.classify_stage("  │ 12 passed") == "resultado"
    assert task_mirror.classify_stage("… usando Bash") == "ferramenta"
    assert task_mirror.classify_stage("… rodando a suíte") == "resumo"
    assert task_mirror.classify_stage("── fim: concluído ──") == "fim"
    assert task_mirror.classify_stage("texto qualquer") == ""
    assert task_mirror.classify_stage("") == ""


def test_status_diz_se_a_boca_esta_plugada(espelho):
    assert task_mirror.status()["voice"] is False
    task_mirror.set_progress_hook(espelho["hook"])
    assert task_mirror.status()["voice"] is True
    task_mirror.set_progress_hook(None)
    assert task_mirror.status()["voice"] is False


def test_set_progress_hook_ignora_o_que_nao_e_chamavel(espelho):
    task_mirror.set_progress_hook("não-função")
    assert task_mirror.progress_hook() is None


# ── a boca fraseia o andamento ──────────────────────────────────────────────

def test_boca_fala_a_etapa_com_a_frase_do_modelo():
    seen = {}

    def caller(messages, max_tokens, **kw):
        seen["messages"] = messages
        seen["kw"] = kw
        return {"text": "Mudança no código do login, criador.", "ttft_ms": 4.0,
                "first_sentence_ms": 5.0, "end_ms": 6.0, "error": None}

    result = mouth_mod.Mouth(_config(), caller=caller).announce_progress(
        "editar core/login.py", stage="edicao")

    assert result.sentence == "Mudança no código do login, criador."
    assert result.fixed is False and result.error == ""
    assert seen["messages"][0]["content"] == mouth_mod.SYS_PROGRESS
    assert "editar core/login.py" in seen["messages"][1]["content"]
    assert seen["kw"]["early_sentence"] is True
    # caminho rápido: o andamento nunca segura a thread do espelho
    assert seen["kw"]["timeout_s"] <= mouth_mod.FAST_ACK_TIMEOUT_S


def test_boca_jamais_declar_terminado_a_partir_de_uma_etapa():
    def caller(messages, max_tokens, **kw):
        return {"text": "Terminei a edição, está tudo pronto.", "ttft_ms": 2.0,
                "first_sentence_ms": 3.0, "end_ms": 4.0, "error": None}

    m = mouth_mod.Mouth(_config(), caller=caller)
    result = m.announce_progress("editar core/login.py", stage="edicao")

    assert result.fixed is True and result.guard_tripped
    assert not any(word in result.sentence.lower()
                   for word in ("pronto", "terminei", "concluído", "feito"))
    assert "mudança no código" in result.sentence.lower()
    assert m.guard_trips == 1


def test_anuncio_de_acao_nao_vira_fala_de_andamento():
    def caller(messages, max_tokens, **kw):
        return {"text": "Abrindo o arquivo agora.", "ttft_ms": 1.0,
                "first_sentence_ms": 1.0, "end_ms": 2.0, "error": None}

    result = mouth_mod.Mouth(_config(), caller=caller).announce_progress(
        "editar core/io.py", stage="edicao")

    assert "abrindo" not in result.sentence.lower()
    assert result.sentence == (
        "O trabalho em segundo plano segue com uma mudança no código.")


def test_caminho_e_codigo_nao_chegam_no_ouvido_do_dono():
    """Modelo morto: o template não recita o que está no log."""
    def caller(messages, max_tokens, **kw):
        return {"text": "", "ttft_ms": 0.0, "first_sentence_ms": 0.0,
                "end_ms": 1.0, "error": "Ollama não respondeu"}

    result = mouth_mod.Mouth(_config(), caller=caller).announce_progress(
        "editar core/login.py", stage="edicao")

    assert "core/login.py" not in result.sentence
    assert result.sentence.endswith("uma mudança no código.")
    assert result.fixed is True


def test_modelo_que_cita_o_arquivo_tambem_e_cortado():
    """O guard léxico não conhece path; o portão do UP-8 conhece."""
    m = mouth_mod.Mouth(_config(), caller=_caller(
        "Mudança em core/login.py, criador."))
    result = m.announce_progress("editar core/login.py", stage="edicao")

    assert "core/login.py" not in result.sentence
    assert result.fixed is True and m.guard_trips == 1
    assert "caminho" in result.guard_tripped
    assert result.sentence == (
        "O trabalho em segundo plano segue com uma mudança no código.")


def test_andamento_sem_etapa_nomeada_usa_o_trecho_se_for_falavel():
    def caller(messages, max_tokens, **kw):
        return {"text": "", "ttft_ms": 0.0, "first_sentence_ms": 0.0,
                "end_ms": 1.0, "error": "sem modelo"}

    result = mouth_mod.Mouth(_config(), caller=caller).announce_progress(
        "O plano está dividido em três partes", stage="")

    assert result.sentence == (
        "O trabalho em segundo plano tem uma etapa nova: "
        "O plano está dividido em três partes.")


def test_andamento_sem_nada_seguro_e_silencio():
    def caller(messages, max_tokens, **kw):
        return {"text": "", "error": "morto", "ttft_ms": 0.0,
                "first_sentence_ms": 0.0, "end_ms": 1.0}

    # linha de ferramenta pura: nem etapa nomeada, nem trecho falável
    result = mouth_mod.Mouth(_config(), caller=caller).announce_progress(
        "$ apt-get install -v ffmpeg", stage="")

    assert result.sentence == (
        "O trabalho em segundo plano tem novidade."), result.sentence


def test_boca_desligada_narra_nada():
    result = mouth_mod.Mouth(_config(mouth_enabled=False),
                             caller=_caller("qualquer coisa")).announce_progress(
        "editar core/io.py", stage="edicao")

    assert result.sentence == "" and result.fixed is True
    assert "sem boca" in result.raw


def test_andamento_vazio_e_silencio_sem_chamar_o_modelo():
    hits = []

    def caller(messages, max_tokens, **kw):
        hits.append(messages)
        return {"text": "x", "error": None}

    result = mouth_mod.Mouth(_config(), caller=caller).announce_progress("   ")

    assert result.sentence == "" and result.raw == "[progresso vazio]"
    assert hits == []


# ── o fio inteiro: espelho -> boca ──────────────────────────────────────────

def test_gancho_da_boca_fala_cada_etapa_nova(espelho):
    e = espelho
    falado: list[str] = []
    notifier = mouth_mod.progress_notifier(
        speak=falado.append,
        mouth=mouth_mod.Mouth(_config(),
                              caller=_caller("Mudança no código, criador.")))
    task_mirror.set_progress_hook(notifier)

    _run(ident="trabalho-8", title="Consertar o login",
         lines=("editar core/login.py", "buscar token em core"))

    assert falado == ["Mudança no código, criador.",
                      "Mudança no código, criador."]
    lines = _activity_lines(e["activity"])
    assert len(lines) == 2
    assert "event=task_progress" in lines[0] and "hook=installado" in lines[0]
    assert "spoken=true" in lines[0]
    assert 'voice="Mudança no código, criador."' in lines[0]
    assert 'stage="busca no projeto"' not in lines[1]      # rótulo canônico
    assert "stage=busca" in lines[1]


def test_gancho_calado_registra_silencio_na_auditoria(espelho):
    e = espelho
    falado: list[str] = []
    notifier = mouth_mod.progress_notifier(
        speak=falado.append,
        mouth=mouth_mod.Mouth(_config(), caller=_caller(
            "", error="modelo frio")))
    task_mirror.set_progress_hook(notifier)

    # frase do template proibida pelo guard? não: ela é segura e sai.
    _run(ident="trabalho-9", lines=("editar core/login.py",))
    assert falado and "segundo plano" in falado[0]

    # agora um andamento que não pode ser falado: boca desligada
    falado.clear()
    task_mirror.set_progress_hook(mouth_mod.progress_notifier(
        speak=falado.append,
        mouth=mouth_mod.Mouth(_config(mouth_enabled=False))))
    _run(ident="trabalho-10", title="Outra tarefa",
         lines=("ler core/io.py",))

    assert falado == []
    line = _activity_lines(e["activity"])[-1]
    assert "spoken=false" in line and "progresso sem boca" in line


def test_gancho_sem_andamento_nao_fala(espelho):
    e = espelho
    falado: list[str] = []
    task_mirror.set_progress_hook(mouth_mod.progress_notifier(
        speak=falado.append, mouth=mouth_mod.Mouth(_config())))

    out = task_mirror.on_progress_update("inexistente", "")

    assert out["notified"] is False and out["reason"] == "sem conteudo"
    assert falado == [] and _activity_lines(e["activity"]) == []


def test_saida_de_voz_quebrada_e_ganho_honesto(espelho):
    e = espelho

    def broken_speak(text):
        raise RuntimeError("áudio fora")

    task_mirror.set_progress_hook(mouth_mod.progress_notifier(
        speak=broken_speak,
        mouth=mouth_mod.Mouth(_config(), caller=_caller("Etapa nova, criador."))))
    _run(ident="trabalho-11", lines=("editar core/io.py",))

    line = _activity_lines(e["activity"])[-1]
    assert "spoken=false" in line and "RuntimeError" in line


def test_progress_notifier_exige_andamento_no_payload():
    notifier = mouth_mod.progress_notifier(
        speak=lambda text: pytest.fail("não deveria falar"),
        mouth=mouth_mod.Mouth(_config()))

    assert notifier({})["spoken"] is False
    assert notifier({"text": "  "})["reason"] == "sem andamento"
