"""Regras como dado: tabela do repo, prioridade, override do dono e reload."""
from __future__ import annotations

import json
import os

import pytest

from core.flow_ops.rules import (DEFAULT_RULES_PATH, Rule, RulesError,
                                 RulesTable, load_rules, norm)


def test_repository_table_loads_and_routes_real_phrases():
    table = load_rules()
    assert len(table) > 0

    musica = table.match("toca Queen")
    assert musica is not None
    assert musica.rule.action == "media_control"

    hora = table.match("que horas sao")
    assert hora is not None and hora.rule.action == "lookup_time"

    app = table.match("abre o WhatsApp")
    assert app is not None and app.rule.action == "open_app"


def test_owner_override_wins_over_every_rule():
    table = load_rules()
    hit = table.match("quero reiniciar o computador agora")
    assert hit is not None
    assert hit.origin == "override"
    assert hit.rule.id == "dono-confirma-antes-de-mexer-na-maquina"
    assert hit.rule.requires_confirm is True
    assert hit.rule.action == "confirm"

    # mesmo com uma regra de prioridade alta casando, o override vem antes
    table.add_override({
        "id": "dono-veta-tudo-de-musica",
        "flow": "reasoning", "action": "deny", "deadline_ms": 1000,
        "match": {"any": ["toca queen"]},
    })
    veto = table.match("toca Queen")
    assert veto is not None
    assert veto.origin == "override"
    assert veto.rule.action == "deny"


def test_override_can_target_a_single_turn_id():
    table = load_rules()
    table.add_override({"id": "so-este-turno", "action": "deny",
                        "match": {"turn_id": "t-42"}})
    assert table.match("bom dia", turn_id="t-42").rule.action == "deny"
    other = table.match("bom dia", turn_id="t-43")
    assert other is None or other.rule.action != "deny"


def test_priority_orders_rules_of_the_same_origin():
    table = RulesTable.from_dict({
        "version": 1,
        "rules": [
            {"id": "baixa", "action": "a", "priority": 1,
             "match": {"any": ["pedido"]}},
            {"id": "alta", "action": "b", "priority": 99,
             "match": {"any": ["pedido"]}},
        ],
    })
    assert table.match("pedido").rule.id == "alta"


def test_owner_override_is_data_only_and_reloads_without_code(tmp_path):
    file = tmp_path / "rules.json"
    file.write_text(json.dumps({"version": 1, "rules": [
        {"id": "conversa", "action": "reason", "deadline_ms": 5000,
         "match": {"regex": ".*"}}]}), encoding="utf-8")
    table = RulesTable.from_file(file)
    assert table.match("desliga tudo").rule.action == "reason"

    # o dono edita o DADO: novo override entra na frente, sem tocar em código
    data = json.loads(file.read_text(encoding="utf-8"))
    data["overrides"] = [{
        "id": "dono-veta-desligar", "action": "deny", "deadline_ms": 1000,
        "match": {"any": ["desliga tudo"]}}]
    file.write_text(json.dumps(data), encoding="utf-8")
    os.utime(file, (os.path.getmtime(file) + 5, os.path.getmtime(file) + 5))

    assert table.reload_if_changed() is True
    hit = table.match("desliga tudo")
    assert hit is not None and hit.origin == "override"
    assert hit.rule.action == "deny"
    assert table.reload_if_changed() is False


def test_invalid_regex_is_refused_with_rule_id():
    with pytest.raises(RulesError) as info:
        RulesTable.from_dict({"version": 1, "rules": [
            {"id": "quebrada", "match": {"regex": "([a-z"}}]})
    assert "quebrada" in str(info.value)
    assert info.value.rule_id == "quebrada"


def test_typo_in_match_is_refused_instead_of_silently_ignored():
    with pytest.raises(RulesError) as info:
        RulesTable.from_dict({"version": 1, "rules": [
            {"id": "typo", "match": {"anys": ["x"]}}]})
    assert "anys" in str(info.value)


def test_duplicate_ids_are_refused():
    with pytest.raises(RulesError):
        RulesTable.from_dict({"version": 1, "rules": [
            {"id": "x", "match": {"any": ["a"]}},
            {"id": "x", "match": {"any": ["b"]}}]})


def test_disabled_rule_never_matches():
    table = RulesTable.from_dict({"version": 1, "rules": [
        {"id": "off", "enabled": False, "match": {"regex": ".*"}}]})
    assert table.match("qualquer coisa") is None


def test_rule_without_condition_never_matches():
    rule = Rule(id="vazia")
    assert rule.matches("qualquer coisa") is False


def test_accent_and_case_do_not_change_match():
    table = RulesTable.from_dict({"version": 1, "rules": [
        {"id": "hora", "match": {"any": ["que horas"]}}]})
    assert table.match("QUE HÓRAS são") is not None
    assert norm("HÓRAS") == "horas"


def test_missing_table_is_empty_only_when_allowed(tmp_path):
    with pytest.raises(RulesError):
        RulesTable.from_file(tmp_path / "nao-existe.json")
    empty = RulesTable.from_file(tmp_path / "nao-existe.json", missing_ok=True)
    assert len(empty) == 0
    assert empty.match("nada") is None


def test_repository_default_path_points_to_config():
    assert DEFAULT_RULES_PATH.name == "flows_rules.json"
    assert DEFAULT_RULES_PATH.is_file()
