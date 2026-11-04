"""Rota livre da boca local: histórico + contexto, template só em falha."""
from __future__ import annotations

from core.flows.config import FlowsConfig
from core.flows.mouth import Mouth, SYS_FREE
from core.flows.router import Router


def _config(**kw) -> FlowsConfig:
    base = dict(enabled=True, mouth_enabled=True,
                mouth_brief_routes=("livre",))
    base.update(kw)
    return FlowsConfig(**base)


def _decision(text="o que você acha do filme?"):
    return Router().parse(text)


def test_free_route_prompt_carries_history_and_turn_context():
    seen = {}

    def caller(messages, max_tokens, **kw):
        seen["messages"] = messages
        seen["kw"] = kw
        return {"text": "Deixa eu organizar isso, criador.", "ttft_ms": 8.0,
                "first_sentence_ms": 8.0, "end_ms": 9.0, "error": None}

    mouth = Mouth(_config(), caller=caller, history=lambda: [
        {"role": "user", "text": "estou vendo um filme"},
        {"role": "assistant", "text": "Bom proveito, criador."},
    ])
    result = mouth.brief("o que você acha do filme?", _decision(),
                         context="rota=livre; alvo=o dono")
    assert result.sentence == "Deixa eu organizar isso, criador."
    assert result.fixed is False
    system = seen["messages"][0]
    assert system["role"] == "system" and system["content"] == SYS_FREE
    user = seen["messages"][1]["content"]
    assert "dono: estou vendo um filme" in user
    assert "Gino: Bom proveito" in user
    assert "rota=livre" in user
    assert seen["kw"]["model"] == _config().mouth_model


def test_free_route_error_falls_back_to_the_template():
    def caller(*_a, **_kw):
        return {"text": "", "ttft_ms": 0.0, "first_sentence_ms": 0.0,
                "end_ms": 1.0, "error": "timeout"}

    mouth = Mouth(_config(), caller=caller)
    result = mouth.brief("o que você acha do filme?", _decision())
    assert result.fixed is True
    assert result.sentence == _decision().draft


def test_free_route_guard_rejects_assertion_and_uses_template():
    def caller(*_a, **_kw):
        return {"text": "Spotify aberto, criador.", "ttft_ms": 1.0,
                "first_sentence_ms": 1.0, "end_ms": 2.0, "error": None}

    mouth = Mouth(_config(), caller=caller)
    result = mouth.brief("o que você acha do filme?", _decision())
    assert result.fixed is True
    assert result.guard_tripped
    assert result.sentence == _decision().draft
    assert mouth.guard_trips == 1


def test_confirmation_route_never_reaches_the_model():
    calls = []

    def caller(*_a, **_kw):
        calls.append(1)
        return {"text": "Confirmo reiniciar.", "ttft_ms": 1.0,
                "first_sentence_ms": 1.0, "end_ms": 2.0, "error": None}

    mouth = Mouth(_config(mouth_brief_routes=("livre", "confirma")), caller=caller)
    decision = Router().parse("reinicia o assistente aí")
    result = mouth.brief("reinicia o assistente aí", decision)
    assert calls == []                      # boca não confirma risco
    assert result.fixed is True
    assert "irreversível" in result.sentence.lower()


def test_history_failure_never_blocks_the_brief():
    def history():
        raise RuntimeError("memória fora")

    def caller(messages, *_a, **_kw):
        return {"text": "Já te digo, criador.", "ttft_ms": 1.0,
                "first_sentence_ms": 1.0, "end_ms": 2.0, "error": None}

    mouth = Mouth(_config(), caller=caller, history=history)
    result = mouth.brief("o que você acha do filme?", _decision())
    assert result.sentence == "Já te digo, criador."
