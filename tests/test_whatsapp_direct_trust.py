"""Trust do WhatsApp no fluxo: chat direto do dono deixa de ser reflexo "untrusted".

O defeito provado (logs/activity.log, 28x turn_blocked): QUALQUER mensagem do
WhatsApp virava ``trust="untrusted"`` em ``_flows_begin_turn`` e o orquestrador
fechava o turno em silêncio — inclusive as do próprio dono no chat direto dele.
A política corrigida (SKILL.md do WhatsApp):

  * chat direto do dono                      -> ``owner`` com ou sem o nome;
    o portão de nome tem PRECEDÊNCIA invertida (regra 2 do dono, incidente
    05/11/2026): no chat direto dele o nome nunca é exigido e a frase
    "chame pelo nome" nunca é enviada;
  * grupo ou chat de terceiro             -> ``untrusted`` como antes;
  * monitor_event                         -> ``untrusted`` sempre;
  * texto do WhatsApp NUNCA executa ação privilegiada, mesmo com trust owner.

Nenhum teste toca rede, áudio ou segredo; o número do dono aparece só como
constante de teste, nunca em log do app.
"""
from __future__ import annotations

import time
from unittest import mock

import main
from core.flows.config import FlowsConfig
from core.flows.live import FlowsRuntime

OWNER_CHAT = "5511999990000@s.whatsapp.net"
THIRD_CHAT = "5521888887777@s.whatsapp.net"
GROUP_CHAT = "5511999990000-1234567890@g.us"


class FakeUI:
    def __init__(self):
        self.lines: list[str] = []

    def write_log(self, text):
        self.lines.append(text)


class FakeRuntime:
    """Registra o begin; devolve um plano falso com o trust recebido."""

    def __init__(self):
        self.begins: list[dict] = []
        self.monitors: list[dict] = []

    def start_turn(self, turn_id, text, *, speak_allowed=True,
                   source="owner", trust="owner", armed=True):
        self.begins.append({"turn_id": turn_id, "text": text,
                            "source": source, "trust": trust})
        plan = mock.Mock()
        plan.blocked = trust != "owner"
        plan.active = not plan.blocked
        return plan

    def monitor_event(self, source, text, trust="untrusted"):
        self.monitors.append({"source": source, "text": text, "trust": trust})
        return {"decision": "blocked"}


def _obj(runtime: FakeRuntime) -> main.JarvisLive:
    obj = object.__new__(main.JarvisLive)
    obj.ui = FakeUI()
    obj._asst_name = "Gino"
    obj._flows_get = lambda: runtime
    obj._sent: list[tuple] = []

    def _notify(text, channel, account=None, chat=None):
        obj._sent.append((text, channel, account, chat))

    obj._owner_text_notify = _notify
    return obj


def _meta(**kw) -> dict:
    meta = {"source": "whatsapp", "speak": False,
            "reply_channel": "whatsapp",
            "wa_account": "principal", "wa_chat": THIRD_CHAT,
            "wa_direct": False}
    meta.update(kw)
    return meta


def _config(**kw):
    base = dict(enabled=True, mouth_enabled=False, partial_prewarm=True,
                partial_speech=False, executor_enabled=False)
    base.update(kw)
    return FlowsConfig(**base)


# ── (a) chat direto do dono com o nome: turno ativo, sem bloqueio ──────────
def test_direct_chat_with_name_is_trust_owner():
    rt = FakeRuntime()
    obj = _obj(rt)
    plan = obj._flows_begin_turn(
        "t1", "Gino, que horas são?",
        _meta(wa_chat=OWNER_CHAT, wa_direct=True))
    assert rt.begins[0]["trust"] == "owner"
    assert rt.begins[0]["source"] == "whatsapp"
    assert plan.active and not plan.blocked
    assert obj._sent == []                      # sem toque de campainha
    assert not any("barrado" in line for line in obj.ui.lines)


def test_direct_with_name_reaches_orchestrator_as_active_turn():
    runtime = FlowsRuntime(_config(), speak=lambda _t: None,
                           log=lambda _m: None, start_expiry_thread=False)
    plan = runtime.start_turn("t9", "Gino, que horas são?",
                              speak_allowed=False, source="whatsapp",
                              trust="owner")
    assert plan is not None
    assert plan.active and not plan.blocked


# ── (b) direto sem nome, portão LIGADO: chat direto tem precedência ─────────
def test_direct_without_name_gate_on_is_owner_and_never_refused():
    """Regra 2 do dono (incidente 05/11/2026): o portão de nome NUNCA barra o
    chat direto nem dispara a frase "chame pelo nome" no chat dele."""
    rt = FakeRuntime()
    obj = _obj(rt)
    with mock.patch("main.get_name_gate_enabled", return_value=True), \
            mock.patch.object(type(obj), "_in_followup_window",
                              lambda _s: False):
        plan = obj._flows_begin_turn("t2", "que horas são?",
                                     _meta(wa_chat=OWNER_CHAT, wa_direct=True,
                                           wa_named=False))
    assert rt.begins[0]["trust"] == "owner"
    assert plan is not None and plan.active and not plan.blocked
    assert obj._sent == []                       # nenhuma recusa no chat dele
    joined = " ".join(obj.ui.lines)
    assert "chame pelo nome" not in joined
    assert OWNER_CHAT not in joined and "principal" not in joined


def test_direct_without_name_gate_off_is_owner():
    rt = FakeRuntime()
    obj = _obj(rt)
    with mock.patch("main.get_name_gate_enabled", return_value=False):
        obj._flows_begin_turn("t3", "que horas são?",
                              _meta(wa_chat=OWNER_CHAT, wa_direct=True, wa_named=False))
    assert rt.begins[0]["trust"] == "owner"
    assert obj._sent == []


def test_direct_without_name_in_followup_window_is_owner():
    rt = FakeRuntime()
    obj = _obj(rt)
    with mock.patch("main.get_name_gate_enabled", return_value=True), \
            mock.patch.object(type(obj), "_in_followup_window",
                              lambda _s: True):
        obj._flows_begin_turn("t4", "e as compras?",
                              _meta(wa_chat=OWNER_CHAT, wa_direct=True, wa_named=False))
    assert rt.begins[0]["trust"] == "owner"
    assert obj._sent == []


# ── (c) grupo/terceiro: untrusted como hoje, sem toque nenhum ───────────────
def test_third_party_chat_stays_untrusted_even_with_name():
    rt = FakeRuntime()
    obj = _obj(rt)
    obj._flows_begin_turn("t5", "Gino, manda um oi",
                          _meta(wa_chat=THIRD_CHAT, wa_direct=False,
                                wa_named=True))
    assert rt.begins[0]["trust"] == "untrusted"
    assert obj._sent == []                      # o terceiro não recebe dica


def test_group_never_opens_a_trusted_turn():
    rt = FakeRuntime()
    obj = _obj(rt)
    # Grupo: o core nunca o marca como direto; revalidando por is_direct,
    # o turno segue untrusted mesmo com o nome na mensagem.
    with mock.patch("core.whatsapp_watch.is_direct", return_value=False):
        obj._flows_begin_turn("t6", "Gino, que horas são no grupo?",
                              {"source": "whatsapp", "speak": False,
                               "reply_channel": "whatsapp",
                               "wa_account": "principal",
                               "wa_chat": GROUP_CHAT})
    assert rt.begins[0]["trust"] == "untrusted"
    assert obj._sent == []


def test_untrusted_orchestrator_turn_is_blocked():
    runtime = FlowsRuntime(_config(), speak=lambda _t: None,
                           log=lambda _m: None, start_expiry_thread=False)
    plan = runtime.start_turn("t7", "qualquer coisa",
                              speak_allowed=False, source="whatsapp",
                              trust="untrusted")
    assert plan is not None and plan.blocked and not plan.active


# ── (d) monitor_event continua untrusted, intocado ──────────────────────────
def test_monitor_event_is_always_untrusted():
    rt = FakeRuntime()
    obj = _obj(rt)
    obj._flows_monitor_event("email", "123456 é seu código")
    assert rt.monitors[0]["trust"] == "untrusted"


# ── fallback: sem flag explícita, a identificação é a do core ───────────────
def test_direct_falls_back_to_whatsapp_watch_is_direct():
    rt = FakeRuntime()
    obj = _obj(rt)
    meta = {"source": "whatsapp", "speak": False,
            "reply_channel": "whatsapp",
            "wa_account": "principal", "wa_chat": OWNER_CHAT}
    with mock.patch("core.whatsapp_watch.is_direct",
                    return_value=True) as patched:
        obj._flows_begin_turn("t8", "Gino, fala comigo", meta)
    patched.assert_called_once()
    assert rt.begins[0]["trust"] == "owner"


# ── (e) texto do WhatsApp NUNCA executa ação privilegiada, even owner ───────
def test_text_turn_with_owner_trust_never_executes_privileged_tool():
    """Mesmo trust=owner, a ação privilegiada para no portão de confirmação.

    É o mesmo runtime que recebe o turno de texto do dono no WhatsApp
    (speak_allowed=False, source="whatsapp"): o executor marca
    ``pending_confirm`` e nada roda; a autoridade final segue sendo a voz do
    dono via ``owner_decision``/``_owner_speaker_confirmed``.
    """
    calls: list[str] = []
    logs: list[str] = []
    runtime = FlowsRuntime(
        _config(), speak=lambda _t: None, log=logs.append,
        executor_runner=lambda tool, args: calls.append(tool) or {"ok": True},
        start_expiry_thread=False)
    plan = runtime.start_turn("t10", "reinicia o assistente",
                              speak_allowed=False, source="whatsapp",
                              trust="owner")
    assert plan is not None and plan.active
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if any("portão de ferramenta" in line for line in logs) or calls:
            break
        time.sleep(0.01)
    assert calls == []                          # nada privilegiado executou
    assert any("nada executado" in line for line in logs)
    runtime.finish("t10")
