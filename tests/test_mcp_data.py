"""Tests for althing.mcp.data persistence layer."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    """Point SYNTH_PANEL_DATA_DIR at a temp directory for every test."""
    monkeypatch.setenv("SYNTH_PANEL_DATA_DIR", str(tmp_path))


# --- import after env is set so _data_dir() picks it up ---
from althing.mcp.data import (
    PackValidationError,
    get_panel_result,
    get_persona_pack,
    list_instrument_packs,
    list_panel_results,
    list_persona_packs,
    load_instrument_pack,
    load_panel_sessions,
    save_instrument_pack,
    save_panel_result,
    save_panel_sessions,
    save_persona_pack,
    update_panel_result,
    validate_persona_pack,
)
from althing.persistence import ConversationMessage, Session

# ---------------------------------------------------------------------------
# Persona packs
# ---------------------------------------------------------------------------


class TestPersonaPacks:
    def test_list_includes_bundled(self):
        """With no user packs, bundled packs are still listed."""
        packs = list_persona_packs()
        builtin = [p for p in packs if p.get("builtin")]
        names = {p["id"] for p in builtin}
        # sp-6wbm added four scale-up packs (job-seekers, recruiters-talent,
        # product-research, ai-eval-buyers) to lift the 24-persona ceiling
        # imposed by the original five packs. sp-b8y47x added students.
        # sy-a0i / gh-476 added four canonical packs for common agent
        # research jobs (skeptical-executives, market-research-critics,
        # broad-professionals, enterprise-ai-buyers). global-respondents is
        # the first population pack (althing.population).
        assert names == {
            "developer",
            "enterprise-buyer",
            "general-consumer",
            "healthcare-patient",
            "startup-founder",
            "job-seekers",
            "recruiters-talent",
            "product-research",
            "ai-eval-buyers",
            "students",
            "skeptical-executives",
            "market-research-critics",
            "broad-professionals",
            "enterprise-ai-buyers",
            "global-respondents",
        }
        assert len(builtin) == 15

    def test_sp_6wbm_scaleup_packs_load(self):
        """sp-6wbm: each new scale-up pack loads, has a non-empty
        persona list with valid shape, and does not collide with any
        existing bundled persona name."""
        import collections

        seen: dict[str, str] = {}
        # Collect names from the original five to detect cross-pack dups.
        for base in (
            "developer",
            "enterprise-buyer",
            "general-consumer",
            "healthcare-patient",
            "startup-founder",
        ):
            for p in get_persona_pack(base)["personas"]:
                n = p.get("name", "").strip()
                if n:
                    seen[n] = base

        expected = {
            "job-seekers": 15,
            "recruiters-talent": 15,
            "product-research": 20,
            "ai-eval-buyers": 20,
        }
        for pack_id, min_count in expected.items():
            pack = get_persona_pack(pack_id)
            assert pack["id"] == pack_id
            personas = pack["personas"]
            assert len(personas) == min_count, f"{pack_id}: expected {min_count}, got {len(personas)}"
            local = collections.Counter(p.get("name", "").strip() for p in personas)
            for n, cnt in local.items():
                assert cnt == 1, f"{pack_id}: duplicate name {n!r}"
                assert n not in seen, f"{pack_id}: name {n!r} collides with bundled pack {seen[n]}"
                seen[n] = pack_id
            # Shape sanity: every persona has a non-empty name and at least one content field.
            for p in personas:
                assert p.get("name"), f"{pack_id}: persona missing name"
                assert any(p.get(k) for k in ("background", "occupation", "personality_traits")), (
                    f"{pack_id}: persona {p.get('name')!r} has no background/occupation/traits"
                )

    def test_save_and_list(self):
        personas = [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]
        result = save_persona_pack("Test Pack", personas)
        assert result["name"] == "Test Pack"
        assert result["persona_count"] == 2
        assert "id" in result

        packs = list_persona_packs()
        user_packs = [p for p in packs if not p.get("builtin")]
        assert len(user_packs) == 1
        assert user_packs[0]["name"] == "Test Pack"
        assert user_packs[0]["persona_count"] == 2

    def test_save_with_custom_id(self):
        result = save_persona_pack("Custom", [{"name": "Eve"}], pack_id="my-pack")
        assert result["id"] == "my-pack"

    def test_get_pack(self):
        save_persona_pack("Get Test", [{"name": "Charlie"}], pack_id="get-test")
        pack = get_persona_pack("get-test")
        assert pack["name"] == "Get Test"
        assert pack["id"] == "get-test"
        assert len(pack["personas"]) == 1

    def test_get_bundled_pack(self):
        pack = get_persona_pack("developer")
        assert pack["name"] == "Developers"
        assert pack["id"] == "developer"
        assert len(pack["personas"]) >= 3

    def test_user_pack_overrides_bundled(self):
        save_persona_pack("My Devs", [{"name": "Custom Dev"}], pack_id="developer")
        pack = get_persona_pack("developer")
        assert pack["name"] == "My Devs"
        assert len(pack["personas"]) == 1

        # User override appears in list as non-builtin
        packs = list_persona_packs()
        dev_packs = [p for p in packs if p["id"] == "developer"]
        assert len(dev_packs) == 1
        assert dev_packs[0]["builtin"] is False

    def test_get_nonexistent_raises(self):
        with pytest.raises(FileNotFoundError):
            get_persona_pack("does-not-exist")

    def test_overwrite_pack(self):
        save_persona_pack("V1", [{"name": "A"}], pack_id="ow")
        save_persona_pack("V2", [{"name": "A"}, {"name": "B"}], pack_id="ow")
        pack = get_persona_pack("ow")
        assert pack["name"] == "V2"
        assert len(pack["personas"]) == 2


# ---------------------------------------------------------------------------
# Pack validation
# ---------------------------------------------------------------------------


class TestPackValidation:
    def test_valid_minimal(self):
        result = validate_persona_pack([{"name": "Alice"}])
        assert result == [{"name": "Alice"}]

    def test_not_a_list(self):
        with pytest.raises(PackValidationError, match="must be a list"):
            validate_persona_pack("not a list")

    def test_empty_list(self):
        with pytest.raises(PackValidationError, match="must not be empty"):
            validate_persona_pack([])

    def test_persona_not_dict(self):
        with pytest.raises(PackValidationError, match="must be a dict"):
            validate_persona_pack(["just a string"])

    def test_missing_name(self):
        with pytest.raises(PackValidationError, match="missing required field 'name'"):
            validate_persona_pack([{"age": 30}])

    def test_blank_name(self):
        with pytest.raises(PackValidationError, match="missing required field 'name'"):
            validate_persona_pack([{"name": "  "}])

    def test_trait_normalization_list(self):
        result = validate_persona_pack(
            [
                {
                    "name": "Alice",
                    "personality_traits": ["Curious", " BOLD ", "shy"],
                }
            ]
        )
        assert result[0]["personality_traits"] == ["curious", "bold", "shy"]

    def test_trait_normalization_csv_string(self):
        result = validate_persona_pack(
            [
                {
                    "name": "Bob",
                    "personality_traits": "Curious, Bold, shy",
                }
            ]
        )
        assert result[0]["personality_traits"] == ["curious", "bold", "shy"]

    def test_trait_invalid_type(self):
        with pytest.raises(PackValidationError, match="personality_traits must be"):
            validate_persona_pack([{"name": "X", "personality_traits": 42}])

    def test_no_mutation_of_input(self):
        original = [{"name": "Alice", "personality_traits": ["LOUD"]}]
        validate_persona_pack(original)
        assert original[0]["personality_traits"] == ["LOUD"]

    def test_save_validates(self):
        with pytest.raises(PackValidationError):
            save_persona_pack("Bad", [{"age": 30}])


# ---------------------------------------------------------------------------
# Panel results
# ---------------------------------------------------------------------------


class TestPanelResults:
    def test_list_empty(self):
        assert list_panel_results() == []

    def test_save_and_list(self):
        rid = save_panel_result(
            results=[{"persona": "Alice", "responses": []}],
            model="haiku",
            total_usage={"input_tokens": 100, "output_tokens": 50},
            total_cost="$0.001",
            persona_count=1,
            question_count=2,
        )
        assert rid.startswith("result-")

        results = list_panel_results()
        assert len(results) == 1
        assert results[0]["model"] == "haiku"
        assert results[0]["persona_count"] == 1

    def test_get_result(self):
        rid = save_panel_result(
            results=[{"persona": "Bob", "responses": [{"q": "hi", "a": "hello"}]}],
            model="sonnet",
            total_usage={"input_tokens": 200, "output_tokens": 100},
            total_cost="$0.01",
            persona_count=1,
            question_count=1,
        )
        result = get_panel_result(rid)
        assert result["model"] == "sonnet"
        assert result["id"] == rid
        assert len(result["results"]) == 1

    def test_get_nonexistent_raises(self):
        with pytest.raises(FileNotFoundError):
            get_panel_result("does-not-exist")

    def test_pre_extend_excluded_from_list(self):
        """Pre-extend snapshots should not appear in list_panel_results."""
        rid = save_panel_result(
            results=[],
            model="haiku",
            total_usage={"input_tokens": 0, "output_tokens": 0},
            total_cost="$0.00",
            persona_count=0,
            question_count=0,
        )
        update_panel_result(rid, {"model": "haiku", "extended": True})
        results = list_panel_results()
        ids = [r["id"] for r in results]
        assert rid in ids
        assert f"{rid}.pre-extend" not in ids


class TestPanelResultSchemaExpansion:
    """Tests for expanded save_panel_result schema (sp-5on.6)."""

    def test_instrument_metadata_round_trip(self):
        """instrument_name, questions, variants_config, models round-trip."""
        questions = [
            {"text": "How do you feel?", "extraction_schema": {"type": "object"}},
            {"text": "Rate 1-10"},
        ]
        rid = save_panel_result(
            results=[{"persona": "Alice", "responses": []}],
            model="sonnet",
            total_usage={"input_tokens": 100, "output_tokens": 50},
            total_cost="$0.01",
            persona_count=1,
            question_count=2,
            instrument_name="pricing-discovery",
            questions=questions,
            variants_config={"n": 3, "seed": 42},
            models=["sonnet", "haiku"],
        )
        result = get_panel_result(rid)
        assert result["instrument_name"] == "pricing-discovery"
        assert result["questions"] == questions
        assert result["variants_config"] == {"n": 3, "seed": 42}
        assert result["models"] == ["sonnet", "haiku"]

    def test_new_fields_optional_backward_compat(self):
        """Existing results without new fields still load fine."""
        rid = save_panel_result(
            results=[{"persona": "Bob", "responses": []}],
            model="haiku",
            total_usage={"input_tokens": 10, "output_tokens": 5},
            total_cost="$0.001",
            persona_count=1,
            question_count=1,
        )
        result = get_panel_result(rid)
        assert "instrument_name" not in result
        assert "questions" not in result
        assert "variants_config" not in result
        assert "models" not in result

    def test_per_result_variant_and_model_fields(self):
        """Per-result _variant_of and _model pass through."""
        results = [
            {
                "persona": "Alice",
                "_variant_of": "Alice",
                "_model": "sonnet",
                "responses": [{"question": "Q1", "response": "A1"}],
            },
            {
                "persona": "Alice (v2)",
                "_variant_of": "Alice",
                "_model": "haiku",
                "responses": [{"question": "Q1", "response": "A1b"}],
            },
        ]
        rid = save_panel_result(
            results=results,
            model="sonnet",
            total_usage={"input_tokens": 200, "output_tokens": 100},
            total_cost="$0.02",
            persona_count=2,
            question_count=1,
        )
        loaded = get_panel_result(rid)
        assert loaded["results"][0]["_variant_of"] == "Alice"
        assert loaded["results"][0]["_model"] == "sonnet"
        assert loaded["results"][1]["_variant_of"] == "Alice"
        assert loaded["results"][1]["_model"] == "haiku"

    def test_per_response_extraction_data(self):
        """Per-response extraction dicts pass through."""
        results = [
            {
                "persona": "Alice",
                "responses": [
                    {
                        "question": "Rate this product",
                        "response": "I'd give it an 8 out of 10",
                        "extraction": {"rating": 8, "sentiment": "positive"},
                        "extraction_is_fallback": False,
                    },
                ],
            },
        ]
        rid = save_panel_result(
            results=results,
            model="sonnet",
            total_usage={"input_tokens": 150, "output_tokens": 75},
            total_cost="$0.015",
            persona_count=1,
            question_count=1,
        )
        loaded = get_panel_result(rid)
        resp = loaded["results"][0]["responses"][0]
        assert resp["extraction"] == {"rating": 8, "sentiment": "positive"}
        assert resp["extraction_is_fallback"] is False

    def test_list_surfaces_instrument_name_and_models(self):
        """list_panel_results includes instrument_name and models when present."""
        save_panel_result(
            results=[],
            model="sonnet",
            total_usage={},
            total_cost="$0.00",
            persona_count=0,
            question_count=0,
            instrument_name="ux-friction",
            models=["sonnet", "haiku"],
        )
        listing = list_panel_results()
        assert len(listing) == 1
        assert listing[0]["instrument_name"] == "ux-friction"
        assert listing[0]["models"] == ["sonnet", "haiku"]

    def test_list_omits_new_fields_when_absent(self):
        """list_panel_results omits instrument_name/models for old results."""
        save_panel_result(
            results=[],
            model="haiku",
            total_usage={},
            total_cost="$0.00",
            persona_count=0,
            question_count=0,
        )
        listing = list_panel_results()
        assert len(listing) == 1
        assert "instrument_name" not in listing[0]
        assert "models" not in listing[0]

    def test_synthesis_round_trip(self):
        """hq-2p32: synthesis kwarg is persisted at top level and round-trips."""
        synthesis = {
            "summary": "panelists liked option B",
            "themes": ["pricing", "ergonomics"],
            "agreements": ["B feels fair"],
            "disagreements": ["color choice"],
            "surprises": ["nobody mentioned weight"],
            "recommendation": "ship B at $29",
            "usage": {"input_tokens": 2000, "output_tokens": 600},
            "cost": "$0.0907",
            "model": "claude-sonnet-4-6",
            "prompt_version": 5,
        }
        rid = save_panel_result(
            results=[{"persona": "Alice", "responses": []}],
            model="sonnet",
            total_usage={"input_tokens": 100, "output_tokens": 50},
            total_cost="$0.10",
            persona_count=1,
            question_count=2,
            synthesis=synthesis,
        )
        loaded = get_panel_result(rid)
        assert loaded["synthesis"] == synthesis

    def test_synthesis_round_trip_with_attachments(self):
        """hq-2p32: synthesis persists alongside attachment refs (the bug shape)."""
        synthesis = {
            "summary": "image-bearing panel synthesis",
            "themes": ["visual", "trust"],
            "agreements": [],
            "disagreements": [],
            "surprises": [],
            "recommendation": "use clearer labels",
            "usage": {"input_tokens": 5000, "output_tokens": 800},
            "cost": "$0.0907",
            "model": "claude-sonnet-4-6",
            "prompt_version": 5,
        }
        attachments = {
            "att-q0-0": {
                "id": "att-q0-0",
                "kind": "image",
                "sha256": "0" * 64,
                "content_type": "image/png",
                "byte_size": 1024,
            }
        }
        rid = save_panel_result(
            results=[{"persona": "Alice", "responses": []}],
            model="sonnet",
            total_usage={"input_tokens": 5100, "output_tokens": 850},
            total_cost="$0.11",
            persona_count=1,
            question_count=3,
            attachments=attachments,
            synthesis=synthesis,
        )
        loaded = get_panel_result(rid)
        # Attachment-bearing save bumps schema to 1.1 — synthesis must
        # still ride along on the same JSON.
        assert loaded["result_format_version"] == "1.1"
        assert loaded["synthesis"] == synthesis

    def test_synthesis_omitted_when_none(self):
        """Default (no synthesis arg) keeps the legacy shape."""
        rid = save_panel_result(
            results=[],
            model="haiku",
            total_usage={},
            total_cost="$0.00",
            persona_count=0,
            question_count=0,
        )
        loaded = get_panel_result(rid)
        assert "synthesis" not in loaded

    def test_full_schema_save_load(self):
        """End-to-end round-trip with all new fields populated."""
        questions = [
            {
                "text": "What frustrates you?",
                "extraction_schema": {"type": "object", "properties": {"themes": {"type": "array"}}},
            },
        ]
        results = [
            {
                "persona": "Sarah",
                "_variant_of": "Sarah",
                "_model": "sonnet",
                "responses": [
                    {
                        "question": "What frustrates you?",
                        "response": "Slow load times",
                        "extraction": {"themes": ["performance"]},
                    },
                ],
            },
        ]
        rid = save_panel_result(
            results=results,
            model="sonnet",
            total_usage={"input_tokens": 500, "output_tokens": 200},
            total_cost="$0.05",
            persona_count=1,
            question_count=1,
            instrument_name="ux-friction",
            questions=questions,
            variants_config={"n": 2, "seed": 123},
            models=["sonnet"],
        )
        loaded = get_panel_result(rid)
        assert loaded["instrument_name"] == "ux-friction"
        assert loaded["questions"] == questions
        assert loaded["variants_config"] == {"n": 2, "seed": 123}
        assert loaded["models"] == ["sonnet"]
        assert loaded["results"][0]["_variant_of"] == "Sarah"
        assert loaded["results"][0]["_model"] == "sonnet"
        assert loaded["results"][0]["responses"][0]["extraction"] == {"themes": ["performance"]}


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------


class TestSessionPersistence:
    def _make_session(self, persona_name: str) -> Session:
        """Create a session with a user message and assistant reply."""
        s = Session()
        s.push_message(
            ConversationMessage(
                role="user",
                content=[{"type": "text", "text": f"Hello {persona_name}"}],
            )
        )
        s.push_message(
            ConversationMessage(
                role="assistant",
                content=[{"type": "text", "text": f"I am {persona_name}"}],
            )
        )
        return s

    def test_save_and_load_round_trip(self):
        """Sessions round-trip: save → load → messages preserved."""
        sessions = {
            "Sarah Chen": self._make_session("Sarah Chen"),
            "Marcus Johnson": self._make_session("Marcus Johnson"),
        }
        rid = save_panel_result(
            results=[],
            model="haiku",
            total_usage={"input_tokens": 0, "output_tokens": 0},
            total_cost="$0.00",
            persona_count=2,
            question_count=1,
        )
        save_panel_sessions(rid, sessions)
        loaded = load_panel_sessions(rid)

        assert len(loaded) == 2
        for _name, session in loaded.items():
            assert len(session.messages) == 2
            # Verify assistant message text contains persona name
            assistant_text = session.messages[1].content[0]["text"]
            assert "I am" in assistant_text

    def test_load_nonexistent_raises(self):
        with pytest.raises(FileNotFoundError):
            load_panel_sessions("no-such-result")

    def test_save_creates_directory(self, tmp_path):
        """save_panel_sessions creates the sessions directory."""
        rid = save_panel_result(
            results=[],
            model="haiku",
            total_usage={},
            total_cost="$0.00",
            persona_count=1,
            question_count=0,
        )
        sessions = {"Alice": self._make_session("Alice")}
        path = save_panel_sessions(rid, sessions)
        assert path.is_dir()
        assert (path / "Alice.json").exists()

    def test_empty_sessions_dict(self):
        rid = save_panel_result(
            results=[],
            model="haiku",
            total_usage={},
            total_cost="$0.00",
            persona_count=0,
            question_count=0,
        )
        path = save_panel_sessions(rid, {})
        assert path.is_dir()
        assert list(path.glob("*.json")) == []

    def test_persona_name_with_slash_sanitised(self):
        """Persona names with slashes are URL-encoded in filenames."""
        sessions = {"Dr. A/B": self._make_session("Dr. A/B")}
        rid = save_panel_result(
            results=[],
            model="haiku",
            total_usage={},
            total_cost="$0.00",
            persona_count=1,
            question_count=0,
        )
        path = save_panel_sessions(rid, sessions)
        assert (path / "Dr.%20A%2FB.json").exists()


# ---------------------------------------------------------------------------
# Update panel result (pre-extend snapshot)
# ---------------------------------------------------------------------------


class TestUpdatePanelResult:
    def test_creates_pre_extend_snapshot(self, tmp_path):
        rid = save_panel_result(
            results=[{"persona": "Alice", "responses": []}],
            model="haiku",
            total_usage={"input_tokens": 100, "output_tokens": 50},
            total_cost="$0.001",
            persona_count=1,
            question_count=1,
        )
        get_panel_result(rid)  # verify result exists before update

        update_panel_result(rid, {"model": "haiku", "extended": True})

        # Snapshot exists with original data
        from althing.mcp.data import _results_dir

        snapshot = _results_dir() / f"{rid}.pre-extend.json"
        assert snapshot.exists()
        snap_data = json.loads(snapshot.read_text())
        assert snap_data["model"] == "haiku"
        assert snap_data["total_cost"] == "$0.001"

        # Main result updated
        updated = get_panel_result(rid)
        assert updated.get("extended") is True

    def test_update_nonexistent_raises(self):
        with pytest.raises(FileNotFoundError):
            update_panel_result("no-such-result", {})

    def test_overwrites_previous_snapshot(self):
        rid = save_panel_result(
            results=[],
            model="haiku",
            total_usage={},
            total_cost="$0.00",
            persona_count=0,
            question_count=0,
        )
        update_panel_result(rid, {"version": 1})
        update_panel_result(rid, {"version": 2})

        # Snapshot should be from the version=1 update, not original
        from althing.mcp.data import _results_dir

        snapshot = _results_dir() / f"{rid}.pre-extend.json"
        snap_data = json.loads(snapshot.read_text())
        assert snap_data.get("version") == 1


# ---------------------------------------------------------------------------
# Instrument packs (F2-B)
# ---------------------------------------------------------------------------


class TestInstrumentPacks:
    def test_save_list_load_roundtrip(self):
        body = {
            "name": "pricing-probe",
            "version": "1.0.0",
            "description": "Branching probe for pricing-sensitive panels",
            "author": "althing",
            "instrument": {
                "version": 3,
                "rounds": [
                    {"name": "intro", "questions": [{"text": "Q1"}]},
                ],
            },
        }
        meta = save_instrument_pack("pricing-probe", body)
        assert meta["name"] == "pricing-probe"
        assert meta["version"] == "1.0.0"
        assert meta["type"] == "instrument"

        listing = list_instrument_packs()
        saved = [p for p in listing if p["id"] == "pricing-probe"]
        assert len(saved) == 1
        assert saved[0]["author"] == "althing"

        loaded = load_instrument_pack("pricing-probe")
        # round-trip preserves the body
        assert loaded["instrument"]["version"] == 3
        assert loaded["description"] == body["description"]
        assert loaded["id"] == "pricing-probe"

    def test_load_missing_raises(self):
        with pytest.raises(FileNotFoundError):
            load_instrument_pack("nope")

    def test_save_fills_default_name(self):
        save_instrument_pack("auto", {"instrument": {"version": 1, "questions": [{"text": "Q"}]}})
        loaded = load_instrument_pack("auto")
        assert loaded["name"] == "auto"

    def test_list_contains_only_bundled_packs(self):
        # Before any user saves, the list should contain only bundled packs
        packs = list_instrument_packs()
        assert len(packs) >= 1  # at least bundled packs exist
        # No user-saved pack dir files should exist yet in our temp env
        user_dir = Path(os.environ.get("SYNTH_PANEL_DATA_DIR", "")) / "packs" / "instruments"
        if user_dir.exists():
            assert list(user_dir.glob("*.yaml")) == []

    def test_save_rejects_non_mapping(self):
        with pytest.raises(ValueError):
            save_instrument_pack("bad", ["not", "a", "dict"])  # type: ignore[arg-type]
