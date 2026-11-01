"""Persona store, model catalog and the ``personas`` action.

Everything runs in a TemporaryDirectory: the real memory/ and skills/ are never
touched. The catalog is written by the test, so paid/free rules are explicit.
"""
from __future__ import annotations

import json
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


@pytest.fixture
def env(tmp_path, monkeypatch):
    memory = tmp_path / "memory"
    memory.mkdir()
    skills = tmp_path / "skills"
    for name in ("frontend-design", "ui-styling"):
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
    assert persona["skills"] == ["frontend-design", "ui-styling"]
    assert persona["model"] == "Qwen3.8-Flash"
    assert persona["model_price_factor"] == 0.0
    assert persona["reasoning"] == "medium"
    assert persona["context_size"] == 200000
    assert persona["created_at"] and persona["updated_at"]
    assert persona["aliases"] == []
    assert persona["history"] == []

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


def test_skills_validation(env):
    persona = personas.upsert(fields(name="Skills Vazias", skills=[]))
    assert persona["skills"] == []

    with pytest.raises(ValueError, match="não existe"):
        personas.upsert(fields(name="Skill Ruim", skills=["ghost-skill"]))
    with pytest.raises(ValueError, match="inválido"):
        personas.upsert(fields(name="Skill Path", skills=["../etc/passwd"]))
    with pytest.raises(ValueError, match="não existe"):
        personas.upsert(fields(name="Skill Sem Md", skills=["not-a-skill"]))


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
