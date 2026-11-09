"""Cascata de modelos gratuitos: melhor → pior, checando grátis ANTES de usar.

Ordem do dono (PLANO-2026-11-07, respondida): "só modelo gratuito, do melhor
para o pior, conferindo grátis/limite antes de usar". Estourar o limite de uso
é momentâneo: o nome sai da frente e volta depois. Se nada é aproveitável, a
cascata devolve None — ela nunca escolhe um modelo pago no lugar.

Nada aqui lê o ``memory/models.json`` real nem o cadastro do dono: o catálogo
é escrito no ``tmp_path`` de cada teste.
"""
from __future__ import annotations

import json
import time

import pytest

from core import personas

BEST = "Qwen3.8-Max"
SECOND = "Kimi-K3.1-Pro"
THIRD = "GLM-5.3-Flash"
FOURTH = "Efficient"
PAID = "Claude-Paid"


def _entry(name: str, factor: float = 0.0, **over) -> dict:
    raw = {
        "value": name.lower(),
        "displayName": name,
        "isFree": factor <= 0.0,
        "priceFactor": factor,
        "originalPriceFactor": factor,
        "availableContextWindows": [200000],
        "maxInputTokens": 200000,
        "efforts": ["low", "medium"],
        "defaultEffort": "medium",
    }
    raw.update(over)
    return raw


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    """Um catálogo vivo de mentira, na ordem em que o dono escreveu a fila."""
    models = tmp_path / "models.json"
    monkeypatch.setattr(personas, "MODELS_PATH", models)

    def write(*entries: dict):
        models.write_text(json.dumps({"models": list(entries)}),
                          encoding="utf-8")

    yield write
    personas.clear_model_exhaustion()


@pytest.fixture(autouse=True)
def _sem_esgotamento_de_antes():
    personas.clear_model_exhaustion()
    yield
    personas.clear_model_exhaustion()


def aisha(model: str = BEST) -> dict:
    return {"id": "aisha", "name": "Aisha", "gender": "f", "model": model}


def test_esgotado_no_meio_da_cascata_desce_para_o_proximo_gratuito(catalog):
    catalog(_entry(BEST), _entry(SECOND), _entry(THIRD))
    assert personas.model_for(aisha())["model"]["displayName"] == BEST

    personas.mark_model_exhausted(BEST, "limite de uso atingido")
    escolha = personas.model_for(aisha())
    assert escolha["model"]["displayName"] == SECOND
    assert escolha["switched"] is True
    assert escolha["previous"] == BEST
    assert "limite de uso" in escolha["reason"]

    # Dois estourados seguidos: a fila continua descendo, nunca para no pago.
    personas.mark_model_exhausted(SECOND, "limite de uso atingido")
    assert personas.model_for(aisha())["model"]["displayName"] == THIRD


def test_cascata_inteira_esgotada_devolve_nada_e_nao_inventa_pago(catalog):
    catalog(_entry(BEST), _entry(SECOND), _entry(THIRD), _entry(PAID, 0.5))
    for name in (BEST, SECOND, THIRD):
        personas.mark_model_exhausted(name, "cota diária")

    escolha = personas.model_for(aisha())
    assert escolha["model"] is None, "nada livre: a cascata não escolhe"
    assert escolha["previous"] == BEST
    assert "nenhum modelo gratuito" in escolha["reason"]
    assert personas.next_free_model() is None
    assert personas.cascade_available() == []
    # O pago continua existindo no relatório — e continua fora da fila.
    estados = {row["model"]: row["state"] for row in personas.cascade_report()}
    assert estados[PAID] == "pago"
    assert all(estados[n].startswith("esgotado") for n in (BEST, SECOND, THIRD))


def test_gratis_e_chechado_antes_de_usar_preco_desconhecido_nao_passa(catalog):
    # Promoção: a ficha diz "isFree", o multiplicador diz que cobra. Grátis é o
    # preço zero conferido agora, não a etiqueta do catálogo.
    # Preço ausente do catálogo também não é grátis: é desconhecido.
    catalog(_entry(BEST, 0.5, isFree=True), _entry(SECOND),
            _entry(THIRD, priceFactor=None, isFree=True))
    assert personas.model_is_free(personas.find_model(BEST)) is False
    assert personas.model_is_free(personas.find_model(THIRD)) is False

    escolha = personas.model_for(aisha(model=BEST))
    assert escolha["model"]["displayName"] == SECOND
    assert escolha["switched"] is True
    assert "não está gratuito" in escolha["reason"]


def test_esgotamento_e_momentaneo_o_nome_volta_a_concorrer(catalog):
    catalog(_entry(BEST), _entry(SECOND))
    personas.mark_model_exhausted(BEST, "limite", ttl_s=0.05)
    assert personas.model_for(aisha())["model"]["displayName"] == SECOND
    time.sleep(0.06)
    assert personas.model_exhausted(BEST) is False
    assert personas.model_for(aisha())["model"]["displayName"] == BEST

    personas.mark_model_exhausted(FOURTH, "limite")
    personas.clear_model_exhaustion(FOURTH)
    assert personas.model_exhausted(FOURTH) is False


def test_fora_da_cascata_o_cadastro_e_respeitado_mas_nunca_pago(catalog):
    catalog(_entry(BEST), _entry(SECOND, 0.5))
    fora = {"id": "fulano", "name": "Fulano", "gender": "m", "model": SECOND}
    escolha = personas.model_for(fora)
    assert escolha["switched"] is False
    assert "fora da cascata" in escolha["reason"]
    # Fora da cascata o cadastro manda: a cascata não troca o nome de quem não
    # pediu, nem quando ele está esgotado — quem decide a troca é a persona.
    assert escolha["model"]["displayName"] == SECOND
    personas.mark_model_exhausted(SECOND, "limite")
    volta = personas.model_for(fora)
    assert personas._entry_name(volta["model"]) == SECOND
    assert volta["switched"] is False
