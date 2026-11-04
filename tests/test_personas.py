"""Persona store, model catalog and the ``personas`` action.

Everything runs in a TemporaryDirectory: the real memory/ and skills/ are never
touched. The catalog is written by the test, so paid/free rules are explicit.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import personas as persona_action  # noqa: E402
from core import personas  # noqa: E402


FREE_MODEL = {
    "value": "Qwen3.8-Flash",
    "displayName": "Qwen3.8-Flash",
    "isFree": True,
    "priceFactor": 0.0,
    "originalPriceFactor": 0.0,
    "availableContextWindows": [32768, 200000],
    "maxInputTokens": 200000,
    "efforts": ["low", "medium", "high"],
    "defaultEffort": "medium",
}

PAID_MODEL = {
    "value": "Claude-Paid",
    "displayName": "Claude Paid",
    "isFree": False,
    "priceFactor": 0.5,
    "originalPriceFactor": 1.0,
    "availableContextWindows": [200000],
    "maxInputTokens": 200000,
    "efforts": ["low", "high"],
    "defaultEffort": "high",
}

EXPENSIVE_MODEL = {
    "value": "Claude-Ultra",
    "displayName": "Claude Ultra",
    "isFree": False,
    "priceFactor": 2.0,
    "originalPriceFactor": 2.0,
    "availableContextWindows": [200000],
    "maxInputTokens": 200000,
    "efforts": ["high"],
    "defaultEffort": "high",
}

# Free model that publishes three windows while its per-request input cap is
# smaller (the qfmodel shape: max_input_tokens 180000, windows up to 1M).
WIDE_MODEL = {
    "value": "Wide-Flash",
    "displayName": "Wide Flash",
    "isFree": True,
    "priceFactor": 0.0,
    "originalPriceFactor": 0.0,
    "availableContextWindows": [200000, 400000, 1000000],
    "maxInputTokens": 180000,
    "efforts": ["high"],
    "defaultEffort": "high",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    memory = tmp_path / "memory"
    memory.mkdir()
    skills = tmp_path / "skills"
    for name in ("frontend-design", "ui-styling", "caveman"):
        folder = skills / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    # A folder without SKILL.md is not a skill.
    (skills / "not-a-skill").mkdir()

    monkeypatch.setattr(personas, "PERSONAS_PATH", memory / "personas.json")
    monkeypatch.setattr(personas, "MODELS_PATH", memory / "models.json")
    monkeypatch.setattr(personas, "SKILLS_DIR", skills)

    # Never read the owner's real config/api_keys.json in a test.
    from memory import config_manager
    monkeypatch.setattr(config_manager, "get_plugin_setting",
                        lambda *args, **kwargs: 1.0)

    def write_catalog(models):
        (memory / "models.json").write_text(
            json.dumps({"models": models}), encoding="utf-8")

    write_catalog([FREE_MODEL, PAID_MODEL, EXPENSIVE_MODEL])
    return {"memory": memory, "skills": skills, "write_catalog": write_catalog}


def fields(**overrides):
    base = {
        "name": "Fulana",
        "gender": "f",
        "voice_id": "fish-ref-abc123",
        "personality": "especialista em frontend, direta e cética",
        "skills": ["frontend-design"],
        "stacks": ["react", "TS", "Next.js"],
        "model": "Qwen3.8-Flash",
        "reasoning": "medium",
        "context_size": 200000,
    }
    base.update(overrides)
    return base


# ── creation, defaults, CRUD ────────────────────────────────────────────────

def test_create_defaults_normalizes_and_snapshots(env):
    params = fields(stacks=["react", "reactjs", "TS", "Next.js"],
                    skills=["frontend-design", "ui-styling", "frontend-design"])
    persona = personas.upsert(params)

    assert persona["id"] == "fulana"
    assert persona["name"] == "Fulana"
    assert persona["gender"] == "f"
    assert persona["stacks"] == ["react", "typescript", "nextjs"]
    assert persona["skills"] == ["frontend-design", "ui-styling", "caveman"]
    assert persona["model"] == "Qwen3.8-Flash"
    assert persona["model_price_factor"] == 0.0
    assert persona["reasoning"] == "medium"
    assert persona["context_size"] == 200000
    assert persona["created_at"] and persona["updated_at"]
    assert persona["aliases"] == []
    assert persona["history"] == []
    # Empty field keeps the default: persona reads everywhere.
    assert persona["allowed_dirs"] == []

    stored = json.loads(Path(personas.PERSONAS_PATH).read_text(encoding="utf-8"))
    assert stored["version"] == 1
    assert stored["retired"] == {}
    assert stored["personas"]["fulana"]["voice_id"] == "fish-ref-abc123"
    assert stored["personas"]["fulana"]["model_price_factor"] == 0.0


def test_create_uses_slugified_name_and_defaults_when_model_omitted(env):
    params = fields(name="Beltrano Backend", gender="m")
    for key in ("model", "reasoning", "context_size"):
        params.pop(key)
    persona = personas.upsert(params)

    assert persona["id"] == "beltrano-backend"
    assert persona["model"] == "Qwen3.8-Flash"
    assert persona["reasoning"] == "medium"
    assert persona["context_size"] == 200000


def test_whatsapp_account_is_validated_and_stored(env, monkeypatch):
    from core import whatsapp
    monkeypatch.setattr(whatsapp, "account_names",
                        lambda: ["principal", "trabalho"])
    persona = personas.upsert(fields(whatsapp_account="trabalho"))
    assert persona["whatsapp_account"] == "trabalho"
    assert personas.get("fulana")["whatsapp_account"] == "trabalho"
    with pytest.raises(ValueError):
        personas.upsert(fields(name="Outra", whatsapp_account="inexistente"))


def test_get_update_list_delete(env):
    created = personas.upsert(fields())

    assert personas.get("Fulana")["id"] == "fulana"
    assert personas.get("fulana")["id"] == "fulana"
    assert personas.get("FULANA")["id"] == "fulana"
    assert personas.get("missing") is None

    updated = personas.upsert({"id": "fulana", "personality": "agora concisa"})
    assert updated["personality"] == "agora concisa"
    assert updated["name"] == "Fulana"
    assert updated["created_at"] == created["created_at"]
    assert updated["voice_id"] == created["voice_id"]
    assert updated["updated_at"] >= created["updated_at"]

    assert [p["id"] for p in personas.list_personas()] == ["fulana"]
    assert personas.delete("Fulana") is True
    assert personas.get("fulana") is None
    assert personas.delete("fulana") is False


def test_upsert_can_change_name_without_changing_id(env):
    personas.upsert(fields())
    renamed = personas.upsert({"id": "fulana", "name": "Fulana Front"})
    assert renamed["id"] == "fulana"
    assert renamed["name"] == "Fulana Front"


# ── names history, retirement and resolution ───────────────────────────────

def test_rename_keeps_alias_and_history(env):
    personas.upsert(fields())
    renamed = personas.upsert({"id": "fulana", "name": "Fulana Front"})
    assert renamed["name"] == "Fulana Front"
    assert renamed["aliases"] == ["Fulana"]
    assert len(renamed["history"]) == 1
    entry = renamed["history"][0]
    assert entry["name"] == "Fulana"
    assert entry["status"] == "renamed"
    assert entry["to"] == "Fulana Front"
    assert entry["at"]

    # Updating another field never duplicates aliases or history.
    touched = personas.upsert({"id": "fulana", "personality": "mais direta"})
    assert touched["aliases"] == ["Fulana"]
    assert touched["history"] == renamed["history"]

    # A second rename accumulates both old names, in order.
    twice = personas.upsert({"id": "fulana", "name": "Fulana Front 2"})
    assert twice["aliases"] == ["Fulana", "Fulana Front"]
    assert [e["to"] for e in twice["history"]] == [
        "Fulana Front", "Fulana Front 2"]

    stored = json.loads(Path(personas.PERSONAS_PATH).read_text(encoding="utf-8"))
    assert stored["personas"]["fulana"]["aliases"] == [
        "Fulana", "Fulana Front"]
    assert stored["personas"]["fulana"]["history"] == twice["history"]


def test_aliases_are_unique_and_capped(env):
    personas.upsert(fields())
    for index in range(30):
        personas.upsert({"id": "fulana", "name": f"Nome {index}"})
    persona = personas.get("fulana")
    assert len(persona["aliases"]) == personas.MAX_ALIASES == 20
    keys = [alias.casefold() for alias in persona["aliases"]]
    assert len(set(keys)) == len(keys)
    # The most recent aliases survive; the oldest are dropped.
    assert "Nome 10" in persona["aliases"]
    assert "Fulana" not in persona["aliases"]


def test_resolve_old_name_after_rename(env):
    personas.upsert(fields())
    personas.upsert({"id": "fulana", "name": "Fulana Front"})

    old = personas.resolve_name("Fulana")
    assert old["status"] == "active"
    assert old["persona"]["id"] == "fulana"
    assert old["former_name"] == "Fulana"

    current = personas.resolve_name("fulana front")
    assert current["status"] == "active"
    assert current["former_name"] == ""

    by_id = personas.resolve_name("fulana")
    assert by_id["status"] == "active"
    assert by_id["persona"]["name"] == "Fulana Front"
    # get() still sees only active personas, but accepts their aliases.
    assert personas.get("Fulana")["id"] == "fulana"


def test_delete_retires_preserving_the_record(env):
    created = personas.upsert(fields())
    assert personas.delete("Fulana") is True
    assert personas.list_personas() == []
    assert personas.get("Fulana") is None

    resolved = personas.resolve_name("Fulana")
    assert resolved["status"] == "retired"
    retired = resolved["persona"]
    assert retired["id"] == created["id"]
    assert retired["personality"] == created["personality"]
    assert retired["voice_id"] == created["voice_id"]
    assert retired["gender"] == "f"
    assert retired["retired_status"] == "left"
    assert retired["retired_at"]
    assert resolved["successor"] is None

    # Removing again finds no active persona.
    assert personas.delete("Fulana") is False
    stored = json.loads(Path(personas.PERSONAS_PATH).read_text(encoding="utf-8"))
    assert "fulana" not in stored["personas"]
    assert stored["retired"]["fulana"]["retired_status"] == "left"


def test_history_and_successor_resolution(env):
    personas.upsert(fields())
    personas.upsert(fields(name="Beltrano", gender="m", voice_id="v2",
                           personality="backend rabugento",
                           stacks=["python"], skills=[]))
    personas.upsert({"id": "fulana", "name": "Fulana Front"})

    retired = personas.retire("Fulana Front", status="fired",
                              successor="Beltrano")
    assert retired["retired_status"] == "fired"
    assert retired["successor"] == "beltrano"
    assert retired["personality"] == "especialista em frontend, direta e cética"

    resolved = personas.resolve_name("Fulana")  # old name from the rename
    assert resolved["status"] == "retired"
    assert resolved["persona"]["name"] == "Fulana Front"
    assert resolved["successor"]["id"] == "beltrano"

    info = personas.history("Fulana")
    assert info["aliases"] == ["Fulana"]
    assert info["history"][0]["to"] == "Fulana Front"
    assert info["retired_status"] == "fired"
    assert info["retired_at"]
    assert info["successor"]["id"] == "beltrano"

    active = personas.history("Beltrano")
    assert active["retired_at"] is None
    assert active["retired_status"] is None
    assert active["successor"] is None


def test_retire_statuses_validation_and_unknown(env):
    personas.upsert(fields())
    with pytest.raises(ValueError, match="status"):
        personas.retire("Fulana", status="banished")
    assert personas.get("fulana") is not None  # nothing moved

    assert personas.retire("Ninguem") is None
    assert personas.delete("Ninguem") is False

    retired = personas.retire("Fulana", status="fired")
    assert retired["retired_status"] == "fired"
    assert personas.get("fulana") is None
    assert personas.resolve_name("fulana")["status"] == "retired"


def test_unknown_and_active_wins_over_retired(env):
    assert personas.resolve_name("Ninguem") == {
        "status": "unknown", "name": "Ninguem"}
    assert personas.resolve_name("") == {"status": "unknown", "name": ""}
    assert personas.history("Ninguem") is None

    personas.upsert(fields())
    personas.delete("Fulana")
    assert personas.resolve_name("Fulana")["status"] == "retired"

    replacement = personas.upsert(fields(name="Fulana", id="fulana-nova",
                                         voice_id="v2"))
    resolved = personas.resolve_name("Fulana")
    assert resolved["status"] == "active"
    assert resolved["persona"]["id"] == replacement["id"] == "fulana-nova"


def test_malformed_store_preserves_retired_and_normalizes(env):
    path = Path(personas.PERSONAS_PATH)
    path.write_text(json.dumps({
        "version": 1,
        "personas": "broken",
        "retired": {
            "fulana": {
                "id": "fulana",
                "name": "Fulana",
                "gender": "f",
                "voice_id": "fish-ref-abc123",
                "personality": "ranzinza",
                "aliases": ["Fulana", "fulana", "Fulana Antiga"],
                "history": [
                    {"name": "Fulana Antiga",
                     "at": "2026-01-01T00:00:00-03:00",
                     "status": "renamed", "to": "Fulana"},
                    "broken",
                ],
                "successor": "beltrano",
            },
            "quebrada": "nope",
        },
    }), encoding="utf-8")

    data = personas.load()
    assert data["personas"] == {}
    retired = data["retired"]["fulana"]
    assert retired["aliases"] == ["Fulana", "Fulana Antiga"]
    assert retired["history"][0]["status"] == "renamed"
    assert retired["retired_status"] == "left"  # missing status repaired
    assert retired["retired_at"] == ""
    assert "quebrada" not in data["retired"]

    assert personas.list_personas() == []
    assert personas.get("Fulana") is None
    resolved = personas.resolve_name("Fulana")
    assert resolved["status"] == "retired"
    assert resolved["persona"]["personality"] == "ranzinza"
    assert resolved["successor"] is None  # successor is not active

    # A later upsert repairs the file without losing the retired record.
    personas.upsert(fields(name="Beltrano", gender="m", voice_id="v2",
                           stacks=["python"], skills=[]))
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["retired"]["fulana"]["retired_status"] == "left"
    assert "beltrano" in stored["personas"]


def test_existing_records_normalize_aliases_and_history(env):
    path = Path(personas.PERSONAS_PATH)
    path.write_text(json.dumps({"version": 1, "personas": {"fulana": {
        "id": "fulana", "name": "Fulana", "gender": "f", "voice_id": "v",
        "personality": "", "skills": [], "stacks": [],
        "model": "Qwen3.8-Flash", "reasoning": "medium",
        "context_size": 200000,
    }}}), encoding="utf-8")

    loaded = personas.get("Fulana")
    assert loaded["aliases"] == []
    assert loaded["history"] == []
    assert loaded["allowed_dirs"] == []
    assert personas.history("Fulana")["aliases"] == []



# ── gender and addressing ───────────────────────────────────────────────────

@pytest.mark.parametrize("gender,name,article,contraction", [
    ("f", "Ana", "a Ana", "pra Ana"),
    ("m", "Bruno", "o Bruno", "pro Bruno"),
    ("n", "Nina", "Nina", "para Nina"),
])
def test_gender_and_address(env, gender, name, article, contraction):
    persona = personas.upsert(fields(name=name, gender=gender))
    assert personas.address(persona) == article
    assert personas.address(persona, contraction=True) == contraction


def test_gender_aliases_and_invalid(env):
    persona = personas.upsert(fields(name="Fulana Alias", gender="Feminino"))
    assert persona["gender"] == "f"
    with pytest.raises(ValueError, match="gênero"):
        personas.upsert(fields(name="Outra", gender="x"))


# ── validation ──────────────────────────────────────────────────────────────

def test_personality_validation(env):
    assert personas.upsert(fields(name="Sem Pers", personality=""))["personality"] == ""

    with pytest.raises(ValueError, match="2000"):
        personas.upsert(fields(name="Longa", personality="x" * 2001))
    with pytest.raises(ValueError, match="segredos"):
        personas.upsert(fields(name="Key", personality="use api_key=abc"))
    with pytest.raises(ValueError, match="segredos"):
        personas.upsert(fields(name="Token", personality="o token está aqui"))
    with pytest.raises(ValueError, match="segredos"):
        personas.upsert(fields(name="Sk", personality="chave sk-live-1234"))


def test_required_fields_and_limits(env):
    with pytest.raises(ValueError, match="nome"):
        personas.upsert(fields(name=""))
    with pytest.raises(ValueError, match="gênero"):
        personas.upsert(fields(name="Sem Genero", gender=""))
    with pytest.raises(ValueError, match="voice_id"):
        personas.upsert(fields(name="Sem Voz", voice_id=""))
    with pytest.raises(ValueError, match="catálogo"):
        personas.upsert(fields(name="Modelo Ruim", model="No-Such-Model"))

    # 12 personas fit; the 13th is refused.
    for index in range(personas.MAX_PERSONAS):
        personas.upsert(fields(name=f"Pessoa {index}", voice_id=f"v{index}"))
    with pytest.raises(ValueError, match="limite"):
        personas.upsert(fields(name="Pessoa 13", voice_id="v13"))


def test_reasoning_and_context_validation(env):
    with pytest.raises(ValueError, match="reasoning"):
        personas.upsert(fields(name="Raciocinio", reasoning="max"))  # not in efforts
    with pytest.raises(ValueError, match="janela"):
        personas.upsert(fields(name="Contexto Grande", context_size=300000))
    with pytest.raises(ValueError, match="janela"):
        personas.upsert(fields(name="Contexto Pequeno", context_size=1024))
    with pytest.raises(ValueError, match="inteiro|positivo"):
        personas.upsert(fields(name="Contexto Zero", context_size=0))

    # '200k' is accepted and the paid model's own effort list is respected.
    paid = personas.upsert(fields(name="Paga", model="Claude-Paid",
                                  reasoning="high", context_size="200k"),
                           confirm_paid=True)
    assert paid["context_size"] == 200000
    assert paid["model_price_factor"] == 0.5


def test_context_ceiling_comes_from_published_windows(env):
    """The ceiling is the biggest announced window, not max_input_tokens."""
    env["write_catalog"]([FREE_MODEL, WIDE_MODEL, PAID_MODEL, EXPENSIVE_MODEL])
    wide = personas.find_model("Wide-Flash")
    assert personas._context_bounds(wide) == (200000, 1000000)
    # The per-request cap (180000) must not shrink the published windows.
    for size in (200000, 400000, 1000000):
        persona = personas.upsert(fields(
            name=f"Contexto {size}", voice_id=f"v{size}",
            model="Wide-Flash", reasoning="high", context_size=size))
        assert persona["context_size"] == size

    with pytest.raises(ValueError, match="janela"):
        personas.upsert(fields(name="Contexto Demais", voice_id="v2m",
                               model="Wide-Flash", reasoning="high",
                               context_size=2000000))

    # A model that announces only 200000 still refuses 1M.
    with pytest.raises(ValueError, match="janela"):
        personas.upsert(fields(name="Contexto Estreito", voice_id="v1m",
                               context_size=1000000))


def test_action_accepts_advertised_context_windows(env):
    env["write_catalog"]([FREE_MODEL, WIDE_MODEL, PAID_MODEL, EXPENSIVE_MODEL])
    created = _call(action="create", **fields(
        name="Ampla", id="ampla", voice_id="v-wide", model="Wide-Flash",
        reasoning="high", context_size=1000000))
    assert created["status"] == "created"
    assert created["persona"]["context_size"] == 1000000
    assert _call(action="show", id="ampla")["persona"]["context_size"] == 1000000

    refused = _call(action="create", **fields(
        name="Apertada", id="apertada", voice_id="v-narrow", model="Wide-Flash",
        reasoning="high", context_size=2000000))
    assert refused["status"] == "error"
    assert "janela" in refused["error"]


# The qfmodel/qmodel shape: three published windows while the per-request input
# cap is 180000 and the efforts say ``xhigh`` (never "max").
FLASH_MODEL = {
    "value": "Flash-X",
    "displayName": "Flash X",
    "isFree": True,
    "priceFactor": 0.0,
    "originalPriceFactor": 0.0,
    "availableContextWindows": [200000, 400000, 1000000],
    "maxInputTokens": 180000,
    "efforts": ["xhigh", "low", "medium"],
    "defaultEffort": "medium",
}


def test_form_context_offers_every_published_window(env):
    """The form's options come from the model, not from a hardcoded ceiling."""
    env["write_catalog"]([FREE_MODEL, WIDE_MODEL, FLASH_MODEL])
    flash = personas.find_model("Flash-X")
    assert personas.context_windows(flash) == [200000, 400000, 1000000]
    assert personas.context_windows("Wide-Flash") == [200000, 400000, 1000000]
    # A narrow model only offers what it publishes.
    assert personas.context_windows("Qwen3.8-Flash") == [32768, 200000]
    assert personas.default_context(flash) == 200000

    persona = personas.upsert(fields(
        name="Um Milhão", voice_id="v-flash", model="Flash-X",
        reasoning="medium", context_size=personas.context_windows(flash)[-1]))
    assert persona["context_size"] == 1000000

    # A size no window publishes is still refused, in words the owner can read.
    with pytest.raises(ValueError, match="fora da janela de Flash X"):
        personas.upsert(fields(name="Fora", voice_id="v-out", model="Flash-X",
                               reasoning="medium", context_size=1100000))


def test_form_reasoning_max_maps_to_the_stored_effort(env):
    """The "Max" label carries the value the backend accepts (``xhigh``)."""
    env["write_catalog"]([FREE_MODEL, FLASH_MODEL])
    flash = personas.find_model("Flash-X")
    labels = {label: value for value, label in personas.reasoning_choices(flash)}
    assert labels["Max"] == "xhigh"
    assert [label for _, label in personas.reasoning_choices(flash)].count("Max") == 1
    assert personas.default_reasoning(flash) == "medium"

    persona = personas.upsert(fields(
        name="Arnaldo", gender="m", voice_id="v-max", model="Flash-X",
        reasoning=labels["Max"]))
    assert persona["reasoning"] == "xhigh"
    reloaded = personas.get("arnaldo")
    assert reloaded["reasoning"] == "xhigh"
    # Reopening the form finds the label again: xhigh is still in the model.
    assert dict(personas.reasoning_choices(
        personas.find_model(reloaded["model"]))).get("xhigh") == "Max"


def test_form_reasoning_choices_follow_a_model_without_efforts(env):
    """A model that publishes no efforts offers the store's own levels."""
    env["write_catalog"]([FREE_MODEL])
    choices = personas.reasoning_choices("Qwen3.8-Flash")
    assert [value for value, _ in choices] == ["low", "medium", "high"]
    assert personas.default_reasoning("Qwen3.8-Flash") == "medium"


def test_whatsapp_account_persists_through_the_form_save_path(env, monkeypatch):
    """Picking an account in the form stores it — by name or by account uuid."""
    monkeypatch.setattr(personas, "_whatsapp_accounts", lambda: [
        {"name": "principal", "uuid": "jarvis", "paired": True},
        {"name": "trabalho", "uuid": "jarvis-trabalho", "paired": True},
    ])
    assert personas.whatsapp_account_choices() == ["principal", "trabalho"]

    persona = personas.upsert(fields(name="Arnaldo", gender="m",
                                     whatsapp_account="trabalho"))
    assert persona["whatsapp_account"] == "trabalho"
    stored = json.loads(Path(personas.PERSONAS_PATH).read_text(encoding="utf-8"))
    assert stored["personas"]["arnaldo"]["whatsapp_account"] == "trabalho"

    # The account uuid is the same account: it is canonicalized to the name.
    again = personas.upsert({"id": "arnaldo", "voice_id": "fish-ref-abc123",
                             "whatsapp_account": "jarvis-trabalho"})
    assert again["whatsapp_account"] == "trabalho"

    with pytest.raises(ValueError, match="não está pareado"):
        personas.upsert({"id": "arnaldo", "voice_id": "fish-ref-abc123",
                         "whatsapp_account": "pessoal"})


class _FakeText:
    def __init__(self, text=""):
        self._text = text

    def text(self):
        return self._text

    def toPlainText(self):
        return self._text


class _FakeCombo:
    """Only the QComboBox surface ``PersonaOverlay._fields`` reads."""

    def __init__(self, items, index=0):
        self._items = list(items)
        self._index = index

    def currentIndex(self):
        return self._index

    def currentData(self):
        if 0 <= self._index < len(self._items):
            return self._items[self._index][0]
        return None

    def currentText(self):
        if 0 <= self._index < len(self._items):
            return self._items[self._index][1]
        return ""

    def itemText(self, index):
        if 0 <= index < len(self._items):
            return self._items[index][1]
        return ""


def test_persona_form_fields_carry_account_context_and_reasoning(env, monkeypatch):
    """The real ``_fields``/``upsert`` path keeps every picker value."""
    from types import SimpleNamespace
    from ui import PersonaOverlay

    env["write_catalog"]([FLASH_MODEL])
    monkeypatch.setattr(personas, "_whatsapp_accounts", lambda: [
        {"name": "principal", "uuid": "jarvis"},
        {"name": "trabalho", "uuid": "jarvis-trabalho"},
    ])
    form = SimpleNamespace(
        _name=_FakeText("Arnaldo"),
        _gender=_FakeCombo([("m", "Masculino")]),
        _voice=_FakeText("fish-ref-abc123"),
        _personality=_FakeText("engenheiro direto"),
        _skills=_FakeText(""),
        _stacks=_FakeText("react, typescript"),
        _allowed_dirs=_FakeText("/projects/therotv, ~/Jarvis"),
        _model=_FakeCombo([("Flash X", "Flash X  ·  0.0x")]),
        _reasoning=_FakeCombo([("xhigh", "Max")]),
        _context=_FakeCombo([(1000000, "1000000  ·  1 milhão")]),
        _whatsapp=_FakeCombo([("", "Conta principal (padrão)"),
                              ("trabalho", "trabalho")], index=1),
        _select=_FakeCombo([("arnaldo", "Arnaldo")]),
    )
    picked = PersonaOverlay._fields(form)
    assert picked["whatsapp_account"] == "trabalho"
    assert picked["context_size"] == 1000000
    assert picked["reasoning"] == "xhigh"
    assert picked["allowed_dirs"] == "/projects/therotv, ~/Jarvis"

    persona = personas.upsert(picked)
    assert persona["whatsapp_account"] == "trabalho"
    assert persona["context_size"] == 1000000
    assert persona["reasoning"] == "xhigh"
    assert persona["allowed_dirs"] == [
        "/projects/therotv",
        os.path.join(os.path.expanduser("~"), "Jarvis"),
    ]


def test_persona_form_has_no_hardcoded_option_lists():
    """The overlay asks core/personas for windows, efforts and accounts."""
    source = (Path(__file__).resolve().parent.parent / "ui.py").read_text(
        encoding="utf-8")
    assert "store.context_windows(" in source
    assert "store.reasoning_choices(" in source
    assert "store.default_context(" in source
    assert "store.whatsapp_account_choices()" in source
    assert '("low", "medium", "high", "max"' not in source
    assert 'self._context.setText("200000")' not in source
    assert '"context_size": context' in source
    assert '"whatsapp_account": self._whatsapp.currentData() or ""' in source
    assert '"allowed_dirs": self._allowed_dirs.text().strip()' in source
    assert "vazio = lê tudo" in source


def test_skills_validation(env):
    persona = personas.upsert(fields(name="Skills Vazias", skills=[]))
    assert persona["skills"] == ["caveman"]

    with pytest.raises(ValueError, match="não existe"):
        personas.upsert(fields(name="Skill Ruim", skills=["ghost-skill"]))
    with pytest.raises(ValueError, match="inválido"):
        personas.upsert(fields(name="Skill Path", skills=["../etc/passwd"]))
    with pytest.raises(ValueError, match="não existe"):
        personas.upsert(fields(name="Skill Sem Md", skills=["not-a-skill"]))


def test_every_persona_gets_caveman_even_without_skills(env):
    """Owner rule 02/11/2026: caveman on every persona, created later included."""
    params = fields(name="Sem Skills", gender="m")
    params.pop("skills")
    persona = personas.upsert(params)
    assert "caveman" in persona["skills"]

    # Updating a record that predates the rule adds caveman, keeps the rest.
    stored = json.loads(Path(personas.PERSONAS_PATH).read_text(encoding="utf-8"))
    stored["personas"]["sem-skills"]["skills"] = ["ui-styling"]
    Path(personas.PERSONAS_PATH).write_text(
        json.dumps(stored), encoding="utf-8")
    updated = personas.upsert({"id": "sem-skills",
                               "voice_id": "fish-ref-abc123"})
    assert updated["skills"] == ["ui-styling", "caveman"]

    # Explicitly asking for caveman never duplicates it.
    again = personas.upsert({"id": "sem-skills",
                             "voice_id": "fish-ref-abc123",
                             "skills": ["caveman"]})
    assert again["skills"] == ["caveman"]


def test_caveman_rule_never_reaches_the_main_assistant_prompt(env):
    """The rule is persona-only: qoder_live loads the assistant prompt itself."""
    source = (Path(__file__).resolve().parent.parent
              / "qoder_live" / "index.mjs").read_text(encoding="utf-8")
    runtime = re.search(r"const RUNTIME_SKILLS = \[(.*?)\];", source, re.S)
    assert runtime is not None
    assert "caveman" not in runtime.group(1)
    # Persona skills come from the persona's own ``skills`` list, not RUNTIME_SKILLS.
    assert "loadPersonaSkills(persona.skills)" in source


def test_runtime_skills_have_no_caveman_variant(env):
    """Owner rule 02/11/2026: caveman never enters the spoken assistant prompt.

    The live prompt is built from RUNTIME_SKILLS only. Caveman and its variants
    stay in the execution channel and in persona prompts, never in Gino's voice.
    """
    source = (Path(__file__).resolve().parent.parent
              / "qoder_live" / "index.mjs").read_text(encoding="utf-8")
    runtime = re.search(r"const RUNTIME_SKILLS = \[(.*?)\];", source, re.S)
    assert runtime is not None
    names = re.findall(r'"([^"]+)"', runtime.group(1))
    assert names, "RUNTIME_SKILLS vazio"
    assert [n for n in names if "caveman" in n.casefold()] == []
    # The assistant prompt loads only loadSkills() (RUNTIME_SKILLS); persona
    # skills come from loadPersonaSkills(), used by personaPrompt() alone.
    assert "const SYSTEM_PROMPT = loadSystemPrompt();" in source
    assert "append: SYSTEM_PROMPT" in source
    assert "append: personaPrompt(persona)" in source


def test_persona_prompt_protects_the_spoken_reply_from_caveman():
    """Owner rule 02/11/2026: caveman governs artifacts, never the spoken reply."""
    source = (Path(__file__).resolve().parent.parent
              / "qoder_live" / "index.mjs").read_text(encoding="utf-8")
    match = re.search(r"function personaPrompt\(persona\) \{(.*?)\n\}",
                      source, re.S)
    assert match is not None
    body = match.group(1)
    assert "Caveman/compressão nunca se aplica à fala da persona" in body
    # The old spoken rule stays, now with the compression exemption inside it.
    assert "Sua resposta será falada" in body
    assert "sem blocos de código" in body
    # [FALA] comes after [SKILLS], so skill text cannot override spoken style.
    assert body.index("[FALA]") > body.index("[SKILLS]")


def test_persona_prompt_forbids_internal_vocabulary_in_speech():
    """Incidente 02/11 19:05: nome de projeto, stack e HLS ditos em áudio.

    A lei é cláusula de prompt, não mecanismo novo: fica dentro do bloco
    [FALA], que continua por último (depois de [SKILLS]).
    """
    root = Path(__file__).resolve().parent.parent
    source = (root / "qoder_live" / "index.mjs").read_text(encoding="utf-8")
    match = re.search(r"function personaPrompt\(persona\) \{(.*?)\n\}",
                      source, re.S)
    assert match is not None
    body = match.group(1)
    assert "Na fala, nunca verbalize nome de projeto" in body
    assert "monorepo ou sub-app" in body
    assert "provedor de IA" in body
    assert "hash de commit ou número de teste" in body
    assert "agentes rodando, latência, saúde" in body
    assert "credencial ou valor de config" in body
    assert "dado pessoal de terceiro" in body
    assert "sem identificador cru" in body
    # A ordem existente não regride: a lei mora no último bloco.
    assert body.index("[FALA]") > body.index("[SKILLS]")
    assert body.index("Na fala, nunca verbalize") > body.index("[SKILLS]")

    # A mesma lei vale para o assistente principal, nas skills de runtime.
    for skill in ("personas", "voice"):
        text = (root / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        assert "nunca verbalize nome de projeto" in text, skill
        assert "sem identificador cru" in text, skill


def test_personas_read_any_directory_and_keep_write_gates():
    """Owner rule 02/11/2026: read is total, always, no confirmation.

    The Qoder CLI only treats listed directories as workspace, so the sidecar
    must add the filesystem root for personas and for the main assistant. The
    write gates stay untouched: personas still have no Bash/Edit/Write/Agent,
    and edits outside the project still ask the owner by voice.
    """
    root = Path(__file__).resolve().parent.parent
    source = (root / "qoder_live" / "index.mjs").read_text(encoding="utf-8")

    assert ("const READ_EVERYWHERE = path.parse(path.resolve(CWD)).root "
            "|| path.sep;") in source

    persona_options = re.search(
        r"function personaOptions\(persona, resumeId\) \{(.*?)\n\}", source, re.S)
    assert persona_options is not None
    body = persona_options.group(1)
    # Empty allowed_dirs keeps the filesystem root; a filled list replaces it.
    assert "additionalDirectories: restricted ? dirs : [READ_EVERYWHERE]" in body
    assert "cwd: personaWorkingDir(persona)" in body
    # Listed tools are auto-approved, so restricted personas must remove the
    # read tools from the allowlist and send them through the directory check.
    assert "PERSONA_ALLOWED_TOOLS.filter((name) => !PERSONA_READ_TOOLS.has(name))" in body
    assert "canUseTool: (toolName, input, options) => personaCanUseTool(" in body

    main_options = re.search(
        r"function optionsFor\(maxTurns\) \{(.*?)\n\}", source, re.S)
    assert main_options is not None
    assert "additionalDirectories: [READ_EVERYWHERE]" in main_options.group(1)

    # Read tools remain allowed; every write/execution tool remains denied.
    allowed = re.search(r"const PERSONA_ALLOWED_TOOLS = \[(.*?)\];", source, re.S)
    assert allowed is not None
    names = re.findall(r'"([^"]+)"', allowed.group(1))
    for tool in ("Read", "Glob", "Grep"):
        assert tool in names
    for tool in ("Bash", "Edit", "Write", "Agent"):
        assert tool not in names
    assert "Persona só tem leitura e memória" in source

    # The new read scope cannot become a write path outside the project.
    assert "function isInsideCwd(target)" in source
    assert "await ownerAllowsOutsideWrite(toolName, target)" in source


def test_allowed_dirs_defaults_normalizes_and_clears(env, tmp_path, monkeypatch):
    """Owner rule 02/11/2026: empty = reads everywhere; list = read roots."""
    persona = personas.upsert(fields())
    assert persona["allowed_dirs"] == []

    home = str(tmp_path / "home")
    monkeypatch.setenv("HOME", home)
    updated = personas.upsert({
        "id": "fulana",
        "allowed_dirs": " ~/Jarvis , /projects/therotv, ~/Jarvis/ , projects ",
    })
    expected = [
        os.path.join(home, "Jarvis"),
        "/projects/therotv",
        os.path.join(str(personas.BASE_DIR), "projects"),
    ]
    assert updated["allowed_dirs"] == expected
    assert personas.get("fulana")["allowed_dirs"] == expected
    assert personas.resolve_name("fulana")["persona"]["allowed_dirs"] == expected
    assert personas.list_personas()[0]["allowed_dirs"] == expected

    cleared = personas.upsert({"id": "fulana", "allowed_dirs": ""})
    assert cleared["allowed_dirs"] == []

    with pytest.raises(ValueError, match="acima de"):
        personas.upsert({"id": "fulana", "allowed_dirs": [
            f"/tmp/dir{index}"
            for index in range(personas.MAX_ALLOWED_DIRS + 1)]})

    with pytest.raises(ValueError, match="inválidos"):
        personas.upsert({"id": "fulana", "allowed_dirs": {"nope": 1}})


def test_action_allowed_dirs_round_trip(env):
    created = _call(action="create", **fields(),
                    allowed_dirs="/projects/therotv, ~/Jarvis")
    assert created["status"] == "created"
    expected = ["/projects/therotv",
                os.path.join(os.path.expanduser("~"), "Jarvis")]
    assert created["persona"]["allowed_dirs"] == expected
    assert _call(action="list")["personas"][0]["allowed_dirs"] == expected

    cleared = _call(action="update", id="fulana", allowed_dirs="")
    assert cleared["status"] == "updated"
    assert cleared["persona"]["allowed_dirs"] == []
    # Clearing the read list never touches the other fields.
    assert cleared["persona"]["name"] == "Fulana"
    assert cleared["persona"]["skills"] == ["frontend-design", "caveman"]


def test_sidecar_restricts_reads_to_allowed_dirs():
    """The sidecar turns allowed_dirs into the session read scope."""
    source = (Path(__file__).resolve().parent.parent
              / "qoder_live" / "index.mjs").read_text(encoding="utf-8")

    assert 'const PERSONA_READ_TOOLS = new Set(["Read", "Glob", "Grep"]);' in source
    assert "allowed_dirs: dirStringList(raw.allowed_dirs)" in source
    assert "function personaAllowedDirs(persona)" in source
    assert "function personaReadAllowed(persona, baseDir, target)" in source
    assert "function personaWorkingDir(persona)" in source
    assert "function personaReadRule(persona)" in source
    assert "Leitura restrita às pastas" in source
    assert "`[FERRAMENTAS] ${personaReadRule(persona)}" in source
    # Changing the field restarts the persona session (config hash).
    assert "persona.allowed_dirs," in source
    # Writes are still denied by name, independent of the read scope.
    assert "Persona só tem leitura e memória" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="node não instalado")
def test_sidecar_selftest_reports_read_scope():
    root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [shutil.which("node"), str(root / "qoder_live" / "index.mjs"),
         "--selftest"],
        cwd=root, env={**os.environ, "JARVIS_CWD": str(root)},
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    for check in ("read_everywhere", "read_scope", "read_gate", "dir_parse"):
        assert payload["checks"][check] is True, check


def test_persona_skill_states_total_read_rule():
    """The policy file carries the owner's 02/11/2026 rule in plain words."""
    text = (Path(__file__).resolve().parent.parent
            / "skills" / "personas" / "SKILL.md").read_text(encoding="utf-8")
    folded = text.casefold()
    assert "02/11/2026" in text
    assert "leitura total" in folded
    assert "sem confirmação" in folded
    assert "editar, excluir" in folded
    # Read is not consent for writing.
    assert "leitura liberada nunca vale como permissão de escrita" in folded
    # The rule now lives in the per-persona field; Gino has no such limit.
    assert "allowed_dirs" in text
    assert "nunca é limitado" in folded


def test_stored_personas_always_carry_caveman(env):
    """Owner rule 02/11/2026: every persona persisted on disk has caveman."""
    personas.upsert(fields(name="Arnaldo", gender="m", skills=["frontend-design"]))
    personas.upsert(fields(name="Aline", gender="f"))
    stored = json.loads(Path(personas.PERSONAS_PATH).read_text(encoding="utf-8"))
    assert stored["personas"], "nenhuma persona gravada"
    for slug, record in stored["personas"].items():
        assert "caveman" in record["skills"], slug


def test_live_arnaldo_and_aline_carry_caveman_exactly_once():
    """The real fichas keep one caveman entry; upsert never duplicates it."""
    path = Path(personas.PERSONAS_PATH)
    if not path.exists():
        pytest.skip("memory/personas.json ainda não existe")
    stored = json.loads(path.read_text(encoding="utf-8"))
    records = stored.get("personas") or {}
    for slug in ("arnaldo", "aline"):
        assert slug in records, f"persona {slug} ausente na ficha"
        skills = [str(skill).casefold() for skill in records[slug].get("skills") or []]
        assert skills.count("caveman") == 1, (slug, skills)


def test_execution_skill_compresses_channel_but_keeps_speech_clean(env):
    """The policy is a file: skills/execution/SKILL.md carries the caveman rule."""
    text = (Path(__file__).resolve().parent.parent
            / "skills" / "execution" / "SKILL.md").read_text(encoding="utf-8")
    assert "Caveman só no canal de execução, nunca na fala" in text
    assert "comprime-se o verbo, nunca a informação" in text
    assert "sem caveman" in text
    assert "TTS pronuncia" in text


# ── model catalog ───────────────────────────────────────────────────────────

def test_model_catalog_reads_live_file(env):
    catalog = personas.model_catalog()
    assert [m["value"] for m in catalog] == [
        "Qwen3.8-Flash", "Claude-Paid", "Claude-Ultra"]
    paid = personas.find_model("claude-paid")
    assert paid["displayName"] == "Claude Paid"
    assert paid["priceFactor"] == 0.5
    assert paid["isFree"] is False
    assert paid["availableContextWindows"] == [200000]
    assert personas.find_model("Qwen3.8-Flash")["value"] == "Qwen3.8-Flash"
    assert personas.find_model("missing") is None


def test_model_catalog_fallback_when_absent_or_malformed(env):
    Path(personas.MODELS_PATH).unlink()
    fallback = personas.model_catalog()
    assert [m["value"] for m in fallback] == ["qfmodel", "efficient"]
    assert all(m["priceFactor"] == 0.0 and m["isFree"] for m in fallback)
    assert personas.find_model("efficient")["priceFactor"] == 0.0
    assert personas.find_model("Qwen3.8-Flash")["displayName"] == "Qwen3.8-Flash"

    Path(personas.MODELS_PATH).write_text("{not json", encoding="utf-8")
    assert [m["value"] for m in personas.model_catalog()] == [
        "qfmodel", "efficient"]

    Path(personas.MODELS_PATH).write_text('{"models": []}', encoding="utf-8")
    assert [m["value"] for m in personas.model_catalog()] == [
        "qfmodel", "efficient"]

    # Without a catalog, the free fallback still allows a persona to be made.
    persona = personas.upsert(fields(name="Offline", model="", context_size=""))
    assert persona["model"] == "Qwen3.8-Flash"
    assert persona["model_price_factor"] == 0.0


def test_paid_model_confirmation_multiplier_and_limit(env):
    with pytest.raises(ValueError, match=r"0\.5x"):
        personas.upsert(fields(name="Paga", model="Claude-Paid",
                               reasoning="high", context_size=200000))
    assert personas.get("Paga") is None

    saved = personas.upsert(fields(name="Paga", model="Claude-Paid",
                                   reasoning="high", context_size=200000),
                            confirm_paid=True)
    assert saved["model_price_factor"] == 0.5

    with pytest.raises(ValueError, match=r"0\.5x"):
        personas.upsert({"id": "paga", "personality": "mudou"})

    with pytest.raises(ValueError, match=r"0\.25x"):
        personas.upsert(fields(name="Cara", model="Claude-Paid",
                               context_size=200000),
                        confirm_paid=True, max_price_factor=0.25)

    with pytest.raises(ValueError, match=r"2\.0x"):
        personas.upsert(fields(name="Ultra", model="Claude-Ultra",
                               context_size=200000),
                        confirm_paid=True, max_price_factor=1.0)


def test_format_multiplier():
    assert personas.format_multiplier(0.0) == "0.0x"
    assert personas.format_multiplier(0.1) == "0.1x"
    assert personas.format_multiplier(0.5) == "0.5x"
    assert personas.format_multiplier(1) == "1.0x"
    assert personas.format_multiplier(2.0) == "2.0x"


# ── store safety ────────────────────────────────────────────────────────────

def test_malformed_store_returns_empty_and_is_repaired_on_save(env):
    path = Path(personas.PERSONAS_PATH)
    path.write_text("not-a-json", encoding="utf-8")
    assert personas.load() == {"version": 1, "personas": {}, "retired": {}}
    assert personas.list_personas() == []

    path.write_text('{"version": 1, "personas": []}', encoding="utf-8")
    assert personas.load() == {"version": 1, "personas": {}, "retired": {}}

    personas.save({"personas": {"x": "quebrada"}})
    data = personas.load()
    # A non-dict entry is ignored, but the file is valid again.
    assert data == {"version": 1, "personas": {}, "retired": {}}
    json.loads(path.read_text(encoding="utf-8"))

    personas.upsert(fields())
    assert personas.get("fulana")["name"] == "Fulana"


def test_save_is_atomic_and_writes_only_the_store(env):
    memory = env["memory"]
    personas.upsert(fields())
    names = sorted(p.name for p in memory.iterdir())
    assert "personas.json" in names
    assert not [name for name in names if name.endswith(".tmp")]
    assert json.loads((memory / "personas.json").read_text(encoding="utf-8"))


def test_upsert_rejects_non_dict_fields(env):
    with pytest.raises(ValueError):
        personas.upsert(["nope"])


# ── action tool ─────────────────────────────────────────────────────────────

def _call(**params):
    return json.loads(persona_action.personas(params))


def test_action_create_requires_confirmation_for_paid_model(env):
    paid = fields(model="Claude-Paid", reasoning="high")

    first = _call(action="create", **paid)
    assert first["status"] == "confirmation_required"
    assert first["multiplier"] == "0.5x"
    assert first["model"] == "Claude-Paid"
    assert "0.5x" in first["message"]
    assert personas.get("Fulana") is None

    second = _call(action="create", confirm_paid=True, **paid)
    assert second["status"] == "created"
    assert second["multiplier"] == "0.5x"
    assert second["persona"]["model_price_factor"] == 0.5
    assert personas.get("Fulana") is not None


def test_action_update_paid_reconfirms(env):
    personas.upsert(fields(model="Claude-Paid", reasoning="high"),
                    confirm_paid=True)
    blocked = _call(action="update", id="fulana", personality="outra")
    assert blocked["status"] == "confirmation_required"
    assert blocked["multiplier"] == "0.5x"

    done = _call(action="update", id="fulana", personality="outra",
                 confirm_paid=True)
    assert done["status"] == "updated"
    assert done["persona"]["personality"] == "outra"


def test_action_max_price_factor_from_config(env, monkeypatch):
    from memory import config_manager
    monkeypatch.setattr(config_manager, "get_plugin_setting",
                        lambda *args, **kwargs: 0.4)
    result = _call(action="create", **fields(model="Claude-Paid"),
                   confirm_paid=True)
    assert result["status"] == "error"
    assert "0.5x" in result["error"]
    assert "0.4x" in result["error"]
    assert personas.get("Fulana") is None


def test_action_models_list_show_delete(env):
    models = _call(action="models")
    assert models["status"] == "ok"
    by_value = {m["value"]: m for m in models["models"]}
    paid = by_value["Claude-Paid"]
    assert paid["displayName"] == "Claude Paid"
    assert paid["isFree"] is False
    assert paid["multiplier"] == "0.5x"
    assert paid["context_windows"] == [200000]
    assert paid["efforts"] == ["low", "high"]
    assert set(paid) >= {"value", "displayName", "isFree", "multiplier",
                         "context_windows", "efforts"}

    created = _call(action="create", **fields())
    assert created["status"] == "created"

    listed = _call(action="list")
    assert listed["count"] == 1
    assert listed["personas"][0]["id"] == "fulana"
    assert listed["personas"][0]["address"] == "a Fulana"

    shown = _call(action="show", name="Fulana")
    assert shown["status"] == "ok"
    assert shown["persona"]["voice_id"] == "fish-ref-abc123"

    deleted = _call(action="delete", name="Fulana")
    assert deleted["status"] == "deleted"
    assert _call(action="show", name="Fulana")["status"] == "not_found"
    assert _call(action="nonsense")["status"] == "error"


def test_action_resolve_history_and_retire(env):
    assert _call(action="create", **fields())["status"] == "created"
    renamed = _call(action="update", id="fulana", name="Fulana Front")
    assert renamed["persona"]["aliases"] == ["Fulana"]

    listed = _call(action="list")
    assert listed["personas"][0]["aliases"] == ["Fulana"]

    resolved = _call(action="resolve", name="Fulana")
    assert resolved["status"] == "active"
    assert resolved["former_name"] == "Fulana"
    assert resolved["persona"]["id"] == "fulana"
    assert resolved["address"] == "a Fulana Front"

    info = _call(action="history", name="Fulana")
    assert info["status"] == "ok"
    assert info["aliases"] == ["Fulana"]
    assert info["history"][0]["status"] == "renamed"

    assert _call(action="create", **fields(
        name="Beltrano", gender="m", voice_id="v2",
        personality="backend rabugento", stacks=["python"], skills=[]))[
            "status"] == "created"

    retired = _call(action="retire", name="Fulana Front",
                    retired_status="fired", successor="Beltrano")
    assert retired["status"] == "retired"
    assert retired["retired_status"] == "fired"
    assert retired["successor"]["id"] == "beltrano"
    assert retired["persona"]["personality"] == fields()["personality"]

    old = _call(action="resolve", name="Fulana")
    assert old["status"] == "retired"
    assert old["persona"]["personality"] == fields()["personality"]
    assert old["retired_status"] == "fired"
    assert old["successor"]["id"] == "beltrano"

    info = _call(action="history", name="Fulana")
    assert info["status"] == "ok"
    assert info["retired_status"] == "fired"
    assert info["successor"]["id"] == "beltrano"

    assert _call(action="resolve", name="Ninguem")["status"] == "unknown"
    assert _call(action="history", name="Ninguem")["status"] == "not_found"
    assert _call(action="retire", name="Ninguem")["status"] == "error"
    assert _call(action="retire", name="Beltrano",
                 retired_status="banished")["status"] == "error"
    assert personas.get("beltrano") is not None


def test_action_delete_retires_with_status(env):
    assert _call(action="create", **fields())["status"] == "created"
    deleted = _call(action="delete", name="Fulana")
    assert deleted["status"] == "deleted"
    assert deleted["retired_status"] == "left"

    resolved = _call(action="resolve", name="Fulana")
    assert resolved["status"] == "retired"
    assert resolved["persona"]["personality"] == fields()["personality"]
    assert _call(action="show", name="Fulana")["status"] == "not_found"
    assert _call(action="list")["count"] == 0

    assert _call(action="create", **fields(
        name="Beltrano", gender="m", voice_id="v2", stacks=["python"],
        skills=[]))["status"] == "created"
    fired = _call(action="delete", name="Beltrano", retired_status="fired")
    assert fired["retired_status"] == "fired"
    assert _call(action="resolve", name="Beltrano")["status"] == "retired"


def test_action_for_project_and_link_project(env, tmp_path, monkeypatch):
    from core import projects
    monkeypatch.setattr(projects, "PROJECTS_PATH", env["memory"] / "projects.json")
    projects.clear_cache()

    project = tmp_path / "frontend-app"
    project.mkdir()
    (project / "package.json").write_text(json.dumps({
        "dependencies": {"react": "^18", "next": "^14"},
        "devDependencies": {"typescript": "^5"},
    }), encoding="utf-8")

    assert _call(action="create", **fields())["status"] == "created"
    assert _call(action="create", **fields(
        name="Beltrano", gender="m", voice_id="v2",
        stacks=["python"], skills=[]))["status"] == "created"

    found = _call(action="for_project", path=str(project))
    assert found["status"] == "match"
    assert found["persona"]["id"] == "fulana"
    assert found["lead"] == "Vou pedir pra Fulana ver."

    linked = _call(action="link_project", path=str(project), persona="Beltrano")
    assert linked["status"] == "linked"
    assert linked["project"]["source"] == "pin"
    assert linked["project"]["persona"] == "beltrano"

    pinned = _call(action="for_project", path=str(project))
    assert pinned["status"] == "match"
    assert pinned["persona"]["id"] == "beltrano"
    assert pinned["source"] == "pin"

    assert _call(action="for_project")["status"] == "error"
    assert _call(action="link_project", path=str(project),
                 persona="Ninguem")["status"] == "error"


def test_action_never_echoes_secret_on_error(env):
    result = _call(action="create", **fields(
        name="Segredo", personality="password=hunter2"))
    assert result["status"] == "error"
    assert "hunter2" not in result["error"]
