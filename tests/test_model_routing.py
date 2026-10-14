"""Tests for auto model routing (``--model auto`` / ``model="auto"``).

Covers the shared policy in :mod:`althing.model_routing`, the routing.json
client in :mod:`althing.synthbench`, blend-aware ``recommend()``, and the
CLI / MCP / SDK surfaces.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from althing import model_routing, synthbench
from althing.model_routing import (
    RouteDecision,
    apply_decision_to_personas,
    classify_question,
    infer_slice,
    infer_topic,
    is_auto_model,
    route_models,
)
from althing.synthbench import LoadedLeaderboard, recommend, runnable_model_for

ROUTING_URL = "https://example.test/routing.json"

_ALL_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "XAI_API_KEY",
    "OPENROUTER_API_KEY",
)


def _cand(config_id: str, label: str, members: list[tuple[str, str, float]], index: float, native: bool = True):
    return {
        "config_id": config_id,
        "label": label,
        "kind": "blend" if len(members) > 1 else "single",
        "althing_native": native,
        "members": [{"model_id": m, "framework": fw, "weight": w} for m, fw, w in members],
        "index": index,
        "ci": [index - 5, index + 5],
        "n": 175,
        "cost_per_100q": None,
    }


BLEND = _cand(
    "ensemble--althing-pair--abc",
    "Ensemble: Qwen 3.8 Flash (Althing), Haiku 4.5 (Althing)",
    [("qwen/qwen3.8-flash", "product", 0.5), ("anthropic/claude-haiku-4.5", "product", 0.5)],
    48.0,
)
QWEN = _cand("althing--qwen3.8-flash--abc", "Althing (Qwen 3.8 Flash)", [("qwen/qwen3.8-flash", "product", 1.0)], 44.0)
SONNET = _cand(
    "althing--claude-sonnet-4--abc", "Althing (Sonnet 4)", [("anthropic/claude-sonnet-4", "product", 1.0)], 52.0
)
RAW = _cand(
    "raw--deepseek--abc", "DeepSeek V4.1 Flash", [("deepseek/deepseek-v4.1-flash", "raw", 1.0)], 60.0, native=False
)
WIDE = _cand(
    "ensemble--wide--abc",
    "Ensemble: four Althing members",
    [
        ("qwen/qwen3.8-flash", "product", 0.4),
        ("anthropic/claude-haiku-4.5", "product", 0.3),
        ("Not A Model (label)", "product", 0.2),
        ("google/gemini-2.5-flash-lite", "product", 0.1),
    ],
    30.0,
)


def _route(slice_id: str, topic: str, candidates: list[dict], *, confident: bool, n: int = 175) -> dict:
    native = [c for c in candidates if c["althing_native"]]
    singles = [c for c in native if c["kind"] == "single"]
    return {
        "slice": slice_id,
        "topic": topic,
        "n_questions": n,
        "confident": confident,
        "best": candidates[0]["config_id"],
        "best_althing": native[0]["config_id"] if native else None,
        "best_single_althing": singles[0]["config_id"] if singles else None,
        "candidates": candidates,
    }


ROUTING: dict[str, Any] = {
    "api": {"name": "synthbench-routing", "version": "1.0.0"},
    "generated_at": "2026-10-14T00:00:00Z",
    "taxonomy": {
        "version": 1,
        "topics": [
            {
                "id": "politics",
                "label": "Politics and policy",
                "description": "",
                "keywords": ["election", "congress", "vote", "president", "nation"],
            },
            {
                "id": "economy",
                "label": "Economy and work",
                "description": "",
                "keywords": ["economy", "inflation", "jobs", "cost of living"],
            },
            {"id": "global", "label": "Global affairs", "description": "", "keywords": ["foreign", "nato"]},
        ],
    },
    "slices": [
        {"id": "us-general", "label": "US general public", "datasets": ["opinionsqa", "gss"]},
        {"id": "us-subgroups", "label": "US demographic subgroups", "datasets": ["subpop"]},
        {"id": "cross-national", "label": "Respondents outside the US", "datasets": ["globalopinionqa"]},
    ],
    "min_questions": 30,
    "routes": [
        _route("us-general", "all", [RAW, BLEND, QWEN], confident=True),
        _route("us-general", "politics", [SONNET, QWEN], confident=True, n=60),
        _route("us-general", "economy", [QWEN, SONNET], confident=False, n=12),
        _route("us-subgroups", "all", [QWEN, SONNET], confident=False, n=100),
        _route("cross-national", "all", [WIDE, QWEN], confident=True, n=100),
    ],
}


def _creds(*names: str):
    allowed = set(names)
    return lambda env: env in allowed


OR_ONLY = _creds("OPENROUTER_API_KEY")


def _route_with(**kwargs: Any) -> RouteDecision:
    kwargs.setdefault("routing", ROUTING)
    kwargs.setdefault("source", "live")
    kwargs.setdefault("has_credential", OR_ONLY)
    kwargs.setdefault("default_model", "haiku")
    return route_models(**kwargs)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SYNTH_PANEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(synthbench.ROUTING_URL_ENV, ROUTING_URL)
    monkeypatch.delenv(synthbench.SYNTHBENCH_OFFLINE_ENV, raising=False)
    monkeypatch.delenv(synthbench.SYNTHBENCH_REFRESH_ENV, raising=False)
    monkeypatch.delenv("ALTHING_MODEL_ALIASES", raising=False)
    for key in _ALL_KEYS:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Slice inference
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("population", "pack", "expected"),
    [
        (None, None, "us-general"),
        (None, "general-consumer", "us-general"),
        ("country=France,Japan", None, "cross-national"),
        ({"country": ["Brazil"]}, None, "cross-national"),
        ("country=United States", None, "us-general"),
        ("country=USA;age=18-29", None, "us-subgroups"),
        ("gender=female", None, "us-subgroups"),
        ({"party": "Democrat"}, "general-consumer", "us-subgroups"),
        (None, "global-respondents", "cross-national"),
        (None, "packs/global-respondents.yaml", "cross-national"),
        (None, "us-subgroups", "us-subgroups"),
        ("country=Germany;age=30-49", None, "cross-national"),
    ],
)
def test_infer_slice(population: Any, pack: str | None, expected: str) -> None:
    slice_id, why = infer_slice(population, pack)
    assert slice_id == expected
    assert why


def test_explicit_use_case_wins_over_inference() -> None:
    d = _route_with(population="country=France", use_case="us-general", topic="all")
    assert d.slice == "us-general"
    assert d.slice_reason == "use case given explicitly"


def test_unknown_use_case_and_topic_raise() -> None:
    with pytest.raises(ValueError, match="unknown use case"):
        _route_with(use_case="mars")
    with pytest.raises(ValueError, match="unknown topic"):
        _route_with(topic="sports")


# ---------------------------------------------------------------------------
# Topic inference
# ---------------------------------------------------------------------------


TOPICS = ROUTING["taxonomy"]["topics"]


def test_keywords_match_whole_words_only() -> None:
    # "nation" is a politics keyword but must not match inside "discrimination".
    assert classify_question("Have you faced discrimination at work?", TOPICS) is None
    assert classify_question("Is the nation on the right track?", TOPICS) == "politics"


def test_keyword_phrases_match_across_whitespace() -> None:
    assert classify_question("How worried are you about the cost  of living?", TOPICS) == "economy"


def test_tie_goes_to_first_listed_topic() -> None:
    # One politics keyword, one economy keyword: politics is listed first.
    assert classify_question("Will the election change inflation?", TOPICS) == "politics"


def test_infer_topic_majority() -> None:
    qs = ["Who gets your vote for Congress?", "Did you vote in the last election?", "How is your job?"]
    topic, why = infer_topic(qs, ROUTING)
    assert topic == "politics"
    assert "2 of 3" in why


def test_infer_topic_without_majority_is_all() -> None:
    qs = [{"text": "Who gets your vote?"}, {"text": "Is inflation a problem?"}, {"text": "Favorite color?"}]
    topic, _why = infer_topic(qs, ROUTING)
    assert topic == "all"


def test_infer_topic_no_questions_is_all() -> None:
    assert infer_topic([], ROUTING)[0] == "all"


# ---------------------------------------------------------------------------
# Selection and the confident fallback chain
# ---------------------------------------------------------------------------


def test_confident_topic_route_is_used() -> None:
    d = _route_with(questions=["Who did you vote for in the election?"])
    assert d.topic == "politics"
    assert d.route_topic == "politics"
    assert d.fallback == "none"
    assert d.confident is True
    assert d.model == "openrouter/anthropic/claude-sonnet-4"
    assert d.model_spec is None
    assert d.config_id == SONNET["config_id"]
    assert "Politics and policy" in d.reason
    assert d.reason.endswith(".")


def test_unconfident_topic_route_falls_back_to_slice_all() -> None:
    d = _route_with(topic="economy")
    assert d.topic == "economy"
    assert d.route_topic == "all"
    assert d.fallback == "slice-all"
    assert d.confident is True
    # best_althing on us-general/all is the blend (the raw row is ignored).
    assert d.model is None
    assert d.model_spec == [("openrouter/qwen/qwen3.8-flash", 0.5), ("openrouter/anthropic/claude-haiku-4.5", 0.5)]
    assert "not confident" in d.reason
    assert "all topics" in d.reason


def test_missing_topic_route_falls_back_to_slice_all() -> None:
    d = _route_with(topic="global")
    assert d.fallback == "slice-all"
    assert "no route" in d.reason


def test_unconfident_slice_all_falls_back_to_default() -> None:
    d = _route_with(population="age=18-29", topic="all")
    assert d.slice == "us-subgroups"
    assert d.fallback == "default"
    assert d.model == "haiku"
    assert d.confident is False
    assert "default model haiku" in d.reason


def test_routing_unavailable_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(synthbench, "load_routing", lambda **_kw: None)
    d = route_models(default_model="gpt-4o-mini", has_credential=OR_ONLY)
    assert d.source == model_routing.SOURCE_UNAVAILABLE
    assert d.model == "gpt-4o-mini"
    assert d.fallback == "default"


def test_allow_blend_false_uses_best_single_althing() -> None:
    d = _route_with(topic="all", allow_blend=False)
    assert d.model == "openrouter/qwen/qwen3.8-flash"
    assert d.model_spec is None


# ---------------------------------------------------------------------------
# Member mapping, dropping and renormalization
# ---------------------------------------------------------------------------


def test_runnable_model_for_mapping() -> None:
    assert runnable_model_for("qwen/qwen3.8-flash", has_credential=OR_ONLY) == "openrouter/qwen/qwen3.8-flash"
    # Direct alias applies only with that provider's credentials.
    assert (
        runnable_model_for("anthropic/claude-haiku-4.5", has_credential=OR_ONLY)
        == "openrouter/anthropic/claude-haiku-4.5"
    )
    assert (
        runnable_model_for("anthropic/claude-haiku-4.5", has_credential=_creds("ANTHROPIC_API_KEY"))
        == "claude-haiku-4-5-20251001"
    )
    assert runnable_model_for("google/gemini-2.5-flash", has_credential=_creds("GOOGLE_API_KEY")) == "gemini-2.5-flash"
    # No alias for this model, so it stays on OpenRouter even with a Gemini key.
    assert (
        runnable_model_for("google/gemini-2.5-flash-lite", has_credential=_creds("GEMINI_API_KEY"))
        == "openrouter/google/gemini-2.5-flash-lite"
    )
    assert runnable_model_for("openrouter/auto") == "openrouter/auto"
    assert runnable_model_for("Althing (Qwen)") is None
    assert runnable_model_for("") is None


def test_non_runnable_member_dropped_and_renormalized() -> None:
    d = _route_with(persona_pack="global-respondents", topic="all")
    assert d.slice == "cross-national"
    spec = dict(d.model_spec or [])
    assert set(spec) == {
        "openrouter/qwen/qwen3.8-flash",
        "openrouter/anthropic/claude-haiku-4.5",
        "openrouter/google/gemini-2.5-flash-lite",
    }
    assert sum(spec.values()) == pytest.approx(1.0)
    # 0.4 / (0.4 + 0.3 + 0.1)
    assert spec["openrouter/qwen/qwen3.8-flash"] == pytest.approx(0.5)
    assert any("Not A Model" in x for x in d.dropped)
    assert "dropped" in d.reason


def test_unreachable_members_dropped_leaving_single_model() -> None:
    # Only an Anthropic key: OpenRouter members cannot run, haiku goes direct.
    d = _route_with(persona_pack="global-respondents", topic="all", has_credential=_creds("ANTHROPIC_API_KEY"))
    assert d.model == "claude-haiku-4-5-20251001"
    assert d.model_spec is None
    assert any("OPENROUTER_API_KEY" in x for x in d.dropped)


def test_no_reachable_members_falls_back_to_default() -> None:
    d = _route_with(persona_pack="global-respondents", topic="all", has_credential=_creds())
    assert d.fallback == "default"
    assert d.model == "haiku"
    assert "can run here" in d.reason


def test_max_members_caps_the_blend() -> None:
    d = _route_with(persona_pack="global-respondents", topic="all", max_members=2)
    assert d.model_spec is not None
    assert [m for m, _ in d.model_spec] == ["openrouter/qwen/qwen3.8-flash", "openrouter/anthropic/claude-haiku-4.5"]
    assert sum(w for _, w in d.model_spec) == pytest.approx(1.0)


def test_apply_decision_splits_personas() -> None:
    d = _route_with(topic="all")
    personas = [{"name": f"P{i}"} for i in range(5)]
    model, persona_models, warnings = apply_decision_to_personas(d, personas)
    assert model == "openrouter/qwen/qwen3.8-flash"
    assert persona_models is not None
    counts: dict[str, int] = {}
    for m in persona_models.values():
        counts[m] = counts.get(m, 0) + 1
    assert counts == {"openrouter/qwen/qwen3.8-flash": 3, "openrouter/anthropic/claude-haiku-4.5": 2}
    assert warnings == []


def test_decision_serialization() -> None:
    d = _route_with(topic="all")
    payload = d.to_dict()
    for key in ("model", "model_spec", "slice", "topic", "confident", "source", "reason"):
        assert key in payload
    assert payload["model_spec"][0] == {"model": "openrouter/qwen/qwen3.8-flash", "weight": 0.5}
    assert d.models_spec_string() == "openrouter/qwen/qwen3.8-flash:0.5,openrouter/anthropic/claude-haiku-4.5:0.5"
    json.dumps(payload)
    assert any("slice=us-general" in line for line in d.format_lines())


def test_is_auto_model() -> None:
    assert is_auto_model("auto")
    assert is_auto_model(" AUTO ")
    assert not is_auto_model("openrouter/auto")
    assert not is_auto_model(None)


# ---------------------------------------------------------------------------
# Blend-aware recommend()
# ---------------------------------------------------------------------------


def _board_entry(**overrides: Any) -> dict[str, Any]:
    entry = {
        "config_id": "ensemble--complementary-4--abc",
        "model": "Ensemble: Qwen 3.8 Flash (Althing), DeepSeek V4.1 Flash",
        "provider_id": "ensemble",
        "model_id": "complementary-4",
        "dataset": "globalopinionqa",
        "framework": "product",
        "is_ensemble": True,
        "sps": 0.83,
        "n": 100,
        "run_count": 3,
        "ensemble_members": [
            {"model_id": "qwen/qwen3.8-flash", "framework": "product"},
            {"model_id": "deepseek/deepseek-v4.1-flash", "framework": "raw"},
        ],
    }
    entry.update(overrides)
    return entry


def test_recommend_ensemble_row_becomes_weighted_members() -> None:
    rec = recommend(":globalopinionqa", leaderboard={"entries": [_board_entry()]})
    assert rec is not None
    assert rec.runnable is True
    assert [m.model for m in rec.members] == [
        "openrouter/qwen/qwen3.8-flash",
        "openrouter/deepseek/deepseek-v4.1-flash",
    ]
    assert [m.framework for m in rec.members] == ["product", "raw"]
    assert [m.weight for m in rec.members] == [0.5, 0.5]
    # Single-model callers get the first runnable member, never "ensemble/complementary-4".
    assert rec.model == "openrouter/qwen/qwen3.8-flash"
    assert rec.model_spec() == [
        ("openrouter/qwen/qwen3.8-flash", 0.5),
        ("openrouter/deepseek/deepseek-v4.1-flash", 0.5),
    ]


def test_recommend_respects_recorded_member_weights() -> None:
    members = [
        {"model_id": "qwen/qwen3.8-flash", "framework": "product", "weight": 3},
        {"model_id": "deepseek/deepseek-v4.1-flash", "framework": "raw", "weight": 1},
    ]
    rec = recommend(":globalopinionqa", leaderboard={"entries": [_board_entry(ensemble_members=members)]})
    assert rec is not None
    assert [m.weight for m in rec.members] == [0.75, 0.25]


def test_recommend_single_row_has_no_members() -> None:
    entry = _board_entry(
        model="Althing (Qwen 3.8 Flash)",
        model_id="qwen/qwen3.8-flash",
        provider_id="althing",
        is_ensemble=False,
        ensemble_members=None,
    )
    rec = recommend(":globalopinionqa", leaderboard={"entries": [entry]})
    assert rec is not None
    assert rec.members == ()
    assert rec.model == "openrouter/qwen/qwen3.8-flash"
    assert rec.model_spec() == [("openrouter/qwen/qwen3.8-flash", 1.0)]


def test_recommend_skips_baseline_rows() -> None:
    board = {
        "entries": [
            _board_entry(
                model="Random Baseline",
                model_id="random-baseline",
                framework="baseline",
                is_baseline=True,
                is_ensemble=False,
                ensemble_members=None,
                sps=0.99,
            ),
            _board_entry(sps=0.5),
        ]
    }
    rec = recommend(":globalopinionqa", leaderboard=board)
    assert rec is not None
    assert rec.is_ensemble is True


# ---------------------------------------------------------------------------
# routing.json client (cache / offline / bundled snapshot)
# ---------------------------------------------------------------------------


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_load_routing_live_fetch_writes_its_own_cache() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=ROUTING, headers={"ETag": '"r1"'})

    with _client(handler) as client:
        loaded = synthbench.load_routing(client=client, warn=lambda _m: None)
    assert loaded is not None
    assert loaded.source == "live"
    assert loaded.leaderboard["routes"] == ROUTING["routes"]
    assert seen == [ROUTING_URL]
    assert synthbench.routing_cache_path().exists()
    assert not synthbench.cache_path().exists()

    # Second call within the TTL is served from cache without the network.
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected network call")

    with _client(explode) as client:
        again = synthbench.load_routing(client=client, warn=lambda _m: None)
    assert again is not None
    assert again.source == "cache"


def test_load_routing_offline_uses_bundled_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(synthbench.SYNTHBENCH_OFFLINE_ENV, "1")
    warnings: list[str] = []
    loaded = synthbench.load_routing(warn=warnings.append)
    assert loaded is not None
    assert loaded.source == "bundled-snapshot"
    assert isinstance(loaded.leaderboard["routes"], list)
    assert any("bundled snapshot" in w for w in warnings)


def test_load_routing_404_uses_bundled_snapshot_and_names_override() -> None:
    warnings: list[str] = []
    with _client(lambda request: httpx.Response(404)) as client:
        loaded = synthbench.load_routing(client=client, warn=warnings.append)
    assert loaded is not None
    assert loaded.source == "bundled-snapshot"
    assert any(synthbench.ROUTING_URL_ENV in w for w in warnings)


def test_load_routing_rejects_document_without_routes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(synthbench, "_BUNDLED_ROUTING_SNAPSHOT_PATH", tmp_path / "missing.json")
    with _client(lambda request: httpx.Response(200, json={"entries": []})) as client:
        loaded = synthbench.load_routing(client=client, warn=lambda _m: None)
    assert loaded is None


def test_route_models_offline_on_bundled_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(synthbench.SYNTHBENCH_OFFLINE_ENV, "1")
    for slice_id in ("us-general", "us-subgroups", "cross-national"):
        d = route_models(use_case=slice_id, default_model="haiku", has_credential=OR_ONLY, warn=lambda _m: None)
        assert d.source == "bundled-snapshot"
        assert d.slice == slice_id
        assert d.reason.endswith(".")
        assert (d.model is None) != (d.model_spec is None)


def test_bundled_routing_snapshot_matches_contract_shape() -> None:
    path = synthbench._BUNDLED_ROUTING_SNAPSHOT_PATH
    assert path.exists(), "routing snapshot must ship in package data"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["api"]["name"] == "synthbench-routing"
    assert doc["api"]["version"].startswith("1.")
    topic_ids = [t["id"] for t in doc["taxonomy"]["topics"]]
    assert topic_ids == ["politics", "economy", "technology", "science-health", "identity", "society", "global"]
    for t in doc["taxonomy"]["topics"]:
        assert t["keywords"], t["id"]
        assert all(k == k.lower() for k in t["keywords"])
    assert {s["id"] for s in doc["slices"]} == {"us-general", "us-subgroups", "cross-national"}
    for route in doc["routes"]:
        assert route["topic"] == "all" or route["topic"] in topic_ids
        cands = route["candidates"]
        assert len(cands) <= 12
        assert [c["index"] for c in cands] == sorted((c["index"] for c in cands), reverse=True)
        ids = {c["config_id"] for c in cands}
        for key in ("best", "best_althing", "best_single_althing"):
            assert route[key] is None or route[key] in ids
        for c in cands:
            assert c["kind"] in ("single", "blend")
            assert c["althing_native"] == all(m["framework"] == "product" for m in c["members"])


def test_bundled_leaderboard_snapshot_is_trimmed_and_current() -> None:
    doc = json.loads(synthbench._BUNDLED_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert doc["_meta"]["snapshot_date"] >= "2026-10-01"
    assert not any(e.get("is_baseline") for e in doc["entries"])
    assert not any("topic_metrics" in e for e in doc["entries"])
    assert any(e.get("ensemble_members") for e in doc["entries"])


# ---------------------------------------------------------------------------
# CLI: panel run --model auto
# ---------------------------------------------------------------------------


def _patch_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        synthbench,
        "load_routing",
        lambda **_kw: LoadedLeaderboard(ROUTING, datetime.now(timezone.utc), source="live"),
    )


def _write_inputs(tmp_path: Path, n_personas: int = 4, question: str = "Is inflation hurting jobs?"):
    personas = tmp_path / "personas.yaml"
    personas.write_text(
        "personas:\n" + "".join(f"  - name: P{i}\n    age: {30 + i}\n" for i in range(n_personas)),
        encoding="utf-8",
    )
    survey = tmp_path / "survey.yaml"
    survey.write_text(f"instrument:\n  questions:\n    - text: {question}\n", encoding="utf-8")
    return personas, survey


def _fake_panel_results(**kwargs):
    from althing.cost import TokenUsage as CostTokenUsage
    from althing.orchestrator import PanelistResult

    persona_models = kwargs.get("persona_models") or {}
    results = [
        PanelistResult(
            persona_name=p["name"],
            responses=[{"question": "Q1", "response": "ok", "error": False}],
            usage=CostTokenUsage(input_tokens=10, output_tokens=5),
            model=persona_models.get(p["name"], kwargs["model"]),
        )
        for p in kwargs["personas"]
    ]
    return results, MagicMock(), {p["name"]: MagicMock() for p in kwargs["personas"]}


class TestCliModelAuto:
    @patch("althing.cli.commands.synthesize_panel")
    @patch("althing.cli.commands.run_panel_parallel")
    @patch("althing.cli.commands.LLMClient")
    def test_blend_decision_runs_as_split_and_is_recorded(
        self, _client_cls, mock_rpp, mock_synth, monkeypatch, tmp_path, capsys
    ) -> None:
        from althing.main import main

        _patch_routing(monkeypatch)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        mock_rpp.side_effect = _fake_panel_results
        mock_synth.return_value = None
        personas, survey = _write_inputs(tmp_path)

        code = main(
            [
                "--output-format",
                "json",
                "panel",
                "run",
                "--personas",
                str(personas),
                "--instrument",
                str(survey),
                "--model",
                "auto",
                "--no-synthesis",
                "--skip-preflight",
            ]
        )
        captured = capsys.readouterr()
        assert code == 0, captured.err
        assert "model routing:" in captured.err
        assert "slice=us-general topic=economy" in captured.err

        kwargs = mock_rpp.call_args.kwargs
        persona_models = kwargs["persona_models"]
        assert sorted(persona_models.values()) == [
            "openrouter/anthropic/claude-haiku-4.5",
            "openrouter/anthropic/claude-haiku-4.5",
            "openrouter/qwen/qwen3.8-flash",
            "openrouter/qwen/qwen3.8-flash",
        ]

        data = json.loads(captured.out)
        routing = data["metadata"]["model_routing"]
        assert routing["slice"] == "us-general"
        assert routing["topic"] == "economy"
        assert routing["fallback"] == "slice-all"
        assert routing["confident"] is True
        assert routing["source"] == "live"
        assert [m["model"] for m in routing["model_spec"]] == [
            "openrouter/qwen/qwen3.8-flash",
            "openrouter/anthropic/claude-haiku-4.5",
        ]
        assert routing["reason"]

    @patch("althing.cli.commands.synthesize_panel")
    @patch("althing.cli.commands.run_panel_parallel")
    @patch("althing.cli.commands.LLMClient")
    def test_single_model_decision_with_explicit_topic(
        self, _client_cls, mock_rpp, mock_synth, monkeypatch, tmp_path, capsys
    ) -> None:
        from althing.main import main

        _patch_routing(monkeypatch)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        mock_rpp.side_effect = _fake_panel_results
        personas, survey = _write_inputs(tmp_path)

        code = main(
            [
                "--output-format",
                "json",
                "panel",
                "run",
                "--personas",
                str(personas),
                "--instrument",
                str(survey),
                "--model",
                "auto",
                "--topic",
                "politics",
                "--no-synthesis",
            ]
        )
        captured = capsys.readouterr()
        assert code == 0, captured.err
        assert mock_rpp.call_args.kwargs["model"] == "openrouter/anthropic/claude-sonnet-4"
        routing = json.loads(captured.out)["metadata"]["model_routing"]
        assert routing["model"] == "openrouter/anthropic/claude-sonnet-4"
        assert routing["topic"] == "politics"
        assert routing["topic_reason"] == "topic given explicitly"

    @pytest.mark.parametrize(
        ("extra", "message"),
        [
            (["--model", "auto", "--models", "haiku:0.5,sonnet:0.5"], "mutually exclusive"),
            (["--model", "auto", "--best-model-for", "Economy & Work"], "mutually exclusive"),
            (["--topic", "politics"], "only applies with --model auto"),
            (["--use-case", "cross-national"], "only applies with --model auto"),
            (["--model", "auto", "--topic", "sports"], "unknown topic"),
        ],
    )
    def test_invalid_combinations(self, extra, message, monkeypatch, tmp_path, capsys) -> None:
        from althing.main import main

        _patch_routing(monkeypatch)
        personas, survey = _write_inputs(tmp_path)
        code = main(["panel", "run", "--personas", str(personas), "--instrument", str(survey), *extra])
        assert code == 1
        assert message in capsys.readouterr().err


# ---------------------------------------------------------------------------
# MCP: model="auto"
# ---------------------------------------------------------------------------


class TestMcpModelAuto:
    @pytest.fixture(autouse=True)
    def _mcp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("mcp")
        _patch_routing(monkeypatch)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    @pytest.mark.asyncio
    async def test_run_panel_auto_splits_blend_and_returns_decision(self) -> None:
        from althing.mcp.server import mcp

        personas = [{"name": f"P{i}"} for i in range(4)]
        with patch("althing.mcp.server._run_panel_async", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {"results": [], "metadata": {}}
            result = await mcp.call_tool(
                "run_panel",
                {
                    "personas": personas,
                    "questions": [{"text": "Is inflation hurting jobs?"}],
                    "model": "auto",
                    "synthesis": False,
                    "decision_being_informed": "auto model routing check",
                },
            )
        data = json.loads(result.content[0].text)
        assert mock_run.call_args[0][2] == "openrouter/qwen/qwen3.8-flash"
        persona_models = mock_run.call_args.kwargs["persona_models"]
        assert set(persona_models.values()) == {
            "openrouter/qwen/qwen3.8-flash",
            "openrouter/anthropic/claude-haiku-4.5",
        }
        assert data["model_routing"]["topic"] == "economy"
        assert data["model_routing"]["slice"] == "us-general"
        assert data["metadata"]["model_routing"] == data["model_routing"]

    @pytest.mark.asyncio
    async def test_run_quick_poll_auto_with_use_case(self) -> None:
        from althing.mcp.server import mcp

        with patch("althing.mcp.server._run_panel_async", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {"results": []}
            result = await mcp.call_tool(
                "run_quick_poll",
                {
                    "question": "Who did you vote for?",
                    "personas": [{"name": "A"}, {"name": "B"}],
                    "model": "auto",
                    "use_case": "us-subgroups",
                    "synthesis": False,
                    "decision_being_informed": "auto model routing quick poll",
                },
            )
        data = json.loads(result.content[0].text)
        routing = data["model_routing"]
        assert routing["slice"] == "us-subgroups"
        assert routing["fallback"] == "default"
        assert mock_run.call_args[0][2] == routing["model"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("args", "field"),
        [
            ({"model": "auto", "models": ["haiku", "sonnet"]}, "models"),
            ({"model": "auto", "persona_models": {"P0": "haiku"}}, "persona_models"),
            ({"use_case": "us-general"}, "use_case"),
            ({"model": "auto", "topic": "sports"}, "topic"),
        ],
    )
    async def test_run_panel_invalid_auto_args(self, args, field) -> None:
        from althing.mcp.server import mcp

        payload = {
            "personas": [{"name": "P0"}],
            "questions": [{"text": "Hello?"}],
            "decision_being_informed": "auto model routing invalid args",
            **args,
        }
        with patch("althing.mcp.server._run_panel_async", new_callable=AsyncMock) as mock_run:
            result = await mcp.call_tool("run_panel", payload)
        data = json.loads(result.content[0].text)
        assert data.get("error_code") == "INVALID_TOOL_ARG", data
        assert field in json.dumps(data)
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# SDK: run_panel(model="auto")
# ---------------------------------------------------------------------------


def test_sdk_run_panel_auto_records_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    from althing import sdk

    _patch_routing(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    captured: dict[str, Any] = {}

    def fake_run_panel_sync(**kwargs):
        captured.update(kwargs)
        results, _reg, _sessions = _fake_panel_results(**kwargs)
        from althing.cost import ZERO_USAGE, CostEstimate

        return results, [], ZERO_USAGE, CostEstimate(), None, None

    monkeypatch.setattr(sdk, "run_panel_sync", fake_run_panel_sync)
    monkeypatch.setattr(sdk, "save_panel_result", lambda **kw: "result-test")
    panel = sdk.run_panel(
        personas=[{"name": f"P{i}"} for i in range(3)],
        questions=["Who did you vote for in the election?"],
        model="auto",
        synthesis=False,
    )
    assert captured["model"] == "openrouter/anthropic/claude-sonnet-4"
    assert panel.metadata["model_routing"]["topic"] == "politics"


def test_sdk_rejects_topic_without_auto() -> None:
    from althing import sdk

    with pytest.raises(ValueError, match="only apply with model='auto'"):
        sdk.run_panel(personas=[{"name": "A"}], questions=["Hi"], model="haiku", topic="politics")
