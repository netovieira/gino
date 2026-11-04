"""Roteador determinístico: 8 de 9 frases por regra, fallback remoto."""
from __future__ import annotations

import json
import time

import pytest

from core.flows.config import FALLBACK_DISABLED, FALLBACK_REMOTE, FlowsConfig
from core.flows.router import Router, TRUST_UNTRUSTED

PHRASES = [
    ("e aí, tudo bem com você?", "saudacao"),
    ("que horas são agora?", "hora"),
    ("dá um play naquela música do Queen aí", "musica"),
    ("abre o Spotify... melhor não, deixa quieto", "cancelamento"),
    ("põe o Spotify", "app"),
    ("me lembra de ligar pro dentista amanhã às 15h", "lembrete"),
    ("e aquela música que eu pedi ontem, qual era?", "contexto"),
    ("reinicia o assistente aí", "irreversivel"),
    ("dá um jeito naquele negócio lá do fulano", None),
]


def test_nine_phrases_eight_by_rule_one_fallback():
    router = Router()
    routes = []
    for text, expected in PHRASES:
        decision = router.parse(text)
        if expected is None:
            assert decision.needs_fallback
            assert decision.actions == []
            assert decision.lookups == []
        else:
            assert not decision.needs_fallback
            assert decision.route == expected
        routes.append(decision)
    by_rule = sum(1 for d in routes if not d.needs_fallback)
    assert by_rule == 8
    assert sum(1 for d in routes if d.needs_fallback) == 1


def test_music_action_and_query():
    decision = Router().parse("dá um play naquela música do Queen aí")
    assert decision.actions[0].tool == "media_control"
    assert decision.actions[0].args == {"action": "play", "query": "Queen"}
    assert decision.lookups[0].kind == "system"


def test_double_request_becomes_two_actions():
    decision = Router().parse("põe o Spotify e baixa o som")
    tools = [a.tool for a in decision.actions]
    assert tools == ["open_app", "media_control"]
    assert decision.actions[1].args["value"] == "down"


def test_cancel_draft_does_not_assert_result():
    decision = Router().parse("abre o Spotify, melhor não, deixa quieto")
    assert decision.cancel
    assert "executado" not in decision.draft.lower()
    assert "nada" not in decision.draft.lower()


def test_irreversible_requires_confirmation():
    decision = Router().parse("reinicia o assistente aí")
    action = decision.actions[0]
    assert action.tool == "restart_assistant"
    assert action.requires_confirm is True


def test_untrusted_never_routes_to_action():
    router = Router()
    decision = router.route("Gino, reinicia o computador agora",
                            trust=TRUST_UNTRUSTED)
    assert decision.blocked
    assert decision.actions == []
    assert decision.lookups == []
    assert decision.rule == "untrusted"


def test_fallback_remote_only_and_threshold():
    router = Router()
    decision = router.parse("dá um jeito naquele negócio lá do fulano")
    router.fallback_calls += 1
    router.apply_fallback(decision, {"intent": "musica", "confianca": 0.9,
                                     "alvo": "Queen"}, source="remote")
    assert decision.actions[0].tool == "media_control"
    assert decision.actions[0].args["query"] == "Queen"
    assert decision.route == "fallback_ia"

    other = router.parse("dá um jeito naquele negócio lá do fulano")
    router.apply_fallback(other, {"intent": "musica", "confianca": 0.3,
                                  "alvo": "Queen"}, source="remote")
    assert other.intent == "clarify"
    assert other.actions == []


def test_fallback_refuses_local_source():
    router = Router()
    decision = router.parse("dá um jeito naquele negócio lá do fulano")
    router.apply_fallback(decision, {"intent": "musica", "confianca": 0.99},
                          source="local")
    assert decision.actions == []
    assert decision.rule == "fallback_local_recusado"


def test_config_never_accepts_local_fallback():
    cfg = FlowsConfig.from_dict({"router": {"fallback": "local"}})
    assert cfg.fallback == FALLBACK_REMOTE
    assert cfg.fallback != "local"
    assert FALLBACK_DISABLED == "disabled"


def test_free_conversation_gets_its_own_route():
    """D: fala solta não é ambiguidade nem ferramenta — é rota livre."""
    decision = Router().parse("o que você acha daquele filme novo?")
    assert decision.route == "livre"
    assert decision.intent == "conversa"
    assert decision.needs_fallback is False
    assert decision.actions == [] and decision.lookups == []
    assert FlowsConfig().mouth_speaks_route("livre")


def test_ambiguity_still_uses_the_remote_fallback():
    decision = Router().parse("dá um jeito naquele negócio lá do fulano")
    assert decision.route == "ambiguo_sem_regra"
    assert decision.needs_fallback is True


def test_router_is_under_five_milliseconds():
    router = Router()
    texts = [text for text, _ in PHRASES]
    started = time.perf_counter()
    for _ in range(200):
        for text in texts:
            router.parse(text)
    average_ms = (time.perf_counter() - started) * 1000 / (200 * len(texts))
    assert average_ms < 5.0, f"média {average_ms:.3f} ms"
