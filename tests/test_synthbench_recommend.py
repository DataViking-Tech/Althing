"""Tests for the SynthBench best-model picker (sp-zq3).

Covers fetch + cache semantics (mirrors ``test_registry_cache.py``),
ranking by topic score vs SPS, ensemble fallback, offline mode, and
parse errors.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from althing import synthbench
from althing.synthbench import (
    CACHE_TTL,
    SYNTHBENCH_OFFLINE_ENV,
    SYNTHBENCH_REFRESH_ENV,
    SYNTHBENCH_URL_ENV,
    cache_path,
    is_runnable_model_id,
    load_leaderboard,
    parse_target,
    rank_entries,
    read_cache,
    recommend,
    write_cache,
)

URL = "https://example.test/leaderboard.json"


def _entry(
    *,
    model: str,
    sps: float,
    topic_scores: dict[str, float] | None = None,
    dataset: str = "globalopinionqa",
    provider: str = "anthropic",
    framework: str = "althing",
    is_ensemble: bool = False,
    n: int = 100,
    jsd: float = 0.1,
    cost_per_100q: float = 0.5,
    run_count: int = 5,
    config_id: str | None = None,
    model_id: str | None = None,
    provider_id: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "config_id": config_id or f"cfg-{model}",
        "model": model,
        "provider": provider,
        "dataset": dataset,
        "framework": framework,
        "is_ensemble": is_ensemble,
        "sps": sps,
        "n": n,
        "jsd": jsd,
        "topic_scores": topic_scores or {},
        "cost_per_100q": cost_per_100q,
        "run_count": run_count,
    }
    if model_id is not None:
        entry["model_id"] = model_id
    if provider_id is not None:
        entry["provider_id"] = provider_id
    return entry


SAMPLE: dict[str, Any] = {
    "generated_at": "2026-04-24T00:00:00Z",
    "synthbench_version": "1.0",
    "entries": [
        _entry(
            model="claude-haiku-4-5-20251001",
            sps=0.82,
            topic_scores={"Economy & Work": 0.85, "Technology & Digital Life": 0.71},
        ),
        _entry(
            model="claude-sonnet-4-6",
            sps=0.78,
            topic_scores={"Economy & Work": 0.82, "Technology & Digital Life": 0.80},
        ),
        _entry(
            model="gemini-2.5-flash",
            sps=0.80,
            topic_scores={"Economy & Work": 0.79, "Technology & Digital Life": 0.90},
            provider="google",
        ),
        # wrong dataset — filtered out
        _entry(
            model="grok-3",
            sps=0.99,
            dataset="gss",
            provider="xai",
        ),
    ],
}


@pytest.fixture(autouse=True)
def _isolate_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SYNTH_PANEL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(SYNTHBENCH_URL_ENV, URL)
    monkeypatch.delenv(SYNTHBENCH_OFFLINE_ENV, raising=False)
    monkeypatch.delenv(SYNTHBENCH_REFRESH_ENV, raising=False)


@pytest.fixture
def no_bundled_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the bundled-snapshot path at a non-existent file.

    sy-nkh: the production code falls back to a package-shipped snapshot
    when the URL is unreachable. Tests that want to exercise the legacy
    "no recommendation possible" branch use this fixture to disable
    that fallback, keeping their assertions about ``None`` returns valid.
    """
    monkeypatch.setattr(
        synthbench,
        "_BUNDLED_SNAPSHOT_PATH",
        tmp_path / "does-not-exist.json",
    )


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _explode(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected network call: {request.method} {request.url}")


# ---------- parse_target ----------


def test_parse_target_topic_only() -> None:
    assert parse_target("Economy & Work") == ("Economy & Work", "globalopinionqa")


def test_parse_target_topic_and_dataset() -> None:
    assert parse_target("Economy & Work:gss") == ("Economy & Work", "gss")


def test_parse_target_dataset_only_with_leading_colon() -> None:
    assert parse_target(":gss") == (None, "gss")


def test_parse_target_rejects_empty() -> None:
    with pytest.raises(ValueError):
        parse_target("   ")


# ---------- rank_entries ----------


def test_rank_entries_by_sps_filters_dataset() -> None:
    ranked = rank_entries(SAMPLE, topic=None, dataset="globalopinionqa")
    models = [e["model"] for e, _ in ranked]
    # Sorted by SPS desc: 0.82 haiku, 0.80 gemini, 0.78 sonnet. grok-3 filtered out.
    assert models == [
        "claude-haiku-4-5-20251001",
        "gemini-2.5-flash",
        "claude-sonnet-4-6",
    ]


def test_rank_entries_by_topic_score() -> None:
    ranked = rank_entries(SAMPLE, topic="Technology & Digital Life", dataset="globalopinionqa")
    models = [e["model"] for e, _ in ranked]
    # 0.90 gemini, 0.80 sonnet, 0.71 haiku
    assert models[0] == "gemini-2.5-flash"


def test_rank_entries_topic_case_insensitive() -> None:
    ranked = rank_entries(SAMPLE, topic="economy & work", dataset="globalopinionqa")
    assert ranked[0][0]["model"] == "claude-haiku-4-5-20251001"


def test_rank_entries_skips_entries_missing_topic_score() -> None:
    leaderboard = {"entries": [_entry(model="x", sps=0.5, topic_scores={})]}
    assert rank_entries(leaderboard, topic="Health & Science", dataset="globalopinionqa") == []


# ---------- recommend (inline leaderboard) ----------


def test_recommend_picks_top_by_sps() -> None:
    rec = recommend(":globalopinionqa", leaderboard=SAMPLE)
    assert rec is not None
    assert rec.model == "claude-haiku-4-5-20251001"
    assert rec.dataset == "globalopinionqa"
    assert rec.topic is None
    assert rec.sps == pytest.approx(0.82)
    assert rec.provider == "anthropic"
    assert rec.n == 100


def test_recommend_picks_top_by_topic() -> None:
    rec = recommend("Technology & Digital Life", leaderboard=SAMPLE)
    assert rec is not None
    assert rec.model == "gemini-2.5-flash"
    assert rec.topic == "Technology & Digital Life"
    assert rec.sps == pytest.approx(0.90)


def test_recommend_returns_none_for_empty_leaderboard() -> None:
    assert recommend("anything", leaderboard={"entries": []}) is None


def test_recommend_resolves_short_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the default alias table (avoid user's ~/.althing/aliases.yaml).
    from althing.llm import aliases

    monkeypatch.setattr(aliases, "_ALIASES_FILE", Path("/nonexistent/aliases.yaml"))
    monkeypatch.delenv("ALTHING_MODEL_ALIASES", raising=False)
    aliases._reset_cache()
    board = {"entries": [_entry(model="haiku", sps=0.9)]}
    rec = recommend(":globalopinionqa", leaderboard=board)
    assert rec is not None
    # 'haiku' alias resolves to canonical model id.
    assert rec.model == "claude-haiku-4-5-20251001"
    assert rec.raw_model == "haiku"


def test_recommend_ensemble_falls_back_to_config_base(monkeypatch: pytest.MonkeyPatch) -> None:
    from althing.llm import aliases

    monkeypatch.setattr(aliases, "_ALIASES_FILE", Path("/nonexistent/aliases.yaml"))
    monkeypatch.delenv("ALTHING_MODEL_ALIASES", raising=False)
    aliases._reset_cache()
    board = {
        "entries": [
            _entry(
                model="",
                sps=0.95,
                framework="product",
                is_ensemble=True,
                config_id="althing:haiku",
            )
        ]
    }
    rec = recommend(":globalopinionqa", leaderboard=board)
    assert rec is not None
    assert rec.is_ensemble is True
    assert rec.model == "claude-haiku-4-5-20251001"
    assert rec.runnable is True


@pytest.mark.parametrize(
    "model, expected",
    [
        ("claude-haiku-4-5-20251001", True),
        ("gemini-2.5-flash", True),
        ("grok-3", True),
        ("gpt-4o-mini", True),
        ("openrouter/anthropic/claude-3.5", True),
        ("haiku", True),
        ("ollama:llama3", True),
        ("llama-3.3-70b-instruct", True),
        ("", False),
        ("   ", False),
        ("Althing (Gemini Flash Lite)", False),
        ("Gemini Flash Lite", False),
        ("claude (sonnet)", False),
    ],
)
def test_is_runnable_model_id(model: str, expected: bool) -> None:
    assert is_runnable_model_id(model) is expected


def test_recommend_display_label_marked_not_runnable() -> None:
    """gh-519: a product/ensemble row whose model field is a display label
    (and whose config_id yields no runnable base) must surface as
    runnable=False rather than stamping a bogus model id."""
    board = {
        "entries": [
            _entry(
                model="Althing (Gemini Flash Lite)",
                sps=0.95,
                framework="product",
                is_ensemble=True,
                # config_id tail is a hash fragment, not a resolvable base
                config_id="althing-gemini-flash-lite-tdefault-ba37570c",
            )
        ]
    }
    rec = recommend(":globalopinionqa", leaderboard=board)
    assert rec is not None
    assert rec.is_ensemble is True
    assert rec.runnable is False
    # The original label is preserved verbatim so the caller can report it.
    assert rec.model == "Althing (Gemini Flash Lite)"


def test_recommend_display_label_resolves_runnable_base_from_config_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a product/ensemble row carries a display label but the config_id
    encodes a resolvable base model, the recommendation becomes runnable."""
    from althing.llm import aliases

    monkeypatch.setattr(aliases, "_ALIASES_FILE", Path("/nonexistent/aliases.yaml"))
    monkeypatch.delenv("ALTHING_MODEL_ALIASES", raising=False)
    aliases._reset_cache()
    board = {
        "entries": [
            _entry(
                model="Althing (Haiku)",
                sps=0.95,
                framework="product",
                is_ensemble=True,
                config_id="althing:haiku",
            )
        ]
    }
    rec = recommend(":globalopinionqa", leaderboard=board)
    assert rec is not None
    assert rec.runnable is True
    assert rec.model == "claude-haiku-4-5-20251001"


def test_recommend_prefers_runnable_model_id_over_display_label() -> None:
    """gh-519 retry: when the display ``model`` is a label but the row exposes
    a runnable ``model_id`` (SynthBench #297), substitute the model_id and mark
    the recommendation runnable instead of refusing."""
    board = {
        "entries": [
            _entry(
                model="Althing (Gemini Flash Lite)",
                provider="Althing (Gemini Flash Lite)",
                model_id="google/gemini-2.5-flash-lite",
                sps=0.95,
                is_ensemble=False,
            )
        ]
    }
    rec = recommend(":globalopinionqa", leaderboard=board)
    assert rec is not None
    # Upstream ids run through OpenRouter (routing contract v1).
    assert rec.model == "openrouter/google/gemini-2.5-flash-lite"
    assert rec.runnable is True
    # The original display label is preserved as raw_model for provenance.
    assert rec.raw_model == "Althing (Gemini Flash Lite)"


def test_recommend_model_id_wins_over_config_id_inference() -> None:
    """A runnable model_id takes precedence over the config_id base heuristic
    so the authoritative upstream id is used rather than a guessed tail."""
    board = {
        "entries": [
            _entry(
                model="Althing (Gemini Flash Lite)",
                model_id="google/gemini-2.5-flash-lite",
                config_id="althing:haiku",
                sps=0.95,
                framework="product",
                is_ensemble=True,
            )
        ]
    }
    rec = recommend(":globalopinionqa", leaderboard=board)
    assert rec is not None
    # Upstream ids run through OpenRouter (routing contract v1).
    assert rec.model == "openrouter/google/gemini-2.5-flash-lite"
    assert rec.runnable is True


def test_recommend_joins_provider_id_with_bare_model_id() -> None:
    """When model_id is a bare slug and provider_id is published separately,
    they are joined into a full provider/model slug."""
    board = {
        "entries": [
            _entry(
                model="Althing (Gemini Flash Lite)",
                model_id="gemini-2.5-flash-lite",
                provider_id="google",
                sps=0.95,
            )
        ]
    }
    rec = recommend(":globalopinionqa", leaderboard=board)
    assert rec is not None
    # Upstream ids run through OpenRouter (routing contract v1).
    assert rec.model == "openrouter/google/gemini-2.5-flash-lite"
    assert rec.runnable is True


def test_recommend_ignores_non_runnable_model_id() -> None:
    """A model_id that is itself a display label must not be substituted —
    the row stays non-runnable so the CLI refuses (no gh-519 regression)."""
    board = {
        "entries": [
            _entry(
                model="Althing (Gemini Flash Lite)",
                model_id="Gemini Flash Lite (preview)",
                config_id="althing-gemini-flash-lite-ba37570c",
                sps=0.95,
                framework="product",
                is_ensemble=True,
            )
        ]
    }
    rec = recommend(":globalopinionqa", leaderboard=board)
    assert rec is not None
    assert rec.runnable is False
    assert rec.model == "Althing (Gemini Flash Lite)"


def test_recommend_runnable_model_field_ignores_model_id() -> None:
    """When the display ``model`` is already runnable, it is used as-is and a
    present model_id does not override it (no surprise reroute)."""
    board = {
        "entries": [
            _entry(
                model="gpt-4o-mini",
                model_id="openai/gpt-4o-mini",
                sps=0.95,
            )
        ]
    }
    rec = recommend(":globalopinionqa", leaderboard=board)
    assert rec is not None
    assert rec.model == "gpt-4o-mini"
    assert rec.runnable is True


def test_recommend_plain_model_is_runnable() -> None:
    rec = recommend("Economy & Work", leaderboard=SAMPLE)
    assert rec is not None
    assert rec.runnable is True


def test_recommend_low_confidence_flag() -> None:
    board = {"entries": [_entry(model="haiku", sps=0.9, run_count=1)]}
    rec = recommend(":globalopinionqa", leaderboard=board)
    assert rec is not None
    assert rec.low_confidence is True


def test_recommend_format_line_has_expected_pieces() -> None:
    rec = recommend("Economy & Work", leaderboard=SAMPLE)
    assert rec is not None
    line = rec.format_line()
    assert "synthbench" in line
    assert "claude-haiku-4-5-20251001" in line
    assert "SPS" in line
    # Default for direct-leaderboard callers (no load_leaderboard hop) is "live"
    # so existing fixtures continue to render a wire-stable source= field.
    assert "source=live" in line


def test_recommend_format_line_source_reflects_recommendation_source() -> None:
    """Wire format: format_line() must surface the source the agent reads."""
    from althing.synthbench import Recommendation

    base = recommend("Economy & Work", leaderboard=SAMPLE)
    assert base is not None
    for source in ("live", "cache", "stale-cache", "bundled-snapshot"):
        rec = Recommendation(**{**base.__dict__, "source": source})
        line = rec.format_line()
        assert f"source={source}" in line, line


# ---------- load_leaderboard (cache + network) ----------


def test_fresh_cache_hit_skips_network() -> None:
    write_cache(SAMPLE, source_url=URL, etag='"abc"')
    with _client(_explode) as client:
        loaded = load_leaderboard(client=client)
    assert loaded is not None
    assert loaded.leaderboard == SAMPLE
    assert loaded.source == "cache"


def test_stale_cache_304_keeps_cached_leaderboard() -> None:
    stale = datetime.now(timezone.utc) - CACHE_TTL - timedelta(hours=1)
    write_cache(SAMPLE, source_url=URL, etag='"abc"', fetched_at=stale)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("if-none-match") == '"abc"'
        return httpx.Response(304)

    with _client(handler) as client:
        loaded = load_leaderboard(client=client)
    assert loaded is not None
    assert loaded.leaderboard == SAMPLE
    # 304 confirmed the cached copy is still current upstream — treat as live.
    assert loaded.source == "live"
    cached = read_cache()
    assert cached is not None
    assert (datetime.now(timezone.utc) - cached.fetched_at) < timedelta(minutes=1)


def test_stale_cache_200_overwrites_with_new_payload() -> None:
    stale = datetime.now(timezone.utc) - timedelta(hours=48)
    write_cache(SAMPLE, source_url=URL, etag='"old"', fetched_at=stale)
    updated = {"entries": [_entry(model="opus", sps=0.99)]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=updated, headers={"ETag": '"new"'})

    with _client(handler) as client:
        loaded = load_leaderboard(client=client)
    assert loaded is not None
    assert loaded.leaderboard == updated
    assert loaded.source == "live"
    cached = read_cache()
    assert cached is not None
    assert cached.etag == '"new"'


def test_stale_cache_network_fail_returns_stale_with_warning() -> None:
    stale = datetime.now(timezone.utc) - timedelta(hours=48)
    write_cache(SAMPLE, source_url=URL, etag='"abc"', fetched_at=stale)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    warnings: list[str] = []
    with _client(handler) as client:
        loaded = load_leaderboard(client=client, warn=warnings.append)
    assert loaded is not None
    assert loaded.leaderboard == SAMPLE
    assert loaded.source == "stale-cache"
    assert any("stale cache" in w for w in warnings)
    assert any("source=stale-cache" in w for w in warnings)


def test_no_cache_and_fetch_fail_returns_none(no_bundled_snapshot: None) -> None:
    # sy-nkh: with the bundled snapshot disabled, this reverts to the
    # pre-fallback contract (warn + None). Used by the snapshot tests
    # below to assert the new fallback behaviour separately.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    warnings: list[str] = []
    with _client(handler) as client:
        loaded = load_leaderboard(client=client, warn=warnings.append)
    assert loaded is None
    assert any("synthbench unavailable" in w for w in warnings)
    # sy-nkh: actionable corrective hint must surface so the user knows
    # they can point at a mirror via ALTHING_SYNTHBENCH_URL.
    assert any("ALTHING_SYNTHBENCH_URL" in w for w in warnings)


def test_no_cache_and_http_404_returns_none(no_bundled_snapshot: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    warnings: list[str] = []
    with _client(handler) as client:
        loaded = load_leaderboard(client=client, warn=warnings.append)
    assert loaded is None


def test_offline_env_prevents_any_network(monkeypatch: pytest.MonkeyPatch) -> None:
    write_cache(
        SAMPLE,
        source_url=URL,
        etag='"abc"',
        fetched_at=datetime.now(timezone.utc) - timedelta(hours=48),
    )
    monkeypatch.setenv(SYNTHBENCH_OFFLINE_ENV, "1")
    with _client(_explode) as client:
        loaded = load_leaderboard(client=client)
    assert loaded is not None
    assert loaded.leaderboard == SAMPLE
    # 48h-old cache + offline → stale-cache, since CACHE_TTL is 24h.
    assert loaded.source == "stale-cache"


def test_offline_with_fresh_cache_reports_cache_source(monkeypatch: pytest.MonkeyPatch) -> None:
    write_cache(SAMPLE, source_url=URL, etag='"abc"')  # default fetched_at = now
    monkeypatch.setenv(SYNTHBENCH_OFFLINE_ENV, "1")
    with _client(_explode) as client:
        loaded = load_leaderboard(client=client)
    assert loaded is not None
    assert loaded.source == "cache"


def test_offline_with_no_cache_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    no_bundled_snapshot: None,
) -> None:
    monkeypatch.setenv(SYNTHBENCH_OFFLINE_ENV, "1")
    with _client(_explode) as client:
        loaded = load_leaderboard(client=client)
    assert loaded is None


def test_refresh_env_bypasses_fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    write_cache(SAMPLE, source_url=URL, etag='"abc"')
    monkeypatch.setenv(SYNTHBENCH_REFRESH_ENV, "1")
    new_payload = {"entries": []}
    seen_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        return httpx.Response(200, json=new_payload)

    with _client(handler) as client:
        loaded = load_leaderboard(client=client)
    assert loaded is not None
    assert loaded.leaderboard == new_payload
    assert "if-none-match" not in seen_headers[0]


# ---------- recommend with real cache lookup ----------


def test_recommend_through_cache_layer() -> None:
    write_cache(SAMPLE, source_url=URL, etag='"abc"')
    with _client(_explode) as client:
        rec = recommend("Economy & Work", client=client)
    assert rec is not None
    assert rec.model == "claude-haiku-4-5-20251001"
    assert rec.cache_age_hours < 1.0


def test_recommend_returns_none_when_leaderboard_unavailable(
    no_bundled_snapshot: None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    warnings: list[str] = []
    with _client(handler) as client:
        rec = recommend("anything", client=client, warn=warnings.append)
    assert rec is None


# ---------- sy-nkh: bundled snapshot fallback ----------


class TestBundledSnapshotFallback:
    """Pin the contract that a fresh install with a broken upstream URL
    still produces a recommendation via the package-bundled snapshot.

    GH #494 origin: ``synthbench.org/data/leaderboard.json`` has been
    404'ing since the v1.5.0 cut. Without the bundled fallback,
    ``--best-model-for`` is silently dead for every PyPI user.
    """

    def test_no_cache_plus_network_error_uses_bundled_snapshot(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        warnings: list[str] = []
        with _client(handler) as client:
            loaded = load_leaderboard(client=client, warn=warnings.append)

        assert loaded is not None, "bundled snapshot must rescue the fresh-install path"
        assert isinstance(loaded.leaderboard.get("entries"), list)
        assert loaded.source == "bundled-snapshot"
        assert any("bundled snapshot" in w for w in warnings), warnings
        # Wire format: the source discriminator must appear in the
        # warning so agents grep-parsing stderr can distinguish this
        # path from stale-cache.
        assert any("source=bundled-snapshot" in w for w in warnings), warnings
        # The actionable override hint travels alongside the fallback so
        # users with a working mirror know how to switch to it.
        assert any("ALTHING_SYNTHBENCH_URL" in w for w in warnings), warnings

    def test_no_cache_plus_404_uses_bundled_snapshot(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        warnings: list[str] = []
        with _client(handler) as client:
            loaded = load_leaderboard(client=client, warn=warnings.append)
        assert loaded is not None
        assert any("bundled snapshot" in w for w in warnings)

    def test_recommendation_from_bundled_snapshot_is_usable(self) -> None:
        """The whole point: --best-model-for must return a real, runnable model."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        with _client(handler) as client:
            rec = recommend(":globalopinionqa", client=client, warn=lambda _m: None)

        assert rec is not None, "recommendation must survive a 404 default URL"
        assert rec.model, "bundled entry must expose a non-empty model string"
        assert rec.sps > 0
        assert rec.dataset == "globalopinionqa"
        # Provenance must propagate so format_line() / agent consumers
        # can render the recommendation honestly instead of claiming
        # source=synthbench.org for snapshot-derived data (sy-klp).
        assert rec.source == "bundled-snapshot"
        assert "source=bundled-snapshot" in rec.format_line()

    def test_bundled_snapshot_handles_known_topics(self) -> None:
        """Every topic mentioned in docs/recommended-models.md must
        resolve against the bundled snapshot — otherwise the docs lie."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        topics = (
            "Economy & Work",
            "Health & Science",
            "Politics & Governance",
            "International Relations & Security",
        )
        with _client(handler) as client:
            for topic in topics:
                rec = recommend(topic, client=client, warn=lambda _m: None)
                assert rec is not None, f"bundled snapshot missing topic {topic!r}"
                assert rec.topic == topic
                assert rec.sps > 0

    def test_offline_mode_uses_bundled_snapshot_when_no_cache(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Air-gapped first-time use must still produce a recommendation."""
        monkeypatch.setenv(SYNTHBENCH_OFFLINE_ENV, "1")

        warnings: list[str] = []
        with _client(_explode) as client:
            loaded = load_leaderboard(client=client, warn=warnings.append)
        assert loaded is not None
        assert any("bundled snapshot" in w for w in warnings)

    def test_stale_cache_still_preferred_over_bundled_snapshot(self) -> None:
        """A user's stale cache trumps the package snapshot even on URL failure.

        Makes the cache 48h old (past the 24h TTL) so we exercise the
        stale-cache branch, then network-fail. The cache must win
        because it represents the user's most recently observed live
        data — the bundled snapshot is the LAST resort, not the
        second-to-last.
        """
        write_cache(
            SAMPLE,
            source_url=URL,
            etag='"abc"',
            fetched_at=datetime.now(timezone.utc) - timedelta(hours=48),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        warnings: list[str] = []
        with _client(handler) as client:
            loaded = load_leaderboard(client=client, warn=warnings.append)
        assert loaded is not None
        assert loaded.leaderboard == SAMPLE
        assert loaded.source == "stale-cache"
        # The "stale cache" path is the explicit prior message — the
        # bundled-snapshot warning must NOT also fire.
        assert any("stale cache" in w for w in warnings)
        assert not any("bundled snapshot" in w for w in warnings)

    def test_bundled_snapshot_file_is_shipped(self) -> None:
        """Sanity check on the package-data wiring — the snapshot file
        must exist at the documented path. Catches a botched
        package-data glob before users do."""
        assert synthbench._BUNDLED_SNAPSHOT_PATH.exists(), (
            f"bundled snapshot missing at {synthbench._BUNDLED_SNAPSHOT_PATH}. "
            "Check pyproject.toml's [tool.setuptools.package-data] glob "
            'for `althing.data = ["*.json"]`.'
        )


# ---------- module-level constant sanity ----------


def test_default_url_points_at_synthbench_org() -> None:
    # Documented in NOTES on sp-zq3.
    assert synthbench.DEFAULT_SYNTHBENCH_URL == "https://synthbench.org/data/leaderboard.json"


def test_cache_path_honors_data_dir_env(tmp_path: Path) -> None:
    assert cache_path() == tmp_path / "synthbench-cache.json"
