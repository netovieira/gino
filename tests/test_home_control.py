"""home_control tests — name resolution, status and service call payloads.

The Home Assistant network is never touched: states come from the fixture below
and the REST layer is mocked.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from actions import home_control as hc

STATES = [
    {"entity_id": "light.luz_da_sala", "state": "on",
     "attributes": {"friendly_name": "Luz da Sala"}},
    {"entity_id": "light.luz_do_quarto", "state": "off",
     "attributes": {"friendly_name": "Luz do Quarto"}},
    {"entity_id": "light.luz_da_cozinha", "state": "unavailable",
     "attributes": {"friendly_name": "Luz da Cozinha"}},
    {"entity_id": "switch.tomada_do_escritorio", "state": "off",
     "attributes": {"friendly_name": "Tomada do Escritório"}},
    {"entity_id": "climate.ar_condicionado_sala", "state": "cool",
     "attributes": {"friendly_name": "Ar Condicionado da Sala"}},
    {"entity_id": "media_player.tv_sala", "state": "off",
     "attributes": {"friendly_name": "TV da Sala"}},
    {"entity_id": "scene.filme", "state": "unknown",
     "attributes": {"friendly_name": "Cena Filme"}},
    {"entity_id": "sensor.temperatura_sala", "state": "23",
     "attributes": {"friendly_name": "Temperatura da Sala"}},
]


class ToolDeclarationTests(unittest.TestCase):
    def test_tool_shape(self):
        tool = hc.TOOL
        self.assertEqual(tool["name"], "home_control")
        self.assertIs(tool["handler"], hc.home_control)
        self.assertEqual(
            tool["parameters"]["properties"]["action"]["enum"],
            ["status", "list", "turn_on", "turn_off", "set", "scene"],
        )

    def test_only_simple_actions_are_auto_authorized(self):
        for action in ("status", "list", "turn_on", "turn_off", "set", "scene",
                       "ligar", "desligar", "ajustar", "cena", ""):
            with self.subTest(action=action):
                self.assertTrue(hc.auto_authorized({"action": action}))
        for action in ("delete_automation", "config"):
            with self.subTest(action=action):
                self.assertFalse(hc.auto_authorized({"action": action}))

    def test_bridge_hook_only_grants_home_control(self):
        self.assertTrue(hc.bridge_preauthorized(
            "home_control", {"action": "turn_on"}))
        self.assertFalse(hc.bridge_preauthorized(
            "send_message", {"action": "turn_on"}))


class ResolveTests(unittest.TestCase):
    def _resolve(self, query, domain=None):
        return hc.resolve_entity(query, STATES, domain=domain)

    def test_entity_id_is_exact(self):
        entity, error = self._resolve("light.luz_da_sala")
        self.assertEqual(error, "")
        self.assertEqual(entity["entity_id"], "light.luz_da_sala")

    def test_friendly_name_ignores_case_and_accents(self):
        entity, error = self._resolve("tomada do escritorio")
        self.assertEqual(error, "")
        self.assertEqual(entity["entity_id"], "switch.tomada_do_escritorio")

    def test_domain_word_narrows_an_ambiguous_name(self):
        entity, error = self._resolve("luz da sala")
        self.assertEqual(error, "")
        self.assertEqual(entity["entity_id"], "light.luz_da_sala")

    def test_bare_domain_word_asks_which_one(self):
        entity, error = self._resolve("luz")
        self.assertIsNone(entity)
        self.assertIn("Qual deles?", error)

    def test_single_domain_match_without_domain_word(self):
        entity, error = self._resolve("filme")
        self.assertEqual(error, "")
        self.assertEqual(entity["entity_id"], "scene.filme")

    def test_unknown_name_has_a_clear_error(self):
        entity, error = self._resolve("televisão da varanda")
        self.assertIsNone(entity)
        self.assertIn("Não encontrei", error)

    def test_empty_query_is_refused(self):
        entity, error = self._resolve("   ")
        self.assertIsNone(entity)
        self.assertIn("Diga qual dispositivo", error)


class HandlerTests(unittest.TestCase):
    def test_status_counts_on_offline_and_ignores_scenes(self):
        with patch.object(hc, "_states", return_value=STATES):
            out = hc.home_control({"action": "status"})
        self.assertIn("6 dispositivos: 2 ligados, 3 desligados, 1 offline", out)
        self.assertIn("Luz da Cozinha", out)

    def test_turn_on_sends_light_turn_on_for_a_human_name(self):
        calls = []

        def fake_api(method, path, payload=None):
            calls.append((method, path, payload))
            return {}

        with patch.object(hc, "_states", return_value=STATES), \
                patch.object(hc, "_api", side_effect=fake_api):
            out = hc.home_control({"action": "ligar",
                                   "entity_id": "luz do quarto"})
        self.assertIn("Luz do Quarto", out)
        self.assertEqual(calls, [("POST", "/api/services/light/turn_on",
                                  {"entity_id": "light.luz_do_quarto"})])

    def test_turn_off_refuses_a_non_toggle_domain(self):
        calls = []
        with patch.object(hc, "_states", return_value=STATES), \
                patch.object(hc, "_api", side_effect=lambda *a, **k: calls.append(a)):
            out = hc.home_control({"action": "turn_off",
                                   "entity_id": "sensor.temperatura_sala"})
        self.assertIn("Não sei ligar/desligar", out)
        self.assertEqual(calls, [])

    def test_set_light_value_becomes_brightness_pct(self):
        calls = []

        def fake_api(method, path, payload=None):
            calls.append((method, path, payload))
            return {}

        with patch.object(hc, "_states", return_value=STATES), \
                patch.object(hc, "_api", side_effect=fake_api):
            out = hc.home_control({"action": "set",
                                   "entity_id": "luz da sala",
                                   "value": "40"})
        self.assertIn("ajustado", out)
        self.assertEqual(calls, [("POST", "/api/services/light/turn_on",
                                  {"entity_id": "light.luz_da_sala",
                                   "brightness_pct": 40.0})])

    def test_scene_activation_uses_scene_turn_on(self):
        calls = []

        def fake_api(method, path, payload=None):
            calls.append((method, path, payload))
            return {}

        with patch.object(hc, "_states", return_value=STATES), \
                patch.object(hc, "_api", side_effect=fake_api):
            out = hc.home_control({"action": "scene", "name": "cena filme"})
        self.assertIn("Cena Filme ativada", out)
        self.assertEqual(calls, [("POST", "/api/services/scene/turn_on",
                                  {"entity_id": "scene.filme"})])

    def test_unknown_action_answers_without_network(self):
        with patch.object(hc, "_states") as states:
            out = hc.home_control({"action": "apagar_automacao"})
        self.assertIn("Ação desconhecida", out)
        states.assert_not_called()


if __name__ == "__main__":
    unittest.main()
