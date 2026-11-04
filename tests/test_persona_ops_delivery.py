"""Arbítrio de entrega: dono ouve a persona, Gino recebe evento mudo."""
from __future__ import annotations

from unittest import mock

from core.persona_ops.delivery import (Addressee, SpeakerArbiter,
                                       decide_delivery)
from core.persona_ops.relay import (OwnerRelay, classify_owner_relay,
                                    find_addressed_persona)
from core.persona_ops.wiring import build

PERSONAS = [
    {"id": "aline", "name": "Aline", "aliases": ["Ally"]},
    {"id": "arnaldo", "name": "Arnaldo", "aliases": []},
]


def test_owner_addressee_speaks_with_the_persona_mouth():
    decision = decide_delivery("aline", "owner")
    assert decision.spoken is True
    assert decision.speaker == "persona"
    assert decision.channel == "persona_voice"
    assert decision.reason == "recado_para_o_dono_na_voz_dela"


def test_gino_addressee_is_an_internal_silent_event():
    decision = decide_delivery("aline", Addressee.GINO)
    assert decision.spoken is False
    assert decision.speaker == "none"
    assert decision.channel == "internal"
    assert decision.reason == "evento_interno_mudo_para_o_gino"


def test_whatsapp_channel_does_not_use_the_room_speaker():
    decision = decide_delivery("aline", "owner", channel="whatsapp")
    assert decision.channel == "persona_whatsapp"
    assert decision.spoken is False
    assert decision.speaker == "persona"


def test_speaker_arbiter_is_one_mouth_and_orders_by_priority():
    said: list[str] = []

    def speak(ticket):
        said.append(ticket.text)

    arbiter = SpeakerArbiter(speak)
    arbiter.submit("baixa", owner="gino", priority=1, drain=False)
    arbiter.submit("alta", owner="persona", persona="aline", priority=10,
                   drain=False)
    arbiter.drain()
    assert said == ["alta", "baixa"]
    assert arbiter.snapshot()["spoken"] == 2


def test_ops_delivers_owner_to_persona_and_silences_gino(tmp_path):
    said: list[str] = []
    ops = build(tmp_path, audit_path=tmp_path / "audit.jsonl",
                speak=lambda ticket: said.append(ticket.text))
    ops.note_ask("aline", "t1", "owner")
    owner = ops.deliver("aline", "Revisado, criador.", addressee="owner")
    assert owner.spoken is True
    assert said == ["Revisado, criador."]
    assert ops.addressee_for("aline") == "owner"

    ops.note_ask("aline", "t2", "gino")
    internal = ops.deliver("aline", "Resumo para o Gino.", addressee="gino")
    assert internal.spoken is False
    assert said == ["Revisado, criador."]  # a boca não falou o recado interno

    ops.clear_asks()
    assert ops.addressee_for("aline", default="owner") == "owner"


def test_ask_queue_is_fifo_per_persona(tmp_path):
    ops = build(tmp_path, speak=lambda ticket: None)
    ops.note_ask("aline", "t1", "owner")
    ops.note_ask("aline", "t2", "gino")
    assert ops.current_ask("aline")["turn_id"] == "t1"
    assert ops.finish_ask("aline")["turn_id"] == "t1"
    assert ops.current_ask("aline")["turn_id"] == "t2"
    assert ops.finish_ask("aline")["turn_id"] == "t2"
    assert ops.current_ask("aline") is None


def test_owner_relay_finds_the_vocative_and_never_calls_gino():
    assert find_addressed_persona("Aline, revisa o card",
                                  PERSONAS).slug == "aline"
    assert find_addressed_persona("fala com o Arnaldo sobre o player",
                                  PERSONAS).slug == "arnaldo"
    assert find_addressed_persona("chama a Ally", PERSONAS).slug == "aline"
    assert find_addressed_persona("quero falar com a Aline", PERSONAS).slug == "aline"
    assert find_addressed_persona("a Aline disse que o build quebrou",
                                  PERSONAS) is None
    plan = classify_owner_relay("Aline, revisa o card", PERSONAS)
    assert plan.is_relay is True
    assert plan.addressee is Addressee.OWNER
    assert plan.suppress_gino_speech is True


def test_owner_relay_asks_the_persona_and_does_not_speak_gino():
    said: list[str] = []
    arbiter = SpeakerArbiter(lambda ticket: said.append(ticket.text))
    gino = mock.Mock()
    relay = OwnerRelay(lambda slug, text: f"{slug}: {text}", arbiter,
                       gino_speak=gino)
    result = relay.deliver("revisa o card", "aline")
    assert result.delivered is True
    assert result.gino_spoken is False
    assert result.persona_spoken is True
    assert said == ["aline: revisa o card"]
    gino.assert_not_called()
