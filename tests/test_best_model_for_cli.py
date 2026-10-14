"""CLI-level tests for ``--best-model-for`` model stamping (gh-519).

These exercise ``_apply_best_model_for``: the bridge between a SynthBench
:class:`Recommendation` and ``args.model``. The core regression guarded here
is that a non-runnable recommendation (a product/ensemble display label like
``"Althing (Gemini Flash Lite)"``) must NOT be stamped onto ``--model`` —
it has to be refused with an actionable message and fall back to the existing
selection, instead of deferring an opaque provider "not a valid model ID"
failure to call time.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import pytest

from althing import synthbench
from althing.cli.commands import _apply_best_model_for
from althing.synthbench import Recommendation


def _rec(*, model: str, runnable: bool, is_ensemble: bool = False, framework: str | None = None) -> Recommendation:
    return Recommendation(
        model=model,
        raw_model=model,
        provider="",
        dataset="globalopinionqa",
        topic="Technology & Digital Life",
        sps=0.9,
        jsd=0.1,
        n=100,
        cost_per_100q=0.5,
        run_count=10,
        framework=framework,
        is_ensemble=is_ensemble,
        fetched_at=datetime.now(timezone.utc),
        cache_age_hours=0.0,
        low_confidence=False,
        runnable=runnable,
        source="live",
    )


def test_runnable_recommendation_is_stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(synthbench, "recommend", lambda spec: _rec(model="gemini-2.5-flash", runnable=True))
    args = argparse.Namespace(model=None)
    picked = _apply_best_model_for(args, "Technology & Digital Life")
    assert picked == "gemini-2.5-flash"
    assert args.model == "gemini-2.5-flash"


def test_nonrunnable_recommendation_is_refused_not_stamped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        synthbench,
        "recommend",
        lambda spec: _rec(
            model="Althing (Gemini Flash Lite)",
            runnable=False,
            is_ensemble=True,
            framework="product",
        ),
    )
    args = argparse.Namespace(model="sonnet")
    picked = _apply_best_model_for(args, "Technology & Digital Life")

    # Refused: returns None and leaves the prior selection untouched so the
    # caller falls through to --model / the default.
    assert picked is None
    assert args.model == "sonnet"

    err = capsys.readouterr().err
    # The display label is surfaced verbatim and the message is actionable.
    assert "Althing (Gemini Flash Lite)" in err
    assert "display label" in err
    assert "--model" in err


def test_no_recommendation_falls_through(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(synthbench, "recommend", lambda spec: None)
    args = argparse.Namespace(model="haiku")
    picked = _apply_best_model_for(args, "Whatever")
    assert picked is None
    assert args.model == "haiku"
    assert "no recommendation" in capsys.readouterr().err


def test_display_label_with_model_id_is_stamped_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gh-519 retry: a top row whose ``model`` is a display label but that
    carries a runnable ``model_id`` (SynthBench #297) is resolved through the
    real ``recommend`` and stamped onto ``--model`` — not refused. Previously
    such a row fell through to the existing selection (e.g. openrouter/auto)."""
    board = {
        "entries": [
            {
                "config_id": "althing-gemini-flash-lite-tdefault-ba37570c",
                "model": "Althing (Gemini Flash Lite)",
                "model_id": "google/gemini-2.5-flash-lite",
                "provider": "Althing (Gemini Flash Lite)",
                "dataset": "globalopinionqa",
                "is_ensemble": False,
                "sps": 0.901,
                "n": 100,
                "jsd": 0.313,
                "topic_scores": {"Technology & Digital Life": 0.901},
                "run_count": 100,
            }
        ]
    }
    real_recommend = synthbench.recommend
    monkeypatch.setattr(
        synthbench,
        "recommend",
        lambda spec: real_recommend(spec, leaderboard=board),
    )
    args = argparse.Namespace(model="openrouter/auto")
    picked = _apply_best_model_for(args, "Technology & Digital Life:globalopinionqa")
    assert picked == "openrouter/google/gemini-2.5-flash-lite"
    assert args.model == "openrouter/google/gemini-2.5-flash-lite"
