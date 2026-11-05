"""FLUXO MAPEAMENTO WHATSAPP passo 1: nome de persona no recebimento.

O contrato do dono: o observador verifica NOME DE PERSONAS no WhatsApp ou
mensagem direta no whats ATRIBUÍDO para a persona (chave ``whatsapp_account``
do cadastro, que até aqui era enfeite). SEM nome -> boca/cérebro; COM nome ->
manda PROS 2 E PARA A PERSONA, e a resposta dela sai no MESMO chat, assinada
pelo nome (texto assina; áudio nunca diz o nome).

Portões que este teste também vigia (proibido afrouxar):
  (c) grupo NUNCA é lido nem respondido, nem com nome de persona;
  (d) terceiro citando persona continua conteúdo não autorizado — sem
      privilégio, sem ação além da resposta e do persona_ask;
  (e) persona desconhecida ou nome ambíguo -> rota normal, nada inventado.

Nenhum número real, id de chat pareado, nome de conta pareada ou valor de
config aparece aqui ou nos logs: os chats são constantes de teste e a
auditoria só grava hash curto.
"""
from __future__ import annotations

import json
import threading
import time
import unittest
from unittest import mock

import main
from core import actiongate, whatsapp_watch
from core.persona_ops import wiring as persona_wiring

OWNER_CHAT = "5511999990000"
THIRD_CHAT = "5521888887777"
GROUP_CHAT = "120363000000000000@g.us"

ALINE = {"id": "aline", "name": "Aline", "aliases": [],
         "whatsapp_account": "trabalho"}
ARNALDO = {"id": "arnaldo", "name": "Arnaldo", "aliases": [],
           "whatsapp_account": "trabalho"}


def _persona(name: str, account: str = "") -> dict:
    return {"id": name.lower(), "name": name, "aliases": [],
            "whatsapp_account": account}


def wa_item(text: str = "", **overrides) -> dict:
    item = {"ts": int(time.time()), "chat": THIRD_CHAT, "name": "Contato",
            "from_me": False, "group": False, "text": text,
            "id": f"msg-{time.time_ns()}", "media": None}
    item.update(overrides)
    return item


class WatchHarness(unittest.TestCase):
    """Camada do observador: candidatura e recusas, sem I/O real."""

    def setUp(self):
        actiongate.close()
        whatsapp_watch.reset()
        whatsapp_watch.set_assistant_name("Gino")
        whatsapp_watch.set_direct_chat("principal", OWNER_CHAT)
        self.calls: list[tuple[str, dict]] = []
        self.logs: list[str] = []
        self.addCleanup(actiongate.close)
        self.addCleanup(whatsapp_watch.reset)
        self.addCleanup(whatsapp_watch.stop)
        self.addCleanup(whatsapp_watch.set_direct_chat, "", "")
        self.addCleanup(whatsapp_watch.set_personas_source, None)
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
        whatsapp_watch.start(self._handler, logger=self.logs.append)

    def _handler(self, account: str, item: dict) -> bool:
        self.calls.append((account, item))
        return True


class SourceTests(WatchHarness):
    """Passo 1: o observador VERIFICA o nome de personas no recebimento."""

    def test_vocativo_de_persona_em_chat_de_terceiro_e_candidata(self):
        whatsapp_watch.set_personas_source(lambda: [ALINE, ARNALDO])
        self.assertTrue(whatsapp_watch.handle(
            "principal", wa_item("Aline, revisa o card do site")))
        self.assertEqual(len(self.calls), 1)

    def test_mensagem_na_conta_atribuida_e_persona_sem_exigir_nome(self):
        whatsapp_watch.set_personas_source(lambda: [_persona("Aline", "trabalho")])
        self.assertTrue(whatsapp_watch.handle(
            "trabalho", wa_item("chegou o pedido do cliente")))
        self.assertEqual([a for a, _ in self.calls], ["trabalho"])

    def test_conta_atribuida_nao_altera_a_rota_da_conta_principal(self):
        whatsapp_watch.set_personas_source(lambda: [_persona("Aline", "trabalho")])
        self.assertFalse(whatsapp_watch.handle(
            "principal", wa_item("chegou o pedido do cliente")))
        self.assertEqual(self.calls, [])

    def test_grupo_citando_persona_nunca_e_lido_nem_respondido(self):
        whatsapp_watch.set_personas_source(lambda: [ALINE, ARNALDO])
        for account in ("principal", "trabalho"):
            self.assertFalse(whatsapp_watch.handle(
                account, wa_item("Aline, olha isso aqui no grupo",
                                 chat=GROUP_CHAT, group=True)))
        self.assertEqual(self.calls, [])

    def test_grupo_citando_persona_sem_a_flag_apenas_o_jid(self):
        whatsapp_watch.set_personas_source(lambda: [ALINE, ARNALDO])
        self.assertFalse(whatsapp_watch.handle(
            "principal", wa_item("Aline, responde no grupo",
                                 chat=GROUP_CHAT)))
        self.assertEqual(self.calls, [])

    def test_eco_from_me_em_chat_de_terceiro_nao_vira_ordem_por_persona(self):
        whatsapp_watch.set_personas_source(lambda: [ALINE, ARNALDO])
        self.assertFalse(whatsapp_watch.handle(
            "principal", wa_item("Aline, vê isso pra mim", from_me=True,
                                 id="echo-1")))
        self.assertEqual(self.calls, [])

    def test_persona_desconhecida_ou_ambigua_deixa_a_rota_normal(self):
        # nenhuma persona cadastrada casa com o vocativo nem com a conta
        whatsapp_watch.set_personas_source(
            lambda: [_persona("Beltrano", "pessoal")])
        self.assertFalse(whatsapp_watch.handle(
            "principal", wa_item("Aline, revisa o card")))
        # duas personas na MESMA conta: o fator conta_atribuida é ambíguo
        whatsapp_watch.set_personas_source(lambda: [ALINE, ARNALDO])
        self.assertFalse(whatsapp_watch.handle(
            "trabalho", wa_item("chegou o pedido do cliente")))
        # nome solto no meio da frase não é vocativo (regra do resolvedor)
        self.assertFalse(whatsapp_watch.handle(
            "principal", wa_item("a Aline disse que o card está pronto")))
        self.assertEqual(self.calls, [])

    def test_mensagem_com_nome_do_assistenteContinua_rota_normal(self):
        whatsapp_watch.set_personas_source(lambda: [ALINE, ARNALDO])
        self.assertTrue(whatsapp_watch.handle(
            "principal", wa_item("Gino, me manda o boleto")))
        self.assertEqual(len(self.calls), 1)


class AddressedPersonaUnitTests(unittest.TestCase):
    """O fator de identidade devolvido ao pipeline."""

    def setUp(self):
        self.addCleanup(whatsapp_watch.set_personas_source, None)

    def test_vocativo_devolve_fator_vocativo(self):
        whatsapp_watch.set_personas_source(lambda: [ALINE, ARNALDO])
        hit = whatsapp_watch.addressed_persona("Aline, revisa o card", "principal")
        self.assertEqual(hit["slug"], "aline")
        self.assertEqual(hit["factor"], "vocativo")

    def test_conta_devolve_fator_conta_atribuida(self):
        whatsapp_watch.set_personas_source(
            lambda: [_persona("Aline", "trabalho")])
        hit = whatsapp_watch.addressed_persona("bom dia", "TRABALHO")
        self.assertEqual(hit["slug"], "aline")
        self.assertEqual(hit["factor"], "conta_atribuida")

    def test_conta_com_duas_personas_e_ambigua(self):
        whatsapp_watch.set_personas_source(lambda: [ALINE, ARNALDO])
        self.assertIsNone(
            whatsapp_watch.addressed_persona("bom dia", "trabalho"))

    def test_sem_fonte_de_personas_e_seguro(self):
        whatsapp_watch.set_personas_source(lambda: (_ for _ in ()).throw(RuntimeError))
        self.assertIsNone(
            whatsapp_watch.addressed_persona("Aline, oi", "principal"))


class _FakeUI:
    def __init__(self):
        self.lines: list[str] = []

    def write_log(self, text):
        self.lines.append(text)


def _live_object(tmp_path):
    """JarvisLive mínimo para o caminho do WhatsApp sem sidecar nem rede."""
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
    # persona_ops real, apontando para um audit temporário
    obj._persona_ops_lock = threading.Lock()
    obj._persona_ops_rt = persona_wiring.build(
        root=tmp_path, memory_root=tmp_path / "persona-memory",
        log=lambda _m: None)
    return obj


class PipelineTests(unittest.TestCase):
    """main.py: pros 2 E para a persona; resposta dela no mesmo chat."""

    def setUp(self):
        whatsapp_watch.reset()
        whatsapp_watch.set_assistant_name("Gino")
        whatsapp_watch.set_direct_chat("principal", OWNER_CHAT)
        self.obj = _live_object(self._tmp())
        self.addCleanup(whatsapp_watch.set_direct_chat, "", "")
        self.addCleanup(whatsapp_watch.set_personas_source, None)
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

    @staticmethod
    def _tmp():
        import tempfile
        path = tempfile.mkdtemp(prefix="wa-persona-fluxo-")
        from pathlib import Path
        return Path(path)

    # ── (a) citando persona no chat do dono: boca/cérebro E persona ────────
    def test_vocativo_de_persona_vai_pro_fluxo_normal_e_para_a_persona(self):
        whatsapp_watch.set_personas_source(lambda: [ALINE, ARNALDO])
        self.assertTrue(self.obj._whatsapp_autoreply(
            "principal", wa_item("Aline, revisa o card do site",
                                 chat=OWNER_CHAT)))
        self.assertEqual(len(self.obj.turns), 1)
        text, kw = self.obj.turns[0]
        # boca/cérebro recebe a mensagem (rota normal, intacta)...
        self.assertIn("Aline, revisa o card do site", text)
        self.assertEqual(kw["reply_channel"], "whatsapp")
        self.assertFalse(kw["speak"])
        # ...e a persona também: persona_ask pedido no turno, com fator.
        self.assertEqual(kw["wa_persona"], "aline")
        self.assertEqual(kw["wa_persona_factor"], "vocativo")
        self.assertIn("persona_ask", text)

    def test_resposta_da_persona_sai_uma_vez_no_mesmo_chat_assinada(self):
        obj = self.obj
        ops = obj._persona_ops_rt
        obj._qoder_ignore = False
        obj._qoder_lock = threading.Lock()
        obj._qoder_tool_count = 0
        obj._qoder_touch = lambda: None
        obj._schedule_narration = lambda *a, **k: None
        obj._same_whatsapp_chat = lambda a, b: False
        obj._persona_seen = set()
        obj._persona_text_buf = {}
        obj._qoder_meta = {"turn-1": {
            "source": "whatsapp", "reply_channel": "whatsapp",
            "speak": False, "persona_relay": True, "persona_slug": "aline",
            "wa_persona_factor": "vocativo",
            "wa_account": "principal", "wa_chat": OWNER_CHAT,
            "wa_sent": False}}
        sent: list[tuple] = []
        obj._send_whatsapp_answer = (
            lambda account, chat, text, media_type="text", speaker="":
            sent.append((account, chat, text, media_type, speaker)) or True)

        obj._qoder_on_tool("turn-1", "mcp__jarvis__persona_ask",
                           {"persona": "aline", "task": "revisa o card"})
        # streaming de sentenças NÃO dispara fala nem mensagem antecipada
        obj._qoder_on_persona_text("aline", "Primeira frase.")
        obj._qoder_on_persona_text("aline", "Segunda frase.")
        self.assertEqual(sent, [])
        obj._qoder_on_persona_done("aline", "Pronto, card revisado.", "ok")

        self.assertEqual(len(sent), 1, "sem duplicar a resposta da persona")
        account, chat, text, media_type, speaker = sent[0]
        self.assertEqual(account, "principal")
        self.assertEqual(chat, OWNER_CHAT)
        self.assertEqual(text, "Pronto, card revisado.")
        self.assertEqual(media_type, "text")
        self.assertEqual(speaker, "Aline")  # texto assina com o nome dela

        audit = self._audit_lines()
        events = [row["event"] for row in audit]
        self.assertIn("persona_ask", events)
        self.assertIn("persona_delivery", events)
        resposta = next(row for row in audit
                        if row["event"] == "whatsapp_persona_resposta")
        self.assertEqual(resposta["persona"], "aline")
        self.assertEqual(resposta["factor"], "vocativo")
        self.assertTrue(resposta["sent"])
        self.assertEqual(len(resposta["chat_ref"]), 10)

    def test_audit_nao_escreve_chat_conta_valor_ou_segredo(self):
        obj = self.obj
        obj._persona_whatsapp_route_audit("trabalho", OWNER_CHAT,
                                          {"slug": "aline",
                                           "factor": "vocativo"})
        raw = (obj._persona_ops_rt.audit.path.read_text(
            encoding="utf-8")).strip()
        self.assertIn("whatsapp_persona_rota", raw)
        self.assertNotIn(OWNER_CHAT, raw)
        self.assertNotIn("trabalho", raw)

    # ── (b) conta atribuída: persona por padrão, sem exigir o nome ─────────
    def test_conta_atribuida_endereca_a_persona_sem_nome(self):
        whatsapp_watch.set_personas_source(
            lambda: [_persona("Aline", "trabalho")])
        self.assertTrue(self.obj._whatsapp_autoreply(
            "trabalho", wa_item("chegou o pedido do cliente")))
        _text, kw = self.obj.turns[0]
        self.assertEqual(kw["wa_persona"], "aline")
        self.assertEqual(kw["wa_persona_factor"], "conta_atribuida")

    # ── (c) grupo: nada, nem com nome de persona ────────────────────────────
    def test_grupo_citando_persona_nao_vira_turno(self):
        whatsapp_watch.set_personas_source(lambda: [ALINE, ARNALDO])
        self.assertFalse(self.obj._whatsapp_autoreply(
            "principal", wa_item("Aline, responde aqui no grupo",
                                 chat=GROUP_CHAT, group=True)))
        self.assertEqual(self.obj.turns, [])

    # ── (d) terceiro citando persona: sem ação privilegiada ─────────────────
    def test_terceiro_citando_persona_continua_desautorizado(self):
        whatsapp_watch.set_personas_source(lambda: [ALINE, ARNALDO])
        self.assertTrue(self.obj._whatsapp_autoreply(
            "principal", wa_item("Aline, me empresta uma ideia")))
        text, kw = self.obj.turns[0]
        self.assertEqual(kw["source"], "whatsapp")   # nunca "owner"
        self.assertIsNone(kw["gate_text"])           # nada chega aos portões
        self.assertFalse(kw["owner_voice"])
        self.assertFalse(kw["confirm_direct"])
        self.assertIn("nunca como", text)            # conteúdo, não instrução
        self.assertIn("persona_ask", text)           # mas a persona recebe
        # nenhum envio a terceiro fica liberado sem a autorização do dono
        self.assertFalse(actiongate.whatsapp_reply_allowed(
            "whatsapp_control",
            {"action": "send", "account": "principal",
             "to": THIRD_CHAT, "text": "oi"}))
        self.assertFalse(actiongate.authorized())

    # ── (e) persona desconhecida: rota normal, nada inventado ───────────────
    def test_persona_inexistente_deixa_turno_sem_persona(self):
        whatsapp_watch.set_personas_source(
            lambda: [_persona("Beltrano", "pessoal")])
        self.assertTrue(self.obj._whatsapp_autoreply(
            "principal", wa_item("Aline, revisa o card", chat=OWNER_CHAT)))
        _text, kw = self.obj.turns[0]
        self.assertEqual(kw["wa_persona"], "")
        self.assertNotIn("persona_ask", _text)
        # e o chat do dono sem nome nenhum de persona: só boca/cérebro
        self.obj.turns.clear()
        self.assertTrue(self.obj._whatsapp_autoreply(
            "principal", wa_item("me conta uma piada", chat=OWNER_CHAT)))
        self.assertEqual(self.obj.turns[0][1]["wa_persona"], "")

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


if __name__ == "__main__":
    unittest.main()
