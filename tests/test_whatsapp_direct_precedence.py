"""Precedência do chat direto do dono sobre o portão de nome (regra 2 do dono).

Incidente 05/11/2026 (logs/activity.log 18:47-18:50, 4x "turno do dono
barrado pelo portão de nome" + recusa enviada no chat dele): com
``name_gate_enabled`` ligado, ``_whatsapp_turn_trust`` deixou o portão de nome
julgar o chat direto do dono e ``_flows_begin_turn`` despejou no chat pessoal
dele a frase "Para eu responder por aqui, chame pelo nome". A lei do dono
(SKILL.md do WhatsApp, regra 2) é absoluta: chat direto dele é turno da
conversa e o nome NUNCA é exigido lá — a decisão de chat direto tem
precedência sobre o portão de nome, em qualquer valor da configuração.

Cobertura mínima (nenhum teste toca rede, áudio ou segredo; os números aqui
são constantes de teste, nunca aparecem em log do app):
  (a) dono SEM nome no chat direto, portão ligado  -> turno owner, zero recusa;
  (b) terceiro SEM nome                            -> recusa normal de sempre
      (não é candidata, turno segue untrusted, nenhuma frase é enviada);
  (c) eco ``from_me`` do dono no chat direto       -> candidata/vira ordem,
      trust owner, sem recusa;
  (d) grupo                                        -> nunca nada (watch e
      fluxo), nem com o nome na mensagem.
"""
from __future__ import annotations

import time
from unittest import mock

import main
from core import whatsapp_watch

OWNER_CHAT = "5511999990000@s.whatsapp.net"
THIRD_CHAT = "5521888887777@s.whatsapp.net"
GROUP_CHAT = "120363000000000000@g.us"


class FakeUI:
    def __init__(self):
        self.lines: list[str] = []

    def write_log(self, text):
        self.lines.append(text)


class FakeRuntime:
    def __init__(self):
        self.begins: list[dict] = []

    def start_turn(self, turn_id, text, *, speak_allowed=True,
                   source="owner", trust="owner", armed=True):
        self.begins.append({"turn_id": turn_id, "text": text,
                            "source": source, "trust": trust})
        plan = mock.Mock()
        plan.blocked = trust != "owner"
        plan.active = not plan.blocked
        return plan


def _obj() -> tuple[main.JarvisLive, FakeRuntime]:
    rt = FakeRuntime()
    obj = object.__new__(main.JarvisLive)
    obj.ui = FakeUI()
    obj._asst_name = "Gino"
    obj._flows_get = lambda: rt
    obj._sent: list[tuple] = []

    def _notify(text, channel, account=None, chat=None):
        obj._sent.append((text, channel, account, chat))

    obj._owner_text_notify = _notify
    return obj, rt


def _meta(chat: str, direct: bool, **kw) -> dict:
    meta = {"source": "whatsapp", "speak": False, "reply_channel": "whatsapp",
            "wa_account": "principal", "wa_chat": chat, "wa_direct": direct}
    meta.update(kw)
    return meta


def _fresh_item(**kw) -> dict:
    item = {"chat": OWNER_CHAT, "name": "Contato", "text": "",
            "ts": time.time()}
    item.update(kw)
    return item


# ── (a) dono sem nome no chat direto: turno, nunca recusa ────────────────────
def test_direct_owner_without_name_gate_on_is_turn_never_refused():
    obj, rt = _obj()
    with mock.patch("main.get_name_gate_enabled", return_value=True), \
            mock.patch.object(type(obj), "_in_followup_window",
                              lambda _s: False):
        plan = obj._flows_begin_turn("a1", "como estava o tempo ontem?",
                                     _meta(OWNER_CHAT, True, wa_named=False))
    assert rt.begins[0]["trust"] == "owner"
    assert plan is not None and plan.active and not plan.blocked
    # nenhuma frase de "chame pelo nome" sai no chat direto do dono
    assert obj._sent == []
    joined = " ".join(obj.ui.lines)
    assert "chame pelo nome" not in joined and "barrado" not in joined


def test_direct_owner_without_name_is_candidate_in_watch():
    item = _fresh_item(text="como estava o tempo ontem?")
    with mock.patch.object(whatsapp_watch, "_is_direct", return_value=True), \
            mock.patch.object(whatsapp_watch, "_trigger_norm", ""):
        assert whatsapp_watch.is_candidate(item, "principal")


# ── (b) terceiro sem nome: recusa normal de sempre, nada novo ────────────────
def test_third_party_without_name_is_not_candidate():
    item = _fresh_item(chat=THIRD_CHAT, text="que horas são?")
    with mock.patch.object(whatsapp_watch, "_is_direct", return_value=False), \
            mock.patch.object(whatsapp_watch, "_is_own_account",
                              return_value=False), \
            mock.patch.object(whatsapp_watch, "_trigger_norm", "gino"), \
            mock.patch.object(whatsapp_watch.whatsapp, "account_number",
                              return_value=""):
        assert not whatsapp_watch.is_candidate(item, "principal")


def test_third_party_without_name_stays_untrusted_and_gets_no_refusal_text():
    obj, rt = _obj()
    with mock.patch("main.get_name_gate_enabled", return_value=True), \
            mock.patch.object(type(obj), "_in_followup_window",
                              lambda _s: False):
        plan = obj._flows_begin_turn("b1", "que horas são?",
                                     _meta(THIRD_CHAT, False, wa_named=False))
    assert rt.begins[0]["trust"] == "untrusted"
    assert plan is not None and plan.blocked and not plan.active
    assert obj._sent == []          # terceiro: silêncio de sempre, sem frase


# ── (c) eco from_me do dono no chat direto: vira ordem, sem recusa ───────────
def test_owner_from_me_echo_in_direct_chat_is_candidate():
    item = _fresh_item(text="busca minha agenda de amanhã", from_me=True)
    with mock.patch.object(whatsapp_watch, "_is_direct", return_value=True), \
            mock.patch.object(whatsapp_watch.whatsapp, "sent_by_me",
                              return_value=False), \
            mock.patch.object(whatsapp_watch, "_trigger_norm", ""):
        assert whatsapp_watch.is_candidate(item, "principal")


def test_owner_from_me_echo_in_direct_chat_opens_owner_turn_no_refusal():
    obj, rt = _obj()
    with mock.patch("main.get_name_gate_enabled", return_value=True), \
            mock.patch.object(type(obj), "_in_followup_window",
                              lambda _s: False):
        plan = obj._flows_begin_turn("c1", "busca minha agenda de amanhã",
                                     _meta(OWNER_CHAT, True, wa_named=False))
    assert rt.begins[0]["trust"] == "owner"
    assert plan is not None and plan.active and not plan.blocked
    assert obj._sent == []


# ── (d) grupo: nunca recebe nada (regra 4, não regredir) ─────────────────────
def test_group_never_is_candidate_not_even_with_the_name():
    with mock.patch.object(whatsapp_watch, "_trigger_norm", "gino"):
        assert not whatsapp_watch.is_candidate(
            _fresh_item(chat=GROUP_CHAT, group=True,
                        text="Gino, olha isso"), "principal")
        assert not whatsapp_watch.is_candidate(
            _fresh_item(chat=GROUP_CHAT, text="Gino, olha isso"),
            "principal")


def test_group_turn_stays_untrusted_and_nothing_is_sent():
    obj, rt = _obj()
    with mock.patch("core.whatsapp_watch.is_direct", return_value=False):
        plan = obj._flows_begin_turn(
            "d1", "Gino, que horas são no grupo?",
            {"source": "whatsapp", "speak": False,
             "reply_channel": "whatsapp",
             "wa_account": "principal", "wa_chat": GROUP_CHAT})
    assert rt.begins[0]["trust"] == "untrusted"
    assert plan is not None and plan.blocked
    assert obj._sent == []
