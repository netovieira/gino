"""Fluxo vivo dos grants por voz: integração em main.py.

Cobre o ciclo completo que os testes unitários do ``core/voice_grants`` não
exercitam sozinhos:

  1. ``_voice_grant_gate`` no fluxo de texto (UI/WhatsApp): frase do dono
     com sessão de senha aberta → grant criado e falado;
  2. ``_voice_grant_gate`` sem sessão de senha → recusa dura, nada criado;
  3. ``_voice_grant_allows`` no fluxo de autorização da bridge: visitante
     com grant ativo cobrindo o escopo → True; o dono → False (ele tem
     as janelas de risco normais); visitante sem grant → False;
  4. ``_voice_identity.set_recent`` espelha o veredito da fala para a trilha
     de grants (rótulo nominal, nunca id de voz);
  5. ciclo completo: criar → consultar → revogar → consultar de novo (nega).

Nada depende de microfone, GPU, WhatsApp emparelhado ou rede: os métodos
de main.py são chamados diretamente num objeto nu (``object.__new__``) com
doubles mínimos, como já faz ``test_voice_gate_main.py``.
"""
from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
from core import owner_decision
from core import voice_grants as vg
from core import voice_identity as vi


# ── doubles ──────────────────────────────────────────────────────────────────

class FakeUI:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_log(self, text: str) -> None:
        self.lines.append(str(text))


def _bare() -> main.JarvisLive:
    """JarvisLive nu, só com os atributos que os métodos de grant consultam."""
    obj = object.__new__(main.JarvisLive)
    obj.ui = FakeUI()
    obj._voice_grants_store = None
    obj._voice_train = None
    obj._owner_text_until = 0.0
    obj._owner_text_channel = ""
    obj._current_pcm = None
    obj._speaker = None          # sem cadastro de voz real
    return obj


@dataclass(frozen=True)
class _FakeVerdict:
    """Veredito mínimo para os caminhos que _voice_grant_allows consulta."""
    who: owner_decision.Who
    privileged: bool
    speaker: str | None
    score: float = 0.0
    reason: str = ""
    action: owner_decision.Action = owner_decision.Action.NORMAL
    normalized_score: float = 0.0
    threshold: float = 0.0
    normalized_threshold: float | None = None
    uncalibrated: bool = False
    profile_state: str = "ok"
    device: str = ""
    cohort: str = ""
    cohort_label: str = ""
    acoustic: str = ""
    speech: bool | None = None
    state: str = ""
    state_changed: bool = False
    threshold_source: str = ""
    threshold_reason: str = ""
    samples: int = 0


# ── (1) _voice_grant_gate: frase do dono com senha aberta → grant criado ────

def test_grant_gate_text_channel_creates_grant_with_password_session():
    obj = _bare()
    with tempfile.TemporaryDirectory() as tmp:
        store = vg.GrantStore(path=Path(tmp) / "grants.json")
        obj._voice_grants_store = store

        # Abre a sessão de texto (simula "senha: <correta>" digitado antes).
        import time
        obj._owner_text_until = time.monotonic() + 600.0

        said: list[str] = []
        obj._say_now = lambda text, **kw: said.append(text)

        handled = main.JarvisLive._voice_grant_gate(
            obj, "dê acesso total a Valmira", None)

        assert handled is True
        # O grant foi criado no store
        assert store.allows("valmira", vg.Scope(vg.SCOPE_ALL)) is True
        # A boca confirmou o acesso
        assert any("Acesso concedido" in s for s in said), said
        # O log registrou a concessão com o rótulo nominal
        assert any("grant concedido" in line for line in obj.ui.lines), obj.ui.lines


# ── (2) _voice_grant_gate: sem sessão de senha → recusa dura ────────────────

def test_grant_gate_without_password_session_refuses():
    obj = _bare()
    with tempfile.TemporaryDirectory() as tmp:
        store = vg.GrantStore(path=Path(tmp) / "grants.json")
        obj._voice_grants_store = store
        # _owner_text_until = 0.0 → sessão fechada

        said: list[str] = []
        obj._say_now = lambda text, **kw: said.append(text)

        handled = main.JarvisLive._voice_grant_gate(
            obj, "dê acesso total a Valmira", None)

        assert handled is True          # a frase foi consumida (recusa falada)
        assert store.allows("valmira", vg.Scope(vg.SCOPE_ALL)) is False
        assert any("decisão do dono" in s for s in said), said


# ── (3a) _voice_grant_allows: visitante com grant ativo → True ───────────────

def test_grant_allows_visitant_with_active_grant():
    obj = _bare()
    with tempfile.TemporaryDirectory() as tmp:
        store = vg.GrantStore(path=Path(tmp) / "grants.json")
        obj._voice_grants_store = store
        PRIV = {"owner_ok": True, "confirmed_by": "senha_digitada"}
        store.add_grant("valmira", vg.Scope(vg.SCOPE_EMAIL), **PRIV)

        # _current_pcm precisa estar setado (o fluxo vivo seta em _on_groq_text)
        obj._current_pcm = b"\x00\x01\x02"

        # Veredito: NÃO é o dono, é a visitante "valmira"
        fake_verdict = _FakeVerdict(
            who=owner_decision.Who.OTHER,
            privileged=False,
            speaker="valmira")
        obj._owner_verdict = lambda pcm, device=None, privileged=False: (
            fake_verdict, "")

        result = main.JarvisLive._voice_grant_allows(
            obj, "send_email", {"to": "x@y.z"})

        assert result is True
        # O log registrou a cobertura do grant
        assert any("grant ativo" in line for line in obj.ui.lines), obj.ui.lines


# ── (3b) _voice_grant_allows: o dono NÃO passa por grant (janelas normais) ──

def test_grant_allows_owner_falls_through():
    obj = _bare()
    with tempfile.TemporaryDirectory() as tmp:
        store = vg.GrantStore(path=Path(tmp) / "grants.json")
        obj._voice_grants_store = store
        obj._current_pcm = b"\x00\x01\x02"

        fake_verdict = _FakeVerdict(
            who=owner_decision.Who.OWNER,
            privileged=True,
            speaker="dono")
        obj._owner_verdict = lambda pcm, device=None, privileged=False: (
            fake_verdict, "")

        # O dono não tem grant, mas também não precisa: o retorno é False
        # porque a autorização dele passa pelas janelas de risco normais.
        result = main.JarvisLive._voice_grant_allows(
            obj, "send_email", {"to": "x@y.z"})

        assert result is False


# ── (3c) _voice_grant_allows: visitante sem grant → negação ─────────────────

def test_grant_allows_visitant_without_grant_denied():
    obj = _bare()
    with tempfile.TemporaryDirectory() as tmp:
        store = vg.GrantStore(path=Path(tmp) / "grants.json")
        obj._voice_grants_store = store
        obj._current_pcm = b"\x00\x01\x02"

        fake_verdict = _FakeVerdict(
            who=owner_decision.Who.OTHER,
            privileged=False,
            speaker="desconhecido")
        obj._owner_verdict = lambda pcm, device=None, privileged=False: (
            fake_verdict, "")

        result = main.JarvisLive._voice_grant_allows(
            obj, "send_email", {"to": "x@y.z"})

        assert result is False


# ── (3d) _voice_grant_allows: escopo de projeto com nome ────────────────────

def test_grant_allows_project_scope_with_name():
    obj = _bare()
    with tempfile.TemporaryDirectory() as tmp:
        store = vg.GrantStore(path=Path(tmp) / "grants.json")
        obj._voice_grants_store = store
        PRIV = {"owner_ok": True, "confirmed_by": "voiceprint_owner"}
        store.add_grant("valmira", vg.Scope(vg.SCOPE_PROJECT, "site2"), **PRIV)

        obj._current_pcm = b"\x00\x01\x02"
        fake_verdict = _FakeVerdict(
            who=owner_decision.Who.OTHER, privileged=False, speaker="valmira")
        obj._owner_verdict = lambda pcm, device=None, privileged=False: (
            fake_verdict, "")

        # Pedido no projeto correto → grant cobre
        assert main.JarvisLive._voice_grant_allows(
            obj, "qoder_control", {"project": "/home/valmira/site2"}) is True
        # Pedido noutro projeto → grant não cobre
        assert main.JarvisLive._voice_grant_allows(
            obj, "qoder_control", {"project": "/home/valmira/outro"}) is False


# ── (4) voice_identity.set_recent espelha o veredito para a trilha ──────────

def test_voice_identity_set_recent_captures_verdict_label():
    vi.clear()
    vi.set_recent(
        who="outro", person="valmira", privileged=False,
        confirmed_by="", score=0.72, device="mic-sala",
        origin=vi.ORIGIN_MIC)

    snap = vi.recent()
    assert snap["person"] == "valmira"
    assert snap["who"] == "outro"
    assert snap["privileged"] is False
    # describe devolve o rótulo nominal, nunca id de voz
    desc = vi.describe_recent()
    assert "valmira" in desc
    import re
    assert not re.search(r"\d{5,}", desc), "descrição não pode conter id numérico"


def test_voice_identity_describe_owner():
    vi.clear()
    vi.set_recent(
        who="owner", person="", privileged=True,
        confirmed_by="voiceprint_owner", score=0.95)
    desc = vi.describe_recent()
    assert "quem=dono" in desc
    assert "origem=owner" in desc


# ── (5) ciclo completo: criar → consultar → revogar → negar ─────────────────

def test_full_live_cycle_create_query_revoke_deny():
    obj = _bare()
    with tempfile.TemporaryDirectory() as tmp:
        store = vg.GrantStore(path=Path(tmp) / "grants.json")
        obj._voice_grants_store = store

        # Abre sessão de senha
        import time
        obj._owner_text_until = time.monotonic() + 600.0
        obj._say_now = lambda text, **kw: None

        # 1. Cria grant pela frase do dono
        main.JarvisLive._voice_grant_gate(
            obj, "dê acesso total a Valmira", None)
        assert store.allows("valmira", vg.Scope(vg.SCOPE_EMAIL)) is True

        # 2. Consulta via _voice_grant_allows (visitante com grant)
        obj._current_pcm = b"\x00\x01\x02"
        fake_verdict = _FakeVerdict(
            who=owner_decision.Who.OTHER, privileged=False, speaker="valmira")
        obj._owner_verdict = lambda pcm, device=None, privileged=False: (
            fake_verdict, "")
        assert main.JarvisLive._voice_grant_allows(
            obj, "send_email", {}) is True

        # 3. Revoga pela frase do dono
        main.JarvisLive._voice_grant_gate(
            obj, "revogue o acesso de Valmira", None)
        assert store.allows("valmira", vg.Scope(vg.SCOPE_EMAIL)) is False

        # 4. Consulta de novo → negação
        assert main.JarvisLive._voice_grant_allows(
            obj, "send_email", {}) is False


# ── (6) nome composto: a correção de person_key vs _fold no fluxo vivo ──────

def test_full_live_cycle_multiword_name():
    """Grant criado com nome composto deve ser consultado e revogado corretamente."""
    obj = _bare()
    with tempfile.TemporaryDirectory() as tmp:
        store = vg.GrantStore(path=Path(tmp) / "grants.json")
        obj._voice_grants_store = store

        import time
        obj._owner_text_until = time.monotonic() + 600.0
        obj._say_now = lambda text, **kw: None

        # Cria grant para nome composto
        main.JarvisLive._voice_grant_gate(
            obj, "dê acesso total a Maria Silva", None)
        assert store.allows("maria-silva", vg.Scope(vg.SCOPE_ALL)) is True

        # Consulta com o mesmo nome composto
        obj._current_pcm = b"\x00\x01\x02"
        fake_verdict = _FakeVerdict(
            who=owner_decision.Who.OTHER, privileged=False,
            speaker="Maria Silva")
        obj._owner_verdict = lambda pcm, device=None, privileged=False: (
            fake_verdict, "")
        assert main.JarvisLive._voice_grant_allows(
            obj, "send_email", {}) is True

        # Revoga
        main.JarvisLive._voice_grant_gate(
            obj, "revogue o acesso de Maria Silva", None)
        assert store.allows("maria-silva", vg.Scope(vg.SCOPE_ALL)) is False


# ── (7) _voice_grant_scope_for_tool: mapeamento de ferramenta → escopo ──────

def test_grant_scope_for_tool_maps_correctly():
    obj = _bare()
    scope = main.JarvisLive._voice_grant_scope_for_tool(
        obj, "send_email", {})
    assert scope is not None
    assert scope.kind == vg.SCOPE_EMAIL

    scope = main.JarvisLive._voice_grant_scope_for_tool(
        obj, "whatsapp_send", {})
    assert scope is not None
    assert scope.kind == vg.SCOPE_WHATSAPP

    scope = main.JarvisLive._voice_grant_scope_for_tool(
        obj, "qoder_control", {"project": "/home/user/site2"})
    assert scope is not None
    assert scope.kind == vg.SCOPE_PROJECT
    assert scope.target == "site2"

    # Ferramenta sem categoria conhecida → None (negação)
    scope = main.JarvisLive._voice_grant_scope_for_tool(
        obj, "home_control", {})
    assert scope is None
