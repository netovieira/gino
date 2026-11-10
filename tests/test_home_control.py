"""home_control tests — name resolution, status and service call payloads.

The Home Assistant network is never touched: states come from the fixture below
and the REST layer is mocked.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actions import home_control as hc
from core import actiongate, voice_identity

#: A forma prometida ao modelo. Não é fotografia: é a porta da frente. Remover
#: ou renomear uma ação, ou mexer no tipo/nome de um parâmetro, quebra aqui; e
#: quem quiser uma ação nova tem de nomeá-la nesta lista (e despachá-la no
#: handler, que o teste abaixo também cobra) — nunca receber o contrato por
#: subconjunto, "startswith" ou qualquer asserção frouxa.
ACTIONS = ["status", "list", "turn_on", "turn_off", "set", "scene",
           "apelido", "apelidos", "esquecer_apelido"]
PARAM_TYPES = {
    "action": "STRING",
    "entity_id": "STRING",
    "target": "STRING",
    "name": "STRING",
    "alias": "STRING",
    "entidade": "STRING",
    "trocar": "BOOLEAN",
    "confirmar": "BOOLEAN",
    "domain": "STRING",
    "params": "OBJECT",
    "value": "STRING",
}

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

        properties = tool["parameters"]["properties"]
        # Os parâmetros pela raiz: nome E tipo de cada um, exatamente. Adicionar,
        # renomear ou retipar um parâmetro tem de passar por aqui.
        self.assertEqual({key: spec["type"] for key, spec in properties.items()},
                         PARAM_TYPES)

        action = properties["action"]
        self.assertEqual(action["type"], "STRING")
        self.assertEqual(action["enum"], ACTIONS)

        # O enum é uma promessa: cada ação anunciada tem de ser despachada de
        # fato pelo handler (ação no enum sem código = modelo enganado).
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        mapa = Path(sandbox.name) / "home_aliases.json"
        with patch.object(hc, "_states", return_value=STATES), \
                patch.object(hc, "_alias_path", mapa), \
                patch.object(hc, "_api",
                             side_effect=AssertionError("tocou a rede")):
            for declared in ACTIONS:
                with self.subTest(action=declared):
                    out = hc.home_control({"action": declared})
                    self.assertNotIn("Ação desconhecida", out)
            # E o contrário: o que não está no enum continua recusado.
            for outside in ("delete_automation", "config", "apelido_remoto"):
                with self.subTest(action=outside):
                    self.assertIn("Ação desconhecida",
                                  hc.home_control({"action": outside}))

        # O contrato escrito e o código andam juntos: a descrição do parâmetro
        # cita exatamente as ações do enum.
        advertised = sorted(
            word for word in action["description"].replace(",", " ")
            .replace(".", " ").split() if word in ACTIONS)
        self.assertEqual(advertised, sorted(ACTIONS))

    def test_the_alias_map_is_the_owner_file_by_default(self):
        """Sem injeção e sem ambiente, o mapa é config/home_aliases.json do repo."""
        self.assertEqual(hc.DEFAULT_ALIAS_PATH,
                         hc.BASE_DIR / "config" / "home_aliases.json")
        with patch.object(hc, "_alias_path", None), \
                patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hc.alias_path(), hc.DEFAULT_ALIAS_PATH)
        # E o caminho injetável é o que vale: teste nunca escreve no arquivo dele.
        with patch.object(hc, "_alias_path", Path("/tmp/so-teste.json")):
            self.assertEqual(hc.alias_path(), Path("/tmp/so-teste.json"))

    def test_only_simple_actions_are_auto_authorized(self):
        for action in (ACTIONS + ["ligar", "desligar", "ajustar", "cena",
                                  "ensinar", "esquecer", ""]):
            with self.subTest(action=action):
                self.assertTrue(hc.auto_authorized({"action": action}))
        for action in ("delete_automation", "config", "apelido_remoto"):
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


class AliasActionTests(unittest.TestCase):
    """As três ações novas, cada uma pelo seu próprio comportamento.

    ``apelido`` grava, ``apelidos`` lê o que está no disco, ``esquecer_apelido``
    reescreve. Zero rede: ``_api`` é um alarme e ``_states`` é a fixture. O mapa
    vive num arquivo temporário — o ``config/home_aliases.json`` do dono não é
    lido nem escrito aqui (a porta dele é o teste de cima). Ensinar é caneta do
    dono desde a decisão 2 (09/11/2026), então o slot de identidade do processo
    é posto como a voz dele (e limpo antes e depois — os portões são estado de
    processo e nunca vazam para o teste seguinte; os casos de RECUSA do portão
    estão em ``tests/test_home_alias_gate.py``).
    """

    def setUp(self):
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.mapa = Path(sandbox.name) / "home_aliases.json"
        for p in (patch.object(hc, "_alias_path", self.mapa),
                  patch.object(hc, "_states", return_value=STATES),
                  patch.object(hc, "_api", side_effect=self._rede)):
            p.start()
            self.addCleanup(p.stop)
        voice_identity.clear()
        actiongate.close()
        self.addCleanup(voice_identity.clear)
        self.addCleanup(actiongate.close)
        voice_identity.set_recent(who="owner", person="dono", privileged=True,
                                  confirmed_by="voiceprint_owner")

    @staticmethod
    def _rede(*args, **kwargs):
        raise AssertionError(f"teste tocou a rede: {args}")

    def _doc(self) -> dict:
        return json.loads(self.mapa.read_text(encoding="utf-8"))

    def _mapa_vazio(self) -> None:
        self.mapa.write_text(json.dumps({"version": 1, "regra": "teste",
                                         "atualizado_em": "2026-11-09",
                                         "apelidos": []}), encoding="utf-8")

    def _ensinar(self, alias: str, entidade: str) -> str:
        return hc.home_control({"action": "apelido", "alias": alias,
                                "entidade": entidade})

    def test_apelido_grava_o_mapa_e_vale_na_chamada_seguinte(self):
        """O nome ensinado sai da fala e entra no arquivo — a regra do dono."""
        out = self._ensinar("luz do quarto", "Luz do Quarto")
        self.assertIn("Anotado", out)
        entry = self._doc()["apelidos"][0]
        self.assertEqual(entry["apelido"], "luz do quarto")
        self.assertEqual(entry["entidade"], "light.luz_do_quarto")
        self.assertEqual(entry["nome"], "Luz do Quarto")
        self.assertEqual(entry["dominio"], "light")
        self.assertTrue(set(entry) <= set(hc.ALIAS_FIELDS))
        entity, error = hc.resolve("luz do quarto", STATES)
        self.assertEqual(error, "")
        self.assertEqual(entity["entity_id"], "light.luz_do_quarto")

    def test_apelido_apontando_para_o_nada_nao_escreve_nada(self):
        """Apelido nasce ancorado em entidade real: alvo falso fica de fora."""
        out = self._ensinar("luz do banheiro", "luz do banheiro")
        self.assertIn("Não anotei", out)
        self.assertFalse(self.mapa.exists())
        self.assertEqual(hc.alias_entries()[0], [])

    def test_apelidos_lista_o_que_esta_no_arquivo(self):
        """Listar é ler o disco: o que o dono ensinou aparece dito, com o id."""
        self._mapa_vazio()
        self.assertIn("Nenhum apelido registrado",
                      hc.home_control({"action": "apelidos"}))
        self._ensinar("luz do quarto", "Luz do Quarto")
        self._ensinar("tomada", "Tomada do Escritório")
        out = hc.home_control({"action": "apelidos"})
        self.assertIn("2 apelido(s) da casa", out)
        self.assertIn("luz do quarto → Luz do Quarto (light.luz_do_quarto)", out)
        self.assertIn("tomada → Tomada do Escritório "
                      "(switch.tomada_do_escritorio)", out)

    def test_esquecer_apelido_reescreve_o_arquivo(self):
        """Esquecer tira a linha do arquivo e deixa as outras — com a
        confirmação do dono: nada apaga apelido existente sem ele confirmar."""
        self._ensinar("luz do quarto", "Luz do Quarto")
        self._ensinar("tomada", "Tomada do Escritório")
        pergunta = hc.home_control({"action": "esquecer_apelido",
                                    "alias": "luz do quarto"})
        self.assertIn("Confirma", pergunta)
        self.assertEqual([entry["apelido"] for entry in self._doc()["apelidos"]],
                         ["luz do quarto", "tomada"])
        out = hc.home_control({"action": "esquecer_apelido",
                               "alias": "luz do quarto", "confirmar": True})
        self.assertIn("não é mais atalho", out)
        self.assertEqual([entry["apelido"] for entry in self._doc()["apelidos"]],
                         ["tomada"])
        self.assertEqual([entry["apelido"] for entry in hc.alias_entries()[0]],
                         ["tomada"])

    def test_esquecer_o_que_nao_existe_nao_mexe_no_mapa(self):
        """Recusa dita, arquivo intacto: esquecer por engano não apaga lição."""
        self._ensinar("tomada", "Tomada do Escritório")
        antes = self.mapa.read_text(encoding="utf-8")
        out = hc.home_control({"action": "esquecer_apelido",
                               "alias": "luz do quarto"})
        self.assertIn("Não esqueci", out)
        self.assertEqual(self.mapa.read_text(encoding="utf-8"), antes)


if __name__ == "__main__":
    unittest.main()
