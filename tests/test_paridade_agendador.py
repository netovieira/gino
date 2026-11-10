"""Paridade C1/C7 do AGENDADOR: voz e texto são o mesmo porto, não dois.

A lei dele (plano-lei 5.1, C1/C7): o que um gate pode, o outro pode, com a
mesma autorização e o mesmo veredito. Estes testes não comparam textos
parecidos — comparam **o registro produzido** e **a recusa produzida** pelos
dois portos, campo a campo.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import schedule as ferramenta  # noqa: E402
from core import obsidian, scheduler  # noqa: E402

GATILHO = {"tipo": "funcao", "modulo": "core.scheduler", "funcao": "agora",
           "destino": ["voz"]}


@pytest.fixture
def cofre(tmp_path, monkeypatch):
    root = tmp_path / "Jarvis"
    monkeypatch.setattr(obsidian, "VAULT", root)
    monkeypatch.setattr(obsidian, "ACTIVITY_LOG", tmp_path / "activity.log")
    monkeypatch.setattr(scheduler, "REGISTRO_SCRIPTS",
                        tmp_path / "config" / "schedule_scripts.json")
    for nome, valor in (("_cache", {"assinatura": None, "linhas": {}}),
                        ("_scripts_cache", {"assinatura": None, "lista": []}),
                        ("_PORTAS", {}), ("_pendente_aviso", set()),
                        ("_pedidos_aprovacao", []), ("_ja_acordei", False)):
        monkeypatch.setattr(scheduler, nome, valor)
    return root


def amanha(hora: int = 9) -> str:
    return (datetime.now() + timedelta(days=1)).replace(
        hour=hora, minute=0, second=0, microsecond=0).isoformat()


def pelo_texto(p: dict) -> dict:
    """Porto de texto: o app/WhatsApp dele chama a action sem boca nenhuma."""
    return json.loads(ferramenta.schedule(p))


def pela_voz(p: dict, falado: list[str]) -> dict:
    """Porto de voz: a mesma action, com o sintetizador do turno preso."""
    return json.loads(ferramenta.schedule(p, speak=falado.append))


CAMPOS_COMUNS = ("titulo", "quando", "gatilho", "origem", "persona",
                 "disparou_em", "cancelado_em", "texto")


def test_mesmo_registro_pelos_dois_portos(cofre):
    falado: list[str] = []
    voz = pela_voz({"acao": "add", "quando": amanha(9),
                    "titulo": "consulta", "gatilho": GATILHO}, falado)
    texto = pelo_texto({"acao": "add", "quando": amanha(9),
                        "titulo": "consulta", "gatilho": GATILHO})
    assert voz["status"] == texto["status"] == "ok"
    uma = scheduler.listar()[0]
    outra = scheduler.listar()[1]
    assert {campo: uma[campo] for campo in CAMPOS_COMUNS} == \
           {campo: outra[campo] for campo in CAMPOS_COMUNS}
    # o que muda é só a identidade: mesma origem, mesmo gatilho, mesma nota
    assert uma["id"] != outra["id"]
    assert uma["gatilho"] == outra["gatilho"]
    assert Path(uma["nota"]).parent == Path(outra["nota"]).parent


@pytest.mark.parametrize("payload, motivo", [
    ({"acao": "add", "quando": "a qualquer hora", "titulo": "x",
      "gatilho": GATILHO}, "hora"),
    ({"acao": "add", "quando": amanha(9), "titulo": "x", "gatilho": {}},
     "gatilho"),
    ({"acao": "add", "quando": amanha(9), "titulo": "x",
      "gatilho": dict(GATILHO, destino=["telegram"])}, "destino"),
    ({"acao": "add", "quando": amanha(9), "titulo": "x",
      "gatilho": dict(GATILHO, modulo="os", funcao="system")}, "modulo"),
    ({"acao": "add", "quando": amanha(9), "titulo": "x",
      "gatilho": GATILHO, "origem": "filtro_email"}, "origem"),
    ({"acao": "add", "quando": amanha(9), "titulo": "x",
      "gatilho": GATILHO, "origem": "fala_visitante"}, "visitante"),
])
def test_a_mesma_recusa_pelos_dois_portos(cofre, payload, motivo):
    """C4 nos dois portos: mesma recusa, mesma palavra, sem fallback."""
    falado: list[str] = []
    voz = pela_voz(payload, falado)
    texto = pelo_texto(payload)
    assert voz["status"] == "recusado", (motivo, voz)
    assert texto["status"] == "recusado", (motivo, texto)
    assert voz["mensagem"] == texto["mensagem"], motivo
    assert scheduler.listar() == []


def test_cancelar_pede_confirmacao_igual_neles_dois(cofre):
    falado: list[str] = []
    pelo_texto({"acao": "add", "quando": amanha(9), "titulo": "pagar a luz",
                "gatilho": GATILHO})
    voz = pela_voz({"acao": "cancel", "titulo": "pagar a luz"}, falado)
    texto = pelo_texto({"acao": "cancel", "titulo": "pagar a luz"})
    assert voz["status"] == texto["status"] == "pergunta"
    assert voz["mensagem"] == texto["mensagem"]
    assert len(scheduler.listar()) == 1, "algum porto cancelou sem confirmação"
    dito = pela_voz({"acao": "cancel", "titulo": "pagar a luz",
                     "confirmacao": True}, falado)
    assert dito["status"] == "ok"
    assert scheduler.listar() == []


def test_persona_e_recusada_igual_por_voz_e_por_texto(cofre):
    falado: list[str] = []
    corpo = {"acao": "add", "quando": amanha(9), "titulo": "aviso da persona",
             "gatilho": GATILHO, "origem": "automatica", "persona": "selma"}
    voz = pela_voz(corpo, falado)
    texto = pelo_texto(corpo)
    assert voz["status"] == texto["status"] == "recusado"
    assert "aprovação" in voz["mensagem"]
    assert voz["mensagem"] == texto["mensagem"]
    assert scheduler.listar() == [], "o porto de voz gravou o que o de texto negou"
    assert len(scheduler.pedidos_aprovacao()) == 2


def test_listar_devolve_as_mesmas_linhas_pelos_dois_portos(cofre):
    falado: list[str] = []
    pelo_texto({"acao": "add", "quando": amanha(9), "titulo": "compra",
                "gatilho": GATILHO})
    agenda_texto = pelo_texto({"acao": "list"})["agenda"]
    agenda_voz = pela_voz({"acao": "list"}, falado)["agenda"]
    assert agenda_texto == agenda_voz


def test_a_descricao_da_ferramenta_declara_os_mesmos_enums_do_modulo(cofre):
    """O schema é a trava: se o enum do schema divergir do módulo, é porta nova."""
    props = ferramenta.TOOL["parameters"]["properties"]
    assert props["acao"]["enum"] == ["add", "list", "cancel", "snooze",
                                     "resumo", "gatilhos", "executar",
                                     "cadastrar"]
    gatilho = props["gatilho"]["properties"]
    assert gatilho["tipo"]["enum"] == list(scheduler.GATILHO_TIPOS)
    assert gatilho["destino"]["items"]["enum"] == list(
        scheduler.GATILHO_DESTINOS)
    assert gatilho["argumentos"]["items"]["properties"]["tipo"][
        "enum"] == list(scheduler.ARGUMENTO_TIPOS)
    assert props["origem"]["enum"] == ["fala_do_dono", "automatica",
                                       "fala_visitante", "filtro_email"]


def test_um_unico_executar_gatilho_para_os_dois_portos(cofre, monkeypatch):
    """Não existe 'falar pela voz' e 'falar pelo texto': existe um caminho."""
    vistos: list[str] = []
    real = scheduler.executar_gatilho

    def espiao(gatilho, **kwargs):
        vistos.append("executar_gatilho")
        return real(gatilho, **kwargs)

    monkeypatch.setattr(scheduler, "executar_gatilho", espiao)
    falado: list[str] = []
    adicionado = pelo_texto({"acao": "add", "quando": amanha(9),
                             "titulo": "rota", "gatilho": GATILHO})
    alvo = {"acao": "executar", "id": adicionado["id"]}
    pela_voz(alvo, falado)
    pelo_texto(alvo)
    assert vistos == ["executar_gatilho", "executar_gatilho"], vistos
