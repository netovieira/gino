"""Guarda na boca: asserção sem fato cai em template determinístico."""
from __future__ import annotations

from core.flow_ops.mouth_guard import (CANCELLED_TEMPLATE, NEUTRAL_TEMPLATE,
                                       SIMULATED_TEMPLATE, MouthWriter,
                                       speak_guarded, template_for)

FACT_SPOTIFY = {"ok": True, "spoken": "Spotify aberto", "assertion": True,
                "simulated": False}


def test_model_sentence_with_assertion_and_no_fact_is_rejected():
    speech = speak_guarded("Abri o WhatsApp, criador.", [])
    assert speech.accepted is False
    assert speech.template is True
    assert speech.text == NEUTRAL_TEMPLATE
    assert "asserção" in speech.reason


def test_authorized_fact_allows_the_assertion():
    speech = speak_guarded("Spotify aberto, chefe.", [FACT_SPOTIFY])
    assert speech.accepted is True
    assert speech.template is False
    assert speech.text == "Spotify aberto, chefe."


def test_simulated_fact_never_authorizes():
    simulated = {"ok": True, "spoken": "Spotify aberto", "simulated": True}
    speech = speak_guarded("Spotify aberto.", [simulated])
    assert speech.accepted is False
    assert speech.text == SIMULATED_TEMPLATE


def test_fact_with_assertion_false_does_not_authorize():
    fact = {"ok": True, "assertion": False, "spoken": "nada confirmado"}
    speech = speak_guarded("Deu certo.", [fact])
    assert speech.accepted is False
    assert speech.text == template_for([fact])


def test_invented_number_falls_back_to_template():
    speech = speak_guarded("Spotify aberto e volume em 40%.", [FACT_SPOTIFY])
    assert speech.accepted is False
    assert "número inventado" in speech.reason
    assert speech.text == "Spotify aberto."


def test_number_present_in_fact_is_allowed():
    fact = {"ok": True, "assertion": True, "spoken": "volume em 40 por cento"}
    speech = speak_guarded("Volume em 40 por cento.", [fact])
    assert speech.accepted is True


def test_assertion_not_matching_the_returned_fact_is_rejected():
    speech = speak_guarded("Abri o WhatsApp.", [FACT_SPOTIFY])
    assert speech.accepted is False
    assert speech.text == "Spotify aberto."


def test_pending_confirmation_requires_exact_phrase():
    fact = {"ok": True, "assertion": False,
            "pending_confirm": "confirmo reiniciar o assistente"}
    wrong = speak_guarded("Confirma aí, criador.", [fact])
    assert wrong.accepted is False
    assert wrong.text == ('Diga "confirmo reiniciar o assistente" se for isso '
                          'mesmo.')
    right = speak_guarded(
        'Diga "confirmo reiniciar o assistente" se for isso mesmo.', [fact])
    assert right.accepted is True


def test_cancelled_turn_never_accepts_success_phrase():
    fact = {"ok": False, "cancelled": True, "spoken": "cancelado",
            "assertion": False}
    speech = speak_guarded("Reiniciei o assistente.", [fact])
    assert speech.accepted is False
    assert speech.text == CANCELLED_TEMPLATE


def test_tool_name_leak_is_rejected():
    speech = speak_guarded("Chamei media_control para isso.", [])
    assert speech.accepted is False
    assert "vazou" in speech.reason


def test_brief_phase_rejects_assertion_and_numbers():
    writer = MouthWriter()
    speech = writer.write("Abri o WhatsApp em 2 segundos.", [], phase="brief",
                          fallback="Conferindo o WhatsApp.")
    assert speech.accepted is False
    assert speech.text == "Conferindo o WhatsApp."
    good = writer.write("Conferindo o pedido.", [], phase="brief")
    assert good.accepted is True


def test_template_is_deterministic_and_never_asserts_success():
    facts = [{"ok": False, "spoken": "a ferramenta falhou; nada confirmado",
              "assertion": False}]
    first = speak_guarded("", facts)
    second = speak_guarded("", facts)
    assert first.text == second.text
    assert first.text == template_for(facts)
    assert "aberto" not in first.text


def test_say_only_speaks_after_guard_and_speaks_template_on_rejection():
    spoken: list[str] = []
    writer = MouthWriter()
    result = writer.say("Deu tudo certo.", [], spoken.append)
    assert result.accepted is False
    assert spoken == [NEUTRAL_TEMPLATE]
    assert writer.rejected == 1

    result = writer.say("Spotify aberto.", [FACT_SPOTIFY], spoken.append)
    assert result.accepted is True
    assert spoken[-1] == "Spotify aberto."


def test_unknown_phase_is_a_programming_error():
    try:
        MouthWriter().write("oi", [], phase="inventada")
    except ValueError as exc:
        assert "inventada" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("fase desconhecida deveria levantar ValueError")
