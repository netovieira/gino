"""Contrato versionado entre as duas metades: ida, volta e recusa clara."""
from __future__ import annotations

import json

import pytest

from core.flow_ops.envelope import (SCHEMA_VERSION, Envelope, SchemaError,
                                     decode, decode_many, encode, make)


def test_roundtrip_preserves_type_id_and_data():
    event = make("task", id="t1", worker="tool", tool="media_control",
                 args={"q": "Queen"})
    line = encode(event)
    assert "\n" not in line and "\r" not in line
    back = decode(line)
    assert back.type == "task"
    assert back.id == "t1"
    assert back.data["tool"] == "media_control"
    assert back.v == SCHEMA_VERSION
    assert back.ts == pytest.approx(event.ts)


def test_newer_schema_is_rejected_with_clear_error():
    line = json.dumps({"v": 3, "type": "task", "id": "t1",
                       "ts": 1.0, "data": {}})
    with pytest.raises(SchemaError) as info:
        decode(line)
    error = info.value
    text = str(error)
    assert error.received == 3
    assert error.supported == (2,)
    assert error.event_type == "task"
    assert "3" in text and "task" in text and "aceita [2]" in text
    assert "mais nova" in text


def test_older_schema_is_rejected_too():
    line = json.dumps({"v": 1, "type": "result", "id": "k1",
                       "ts": 1.0, "data": {}})
    with pytest.raises(SchemaError) as info:
        decode(line)
    assert info.value.received == 1
    assert "result" in str(info.value)


def test_missing_version_is_rejected():
    with pytest.raises(SchemaError) as info:
        decode(json.dumps({"type": "brief", "id": "t1"}))
    assert info.value.received is None
    assert "sem versão" in str(info.value)


def test_decode_accepts_explicit_compatible_version_set():
    line = json.dumps({"v": 1, "type": "brief", "id": "t1",
                       "ts": 1.0, "data": {"kind": "intent"}})
    event = decode(line, accepted=(1, 2))
    assert event.v == 1
    assert event.data["kind"] == "intent"


def test_two_events_in_one_line_are_refused():
    payload = json.dumps({"v": 2, "type": "brief", "id": "t1", "data": {}})
    with pytest.raises(SchemaError):
        decode(payload + "\n" + payload)


def test_data_must_be_object():
    line = json.dumps({"v": 2, "type": "task", "id": "t1", "data": [1, 2]})
    with pytest.raises(SchemaError) as info:
        decode(line)
    assert "data" in str(info.value)


def test_envelope_without_type_is_refused():
    with pytest.raises(SchemaError):
        Envelope(type="")


def test_decode_many_numbers_bad_line():
    good = encode(make("brief", id="t1", kind="intent"))
    bad = json.dumps({"v": 9, "type": "task", "id": "t2", "data": {}})
    with pytest.raises(SchemaError) as info:
        decode_many([good, bad])
    assert info.value.line_no == 2


def test_error_serializes_for_log_without_secret():
    with pytest.raises(SchemaError) as info:
        decode(json.dumps({"v": 7, "type": "task", "id": "t1", "data": {}}))
    payload = info.value.as_dict()
    assert payload["error"] == "schema_incompativel"
    assert payload["received"] == 7
    assert payload["supported"] == [2]
