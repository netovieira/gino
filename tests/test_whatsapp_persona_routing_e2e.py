"""UP-1: WhatsApp persona routing end-to-end validation.

Prova que o evento ``whatsapp_persona_rota`` vai ao audit.jsonl E que o
evento ``persona_routed`` vai ao activity.log, com nome da persona,
mensagem original e fator de identificacao.

Cobertura:
  (a) audit.jsonl registra ``whatsapp_persona_rota`` com persona/fator;
  (b) activity.log registra ``persona_routed`` com nome e mensagem;
  (c) nenhum dos dois logs escreve chat, conta ou segredo;
  (d) rota por vocativo E por conta atribuida sao cobertas;
  (e) sem persona, nenhum evento e emitido.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import main
from core import activity as _activity
from core import actiongate, whatsapp_watch
from core.persona_ops import wiring as persona_wiring

OWNER_CHAT = "5511999990000"
THIRD_CHAT = "5521888887777"


def _persona(name: str, account: str = "") -> dict:
    # Conta preenchida exige numero direto declarado (regra do dono 09/11):
    # sem o booleano a persona e ativa mas nao tem numero, e a rota de conta
    # tem de recusar.
    return {"id": name.lower(), "name": name, "aliases": [],
            "whatsapp_account": account,
            "direct_whatsapp": bool(account)}


def wa_item(text: str = "", **overrides) -> dict:
    item = {"ts": int(time.time()), "chat": THIRD_CHAT, "name": "Contato",
            "from_me": False, "group": False, "text": text,
            "id": f"msg-{time.time_ns()}", "media": None}
    item.update(overrides)
    return item


class _FakeUI:
    def __init__(self):
        self.lines: list[str] = []

    def write_log(self, text):
        self.lines.append(text)


def _live_object(tmp_path: Path):
    obj = object.__new__(main.JarvisLive)
    obj.ui = _FakeUI()
    obj._asst_name = "Gino"
    obj._next_turn_ref = lambda _chan: None
    obj._session_log = []
    obj._chat_memory = None
    obj._index_event = lambda *a, **k: None
    obj._memory_record_user = lambda _t: None
    obj._begin_user_epoch = lambda: {}
    obj._owner_text_gate = lambda *a, **k: False
    obj._voice_bootstrap_gate = lambda *a, **k: False
    obj._qoder_live_ready = lambda: True
    obj._last_user_speech = 0.0
    obj.turns: list[tuple[str, dict]] = []
    obj._qoder_turn = lambda text, **kw: obj.turns.append((text, kw))
    obj._persona_ops_lock = threading.Lock()
    obj._persona_ops_rt = persona_wiring.build(
        root=tmp_path, memory_root=tmp_path / "persona-memory",
        log=lambda _m: None)
    return obj


class PersonaRoutingE2ETests(unittest.TestCase):
    """Eventos whatsapp_persona_rota (audit) + persona_routed (activity)."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="wa-persona-e2e-"))
        whatsapp_watch.reset()
        whatsapp_watch.set_assistant_name("Gino")
        whatsapp_watch.set_direct_chat("principal", OWNER_CHAT)
        self.obj = _live_object(self._tmp)
        self.addCleanup(whatsapp_watch.set_direct_chat, "", "")
        self.addCleanup(whatsapp_watch.set_personas_source, None)
        self.addCleanup(whatsapp_watch.reset)
        for target in (
            mock.patch("core.whatsapp_watch.whatsapp.default_account_name",
                       return_value="principal"),
            mock.patch("core.whatsapp_watch.whatsapp.account_number",
                       return_value=""),
            mock.patch("core.whatsapp_watch.whatsapp.list_accounts",
                       return_value=[]),
            mock.patch("memory.config_manager.get_plugin_setting",
                       side_effect=lambda _ns, key, default=None:
                           OWNER_CHAT if key == "owner_chat" else default),
        ):
            target.start()
            self.addCleanup(target.stop)
        get_patch = mock.patch(
            "core.personas.get",
            side_effect=lambda key: {"id": "aline", "name": "Aline"}
            if str(key) in ("aline", "Aline") else None)
        get_patch.start()
        self.addCleanup(get_patch.stop)

    def _audit_lines(self) -> list[dict]:
        path = self.obj._persona_ops_rt.audit.path
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
        return rows

    def _activity_lines(self) -> list[str]:
        return list(self.obj.ui.lines)

    # ── (a) audit.jsonl registra whatsapp_persona_rota ───────────────────────
    def test_audit_jsonl_registra_whatsapp_persona_rota_com_vocativo(self):
        whatsapp_watch.set_personas_source(
            lambda: [_persona("Aline", ""), _persona("Arnaldo", "")])
        self.assertTrue(self.obj._whatsapp_autoreply(
            "principal", wa_item("Aline, revisa o card do site",
                                 chat=OWNER_CHAT)))
        events = [row["event"] for row in self._audit_lines()]
        self.assertIn("whatsapp_persona_rota", events)
        rota = next(row for row in self._audit_lines()
                    if row["event"] == "whatsapp_persona_rota")
        self.assertEqual(rota["persona"], "aline")
        self.assertEqual(rota["factor"], "vocativo")

    def test_audit_jsonl_registra_whatsapp_persona_rota_com_conta(self):
        whatsapp_watch.set_personas_source(
            lambda: [_persona("Aline", "trabalho")])
        self.assertTrue(self.obj._whatsapp_autoreply(
            "trabalho", wa_item("chegou o pedido")))
        rota = next((row for row in self._audit_lines()
                     if row["event"] == "whatsapp_persona_rota"), None)
        self.assertIsNotNone(rota)
        self.assertEqual(rota["persona"], "aline")
        self.assertEqual(rota["factor"], "conta_atribuida")

    # ── (b) activity.log registra persona_routed ─────────────────────────────
    def test_activity_log_registra_persona_routed_com_nome_e_mensagem(self):
        whatsapp_watch.set_personas_source(
            lambda: [_persona("Aline", ""), _persona("Arnaldo", "")])
        self.assertTrue(self.obj._whatsapp_autoreply(
            "principal", wa_item("Aline, revisa o card do site",
                                 chat=OWNER_CHAT)))
        routed = [line for line in self._activity_lines()
                  if "persona_routed" in line]
        self.assertTrue(routed, "evento persona_routed ausente no activity.log")
        self.assertIn("Aline", routed[0])
        self.assertIn("vocativo", routed[0])
        self.assertIn("revisa o card", routed[0])

    def test_activity_log_persona_routed_tem_meta_chan_whatsapp(self):
        whatsapp_watch.set_personas_source(
            lambda: [_persona("Aline", "")])
        self.assertTrue(self.obj._whatsapp_autoreply(
            "principal", wa_item("Aline, oi", chat=OWNER_CHAT)))
        routed = [line for line in self._activity_lines()
                  if "persona_routed" in line]
        self.assertTrue(routed)
        meta = _activity.parse(routed[0])
        self.assertIsNotNone(meta)
        self.assertEqual(meta.get("chan"), "whatsapp")
        self.assertEqual(meta.get("origin"), "persona_routed")

    # ── (c) nenhum log escreve chat, conta ou segredo ────────────────────────
    def test_audit_jsonl_nao_escreve_chat_nem_conta(self):
        whatsapp_watch.set_personas_source(
            lambda: [_persona("Aline", "trabalho")])
        self.assertTrue(self.obj._whatsapp_autoreply(
            "trabalho", wa_item("bom dia")))
        raw = self.obj._persona_ops_rt.audit.path.read_text(
            encoding="utf-8")
        self.assertNotIn(OWNER_CHAT, raw)
        self.assertNotIn(THIRD_CHAT, raw)
        self.assertNotIn("trabalho", raw)

    def test_activity_log_nao_escreve_chat_nem_conta(self):
        whatsapp_watch.set_personas_source(
            lambda: [_persona("Aline", "trabalho")])
        self.assertTrue(self.obj._whatsapp_autoreply(
            "trabalho", wa_item("bom dia")))
        for line in self._activity_lines():
            if "persona_routed" not in line:
                continue
            self.assertNotIn(OWNER_CHAT, line)
            self.assertNotIn(THIRD_CHAT, line)
            self.assertNotIn("trabalho", line)

    # ── (d) sem persona, nenhum evento e emitido ─────────────────────────────
    def test_sem_persona_nenhum_evento_e_emitido(self):
        whatsapp_watch.set_personas_source(lambda: [])
        self.assertTrue(self.obj._whatsapp_autoreply(
            "principal", wa_item("Gino, me manda o boleto",
                                 chat=OWNER_CHAT)))
        audit_events = [row["event"] for row in self._audit_lines()]
        self.assertNotIn("whatsapp_persona_rota", audit_events)
        routed = [line for line in self._activity_lines()
                  if "persona_routed" in line]
        self.assertEqual(routed, [])

    def test_persona_inexistente_nao_emite_evento_de_rota(self):
        whatsapp_watch.set_personas_source(
            lambda: [_persona("Beltrano", "pessoal")])
        self.assertTrue(self.obj._whatsapp_autoreply(
            "principal", wa_item("Aline, revisa o card", chat=OWNER_CHAT)))
        audit_events = [row["event"] for row in self._audit_lines()]
        self.assertNotIn("whatsapp_persona_rota", audit_events)
        routed = [line for line in self._activity_lines()
                  if "persona_routed" in line]
        self.assertEqual(routed, [])


if __name__ == "__main__":
    unittest.main()
