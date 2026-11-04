"""Guard anti-asserção da boca: lei (a) e lei (b) em código."""
from __future__ import annotations

from core.flows.config import FlowsConfig
from core.flows.guard import ensure_facts, guard, template_for
from core.flows.mouth import Mouth
from core.flows.router import Decision


def _fact(spoken: str, **kw):
    fact = {"spoken": spoken, "ok": True, "assertion": True}
    fact.update(kw)
    return fact


def test_brief_rejects_assertion():
    ok, why = guard("Spotify aberto, criador.", None, "brief")
    assert not ok
    assert "asserção" in why


def test_brief_rejects_number():
    ok, why = guard("São 15 horas.", None, "brief")
    assert not ok
    assert "número" in why


def test_brief_rejects_tool_leak():
    ok, why = guard("Chamando media_control.", None, "brief")
    assert not ok
    assert "ferramenta" in why


def test_complement_rejects_assertion_without_fact():
    ok, why = guard("Reiniciei o assistente.", [], "complement")
    assert not ok
    assert "sem fato" in why


def test_simulated_fact_does_not_authorize_assertion():
    simulated = _fact("Spotify aberto", assertion=False, simulated=True)
    ok, why = guard("Spotify aberto.", [simulated], "complement")
    assert not ok


def test_complement_accepts_fact_and_its_number():
    facts = [_fact("volume em 40%")]
    ok, why = guard("Volume em 40%.", facts, "complement")
    assert ok, why


def test_complement_rejects_invented_number():
    facts = [_fact("volume em 40%")]
    ok, why = guard("Volume em 70%.", facts, "complement")
    assert not ok
    assert "número" in why


def test_pending_confirmation_must_be_spoken_exactly():
    facts = [{"spoken": "só falta a confirmação", "ok": True,
              "assertion": False, "pending_confirm":
              "confirmo reiniciar o assistente"}]
    ok, why = guard("Reiniciar é irreversível.", facts, "complement")
    assert not ok
    ok, why = guard('Diga "confirmo reiniciar o assistente" se for isso mesmo.',
                    facts, "complement")
    assert ok, why


def test_template_is_deterministic():
    assert template_for([_fact("Spotify aberto")]) == "Spotify aberto."
    assert template_for([], "Conferindo a hora.") == "Conferindo a hora."
    assert "Nada foi executado" in template_for([{"cancelled": True}])


def test_ensure_facts_appends_omitted_fact():
    sentence, missing = ensure_facts(
        "Spotify aberto.", [_fact("Spotify aberto"), _fact("volume em 40%")])
    assert missing == ["volume em 40%"]
    assert "volume em 40%" in sentence


def _decision(draft="Conferindo a hora.") -> Decision:
    return Decision(intent="lookup", rule="hora", draft=draft,
                    target="a hora", brief_label="consultar a hora")


def test_mouth_falls_back_when_model_asserts_in_brief():
    def caller(messages, max_tokens, **kwargs):
        return {"text": "Spotify já aberto.", "ttft_ms": 1,
                "first_sentence_ms": 2, "end_ms": 3, "error": None}

    mouth = Mouth(FlowsConfig(enabled=True, mouth_enabled=True), caller=caller)
    result = mouth.brief("que horas são?", _decision())
    assert result.sentence == "Conferindo a hora."
    assert result.guard_tripped
    assert mouth.guard_trips == 1


def test_mouth_uses_safe_model_sentence():
    def caller(messages, max_tokens, **kwargs):
        return {"text": "Conferindo a hora.", "ttft_ms": 1,
                "first_sentence_ms": 2, "end_ms": 3, "error": None}

    mouth = Mouth(FlowsConfig(enabled=True, mouth_enabled=True), caller=caller)
    result = mouth.brief("que horas são?", _decision())
    assert result.sentence == "Conferindo a hora."
    assert not result.guard_tripped


def test_mouth_disabled_never_calls_model():
    calls = []

    def caller(*args, **kwargs):
        calls.append(args)
        return {"text": "x", "error": None}

    mouth = Mouth(FlowsConfig(enabled=True, mouth_enabled=False), caller=caller)
    result = mouth.brief("que horas são?", _decision())
    assert calls == []
    assert result.sentence == "Conferindo a hora."


def test_complement_guard_rejects_invented_sentence():
    def caller(messages, max_tokens, **kwargs):
        return {"text": "Volume em 90%.", "ttft_ms": 1,
                "first_sentence_ms": 2, "end_ms": 3, "error": None}

    mouth = Mouth(FlowsConfig(enabled=True, mouth_enabled=True), caller=caller)
    result = mouth.complement("abaixa o som", _decision("Ajustando."),
                              [_fact("volume em 40%")])
    assert result.sentence.startswith("Volume em 40%")
    assert result.guard_tripped
