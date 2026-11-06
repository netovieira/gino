"""Store de grants por pessoa + parser da frase de elevação (FLUXOS.md ramificação *).

Valida o requisito 3 do contrato:
  (a) grant só nasce com veredito privilegiado (owner_ok + confirmed_by de dono);
  (b) escopo nominal (pessoa → projeto/categoria) com expiração e revogação;
  (c) padrão é NEGAÇÃO — sem grant ativo, allows devolve False;
  (d) escrita de perfil de voz está FORA do alcance de qualquer grant;
  (e) log/fala só com rótulo nominal — nunca id numérico/dado pessoal.

Tudo roda contra um GrantStore em tmp_path; nada toca o memory/voice_grants.json real.
"""
from __future__ import annotations

import time

import pytest

from core import voice_grants as vg


@pytest.fixture()
def store(tmp_path):
    return vg.GrantStore(path=tmp_path / "voice_grants.json")


OWNER = "valmira"
OTHER = {"owner_ok": False, "confirmed_by": "voiceprint_other"}
PRIV = {"owner_ok": True, "confirmed_by": "voiceprint_owner"}
SENHA = {"owner_ok": True, "confirmed_by": "senha_digitada"}


# ── parser das duas frases do contrato ─────────────────────────────────────
class TestParser:
    def test_full_access_phrase(self):
        plan = vg.parse_grant_phrase("dê acesso total a Valmira")
        assert plan is not None
        assert plan["action"] == "grant_full"
        assert plan["person"] == "valmira"
        kinds = {s.kind for s in plan["scopes"]}
        assert kinds == {vg.SCOPE_ALL}

    def test_scoped_phrase_with_project(self):
        plan = vg.parse_grant_phrase(
            "pode responder e executar tudo que o usuário Valmira pedir "
            "no projeto SITE2")
        assert plan is not None
        assert plan["action"] == "grant_scoped"
        assert plan["person"] == "valmira"
        kinds = {s.kind for s in plan["scopes"]}
        assert vg.SCOPE_READ in kinds
        assert vg.SCOPE_EXECUTE in kinds
        proj = [s for s in plan["scopes"] if s.kind == vg.SCOPE_PROJECT]
        assert proj and proj[0].target == "site2"

    def test_revoke_phrases(self):
        assert vg.parse_grant_phrase(
            "revogue o acesso de Valmira")["action"] == "revoke"
        assert vg.parse_grant_phrase(
            "revogue todos os acessos")["action"] == "revoke_all"

    def test_plain_speech_is_not_a_grant_phrase(self):
        # "sim" falado sozinho NÃO é frase de elevação — nada é criado.
        assert vg.parse_grant_phrase("sim") is None
        assert vg.parse_grant_phrase("bom dia") is None
        assert vg.parse_grant_phrase("como está o tempo?") is None


# ── (a) criação exige veredito privilegiado ─────────────────────────────────
class TestCreationRequiresOwnerVerdict:
    def test_refused_without_owner_verdict(self, store):
        res = store.add_grant(OWNER, vg.Scope(vg.SCOPE_ALL), **OTHER)
        assert res["granted"] is False
        assert store.allows(OWNER, vg.Scope(vg.SCOPE_ALL)) is False

    def test_refused_with_bogus_confirmed_by(self, store):
        res = store.add_grant(
            OWNER, vg.Scope(vg.SCOPE_ALL),
            owner_ok=True, confirmed_by="voz_duvidosa")
        assert res["granted"] is False

    def test_granted_with_privileged_voiceprint(self, store):
        res = store.add_grant(OWNER, vg.Scope(vg.SCOPE_ALL), **PRIV)
        assert res["granted"] is True
        assert store.allows(OWNER, vg.Scope(vg.SCOPE_ALL)) is True

    def test_granted_with_typed_password(self, store):
        res = store.add_grant(
            OWNER, vg.Scope(vg.SCOPE_EMAIL), **SENHA)
        assert res["granted"] is True


# ── (d) escrita do perfil de voz fora do alcance de qualquer grant ─────────
class TestProfileWriteNeverGrantable:
    def test_profile_write_scope_refused_even_for_owner(self, store):
        res = store.add_grant(
            OWNER, vg.Scope(vg.PROFILE_WRITE_SCOPE), **PRIV)
        assert res["granted"] is False
        assert "não é concedível" in res["why"]

    def test_kind_containing_voz_refused(self, store):
        res = store.add_grant(
            OWNER, vg.Scope("perfil_de_voz_escrita"), **PRIV)
        assert res["granted"] is False


# ── (b)+(c) escopo nominal, expiração, padrão negação ──────────────────────
class TestScopeAndExpiry:
    def test_project_grant_is_nominal_scoped(self, store):
        store.add_grant(
            OWNER, vg.Scope(vg.SCOPE_PROJECT, "site2"), **PRIV)
        # mesmo escopo noutro projeto → negação
        assert store.allows(
            OWNER, vg.Scope(vg.SCOPE_PROJECT, "site2")) is True
        assert store.allows(
            OWNER, vg.Scope(vg.SCOPE_PROJECT, "outro")) is False

    def test_full_grant_covers_categories(self, store):
        store.add_grant(OWNER, vg.Scope(vg.SCOPE_ALL), **PRIV)
        assert store.allows(OWNER, vg.Scope(vg.SCOPE_EMAIL)) is True
        assert store.allows(OWNER, vg.Scope(vg.SCOPE_WHATSAPP)) is True
        assert store.allows(
            OWNER, vg.Scope(vg.SCOPE_PROJECT, "qualquer")) is True

    def test_read_execute_are_not_super_scopes(self, store):
        store.add_grant(OWNER, vg.Scope(vg.SCOPE_READ), **PRIV)
        # leitura não cobre email nem whatsapp nem projeto
        assert store.allows(OWNER, vg.Scope(vg.SCOPE_EMAIL)) is False
        assert store.allows(OWNER, vg.Scope(vg.SCOPE_WHATSAPP)) is False
        assert store.allows(
            OWNER, vg.Scope(vg.SCOPE_PROJECT, "site2")) is False

    def test_default_is_denial(self, store):
        assert store.allows(
            OWNER, vg.Scope(vg.SCOPE_PROJECT, "site2")) is False

    def test_project_without_name_refused(self, store):
        res = store.add_grant(
            OWNER, vg.Scope(vg.SCOPE_PROJECT), **PRIV)
        assert res["granted"] is False

    def test_grant_expires(self, store):
        now = time.monotonic()
        store.add_grant(
            OWNER, vg.Scope(vg.SCOPE_EMAIL),
            owner_ok=True, confirmed_by="senha_digitada",
            ttl_seconds=5.0, now=now)
        assert store.allows(
            OWNER, vg.Scope(vg.SCOPE_EMAIL), now=now) is True
        assert store.allows(
            OWNER, vg.Scope(vg.SCOPE_EMAIL), now=now + 6.0) is False


# ── revogação (b, falada) ───────────────────────────────────────────────────
class TestRevocation:
    def test_revoke_single_scope(self, store):
        store.add_grant(OWNER, vg.Scope(vg.SCOPE_EMAIL), **PRIV)
        store.add_grant(
            OWNER, vg.Scope(vg.SCOPE_WHATSAPP), **PRIV)
        removed = store.revoke(OWNER, vg.Scope(vg.SCOPE_EMAIL))
        assert removed == 1
        assert store.allows(OWNER, vg.Scope(vg.SCOPE_EMAIL)) is False
        assert store.allows(OWNER, vg.Scope(vg.SCOPE_WHATSAPP)) is True

    def test_revoke_all_for_person(self, store):
        store.add_grant(OWNER, vg.Scope(vg.SCOPE_EMAIL), **PRIV)
        store.add_grant(
            OWNER, vg.Scope(vg.SCOPE_PROJECT, "site2"), **PRIV)
        removed = store.revoke(OWNER)
        assert removed == 2
        assert store.active(OWNER) == []

    def test_revoke_all_scope(self, store):
        store.add_grant(OWNER, vg.Scope(vg.SCOPE_EMAIL), **PRIV)
        store.add_grant("outra", vg.Scope(vg.SCOPE_EMAIL), **PRIV)
        removed = store.revoke(None, None) if False else None
        # revoke_all remove tudo de todos os rótulos
        plan = vg.parse_grant_phrase("revogue todos os acessos")
        assert plan["action"] == "revoke_all"
        res = vg.grant_from_phrase(
            "revogue todos os acessos", owner_ok=True,
            confirmed_by="senha_digitada", store=store)
        assert res["handled"] is True
        assert store.load() == []


# ── (e) rótulo, nunca dado ─────────────────────────────────────────────────
class TestLabelNotData:
    def test_person_key_rejects_long_digit_runs(self):
        assert vg.person_key("5531999998888") == ""
        assert vg.person_key("fulano@gmail.com") == ""

    def test_person_key_accepts_nominal(self):
        assert vg.person_key("Valmira") == "valmira"

    def test_describe_is_nominal_only(self, store):
        store.add_grant(
            OWNER, vg.Scope(vg.SCOPE_PROJECT, "site2"), **PRIV)
        desc = store.describe(OWNER)
        assert "valmira" in desc
        assert "projeto/site2" in desc
        # nenhum número com 5+ dígitos (telefone/CPF) na descrição
        import re
        assert not re.search(r"\d{5,}", desc)


# ── "sim" falado sozinho nunca cria grant ──────────────────────────────────
class TestSpokenSimIsNotCredential:
    def test_sim_does_not_create(self, store):
        # grant_from_phrase com frase comum → None (nada tratado, nada criado)
        res = vg.grant_from_phrase(
            "sim", owner_ok=True, confirmed_by="senha_digitada",
            store=store)
        assert res is None
        assert store.allows(OWNER, vg.Scope(vg.SCOPE_ALL)) is False
