"""Chão dos módulos novos: tetos de concorrência e delegação.

Dois fatos que o commit ``13beb10`` deixou sem prova, e que a tarefa do dono
de 09/11/2026 exige:

(a) ``core.persona_ops.limits.read_limits()`` cai nos padrões (4 e 4) quando o
    arquivo de config não existe ou está corrompido — sem exceção. O config é
    ``config/api_keys.json`` lido pelo ``memory.config_manager``; é o mesmo
    arquivo que a UI do ``plugins/persona_limits.py`` grava, então a leitura
    não pode derrubar o assistente vivo.
(b) ``core.persona_ops.delegation.plan()`` decide pelo dono do assunto em
    código: nenhum modelo é chamado, nenhuma rede é tocada.

Nenhum teste lê o config real do dono nem o ``memory/personas.json`` real:
tudo em ``tmp_path``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import gemini  # noqa: E402
from core import personas as persona_store  # noqa: E402
from core.persona_ops import delegation, limits  # noqa: E402
from memory import config_manager  # noqa: E402

DEFAULTS = {"max_concurrent_personas": 4, "max_tasks_per_persona": 4}

QUADRO = [
    {"id": "aline", "name": "Aline", "gender": "f",
     "role": "analista de frontend", "stacks": ["react", "ts"],
     "skills": ["frontend-design"]},
    {"id": "arnaldo", "name": "Arnaldo", "gender": "m",
     "role": "eletricista de rede", "stacks": ["php"]},
]


@pytest.fixture
def config_em_teste(tmp_path, monkeypatch):
    """O config da casa apontado para um arquivo temporário inexistente."""
    path = tmp_path / "config" / "api_keys.json"
    monkeypatch.setattr(config_manager, "CONFIG_FILE", path)
    return path


@pytest.fixture
def quadro_isolado(tmp_path, monkeypatch):
    """Quadro e config em tmp_path, com as contas vivas zeradas.

    ``SLOTS`` é singleton do processo: sem reset o teste contamina vizinho.
    O config apontado para arquivo inexistente deixa ``read_limits()`` com
    os padrões de sempre — nada aqui lê o ``config/api_keys.json`` do dono.
    """
    monkeypatch.setattr(persona_store, "PERSONAS_PATH",
                        tmp_path / "personas.json")
    monkeypatch.setattr(config_manager, "CONFIG_FILE",
                        tmp_path / "config" / "api_keys.json")
    limits.SLOTS.reset()
    yield
    limits.SLOTS.reset()


@pytest.fixture
def modelo_proibido(monkeypatch):
    """Armadilha: qualquer porta de modelo registrada é falha do teste."""
    chamadas: list = []

    def armadilha(*args, **kwargs):
        chamadas.append((args, kwargs))
        raise AssertionError("a delegação chamou um modelo")

    for nome in ("call", "chat", "ask", "generate"):
        if hasattr(gemini, nome):
            monkeypatch.setattr(gemini, nome, armadilha)
    return chamadas


# ── (a) read_limits: config ausente ou corrompido vira padrão, nunca exceção ──
def test_sem_arquivo_de_config_devolve_os_padraos(config_em_teste):
    assert not config_em_teste.exists()
    assert limits.read_limits() == DEFAULTS
    assert limits.max_concurrent_personas() == 4
    assert limits.max_tasks_per_persona() == 4


@pytest.mark.parametrize("corpo", [
    "{ isto nao e json",                          # JSON truncado
    "",                                            # arquivo vazio
    "[1, 2, 3]",                                   # JSON que não é objeto
    json.dumps({"plugin_config": "nao-e-dict"}),   # namespace errado
    json.dumps({"plugin_config": {"personas": "nao-e-dict"}}),
    json.dumps({"plugin_config": {"personas": {    # valores sem sentido
        "max_concurrent_personas": "quatro",
        "max_tasks_per_persona": None}}}),
    json.dumps({"plugin_config": {"personas": {    # dicionário onde é número
        "max_concurrent_personas": {"a": 1},
        "max_tasks_per_persona": []}}}),
], ids=["truncado", "vazio", "lista", "plugin_config", "personas",
        "texto", "tipos"])
def test_config_corrompido_devolve_os_padraos(config_em_teste, corpo):
    config_em_teste.parent.mkdir(parents=True, exist_ok=True)
    config_em_teste.write_text(corpo, encoding="utf-8")
    assert limits.read_limits() == DEFAULTS


def test_config_valido_e_lido_de_verdade(config_em_teste):
    """Guarda contra o falso positivo: se a leitura estivesse morta, os
    testes de cima passariam devolvendo padrão para tudo."""
    config_em_teste.parent.mkdir(parents=True, exist_ok=True)
    config_em_teste.write_text(json.dumps({"plugin_config": {"personas": {
        "max_concurrent_personas": 7, "max_tasks_per_persona": 2}}}),
        encoding="utf-8")
    assert limits.read_limits() == {"max_concurrent_personas": 7,
                                    "max_tasks_per_persona": 2}


def test_leitor_quedado_devolve_os_padraos(monkeypatch):
    """Nem exceção do ``config_manager`` derruba a leitura dos tetos."""
    def _explode(namespace):
        raise RuntimeError("sem config_manager")
    monkeypatch.setattr(config_manager, "get_plugin_config", _explode)
    assert limits.read_limits() == DEFAULTS


def test_numero_fora_da_faixa_vira_teto_seguro():
    caps = limits.read_limits({"max_concurrent_personas": -9,
                               "max_tasks_per_persona": 10 ** 6})
    assert caps == {"max_concurrent_personas": limits.MIN_LIMIT,
                    "max_tasks_per_persona": limits.MAX_LIMIT}


# ── (b) delegação: decide pelo dono do assunto, sem modelo ───────────────────
def test_delega_quem_e_dono_do_assunto_sem_chamar_modelo(
        quadro_isolado, modelo_proibido):
    plano = delegation.plan("refaça o telão com react", QUADRO)
    assert plano.delegated is True
    assert plano.slug == "aline"
    assert plano.reason == "assunto_da_persona"
    assert plano.role == "analista de frontend"
    assert plano.tool_call == {"tool": "persona_ask", "persona": "aline",
                               "task": "refaça o telão com react"}
    assert modelo_proibido == []


def test_assunto_do_orquestrador_nunca_e_delegado(quadro_isolado,
                                                  modelo_proibido):
    for tarefa in ("aumente os limites de personas para 6",
                   "cadastre uma persona nova",
                   "ajuste a cascata de modelos gratuitos"):
        plano = delegation.plan(tarefa, QUADRO)
        assert plano.delegated is False, tarefa
        assert plano.reason == "assunto_do_orquestrador", tarefa
    assert modelo_proibido == []


def test_sem_dono_para_o_assunto_gino_executa(quadro_isolado,
                                               modelo_proibido):
    plano = delegation.plan("troque a lâmpada do corredor", [])
    assert plano.delegated is False
    assert plano.reason == "sem_personas_cadastradas"

    plano = delegation.plan("refaça o telão com react", QUADRO[:1])
    assert plano.delegated is True

    plano = delegation.plan("compilar o kernel em assembly", QUADRO)
    assert plano.delegated is False
    assert plano.reason == "sem_persona_para_o_assunto"
    assert modelo_proibido == []


def test_empate_vira_pergunta_e_nao_sorteio(quadro_isolado, modelo_proibido):
    espelhadas = [
        {"id": "uma", "name": "Uma", "gender": "f",
         "role": "analista de frontend", "stacks": ["react"]},
        {"id": "outra", "name": "Outra", "gender": "f",
         "role": "analista de frontend", "stacks": ["react"]},
    ]
    plano = delegation.plan("muda o frontend em react", espelhadas)
    assert plano.delegated is False
    assert plano.reason == "assunto_ambiguo"
    assert set(plano.candidates) == {"uma", "outra"}
    assert plano.lead
    assert modelo_proibido == []


def test_teto_de_concorrencia_avisa_sem_impedir_a_decisao(quadro_isolado):
    """O plano continua delegando; o que muda é a nota ``blocked``."""
    caps = limits.read_limits()
    for index in range(caps["max_concurrent_personas"]):
        limits.SLOTS.request(f"persona{index}")
    assert limits.SLOTS.can_request("aline")[0] is False

    plano = delegation.plan("refaça o telão com react", QUADRO)
    assert plano.delegated is True
    assert "teto" in plano.blocked
