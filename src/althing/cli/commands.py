"""Subcommand handlers for althing CLI.

Each handler receives parsed args and output format, returns an exit code.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from althing.plugins.lint import LintReport

import yaml

from althing._runners import (
    build_synthesis_error_payload,
    detect_synthesis_context_overflow,
    detect_total_failure,
    format_synthesis_overflow_message,
    format_total_failure_message,
)
from althing.checkpoint import (
    DEFAULT_CHECKPOINT_EVERY,
    CheckpointCollisionError,
    CheckpointDriftError,
    CheckpointError,
    CheckpointLockError,
    CheckpointNotFoundError,
    CheckpointWriter,
    checkpoint_dir_for,
    default_checkpoint_root,
    ensure_config_matches,
    list_runs,
    load_checkpoint,
    new_run_id,
    parse_duration,
    prune_runs,
)
from althing.cli.output import (
    OutputFormat,
    emit,
    format_prose_for_inspect,
    terminal_columns,
)
from althing.cli.progress import PanelProgressBar
from althing.convergence import (
    DEFAULT_CHECK_EVERY,
    DEFAULT_EPSILON,
    DEFAULT_M_CONSECUTIVE,
    DEFAULT_MIN_N,
    ConvergenceTracker,
    SynthbenchUnavailableError,
    derive_pick_one_schema_from_baseline,
    identify_tracked_questions,
    load_synthbench_baseline,
)
from althing.cost import (
    ZERO_USAGE,
    CostEstimate,
    CostGate,
    TokenUsage,
    aggregate_per_model,
    build_cost_fallback_warnings,
    estimate_cost,
    format_summary,
    lookup_pricing,
)
from althing.credentials import has_credential
from althing.diff import CategoricalQuestionDiff, RunDiff, TextQuestionDiff
from althing.instrument import Instrument, InstrumentError, parse_instrument
from althing.llm import fast_default as _fast_default
from althing.llm.client import LLMClient
from althing.metadata import PanelTimer, build_metadata
from althing.orchestrator import PanelistResult, RunAbortedError, run_panel_parallel
from althing.pack_diff import (
    CompositionStats,
    PackDiff,
    PersonaChange,
    compute_pack_diff,
    load_pack,
    trait_delta,
)
from althing.persistence import Session
from althing.perturbation import generate_panel_variants
from althing.prompts import (
    build_question_prompt,
    compile_jinja2_template,
    is_jinja2_template,
    load_prompt_template,
    persona_system_prompt,
    persona_system_prompt_from_template,
)
from althing.question_budget import QuestionFailureBudget
from althing.response_coercion import is_typed_schema
from althing.runtime import AgentRuntime
from althing.synthesis import (
    STRATEGY_MAP_REDUCE,
    STRATEGY_SINGLE,
    MapChunkOverflowError,
    select_strategy,
    synthesize_panel,
    synthesize_panel_mapreduce,
)
from althing.templates import find_unresolved_in_questions
from althing.text_width import display_width, pad

logger = logging.getLogger(__name__)

# Preference chain for --model default (sp-f4t). Each entry is
# (env var that signals provider availability, model alias or name).
# Walked in order: the first provider with credentials wins. The alias
# side prefers the provider's workhorse model so the user gets a
# sensible default without guessing which canonical ID to type.
_DEFAULT_MODEL_PREFERENCE: list[tuple[str, str]] = [
    ("ANTHROPIC_API_KEY", "sonnet"),
    ("OPENAI_API_KEY", "gpt-4o-mini"),
    ("GEMINI_API_KEY", "gemini-2.5-flash"),
    ("GOOGLE_API_KEY", "gemini-2.5-flash"),
    ("XAI_API_KEY", "grok-3"),
    ("OPENROUTER_API_KEY", "openrouter/auto"),
]

# sp-inline-calibration (sp-a6jc): redistribution-tier allowlist for
# --calibrate-against. Only datasets that may be republished inline as
# part of a althing run output are allowed; gated datasets
# (OpinionsQA, PewTech, GlobalOpinionQA, WVS, ...) require post-hoc
# calibration. Override with SYNTHBENCH_ALLOW_GATED=1 (internal only).
_INLINE_CALIBRATION_ALLOWED: frozenset[str] = frozenset({"gss", "ntia"})


def _looks_numeric_key(value: Any) -> bool:
    """True when *value* is numeric or a string that coerces to one.

    Mirrors ``convergence._looks_numeric`` so we can classify a
    baseline's option-string set as Likert-looking for the §7 error
    message without importing a private symbol.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
        except ValueError:
            return False
        return True
    return False


def _resolve_default_model() -> tuple[str, str | None]:
    """Pick a default model alias based on which API keys are present.

    Returns ``(model_alias, source_env_var)``. Consults both the
    environment and the on-disk credential store so ``althing login``
    is enough to get a working default. If nothing is available the
    fallback is still ``"sonnet"`` so the LLM client's own credential
    check emits the canonical error message.
    """
    for env_var, alias in _DEFAULT_MODEL_PREFERENCE:
        if has_credential(env_var):
            return alias, env_var
    return "sonnet", None


def _apply_best_model_for(args: argparse.Namespace, spec: str) -> str | None:
    """Resolve a SynthBench recommendation and stamp it onto ``args.model``.

    Returns the picked model on success, or ``None`` when the
    leaderboard is unavailable / yields no candidate (the caller falls
    back to whatever ``--model`` / default was already in effect).
    Emits the recommendation line to stderr so users can cancel and
    override.
    """
    from althing import synthbench

    try:
        rec = synthbench.recommend(spec)
    except ValueError as exc:
        print(f"Error: --best-model-for: {exc}", file=sys.stderr)
        return None
    if rec is None:
        print(
            f"synthbench: no recommendation for '{spec}' — "
            f"leaderboard unavailable or no matching entries; "
            f"continuing with existing model selection.",
            file=sys.stderr,
        )
        return None

    print(rec.format_line(), file=sys.stderr)

    # gh-519: SynthBench product/ensemble rows can surface a display label
    # (e.g. "Althing (Gemini Flash Lite)") in the model field instead of
    # a runnable provider model id. Stamping that onto --model defers the
    # failure to call time as an opaque provider "not a valid model ID"
    # error. Refuse here with an actionable message and fall back to the
    # existing --model / default selection (matching the no-recommendation
    # path), so --dry-run also makes the non-runnable pick obvious upfront.
    if not rec.runnable:
        print(
            f"synthbench: recommendation '{rec.model}' is a display label, not a "
            f"runnable model id — skipping. The top entry for '{spec}' is a "
            f"product/ensemble config with no resolvable base model. Pass --model "
            f"with a provider model id (e.g. claude-sonnet-4-6, gemini-2.5-flash) "
            f"explicitly, or choose a different topic. Continuing with existing "
            f"model selection.",
            file=sys.stderr,
        )
        return None

    if rec.source == "bundled-snapshot":
        print(
            "synthbench: note — recommendation derived from the package-bundled "
            "snapshot, not live leaderboard data. Treat as best-effort; for current "
            f"picks once the upstream is reachable, unset ${synthbench.SYNTHBENCH_OFFLINE_ENV} "
            f"and clear the cache, or set ${synthbench.SYNTHBENCH_URL_ENV} to a working mirror.",
            file=sys.stderr,
        )
    elif rec.source == "stale-cache":
        print(
            "synthbench: note — recommendation from stale cache (live fetch failed). "
            f"Set ${synthbench.SYNTHBENCH_REFRESH_ENV}=1 on the next run to retry once "
            "the upstream is reachable.",
            file=sys.stderr,
        )
    if rec.low_confidence:
        print(
            f"synthbench: warning — recommendation has run_count={rec.run_count} "
            f"(< {synthbench.MIN_RUN_COUNT}); results may be noisy.",
            file=sys.stderr,
        )
    if rec.is_ensemble or rec.framework == "product":
        print(
            f"synthbench: note — top entry is a product/ensemble config; using underlying base model '{rec.model}'.",
            file=sys.stderr,
        )
    args.model = rec.model
    return rec.model


def _resolve_model(args: argparse.Namespace) -> str:
    """Return the model alias from CLI args.

    When ``--model`` is not supplied, walk the preference chain in
    :data:`_DEFAULT_MODEL_PREFERENCE` and pick the first provider with
    credentials present in the environment. Falls back to ``"sonnet"``
    if nothing is set so the LLM client's missing-credentials error is
    still the one the user sees.
    """
    if args.model:
        return args.model
    alias, _source = _resolve_default_model()
    return alias


def parse_models_spec(spec: str) -> list[tuple[str, float]]:
    """Parse a --models spec string into [(model, weight)] pairs.

    Accepted formats:

    * **Weighted** (per-persona assignment): ``"haiku:0.5,gemini:0.5"``
    * **Ensemble** (no weights): ``"haiku,sonnet"`` — each model gets weight 1.0

    When no entry contains ``:``, the spec is treated as an ensemble list.
    Weights must be positive. Order is preserved as given (the assignment
    algorithm walks the list left-to-right; see
    :func:`assign_models_to_personas` for the full algorithm).

    Validation:

    * ``weight <= 0`` → ``ValueError``
    * non-numeric weight → ``ValueError``
    * empty spec or empty model name → ``ValueError``
    * mixing weighted and unweighted entries (e.g. ``"haiku,sonnet:0.5"``)
      → ``ValueError``

    Sum-of-weights is **not** enforced here — specs like ``"a:2,b:3"`` parse
    cleanly and are normalized by :func:`assign_models_to_personas`. Callers
    that want to warn on non-unit sums should inspect the parsed pairs.
    """
    entries = [e.strip() for e in spec.split(",") if e.strip()]
    if not entries:
        raise ValueError("Empty --models spec")

    # Detect ensemble vs weighted: ensemble if *no* entry contains ':'
    is_ensemble = all(":" not in e for e in entries)

    pairs: list[tuple[str, float]] = []
    for entry in entries:
        if is_ensemble:
            if not entry:
                raise ValueError("Empty model name in spec")
            pairs.append((entry, 1.0))
        else:
            if ":" not in entry:
                raise ValueError(f"Invalid model spec entry '{entry}': expected 'model:weight' (e.g. 'haiku:0.5')")
            model_part, weight_str = entry.rsplit(":", 1)
            model_part = model_part.strip()
            weight_str = weight_str.strip()
            if not model_part:
                raise ValueError(f"Empty model name in spec entry '{entry}'")
            try:
                weight = float(weight_str)
            except ValueError as exc:
                raise ValueError(f"Invalid weight '{weight_str}' in spec entry '{entry}': must be a number") from exc
            if weight <= 0:
                raise ValueError(f"Weight must be positive, got {weight} for model '{model_part}'")
            pairs.append((model_part, weight))
    return pairs


def is_ensemble_spec(spec: str) -> bool:
    """Return True when *spec* is a weight-free ensemble list (e.g. ``"haiku,sonnet"``)."""
    return all(":" not in e for e in spec.split(",") if e.strip())


def assign_models_to_personas(
    personas: list[dict[str, Any]],
    model_spec: list[tuple[str, float]],
    default_model: str,
) -> tuple[dict[str, str], list[str]]:
    """Assign models to personas using Hamilton's largest-remainder method.

    Resolution order: persona YAML 'model' field > weighted assignment > default.

    Algorithm (fully deterministic — same inputs produce the same output):

    1. **YAML override pass.** Walk ``personas`` in list order. Any persona
       with a ``model`` field keeps that model and is removed from the
       assignment pool. This override takes precedence over the ``--models``
       spec unconditionally.
    2. **Normalize weights.** Compute ``total = sum(weights)`` and divide each
       weight by it. Absolute weights don't matter, only ratios — so
       ``"a:0.5,b:0.5"``, ``"a:1,b:1"`` and ``"a:50,b:50"`` all behave the
       same.
    3. **Largest-remainder allocation.** Floor each ``weight * N`` quota,
       then hand out leftover seats in descending order of fractional
       remainder. Ties are broken deterministically by position in the spec.
    4. **Min-1 guarantee** (sp-27rz). When ``N >= len(model_spec)``, every
       model in ``model_spec`` is guaranteed at least one persona: any
       zero-count model pulls a seat from the currently largest bucket.
       The prior round-based allocation could silently hand the last model
       0 personas, dropping it from per_model_results without any warning.
       When there are fewer personas than models, affected models still
       end up at zero and a warning is appended to the returned list so
       callers can surface it to the user.

    Edge cases:

    * **Empty pool** (every persona has a YAML ``model`` override):
      ``model_spec`` is effectively ignored and the YAML map is returned.
    * **Weights that don't sum to 1.0** (e.g. ``"a:2,b:3"``): normalized
      internally, so the split still works. The CLI warns about this so
      the user knows the ratio they intended is what got applied.
    * **Zero weight**: rejected upstream by :func:`parse_models_spec`.
    * **``default_model``**: currently unused by the algorithm — YAML
      override and the ``--models`` spec between them cover every persona.
      Kept in the signature so callers can pass the CLI's fallback for
      future use.

    Returns a ``(assignments, warnings)`` tuple.
    """
    warnings: list[str] = []
    needs_assignment: list[str] = []
    result: dict[str, str] = {}
    for p in personas:
        name = p.get("name", "Anonymous")
        # sp-4loufu: accept ``llm_overrides.model`` as an equivalent to
        # the legacy top-level ``model`` field. Top-level wins so
        # existing YAML keeps its behaviour.
        explicit_model = p.get("model") or (p.get("llm_overrides") or {}).get("model")
        if explicit_model:
            result[name] = explicit_model
        else:
            needs_assignment.append(name)

    if not needs_assignment:
        return result, warnings

    n = len(needs_assignment)
    total_weight = sum(w for _, w in model_spec)
    normalized = [(m, w / total_weight) for m, w in model_spec]

    # Hamilton's method (largest-remainder): floor each quota, then hand out
    # leftover seats in descending order of fractional remainder. Deterministic
    # ties broken by position in the spec.
    quotas = [w * n for _, w in normalized]
    counts = [int(q) for q in quotas]
    remainders = [q - int(q) for q in quotas]
    seats_left = n - sum(counts)
    order = sorted(range(len(counts)), key=lambda i: (-remainders[i], i))
    for i in order[:seats_left]:
        counts[i] += 1

    # Min-1 guarantee: when there are enough personas to cover every model,
    # ensure no model is left at zero by taking one seat from the currently
    # largest bucket. Preserves proportions as closely as possible while
    # eliminating the silent-drop bug.
    if n >= len(model_spec):
        for zi, c in enumerate(counts):
            if c != 0:
                continue
            donor = max(
                range(len(counts)),
                key=lambda i: (counts[i], -i),
            )
            if counts[donor] <= 1:
                break
            counts[donor] -= 1
            counts[zi] += 1

    # Any remaining zeros mean personas < models — surface loudly.
    for (model, _), c in zip(normalized, counts):
        if c == 0:
            warnings.append(
                f"model_allocation: '{model}' received 0 of {n} personas "
                f"(fewer personas than weighted models); it will be absent "
                f"from per_model_results"
            )

    idx = 0
    for (model, _), c in zip(normalized, counts):
        for _ in range(c):
            result[needs_assignment[idx]] = model
            idx += 1

    return result, warnings


# sp-zdul: display helpers for --models weighted assignment
_WEIGHT_SUM_TOLERANCE = 0.02


def check_weight_sum(
    model_spec: list[tuple[str, float]],
    tolerance: float = _WEIGHT_SUM_TOLERANCE,
) -> tuple[float, bool]:
    """Return ``(sum_of_weights, is_close_to_one)``.

    Weights are normalized internally by :func:`assign_models_to_personas`,
    but a user who wrote ``"a:0.3,b:0.3,c:0.3"`` probably meant ``0.33`` each
    and is relying on equal split. Warning loudly when the sum deviates
    from 1.0 by more than ``tolerance`` catches these typos.
    """
    total = sum(w for _, w in model_spec)
    return total, abs(total - 1.0) <= tolerance


def format_assignment_breakdown(persona_models: dict[str, str]) -> str:
    """Format a per-persona → model breakdown for pre-run display.

    Example output::

        Model assignment:
          Maya Chen       → haiku
          Derek Washington → gpt-mini
          ...
        Totals: haiku=2, gpt-mini=2, gemini-flash=2
    """
    if not persona_models:
        return ""
    name_width = max(display_width(n) for n in persona_models)
    lines = ["Model assignment:"]
    for name, mdl in persona_models.items():
        lines.append(f"  {pad(name, name_width)} → {mdl}")
    counts: dict[str, int] = {}
    for mdl in persona_models.values():
        counts[mdl] = counts.get(mdl, 0) + 1
    totals = ", ".join(f"{m}={c}" for m, c in sorted(counts.items()))
    lines.append(f"Totals: {totals}")
    return "\n".join(lines)


def _load_profile(args: argparse.Namespace) -> tuple[Any, int | None]:
    """Load and apply a profile from --profile or --config.

    Returns ``(profile_or_None, error_exit_code_or_None)``. On error,
    the message is already printed to stderr and the caller should return
    the exit code.
    """
    from althing.profiles import (
        Profile,
        apply_profile_to_args,
        load_profile_by_name,
        load_profile_from_path,
    )

    profile_name = getattr(args, "profile", None)
    config_path = getattr(args, "config", None)

    if profile_name and config_path:
        print("Error: --profile and --config are mutually exclusive.", file=sys.stderr)
        return None, 1

    profile: Profile | None = None
    if profile_name:
        try:
            profile = load_profile_by_name(profile_name)
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error loading profile: {exc}", file=sys.stderr)
            return None, 1
    elif config_path:
        try:
            profile = load_profile_from_path(config_path)
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error loading config: {exc}", file=sys.stderr)
            return None, 1

    if profile is not None:
        apply_profile_to_args(profile, args)

    return profile, None


def _announce_default_model(args: argparse.Namespace) -> None:
    """Print the auto-selected default model to stderr.

    sp-f4t: the CLI help claims "Default: best available" but the
    selection was previously silent — an operator running a panel
    against the auto-picked model could not tell *which* provider was
    being used. Surface the selection loudly so the user can ^C and
    re-run with ``--model`` if the pick is wrong.
    """
    if getattr(args, "model", None):
        return
    alias, source = _resolve_default_model()
    if source:
        logger.info(
            "--model not specified; using '%s' (detected %s). Override with --model <name>.",
            alias,
            source,
        )
    else:
        logger.warning(
            "--model not specified and no provider API keys detected; falling back to '%s'. Set one of: %s.",
            alias,
            ", ".join(env for env, _ in _DEFAULT_MODEL_PREFERENCE),
        )


def handle_prompt(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Run a single non-interactive prompt and exit."""
    prompt_text = " ".join(args.text)
    _announce_default_model(args)
    model = _resolve_model(args)

    client = LLMClient()
    session = Session()
    runtime = AgentRuntime(
        client=client,
        session=session,
        model=model,
    )

    try:
        summary = runtime.run_turn(prompt_text)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Extract assistant response text
    response_text = ""
    for msg in summary.assistant_messages:
        for block in msg.content:
            if isinstance(block, dict) and block.get("type") == "text":
                response_text += block.get("text", "")

    usage_dict = summary.usage.to_dict() if summary.usage else None

    # Show cost summary for text output
    if fmt is OutputFormat.TEXT:
        print(response_text)
        pricing, is_estimated = lookup_pricing(model)
        cost = estimate_cost(summary.usage, pricing)
        print(format_summary("Cost", summary.usage, cost, model=model, is_estimated=is_estimated), file=sys.stderr)
    else:
        extra: dict[str, Any] = {}
        pricing, is_estimated = lookup_pricing(model)
        cost = estimate_cost(summary.usage, pricing)
        extra["cost"] = cost.format_usd()
        extra["model"] = model
        emit(fmt, message=response_text, usage=usage_dict, extra=extra)

    return 0


# ---------------------------------------------------------------------------
# YAML loading helpers
# ---------------------------------------------------------------------------


def _load_yaml(path: str) -> Any:
    """Load and return parsed YAML from *path*."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    with open(p, encoding="utf-8") as f:
        try:
            return yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in {path}: {exc}") from exc


def _load_personas(path_or_name: str) -> list[dict[str, Any]]:
    """Load personas from a YAML file *or* an installed pack name.

    Resolution order (sy-n80 / gh-508):

    1. If ``path_or_name`` is an existing file, parse it as YAML.
    2. Otherwise try :func:`get_persona_pack` — this covers both
       user-saved packs under ``$SYNTH_PANEL_DATA_DIR/persona_packs/``
       and bundled packs that ship in the wheel.
    3. Otherwise consult the registry (best-effort, offline-safe): if
       the id is a known *registry* pack, raise a targeted
       :class:`FileNotFoundError` whose message points the caller at
       ``althing pack import <id>`` so the happy path is one
       command away.
    4. Otherwise raise a generic "not found as file or installed pack"
       error that distinguishes filesystem paths from pack aliases.

    Expected file format::

        personas:
          - name: Alice
            age: 34
            occupation: Teacher
            background: ...
            personality_traits: [curious, empathetic]
    """
    if Path(path_or_name).exists():
        data = _load_yaml(path_or_name)
    else:
        from althing.mcp.data import get_persona_pack

        try:
            data = get_persona_pack(path_or_name)
        except (FileNotFoundError, ValueError):
            # Registry hint: distinguishing "this name is a known
            # registry pack you haven't installed" from "this is just
            # garbage" is the single most useful diagnostic for an
            # agent debugging --personas. The lookup is best-effort —
            # offline / network failures degrade silently to the
            # generic error so we never block a user who's working
            # locally.
            hint = _registry_install_hint(path_or_name)
            if hint:
                raise FileNotFoundError(hint) from None
            raise FileNotFoundError(
                f"Personas not found as file or installed pack: {path_or_name!r}. "
                f"Run `althing pack list` to see installed packs, or "
                f"`althing pack search <query>` to find registry packs."
            ) from None

    if isinstance(data, dict) and "personas" in data:
        return data["personas"]
    if isinstance(data, list):
        return data
    raise ValueError(f"Invalid personas file: expected 'personas' key or a list, got {type(data).__name__}")


def _registry_install_hint(pack_id: str) -> str | None:
    """Return an actionable message if *pack_id* is a known registry pack.

    Best-effort: any failure (offline, network, registry parse) returns
    ``None`` so the caller falls back to the generic "not found" error.
    """
    try:
        from althing.registry import resolve_pack

        entry = resolve_pack(pack_id)
    except Exception:
        return None
    if entry is None:
        return None
    return (
        f"{pack_id!r} is a registry pack, not yet installed locally. "
        f"Run `althing pack import {pack_id}` to install it, then "
        f"re-run with `--personas {pack_id}`."
    )


def _merge_persona_lists_with_collisions(
    base: list[dict[str, Any]],
    merge_paths: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Append personas from each merge path onto *base*, recording name collisions.

    Files are loaded in order and their personas appended. If a later
    persona shares a ``name`` with an earlier one, the later entry
    replaces the earlier one in place (order of first occurrence is
    preserved) and the collision is recorded in the returned list.
    Personas without a ``name`` are always appended — we cannot safely
    dedupe them and so never flag them as collisions.

    Returns ``(merged, collisions)`` where each collision is a dict
    ``{"name": str, "source_path": str}`` identifying the dropped entry
    and the merge file whose persona replaced it.
    """
    by_name: dict[str, int] = {}
    merged: list[dict[str, Any]] = []
    collisions: list[dict[str, Any]] = []
    for p in base:
        name = p.get("name") if isinstance(p, dict) else None
        if isinstance(name, str) and name:
            by_name[name] = len(merged)
        merged.append(p)
    for path in merge_paths:
        for p in _load_personas(path):
            name = p.get("name") if isinstance(p, dict) else None
            if isinstance(name, str) and name and name in by_name:
                merged[by_name[name]] = p
                collisions.append({"name": name, "source_path": path})
            else:
                if isinstance(name, str) and name:
                    by_name[name] = len(merged)
                merged.append(p)
    return merged, collisions


def _merge_persona_lists(base: list[dict[str, Any]], merge_paths: list[str]) -> list[dict[str, Any]]:
    """Thin wrapper over :func:`_merge_persona_lists_with_collisions` that
    discards collision metadata. Retained for callers that only need the
    merged list.
    """
    merged, _ = _merge_persona_lists_with_collisions(base, merge_paths)
    return merged


def _load_instrument(path_or_name: str) -> Instrument:
    """Load an instrument from a file path *or* an installed pack name.

    Supports v1 (flat), v2 (linear rounds), v3 (branching). When
    ``path_or_name`` is not an existing file, falls back to
    :func:`load_instrument_pack` which looks under
    ``$SYNTH_PANEL_DATA_DIR/packs/instruments/``. The unified resolver
    is what makes ``panel run --instrument <name>`` accept pack names
    in addition to file paths.
    """
    if Path(path_or_name).exists():
        data = _load_yaml(path_or_name)
    else:
        from althing.mcp.data import load_instrument_pack

        try:
            data = load_instrument_pack(path_or_name)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Instrument not found as file or installed pack: {path_or_name}") from exc

    if isinstance(data, dict) and "instrument" in data:
        raw = data["instrument"]
    elif isinstance(data, dict) and ("questions" in data or "rounds" in data):
        raw = data
    else:
        raise ValueError("Invalid instrument: expected 'instrument', 'questions', or 'rounds' key")
    raw.setdefault("version", 1)
    return parse_instrument(raw)


def _collect_template_vars(args: argparse.Namespace) -> dict[str, str]:
    """Merge ``--vars-file`` and repeated ``--var KEY=VALUE`` flags into a
    single substitution context.

    CLI ``--var`` entries override matching keys from ``--vars-file``
    so ad-hoc overrides win on conflict. Both sources stringify their
    values because the underlying template engine operates on strings.
    Raises :class:`ValueError` on malformed input so the caller can
    surface a friendly message.
    """
    merged: dict[str, str] = {}

    vars_file = getattr(args, "vars_file", None)
    if vars_file:
        try:
            data = _load_yaml(vars_file)
        except FileNotFoundError as exc:
            raise ValueError(str(exc)) from exc
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError(f"--vars-file must be a YAML mapping, got {type(data).__name__}")
        for k, v in data.items():
            merged[str(k)] = _stringify_var(v)

    for entry in getattr(args, "vars", None) or []:
        if "=" not in entry:
            raise ValueError(f"--var expects KEY=VALUE, got: {entry!r}")
        key, value = entry.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"--var has empty key: {entry!r}")
        merged[key] = value
    return merged


def _stringify_var(value: Any) -> str:
    """Convert a YAML-loaded var value to a string suitable for substitution.

    Lists and tuples are joined with ``", "`` so ``candidates: [A, B, C]``
    in a vars file renders the way a human would write it inline. Other
    scalars go through ``str()``.
    """
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _apply_vars_to_instrument(instrument: Instrument, template_vars: dict[str, str]) -> None:
    """Substitute ``template_vars`` into every round's question text.

    Mutates the instrument in place: each ``Round.questions`` list is
    replaced with its rendered form. Callers in ``handle_panel_run``
    invoke :func:`find_unresolved_in_questions` *before* this function
    so any missing key aborts the run (sp-6yi); any ``{placeholder}``
    that survives here is either dynamic (resolved downstream) or
    explicitly opted in via ``--allow-unresolved``.

    Thin wrapper over the shared engine in :mod:`althing.templates`
    so the CLI and the MCP ``run_panel(vars=...)`` path (GH#562) render
    identically.
    """
    from althing.templates import apply_vars_to_instrument

    apply_vars_to_instrument(instrument, template_vars)


def _load_schema(value: str) -> dict[str, Any]:
    """Load a JSON Schema from a file path or inline JSON string."""
    p = Path(value)
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    # Try parsing as inline JSON
    try:
        schema = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Schema is not a valid file path or JSON string: {exc}") from exc
    if not isinstance(schema, dict):
        raise ValueError(f"Schema must be a JSON object, got {type(schema).__name__}")
    return schema


# sp-ekayy9: rough output-token defaults per response_schema type. The
# dry-run preview multiplies these by persona_count × question_count to
# get a worst-case bound for cost estimation. Real responses are usually
# shorter (especially for free text), so the printed estimate is an upper
# bound — exactly what an operator needs before paying for a panel run.
_DRY_RUN_OUTPUT_TOKENS_TEXT_DEFAULT = 300
_DRY_RUN_OUTPUT_TOKENS_BY_SCHEMA: dict[str, int] = {
    "scale": 30,
    "enum": 30,
    "tagged_themes": 100,
}


def _estimate_output_tokens_per_response(question: Any) -> int:
    """Return a rough per-response output-token estimate for *question*."""
    if not isinstance(question, dict):
        return _DRY_RUN_OUTPUT_TOKENS_TEXT_DEFAULT
    rs = question.get("response_schema")
    if not isinstance(rs, dict):
        return _DRY_RUN_OUTPUT_TOKENS_TEXT_DEFAULT
    rs_type = rs.get("type")
    if rs_type == "text":
        max_tokens = rs.get("max_tokens")
        if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
            return max_tokens
        return _DRY_RUN_OUTPUT_TOKENS_TEXT_DEFAULT
    if isinstance(rs_type, str) and rs_type in _DRY_RUN_OUTPUT_TOKENS_BY_SCHEMA:
        return _DRY_RUN_OUTPUT_TOKENS_BY_SCHEMA[rs_type]
    return _DRY_RUN_OUTPUT_TOKENS_TEXT_DEFAULT


def _question_meta_for_save(questions: list[Any]) -> list[dict[str, Any]]:
    """Build the per-question metadata persisted alongside a saved result.

    sy-547: ``poll-summary`` / ``analyze`` read the saved result's
    ``questions`` field to recover each question's ``response_schema`` and
    bucket it as enum/scale. Previously the standard run path never passed
    ``questions=`` to :func:`save_panel_result`, so the schema was lost and
    every question degraded to ``kind=text``. Emit a minimal
    ``{text, response_schema?}`` entry per authored question, preserving the
    declared schema verbatim so the downstream ``_detect_kind`` is schema-
    driven rather than guessing from response shape.
    """
    meta: list[dict[str, Any]] = []
    for q in questions:
        if not isinstance(q, dict):
            meta.append({"text": str(q)})
            continue
        entry: dict[str, Any] = {"text": build_question_prompt(q)}
        rs = q.get("response_schema")
        if rs is not None:
            entry["response_schema"] = rs
        meta.append(entry)
    return meta


def _blend_dropped_models(ensemble: Any) -> list[str]:
    """Return blend members that produced zero usable responses (sy-546).

    A model is "dropped" when, across all its panelists, every primary
    (non-follow-up) response is an error or empty — i.e. the model
    contributed nothing to the blended distributions. A bad slug that
    404s on every call lands here, as does a model that died mid-run.
    Order follows ``ensemble.models``.
    """
    dropped: list[str] = []
    for mr in ensemble.model_results:
        usable = 0
        for pr in mr.panelist_results:
            for resp in pr.responses:
                if not isinstance(resp, dict) or resp.get("follow_up"):
                    continue
                if resp.get("error"):
                    continue
                val = resp.get("response")
                if val is None:
                    continue
                if isinstance(val, str) and (not val.strip() or val.strip().startswith("[error:")):
                    continue
                usable += 1
        if usable == 0:
            dropped.append(mr.model)
    return dropped


def _run_model_preflight(
    models: list[str],
    *,
    args: Any,
    client: Any | None = None,
) -> int | None:
    """Probe *models* for reachability; return an exit code to abort or None.

    sy-546. Aborts (returns 1) when the reachable-model count violates the
    configured guard:

    * ``--min-models N`` set → abort if fewer than N models are reachable.
    * Otherwise (default, or ``--require-all-models``) → abort if ANY model
      is unreachable.

    Unreachable models are always reported. Inconclusive probes (transient /
    auth / missing-credential failures) never count against the guard — they
    are not a property of the slug.
    """
    from althing.preflight import preflight_models

    print(
        f"Pre-flight: probing {len(set(models))} model(s) for reachability...",
        file=sys.stderr,
    )
    report = preflight_models(models, client=client)

    reachable = [p.model for p in report.probes if p.status == "reachable"]
    inconclusive = [p for p in report.probes if p.status == "inconclusive"]
    for p in inconclusive:
        print(
            f"Pre-flight: could not verify {p.model} (inconclusive: {p.detail or 'unknown error'}); "
            "proceeding — it will surface at call time if truly broken.",
            file=sys.stderr,
        )

    min_models = getattr(args, "min_models", None)
    require_all = getattr(args, "require_all_models", False)

    if min_models is not None and not require_all:
        # Relaxed guard: only abort if too few models are reachable. Treat
        # inconclusive as potentially-usable so a flaky probe doesn't trip
        # the floor.
        usable = len(reachable) + len(inconclusive)
        if usable < min_models:
            if report.unreachable:
                print(report.failure_message(), file=sys.stderr)
            print(
                f"Pre-flight failed: only {usable} model(s) reachable, --min-models requires {min_models}.",
                file=sys.stderr,
            )
            return 1
        if report.unreachable:
            bad = ", ".join(p.model for p in report.unreachable)
            print(
                f"Pre-flight: WARNING — {len(report.unreachable)} model(s) unreachable ({bad}); "
                f"proceeding with {len(reachable)} reachable model(s) per --min-models={min_models}.",
                file=sys.stderr,
            )
        return None

    # Default / --require-all-models: any unreachable slug is fatal.
    if report.unreachable:
        print(report.failure_message(), file=sys.stderr)
        return 1

    print(
        f"Pre-flight: OK — all {len(reachable)} model(s) reachable.",
        file=sys.stderr,
    )
    return None


def _iter_instrument_attachments(instrument: Instrument):
    """Yield every attachment dict the instrument would send to the model.

    Covers the shared bank (``Instrument.attachments`` values) and every
    per-question ``inline_attachments`` dict. Bank-ref strings in a
    question's ``attachments`` list resolve back to bank entries, which are
    already yielded from the bank, so we don't need to dereference them here.
    """
    for att in instrument.attachments.values():
        if isinstance(att, dict):
            yield att
    for q in instrument.questions:
        if not isinstance(q, dict):
            continue
        for att in q.get("inline_attachments") or []:
            if isinstance(att, dict):
                yield att
        # Defensive: some authoring shapes inline dicts directly under
        # `attachments` (not just bank-ref strings).
        for att in q.get("attachments") or []:
            if isinstance(att, dict):
                yield att


def _instrument_visual_attachment_kinds(instrument: Instrument) -> list[str]:
    """Return the distinct visual-attachment kinds present in *instrument*.

    A "visual" attachment is one the run-time guard
    (:func:`assert_supports_attachments`) would reject on a text-only model:
    ``image`` (-> ImageBlock), ``document`` (-> DocumentBlock), and ``url``
    attachments fetched as a screenshot (``fetch_mode: screenshot``), which
    lower to an image block. Returned in a stable order for deterministic
    output.
    """
    found: set[str] = set()
    for att in _iter_instrument_attachments(instrument):
        atype = att.get("type")
        if atype == "image":
            found.add("image")
        elif atype == "document":
            found.add("document")
        elif atype == "url" and att.get("fetch_mode") == "screenshot":
            found.add("screenshot")
    order = ["image", "document", "screenshot"]
    return [k for k in order if k in found]


def _emit_dry_run_preview(
    *,
    personas: list[dict[str, Any]],
    instrument: Instrument,
    system_prompt_fn,
    model: str,
    fmt: OutputFormat,
    personas_merge_warnings: list[dict[str, Any]] | None = None,
    personas_merge_used: bool = False,
) -> None:
    """Print the fully substituted panel inputs without any LLM call.

    Shows each question as it will appear to the LLM (after --var
    substitution, which has already been applied to the instrument),
    persona/question counts, panel composition, a rough token estimate,
    and an estimated cost from the local pricing table.
    """
    persona_count = len(personas)
    questions = instrument.questions
    question_count = len(questions)
    llm_calls = persona_count * question_count

    # sy-547 (d): count questions with an enforceable typed response_schema
    # (enum/scale) so the preview can be explicit that these are coerced
    # post-hoc, not constrained at generation.
    typed_schema_count = sum(1 for q in questions if isinstance(q, dict) and is_typed_schema(q.get("response_schema")))

    system_prompt_chars = sum(len(system_prompt_fn(p)) for p in personas)
    question_chars = sum(len(build_question_prompt(q)) for q in questions)
    follow_up_chars = 0
    for q in questions:
        follow_ups = q.get("follow_ups") if isinstance(q, dict) else None
        if not follow_ups:
            continue
        for fu in follow_ups:
            if isinstance(fu, dict):
                follow_up_chars += len(str(fu.get("text", "")))
            else:
                follow_up_chars += len(str(fu))
    total_chars = system_prompt_chars + persona_count * (question_chars + follow_up_chars)
    estimated_input_tokens = max(1, total_chars // 4)

    output_tokens_per_persona = sum(_estimate_output_tokens_per_response(q) for q in questions)
    estimated_output_tokens = persona_count * output_tokens_per_persona

    pricing, pricing_is_estimated = lookup_pricing(model)
    cost = estimate_cost(
        TokenUsage(
            input_tokens=estimated_input_tokens,
            output_tokens=estimated_output_tokens,
        ),
        pricing,
    )

    # #536: a real run fast-fails when image/document/screenshot attachments
    # are routed to a text-only model (assert_supports_attachments). Mirror
    # that guard in dry-run so validation surfaces the misconfiguration
    # instead of reporting "OK" and letting the user burn tokens on the
    # real run.
    from althing.llm.capabilities import model_supports_vision

    visual_kinds = _instrument_visual_attachment_kinds(instrument)
    vision_conflict = bool(visual_kinds) and not model_supports_vision(model)
    vision_warning = None
    if vision_conflict:
        kinds_str = ", ".join(visual_kinds)
        vision_warning = (
            f"model {model!r} is text-only but the instrument has "
            f"{kinds_str} attachment(s). A real run would fail "
            "(or silently burn tokens returning a no-image response). "
            "Switch to a vision-capable model (e.g. sonnet, opus, "
            "claude-haiku-4-5) or remove the attachments."
        )

    # P1-2: name the engine the real run will use so a dry-run reader knows
    # whether route_when routing will actually execute.
    branching = any(r.route_when for r in instrument.rounds)
    if instrument.is_multi_round:
        engine_label = "multi-round router" + (" (branching)" if branching else " (linear rounds)")
    else:
        engine_label = "single-round parallel panel"

    if fmt is OutputFormat.TEXT:
        print("DRY RUN — no LLM calls will be made", file=sys.stderr)
        print(f"Model: {model}", file=sys.stderr)
        print(f"Personas: {persona_count}", file=sys.stderr)
        if instrument.is_multi_round:
            print(f"Questions: {question_count} across {len(instrument.rounds)} rounds", file=sys.stderr)
        else:
            print(f"Questions: {question_count}", file=sys.stderr)
        print(f"Engine: {engine_label}", file=sys.stderr)
        if branching:
            print(
                "Note: route_when predicates decide the executed path at run time; "
                "the question/call/cost figures below cover every declared round "
                "and are an UPPER BOUND — the routed run may execute fewer.",
                file=sys.stderr,
            )
        print(
            f"Panel: {persona_count} personas x {question_count} questions = {llm_calls:,} LLM calls",
            file=sys.stderr,
        )
        if instrument.is_multi_round:
            print("", file=sys.stderr)
            print("Instrument DAG:", file=sys.stderr)
            for line in _render_text_dag(instrument).splitlines():
                print(f"  {line}", file=sys.stderr)
        print("", file=sys.stderr)

        for round_ in instrument.rounds:
            if instrument.is_multi_round:
                print(f"Round: {round_.name}", file=sys.stderr)
            for idx, q in enumerate(round_.questions, start=1):
                prompt_text = build_question_prompt(q)
                prefix = f"  {idx}." if instrument.is_multi_round else f"{idx}."
                print(f"{prefix} {prompt_text}", file=sys.stderr)
                follow_ups = q.get("follow_ups") if isinstance(q, dict) else None
                if follow_ups:
                    for fu in follow_ups:
                        fu_text = fu.get("text", "") if isinstance(fu, dict) else str(fu)
                        indent = "     " if instrument.is_multi_round else "   "
                        print(f"{indent}↳ (follow-up) {fu_text}", file=sys.stderr)
            if instrument.is_multi_round:
                print("", file=sys.stderr)

        print(
            f"Estimated input tokens: ~{estimated_input_tokens:,} "
            f"({persona_count} personas x {question_count} questions, ~4 chars/token)",
            file=sys.stderr,
        )
        print(
            f"Estimated output tokens: ~{estimated_output_tokens:,} (rough heuristic from response_schema)",
            file=sys.stderr,
        )
        cost_suffix = " [pricing=estimated-default]" if pricing_is_estimated else ""
        print(
            f"Estimated cost ({model}): ~${cost.total_cost:.4f}{cost_suffix}",
            file=sys.stderr,
        )
        if vision_warning:
            print(f"Validation: WARNING — {vision_warning}", file=sys.stderr)
        else:
            print("Validation: OK (instrument spec is structurally valid)", file=sys.stderr)
        # sy-547 (d): be explicit that a typed response_schema is NOT
        # enforced at generation. The real run coerces free-text answers to
        # the nearest enum option / in-range integer post-hoc and flags any
        # that can't be mapped, but "Validation: OK" must not read as
        # "output is guaranteed to be one of the options".
        if typed_schema_count:
            print(
                f"Note: {typed_schema_count} question(s) declare an enum/scale response_schema. "
                "These are NOT constrained at generation — the run coerces each free-text answer "
                "to the nearest option / in-range integer post-hoc and flags unmappable answers. "
                "Dry-run does not perform that coercion.",
                file=sys.stderr,
            )
        return

    preview: dict[str, Any] = {
        "dry_run": True,
        "model": model,
        "persona_count": persona_count,
        "question_count": question_count,
        "llm_calls": llm_calls,
        "estimated_input_tokens": estimated_input_tokens,
        "estimated_output_tokens": estimated_output_tokens,
        "estimated_cost_usd": round(cost.total_cost, 6),
        "cost_is_estimated": pricing_is_estimated,
        "validation": "warning" if vision_warning else "ok",
        # P1-2: which engine the real run dispatches through. For branching
        # instruments the question/call/cost figures are an upper bound over
        # every declared round — route_when decides the executed path.
        "engine": "multi_round" if instrument.is_multi_round else "single_round",
        # sy-547 (d): advertise that typed response_schemas are coerced
        # post-hoc, not enforced at generation, so JSON consumers don't
        # treat "validation: ok" as "output guaranteed constrained".
        "typed_schema_question_count": typed_schema_count,
        "typed_schema_enforced": False,
        "rounds": [
            {
                "name": r.name,
                "questions": [build_question_prompt(q) for q in r.questions],
                **({"depends_on": r.depends_on} if r.depends_on else {}),
                **({"route_when": r.route_when} if r.route_when else {}),
            }
            for r in instrument.rounds
        ],
    }
    if branching:
        preview["estimates_are_upper_bound"] = True
    # sp-g270: surface collision metadata so dashboards and MCP
    # consumers see the dropped names without parsing stderr.
    if personas_merge_used:
        preview["personas_merge_warnings"] = personas_merge_warnings or []
    # #536: structured surface of the vision/text-only conflict so JSON
    # consumers can gate on it without scraping the warning string.
    if vision_warning:
        preview["vision_conflict"] = {
            "model": model,
            "attachment_kinds": visual_kinds,
            "message": vision_warning,
        }
    emit(fmt, message="Dry-run preview", extra=preview)


# ── sp-hsk3: checkpoint helpers ──────────────────────────────────────
# These bridge :mod:`althing.checkpoint` (which speaks JSON dicts,
# no domain types) with the orchestrator's :class:`PanelistResult`.
# Kept here rather than in ``checkpoint.py`` so that module stays free
# of CLI / cost concerns and is reusable from the MCP surface later.


def _panelist_result_to_ckpt_dict(pr: PanelistResult, fallback_model: str) -> dict[str, Any]:
    """Serialize a :class:`PanelistResult` in the same shape the CLI emits."""
    pr_model = pr.model or fallback_model
    pricing, _is_estimated = lookup_pricing(pr_model)
    cost = estimate_cost(pr.usage, pricing)
    record: dict[str, Any] = {
        "persona": pr.persona_name,
        "responses": pr.responses,
        "usage": pr.usage.to_dict(),
        "cost": cost.format_usd(),
        "error": pr.error,
    }
    if pr.model:
        record["model"] = pr.model
    return record


def _panelist_result_from_dict(record: dict[str, Any]) -> PanelistResult:
    """Reconstitute a :class:`PanelistResult` from a checkpoint record."""
    usage_dict = record.get("usage") or {}
    usage = TokenUsage.from_dict(usage_dict) if usage_dict else ZERO_USAGE
    return PanelistResult(
        persona_name=record.get("persona", "Anonymous"),
        responses=list(record.get("responses") or []),
        usage=usage,
        error=record.get("error"),
        model=record.get("model"),
    )


def _merge_usage_dicts(current: dict[str, Any], increment: dict[str, Any]) -> dict[str, Any]:
    """Add two ``TokenUsage.to_dict()`` payloads without dropping provider cost."""
    from althing.checkpoint import _merge_usage

    return _merge_usage(current, increment)


def _build_run_config_fingerprint(
    *,
    personas: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    model: str,
    persona_models: dict[str, str] | None,
    temperature: float | None,
    top_p: float | None,
    response_schema: dict[str, Any] | None,
    extract_schema: dict[str, Any] | None,
    template_vars: dict[str, str] | None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Build a stable-key config dict whose hash detects resume drift.

    Only the fields that would materially alter panelist responses are
    included. Things like --checkpoint-every or stdout formatting are
    deliberately excluded so a user can change them mid-resume.
    """
    question_texts = [(q.get("text") if isinstance(q, dict) else str(q)) for q in questions]
    return {
        "persona_names": [p.get("name", "Anonymous") for p in personas],
        "persona_count": len(personas),
        "question_texts": question_texts,
        "question_count": len(questions),
        "model": model,
        "persona_models": dict(persona_models) if persona_models else None,
        "temperature": temperature,
        "top_p": top_p,
        "seed": seed,
        "response_schema": response_schema,
        "extract_schema": extract_schema,
        "template_vars": dict(template_vars) if template_vars else None,
    }


def _build_resume_cli_args(args: argparse.Namespace) -> dict[str, Any]:
    """Snapshot the CLI args needed to re-run this command via --resume.

    Stored alongside the checkpoint so ``althing panel run --resume <id>``
    can recover --personas / --instrument paths without the user having to
    re-pass them. Deliberately excluded from the config fingerprint so that
    moving the file (or running from a different cwd) doesn't trigger drift
    — only semantic changes to the personas / questions / model do.
    """
    return {
        "personas": getattr(args, "personas", None),
        "instrument": getattr(args, "instrument", None),
        "personas_merge": list(getattr(args, "personas_merge", None) or []),
        "personas_merge_on_collision": getattr(args, "personas_merge_on_collision", None),
    }


def _apply_resume_cli_args(args: argparse.Namespace, saved: dict[str, Any]) -> None:
    """Fill in absent --personas / --instrument from a checkpoint's saved cli_args.

    Only fills fields the current invocation left unset so an explicit
    flag from the user always wins.
    """
    if getattr(args, "personas", None) is None and saved.get("personas"):
        args.personas = saved["personas"]
    if getattr(args, "instrument", None) is None and saved.get("instrument"):
        args.instrument = saved["instrument"]
    if not getattr(args, "personas_merge", None) and saved.get("personas_merge"):
        args.personas_merge = list(saved["personas_merge"])
    # ``personas_merge_on_collision`` has a default ('dedup') so we can't
    # tell whether the user explicitly passed it. Restore the saved value
    # only when the current value is the default and the saved value
    # differs — otherwise an explicit ``--personas-merge-on-collision=dedup``
    # would silently get overridden.
    if (
        getattr(args, "personas_merge_on_collision", None) == "dedup"
        and saved.get("personas_merge_on_collision")
        and saved["personas_merge_on_collision"] != "dedup"
    ):
        args.personas_merge_on_collision = saved["personas_merge_on_collision"]


def handle_panel_run(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Run a panel: load personas + instrument, run panelists in parallel."""
    # ── sp-prof: profile loading ──────────────────────────────────────
    # Load profile defaults before anything else so CLI flags can override.
    profile, profile_err = _load_profile(args)
    if profile_err is not None:
        return profile_err

    # ── sy-ws76: --resume early-arg recovery ──────────────────────────
    # If the user typed ``althing panel run --resume <id>`` and omitted
    # --personas / --instrument, recover those paths from the checkpoint
    # before any downstream loader runs. We keep the heavy drift check
    # below in the main checkpoint path; this just patches missing args.
    resume_id = getattr(args, "resume", None)
    if resume_id and (args.personas is None or args.instrument is None):
        checkpoint_root_arg = getattr(args, "checkpoint_dir", None)
        root_path = Path(checkpoint_root_arg) if checkpoint_root_arg else default_checkpoint_root()
        try:
            preview_ckpt = load_checkpoint(resume_id, root_path)
        except CheckpointNotFoundError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        except CheckpointError as exc:
            print(f"Error: checkpoint load failed: {exc}", file=sys.stderr)
            return 1
        if preview_ckpt.cli_args:
            _apply_resume_cli_args(args, preview_ckpt.cli_args)

    if args.personas is None:
        print(
            "Error: --personas is required (or pass --resume <run-id> to recover the path from a checkpoint).",
            file=sys.stderr,
        )
        return 1
    if args.instrument is None:
        print(
            "Error: --instrument is required (or pass --resume <run-id> to recover the path from a checkpoint).",
            file=sys.stderr,
        )
        return 1

    # Validate mutual exclusivity of --model and --models
    has_model = getattr(args, "model", None)
    has_models = getattr(args, "models", None)
    if has_model and has_models:
        print("Error: --model and --models are mutually exclusive.", file=sys.stderr)
        return 1

    # sp-zq3: --best-model-for consults the SynthBench leaderboard and
    # overrides --model. Must be applied before _resolve_model() so the
    # rest of the pipeline sees the recommended model as if the user had
    # typed --model themselves.
    best_for = getattr(args, "best_model_for", None)
    if best_for:
        if has_models:
            print(
                "Error: --best-model-for and --models are mutually exclusive.",
                file=sys.stderr,
            )
            return 1
        picked = _apply_best_model_for(args, best_for)
        if picked is None:
            # Fall through with whatever --model (or default) was already set.
            # _apply_best_model_for has already printed a user-visible
            # "synthbench unavailable" line to stderr.
            pass

    if not has_models:
        _announce_default_model(args)
    model = _resolve_model(args)

    try:
        personas = _load_personas(args.personas)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error loading personas: {exc}", file=sys.stderr)
        return 1

    # sp-on4: --personas-merge appends personas from additional files onto
    # the base --personas pack. When a persona name collides, the later
    # entry wins so callers can override pack defaults with a follow-on file.
    #
    # sp-g270: silent deduplication is dangerous at n>=50 with bundled
    # packs — a 20-name collision can quietly shrink a declared panel by
    # 20% with no visible signal. Every merge now records collisions,
    # emits a stderr warning, and surfaces them in JSON output. The
    # --personas-merge-on-collision flag lets users upgrade dedup to a
    # hard error for scripts that require exact panel size.
    merge_paths = getattr(args, "personas_merge", None) or []
    personas_merge_warnings: list[dict[str, Any]] = []
    merge_used = bool(merge_paths)
    on_collision = getattr(args, "personas_merge_on_collision", "dedup") or "dedup"
    if merge_paths:
        try:
            merged, collisions = _merge_persona_lists_with_collisions(personas, merge_paths)
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error loading --personas-merge: {exc}", file=sys.stderr)
            return 1

        if on_collision == "keep":
            print(
                "Error: --personas-merge-on-collision=keep is reserved and "
                "not yet implemented. Use 'dedup' (default) or 'error'.",
                file=sys.stderr,
            )
            return 1

        if collisions:
            post_count = len(merged)
            pre_count = post_count + len(collisions)
            names_joined = ", ".join(c["name"] for c in collisions)

            if on_collision == "error":
                print(
                    f"Error: --personas-merge introduced {len(collisions)} "
                    f"name collision(s) ({names_joined}); "
                    f"--personas-merge-on-collision=error. Rename or drop "
                    f"the duplicate personas before re-running.",
                    file=sys.stderr,
                )
                return 1

            # dedup (default): keep current silently-later-wins behavior,
            # but make the drop loud.
            print(
                f"Warning: --personas-merge name collisions dropped "
                f"{len(collisions)} persona(s): {names_joined}. "
                f"Panel size is {post_count} (would be {pre_count} "
                f"without dedup). Pass "
                f"--personas-merge-on-collision=error to fail instead.",
                file=sys.stderr,
            )
            personas_merge_warnings = [
                {
                    "type": "name_collision",
                    "name": c["name"],
                    "source_path": c["source_path"],
                    "post_dedup_count": post_count,
                    "pre_dedup_count": pre_count,
                }
                for c in collisions
            ]
        personas = merged

    # --population narrows the panel to personas matching attribute values
    # (population packs such as global-respondents; see althing.population).
    population_spec = getattr(args, "population", None)
    if population_spec:
        from althing.population import PopulationError, filter_personas, parse_population

        try:
            personas = filter_personas(personas, parse_population(population_spec))
        except PopulationError as exc:
            print(f"Error: --population: {exc}", file=sys.stderr)
            return 1

    # synthbench#261 / sy-2ag: when --model was NOT given and the
    # auto-resolved default is a known-slow router (openrouter/auto), a
    # ≥10-persona panel can hang for many minutes. Swap the default for a
    # fast equivalent via the shared policy and say so on stderr. Explicit
    # --model / --models / --best-model-for picks are honored verbatim
    # (all three leave args.model / has_models truthy by this point).
    if not getattr(args, "model", None) and not has_models:
        fast_model, swapped_from = _fast_default.fast_default_for_panel(model, len(personas))
        if swapped_from is not None:
            model = fast_model
            print(
                "Note: "
                + _fast_default.format_fast_default_note(
                    fast_model, swapped_from, len(personas), override_hint="--model"
                ),
                file=sys.stderr,
            )

    try:
        instrument = _load_instrument(args.instrument)
    except (FileNotFoundError, ValueError, InstrumentError) as exc:
        print(f"Error loading instrument: {exc}", file=sys.stderr)
        return 1

    # Surface parser warnings (e.g. unreachable rounds) to stderr — these
    # are non-fatal but the user should see them before any LLM call fires.
    for w in instrument.warnings:
        logger.warning("instrument: %s", w)

    # ── sp-1hb: variable substitution ──────────────────────────────────
    # Bundled instrument packs ship with {candidates}, {theme_0}, and
    # similar placeholders so they can be reused as research templates.
    # Without --var the user had to hand-edit YAML; this resolves the
    # variables at load time so the panelists see substituted text.
    try:
        template_vars = _collect_template_vars(args)
    except ValueError as exc:
        print(f"Error parsing --var / --vars-file: {exc}", file=sys.stderr)
        return 1

    # ── sp-6yi: fail fast on unsubstituted placeholders ────────────────
    # If any question still contains a literal {placeholder} whose key is
    # not in template_vars, abort before burning LLM calls on a panel the
    # personas will correctly refuse to answer. --allow-unresolved opts
    # out for the rare case where the braces are genuinely intentional.
    unresolved = find_unresolved_in_questions(
        [q for rnd in instrument.rounds for q in rnd.questions],
        template_vars,
    )
    if unresolved:
        keys = ", ".join(unresolved)
        example = unresolved[0]
        if getattr(args, "allow_unresolved", False):
            print(
                f"Warning: instrument has unsubstituted placeholders ({keys}). "
                f"Proceeding because --allow-unresolved was passed; personas "
                f"will see the literal braces.",
                file=sys.stderr,
            )
        else:
            print(
                f"Error: instrument has unsubstituted placeholders: {keys}. "
                f"Pass a value for each (e.g. --var {example}=...) or "
                f"--allow-unresolved to proceed anyway.",
                file=sys.stderr,
            )
            return 1

    if template_vars:
        _apply_vars_to_instrument(instrument, template_vars)

    questions = instrument.questions
    if not questions:
        print("Error: instrument has no questions", file=sys.stderr)
        return 1

    # Load optional response schema
    response_schema: dict[str, Any] | None = None
    if getattr(args, "schema", None):
        try:
            response_schema = _load_schema(args.schema)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            print(f"Error loading schema: {exc}", file=sys.stderr)
            return 1

    # Load optional extraction schema (--extract-schema)
    extract_schema: dict[str, Any] | None = None
    if getattr(args, "extract_schema", None):
        try:
            extract_schema = _load_schema(args.extract_schema)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            print(f"Error loading extract-schema: {exc}", file=sys.stderr)
            return 1

    # Load optional prompt template (--prompt-template).
    # Compile Jinja2 templates once here (load time) so per-persona
    # rendering only calls .render() on the pre-compiled object.
    prompt_template_path = getattr(args, "prompt_template", None)
    _compiled_prompt_template = None
    _raw_prompt_template: str | None = None
    if prompt_template_path:
        try:
            _raw_prompt_template = load_prompt_template(prompt_template_path)
        except FileNotFoundError as exc:
            print(f"Error loading prompt template: {exc}", file=sys.stderr)
            return 1
        if is_jinja2_template(_raw_prompt_template):
            import jinja2 as _jinja2

            try:
                _compiled_prompt_template = compile_jinja2_template(_raw_prompt_template)
            except _jinja2.TemplateSyntaxError as exc:
                print(f"Error in Jinja2 prompt template: {exc}", file=sys.stderr)
                return 1

    # Build the system prompt function.
    # Resolution order: --prompt-template > instrument.system_prompt_template > default
    if _compiled_prompt_template is not None:
        _pt = _compiled_prompt_template

        def system_prompt_fn(persona: dict) -> str:
            return persona_system_prompt_from_template(persona, _pt)

    elif _raw_prompt_template is not None:
        _pt_str = _raw_prompt_template

        def system_prompt_fn(persona: dict) -> str:
            return persona_system_prompt_from_template(persona, _pt_str)

    elif instrument.system_prompt_template is not None:
        _inst_spt = instrument.system_prompt_template
        if is_jinja2_template(_inst_spt):
            _inst_compiled = compile_jinja2_template(_inst_spt)

            def system_prompt_fn(persona: dict) -> str:
                return persona_system_prompt_from_template(persona, _inst_compiled)

        else:

            def system_prompt_fn(persona: dict) -> str:
                return persona_system_prompt_from_template(persona, _inst_spt)

    else:
        system_prompt_fn = persona_system_prompt

    # Generation parameters
    temperature: float | None = getattr(args, "temperature", None)
    top_p: float | None = getattr(args, "top_p", None)
    seed: int | None = getattr(args, "seed", None)

    # ── sp-blend: multi-model ensemble ───────────────────────────────────
    # Parse --models spec and build per-persona model assignments.
    # Resolution order: persona YAML 'model' > --models weighted > --model.
    # Resolved before --dry-run so the preview shows the same breakdown
    # the real run would.
    blend_mode = getattr(args, "blend", False)
    persona_models: dict[str, str] | None = None
    model_spec: list[tuple[str, float]] | None = None
    ensemble_mode = has_models and is_ensemble_spec(has_models)
    assignment_warnings: list[str] = []

    if has_models:
        try:
            model_spec = parse_models_spec(has_models)
        except ValueError as exc:
            print(f"Error parsing --models: {exc}", file=sys.stderr)
            return 1
        # gh-r4: a one-entry weight-free spec ("--models X") is a plain
        # single-model run, not an ensemble. Routing it through the
        # ensemble branch silently skipped synthesis, exited 0 with a bare
        # "Ensemble complete", and bypassed the standard-path cost gate /
        # summary. Demote it to the same path "--model X" takes; ensemble
        # comparison needs >= 2 models. (--blend keeps its own validation.)
        if ensemble_mode and not blend_mode and len(model_spec) == 1:
            model = model_spec[0][0]
            args.model = model
            has_model = model
            has_models = None
            model_spec = None
            ensemble_mode = False
            print(
                f"Note: --models with a single model ({model}) runs a standard "
                "panel run (synthesis included). Ensemble comparison requires "
                "two or more models.",
                file=sys.stderr,
            )
    if has_models:
        # The demotion above nulls has_models and model_spec together, so a
        # truthy has_models implies a parsed spec; assert for mypy's sake.
        assert model_spec is not None
        # sp-zdul: warn if weighted sum is far from 1.0 — the user likely
        # intended an exact-ratio split and a typo (e.g. "0.3,0.3,0.3")
        # silently reweights their panel.
        if not ensemble_mode:
            weight_sum, is_close = check_weight_sum(model_spec)
            if not is_close:
                print(
                    f"Warning: --models weights sum to {weight_sum:.3f}, not 1.0. "
                    f"Weights will be normalized, preserving the ratio.",
                    file=sys.stderr,
                )
        if not ensemble_mode and not blend_mode:
            persona_models, assignment_warnings = assign_models_to_personas(personas, model_spec, model)
            for w in assignment_warnings:
                logger.warning("model allocation: %s", w)
                print(f"Warning: {w}", file=sys.stderr)
        # Use the first model in the spec as the "primary" for cost fallback
        if not has_model:
            model = model_spec[0][0]
    elif not has_models:
        # Even without --models, respect per-persona YAML model overrides.
        # Recognise both the legacy top-level ``model`` field and the
        # newer ``llm_overrides.model`` (sp-4loufu); top-level wins on
        # collision so existing personas keep their behaviour.
        yaml_overrides: dict[str, str] = {}
        for p in personas:
            name = p.get("name", "Anonymous")
            mdl = p.get("model") or (p.get("llm_overrides") or {}).get("model")
            if mdl:
                yaml_overrides[name] = mdl
        if yaml_overrides:
            persona_models = yaml_overrides

    # Validate --blend requires --models
    if blend_mode and not has_models:
        print("Error: --blend requires --models.", file=sys.stderr)
        return 1

    # sp-zdul: surface the deterministic persona→model assignment before
    # any LLM calls happen. An operator can ^C if the split looks wrong
    # (e.g. unbalanced due to rounding on small panels, or YAML overrides
    # they forgot about).
    if persona_models:
        print(format_assignment_breakdown(persona_models), file=sys.stderr)

    # ── sy-546: model reachability pre-flight ────────────────────────────
    # For multi-model runs (--models weighted / ensemble / --blend), probe
    # every distinct slug with a 1-token call before spending. A bogus slug
    # (e.g. a 404'ing OpenRouter id) deterministically fails every call and
    # silently shrinks an ensemble/blend; catch it here and fail fast naming
    # the bad slug(s). Runs on BOTH --dry-run and real runs so a dry-run's
    # "OK" actually means the spec is runnable. Bypassable with
    # --skip-preflight. Transient/auth/credential failures are inconclusive
    # and never block the run.
    preflight_models_list: list[str] = []
    if model_spec is not None and len(model_spec) > 1:
        preflight_models_list = [m for m, _w in model_spec]
    elif persona_models:
        preflight_models_list = sorted(set(persona_models.values()))
    if len(set(preflight_models_list)) > 1 and not getattr(args, "skip_preflight", False):
        rc = _run_model_preflight(preflight_models_list, args=args, client=None)
        if rc is not None:
            return rc

    # ── P1-2: multi-round flag compatibility audit ───────────────────────
    # Multi-round (v2/v3) instruments dispatch through the router-driven
    # engine below. Several panel-run features are only wired into the
    # single-round orchestrator path; refuse those combinations LOUDLY
    # here — before any LLM spend, and before --dry-run so a dry-run "OK"
    # means the real run is actually runnable (same philosophy as sy-546).
    if instrument.is_multi_round:
        flag_errors = _multi_round_flag_errors(
            args,
            instrument,
            ensemble_mode=ensemble_mode,
            blend_mode=blend_mode,
        )
        if flag_errors:
            for msg in flag_errors:
                print(f"Error: {msg}", file=sys.stderr)
            return 1

    # ── sp-x8g: --dry-run preview ────────────────────────────────────────
    # Short-circuit before any LLM-invoking code (variant expansion,
    # ensemble, blend, orchestrator). Shows the user what each question
    # will look like after --var substitution + prompt templating, plus
    # a rough input-token estimate, without spending any tokens.
    if getattr(args, "dry_run", False):
        _emit_dry_run_preview(
            personas=personas,
            instrument=instrument,
            system_prompt_fn=system_prompt_fn,
            model=model,
            fmt=fmt,
            personas_merge_warnings=personas_merge_warnings,
            personas_merge_used=merge_used,
        )
        return 0

    import uuid as _uuid

    request_id = _uuid.uuid4().hex[:12]
    logger.info("[%s] panel run: model=%s personas=%d questions=%d", request_id, model, len(personas), len(questions))

    max_concurrent = getattr(args, "max_concurrent", None)
    rate_limit_rps = getattr(args, "rate_limit_rps", None)
    client = LLMClient(
        max_concurrent=max_concurrent,
        rate_limit_rps=rate_limit_rps,
    )
    timer = PanelTimer()

    # ── P1-2: dispatch multi-round instruments through the real engine ──
    # Previously the CLI flat-concatenated every round's questions into one
    # run_panel_parallel call: every persona was asked every question from
    # every branch, no route_when predicate was ever evaluated, and the
    # output fabricated a linear "path". Multi-round instruments now run
    # through run_multi_round_panel — the same engine the MCP/SDK surfaces
    # use — and emit the rounds-shaped envelope with the REAL routing path.
    # The incompatible-flag audit above has already rejected unsupported
    # combinations, so everything reaching this point is safe to thread
    # through. Single-round instruments continue on the legacy path below,
    # unchanged.
    if instrument.is_multi_round:
        return _handle_panel_run_multi_round(
            args=args,
            fmt=fmt,
            client=client,
            timer=timer,
            personas=personas,
            instrument=instrument,
            model=model,
            system_prompt_fn=system_prompt_fn,
            response_schema=response_schema,
            extract_schema=extract_schema,
            temperature=temperature,
            top_p=top_p,
            seed=seed,
            persona_models=persona_models,
            template_vars=template_vars,
            personas_merge_warnings=personas_merge_warnings,
            merge_used=merge_used,
            profile=profile,
            max_concurrent=max_concurrent,
            request_id=request_id,
        )

    # ── sp-5on.15: variant expansion ─────────────────────────────────
    variants_k = getattr(args, "variants", None)
    if variants_k is not None:
        if variants_k < 1 or variants_k > 20:
            print("Error: --variants must be between 1 and 20.", file=sys.stderr)
            return 1
        orig_count = len(personas)
        total = variants_k * orig_count
        print(
            f"Generating {variants_k} variants for {orig_count} personas ({total} total sessions)...",
            file=sys.stderr,
        )
        variant_sets = generate_panel_variants(
            personas,
            client,
            k=variants_k,
            model=model,
        )
        personas = [v.persona for vs in variant_sets for v in vs.variants]
        print(
            f"Variant expansion complete: {len(personas)} variant personas ready.",
            file=sys.stderr,
        )

    # sp-utnk: optional mid-run cost gate. Declared before mode branching
    # so downstream reporting code can inspect gate state regardless of
    # which runner path executed. Only the parallel single-model path
    # wires it into the orchestrator — ensemble/blend runs ignore it
    # because their cost-accounting structure is richer and needs a
    # separate design.
    cost_gate: CostGate | None = None
    max_cost_usd = getattr(args, "max_cost", None)
    if max_cost_usd is not None and max_cost_usd <= 0:
        print(
            f"Error: --max-cost must be > 0, got {max_cost_usd}.",
            file=sys.stderr,
        )
        return 1

    # sp-xw2z6o: optional per-question failure budget. Accepts an integer
    # count (>= 1) or a fractional (0, 1) value; reject everything else
    # parse-time so a typo'd "0" or "1.5" is loud instead of silently
    # disabling the feature.
    question_budget_value: int | float | None = None
    raw_qfb = getattr(args, "question_failure_budget", None)
    if raw_qfb is not None:
        try:
            parsed_float = float(raw_qfb)
        except (TypeError, ValueError):
            print(
                f"Error: --question-failure-budget must be an integer (>= 1) or a fraction in (0, 1), got {raw_qfb!r}.",
                file=sys.stderr,
            )
            return 1
        # Integer-form when the float lands exactly on a whole number
        # (e.g. "2", "2.0"). Otherwise treat as a fractional threshold.
        if parsed_float.is_integer():
            parsed_int = int(parsed_float)
            if parsed_int < 1:
                print(
                    f"Error: --question-failure-budget integer must be >= 1, got {raw_qfb!r}.",
                    file=sys.stderr,
                )
                return 1
            question_budget_value = parsed_int
        else:
            if parsed_float <= 0 or parsed_float >= 1:
                print(
                    f"Error: --question-failure-budget fraction must be in (0, 1), got {raw_qfb!r}.",
                    file=sys.stderr,
                )
                return 1
            question_budget_value = parsed_float

    # ── Ensemble mode: run with each model, compare across models ────────
    # hq-hjq8: blend_mode wins. ``--blend`` with an ensemble-shape spec
    # (``a,b`` no colons) sets BOTH flags, but the user's intent is the
    # weight-averaged blend output documented in docs/ensemble.md, not the
    # bare per-model rollup that returns here. Falling through to the blend
    # branch below produces synthesis + blended distributions; the bare
    # ensemble path skips both.
    if ensemble_mode and not blend_mode:
        from althing.ensemble import build_ensemble_output, ensemble_run

        ensemble_models = [m for m, _w in model_spec]
        ens_result = ensemble_run(
            personas,
            questions,
            ensemble_models,
            client,
            system_prompt_fn=system_prompt_fn,
            question_prompt_fn=build_question_prompt,
            response_schema=response_schema,
            extract_schema=extract_schema,
            temperature=temperature,
            top_p=top_p,
            seed=seed,
            attachment_bank=instrument.attachments or None,
        )
        timer.stop()

        # sp-efip: fail loud if every panelist of every model failed.
        # Without this, ensemble runs with a knowingly-bad model name
        # silently returned exit 0 + an empty-but-well-shaped result.
        ensemble_panelists: list[PanelistResult] = []
        for mr in ens_result.model_results:
            ensemble_panelists.extend(mr.panelist_results)
        ensemble_failure = detect_total_failure(ensemble_panelists)
        if ensemble_failure is not None:
            msg = format_total_failure_message(ensemble_failure)
            print(_build_total_failure_banner(ensemble_failure), file=sys.stderr)
            if fmt is not OutputFormat.TEXT:
                emit(
                    fmt,
                    message=msg,
                    extra={
                        "run_invalid": True,
                        "total_failure": ensemble_failure,
                        "error": msg,
                    },
                )
            return 2

        # Per-persona usage/cost attribution (sp-hwe): pass the richer
        # ``format_panelist_result`` formatter so each result row in
        # ``per_model_results[*].results`` carries its own token counts
        # + priced cost, letting consumers answer "which model/persona
        # burned the most tokens?" without re-running the panel.
        from althing._runners import format_panelist_result as _fmt_panelist

        output = build_ensemble_output(ens_result, panelist_formatter=_fmt_panelist)
        # gh-r4: the ensemble path never runs synthesis. Say so explicitly
        # in the payload instead of leaving consumers to infer it from a
        # missing key ("panel inspect" showed only 'Synthesis: not run'
        # with no explanation, and text stdout said nothing at all).
        output["synthesis"] = None
        output["synthesis_status"] = "skipped"
        # sp-atvc: attach a metadata bundle whose cost.per_model covers
        # every ensemble model, not just the first in the spec.
        ens_per_model_meta = {mr.model: (mr.usage, mr.cost) for mr in ens_result.model_results}
        output["metadata"] = build_metadata(
            panelist_model=ensemble_models[0],
            panelist_usage=ens_result.total_usage,
            panelist_cost=ens_result.total_cost,
            total_usage=ens_result.total_usage,
            total_cost=ens_result.total_cost,
            persona_count=ens_result.persona_count,
            question_count=ens_result.question_count,
            timer=timer,
            template_vars=template_vars or None,
            panelist_per_model=ens_per_model_meta,
        )
        # hq-0pnq: --save was a silent no-op in the ensemble path because
        # this branch returned before the persistence block at the end of
        # handle_panel_run(). Flatten per-model panelist results into the
        # standard saved-result shape so consumers (`althing inspect`,
        # `althing analyze`) see ensemble runs the same way they see
        # single-model runs.
        #
        # sy-659 (#471): persist BEFORE emit so the saved-result handle +
        # path can be injected into the JSON object agents parse from stdout.
        ensemble_result_id: str | None = None
        if getattr(args, "save", False):
            from althing.mcp.data import _results_dir, save_panel_result

            inst_name: str | None = None
            inst_arg = getattr(args, "instrument", None)
            if inst_arg:
                inst_path = Path(inst_arg)
                inst_name = inst_path.stem if inst_path.exists() else inst_arg

            ensemble_results: list[dict[str, Any]] = []
            for mr in ens_result.model_results:
                for pr in mr.panelist_results:
                    ensemble_results.append(_fmt_panelist(pr, mr.model))

            ensemble_result_id = save_panel_result(
                results=ensemble_results,
                model=ensemble_models[0],
                total_usage=ens_result.total_usage.to_dict(),
                total_cost=ens_result.total_cost.format_usd(),
                persona_count=ens_result.persona_count,
                question_count=ens_result.question_count,
                instrument_name=inst_name,
                models=list(ensemble_models),
                metadata=output["metadata"],
            )
            output["result_id"] = ensemble_result_id
            output["saved_path"] = str(_results_dir() / f"{ensemble_result_id}.json")

        # gh-r4: text-mode stdout used to be the single line "Ensemble
        # complete" — no cost, no error count, no synthesis status, no
        # pointer to the results. Print a terse run summary instead.
        # JSON/NDJSON keep the same message and carry everything in extra.
        if fmt is OutputFormat.TEXT:
            n_models = len(ensemble_models)
            incidents = output.get("ensemble_incidents") or []
            print(
                f"Ensemble complete: {n_models} model(s) x "
                f"{ens_result.persona_count} persona(s) x "
                f"{ens_result.question_count} question(s)"
            )
            print(f"  Models:    {', '.join(ensemble_models)}")
            print(f"  Errors:    {len(incidents)} incident(s)")
            print(f"  Cost:      {ens_result.total_cost.format_usd()} (recorded)")
            if ensemble_result_id is not None:
                print(
                    "  Synthesis: skipped — ensemble runs never synthesize "
                    "automatically. Run: "
                    f"althing panel synthesize {ensemble_result_id}"
                )
                print(f"  Results:   {output['saved_path']}")
            else:
                print(
                    "  Synthesis: skipped — ensemble runs never synthesize "
                    "automatically. Re-run with --save, then run: "
                    "althing panel synthesize <result-id>"
                )
                print(
                    "  Results:   not saved — re-run with --save to persist "
                    "them (full per-model payload available via "
                    "--output-format json)"
                )
        else:
            emit(fmt, message="Ensemble complete", extra=output)

        if ensemble_result_id is not None:
            _print_saved_result_hint(ensemble_result_id)

        return 0

    # ── sp-inline-calibration (sp-a6jc): --calibrate-against validation ──
    # Validate the spec, gate against the redistribution-tier allowlist,
    # and reconcile against --convergence-baseline BEFORE any LLM spend
    # so a malformed or gated request never burns a panelist call.
    calibrate_against_spec = getattr(args, "calibrate_against", None)
    if calibrate_against_spec is not None:
        dataset, sep, question = calibrate_against_spec.partition(":")
        if not sep or not dataset or not question:
            print(
                "Error: --calibrate-against requires DATASET:QUESTION (colon-separated, both non-empty).",
                file=sys.stderr,
            )
            return 2
        if dataset not in _INLINE_CALIBRATION_ALLOWED and os.environ.get("SYNTHBENCH_ALLOW_GATED") != "1":
            allowed = ", ".join(sorted(_INLINE_CALIBRATION_ALLOWED))
            print(
                f"Error: --calibrate-against only supports inline-publishable "
                f"datasets ({allowed}). For gated datasets use post-hoc "
                f"calibration.",
                file=sys.stderr,
            )
            return 1
        existing_baseline = getattr(args, "convergence_baseline", None)
        if existing_baseline and existing_baseline != calibrate_against_spec:
            print(
                "Error: --calibrate-against and --convergence-baseline must reference the same DATASET:QUESTION.",
                file=sys.stderr,
            )
            return 2

    # ── sp-ezz: --submit-to-synthbench parse-time validation ─────────────
    # Hard-fail BEFORE any LLM spend if the flag is misused. Only
    # calibrated runs produce the SynthBench-shaped per-question JSD that
    # is the leaderboard's currency, so a bare submission would never be
    # accepted by the server anyway. Missing-API-key surfaces here too so
    # the user does not discover it after a 20-panelist run completes.
    submit_to_synthbench = bool(getattr(args, "submit_to_synthbench", False))
    if submit_to_synthbench:
        if calibrate_against_spec is None:
            print(
                "Error: --submit-to-synthbench requires --calibrate-against. "
                "Only calibrated runs produce a SynthBench-shaped score; bare "
                "panel runs cannot be submitted to the leaderboard.",
                file=sys.stderr,
            )
            return 2
        from althing.synthbench_submit import ACCOUNT_URL, API_KEY_ENV_NAME

        if not os.environ.get(API_KEY_ENV_NAME):
            print(
                f"Error: --submit-to-synthbench requires {API_KEY_ENV_NAME} in the "
                f"environment. Mint a key at {ACCOUNT_URL}.",
                file=sys.stderr,
            )
            return 2

    # ── sp-yaru: convergence telemetry ───────────────────────────────────
    # Build the tracker when any convergence flag opts in. We always
    # respect --auto-stop / --convergence-baseline even if the user
    # forgot --convergence-check-every, by falling back to the default
    # cadence so those flags never silently no-op. --calibrate-against
    # also force-enables tracking (sp-a6jc); cadence is NOT implicit, so
    # users must still pair it with --convergence-check-every per S-gate
    # OQ3 to avoid surprise cost on small n.
    convergence_tracker: ConvergenceTracker | None = None
    convergence_baseline_payload: dict[str, Any] | None = None
    convergence_baseline_error: str | None = None
    # sp-ttwy: calibration provenance threaded into build_report (T4 consumes)
    extractor_label: str | None = None
    auto_derived: bool = False
    wants_convergence = any(
        [
            getattr(args, "convergence_check_every", None) is not None,
            getattr(args, "auto_stop", False),
            getattr(args, "convergence_log", None) is not None,
            getattr(args, "convergence_baseline", None) is not None,
            getattr(args, "convergence_eps", None) is not None,
            getattr(args, "convergence_min_n", None) is not None,
            getattr(args, "convergence_m", None) is not None,
            calibrate_against_spec is not None,
        ]
    )
    if wants_convergence:
        tracked = identify_tracked_questions(questions)
        if not tracked:
            print(
                "Warning: --convergence-* flags set but the instrument has no "
                "bounded (Likert / yes-no / pick-one / enum) questions; "
                "convergence tracking disabled.",
                file=sys.stderr,
            )
        else:
            # Resolve baseline before kicking off the run so a missing
            # synthbench dep fails fast — we do not want to burn LLM
            # spend on a run whose report we cannot assemble.
            # sp-ttwy: --calibrate-against also sources the baseline here;
            # the conflict check above guarantees both flags (when set
            # together) reference the same DATASET:QUESTION, so this is
            # still a single fetch — acceptance requires "Baseline fetched
            # exactly once even when both convergence + calibrate flags
            # present".
            baseline_spec = getattr(args, "convergence_baseline", None) or calibrate_against_spec
            if baseline_spec:
                try:
                    convergence_baseline_payload = load_synthbench_baseline(baseline_spec)
                except SynthbenchUnavailableError as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    return 1
                except (ValueError, RuntimeError) as exc:
                    convergence_baseline_error = str(exc)
                    print(
                        f"Warning: could not load convergence baseline: {exc}",
                        file=sys.stderr,
                    )
            # sp-ttwy: auto-derive a pick_one extraction schema from the
            # baseline's small-enum distribution, or classify a
            # user-supplied schema for provenance. Happens BEFORE any
            # panelist call (R2 regression risk: a late failure after 20
            # panelists were prompted would burn real LLM spend).
            if calibrate_against_spec is not None:
                if extract_schema is not None:
                    # User supplied --extract-schema: skip derivation,
                    # label for provenance. Likert is detected by the
                    # schema's `rating` property (matches LIKERT_SCHEMA).
                    schema_props = extract_schema.get("properties") or {}
                    if "rating" in schema_props:
                        extractor_label = "likert:manual"
                    else:
                        extractor_label = "pick_one:manual"
                elif convergence_baseline_payload is not None:
                    derived_schema = derive_pick_one_schema_from_baseline(convergence_baseline_payload)
                    if derived_schema is not None:
                        extract_schema = derived_schema
                        extractor_label = "pick_one:auto-derived"
                        auto_derived = True
                        # Stderr log in sp-yaru style (S-gate OQ2: stderr,
                        # NOT stdout, NOT nested in the report — provenance
                        # lives in calibration.auto_derived + .extractor).
                        enum_options = derived_schema["properties"]["choice"]["enum"]
                        print(
                            f"[convergence] auto-derived pick_one schema from "
                            f"{calibrate_against_spec} → {len(enum_options)} options: "
                            f"{enum_options}",
                            file=sys.stderr,
                        )
                    else:
                        # Hard-fail per structure.md §7: distinguish the
                        # two failure modes so the user knows whether to
                        # pick a smaller question or supply a Likert
                        # schema explicitly.
                        distribution = convergence_baseline_payload.get("human_distribution") or {}
                        keys = list(distribution.keys())
                        is_likert = bool(keys) and any(_looks_numeric_key(k) for k in keys)
                        if is_likert:
                            print(
                                "Error: --calibrate-against cannot auto-derive "
                                "for Likert/ranking baselines. Supply "
                                "--extract-schema likert (or custom).",
                                file=sys.stderr,
                            )
                        else:
                            n_options = len(keys)
                            print(
                                f"Error: --calibrate-against needs "
                                f"--extract-schema: baseline has {n_options} "
                                f"options (max 5 for auto-derive). Supply a "
                                f"schema or pick a smaller-support question.",
                                file=sys.stderr,
                            )
                        return 1
            convergence_tracker = ConvergenceTracker(
                tracked,
                check_every=getattr(args, "convergence_check_every", None) or DEFAULT_CHECK_EVERY,
                epsilon=getattr(args, "convergence_eps", None) or DEFAULT_EPSILON,
                min_n=(
                    getattr(args, "convergence_min_n", None)
                    if getattr(args, "convergence_min_n", None) is not None
                    else DEFAULT_MIN_N
                ),
                m_consecutive=getattr(args, "convergence_m", None) or DEFAULT_M_CONSECUTIVE,
                auto_stop=bool(getattr(args, "auto_stop", False)),
                log_path=getattr(args, "convergence_log", None),
            )

    # Run all panelists in parallel via the orchestrator
    blend_result = None  # populated only when --blend is active
    blend_drop_warning: str | None = None  # sy-546: set when a blend member dropped

    # ── sp-hsk3: checkpoint + resume wiring ────────────────────────────
    # We snapshot progress every K completed panelists so a crashed or
    # SIGINT'd run can resume via `althing panel run --resume <id>`.
    # Only wired into the standard (non-ensemble, non-blend) path: those
    # paths have their own multi-run structure and would need their own
    # checkpoint strategy. If the user combined them with --resume or
    # --checkpoint-dir we fail loud earlier rather than silently ignoring
    # the flags.
    checkpoint_root = getattr(args, "checkpoint_dir", None)
    resume_id = getattr(args, "resume", None)
    checkpoint_every = getattr(args, "checkpoint_every", None) or DEFAULT_CHECKPOINT_EVERY
    wants_checkpoint = bool(checkpoint_root) or bool(resume_id)
    if wants_checkpoint and (blend_mode or ensemble_mode or getattr(args, "variants", None)):
        print(
            "Error: --checkpoint-dir and --resume are not supported with "
            "--blend, --models ensemble runs, or --variants; rerun without "
            "those flags.",
            file=sys.stderr,
        )
        return 1

    checkpoint_writer: CheckpointWriter | None = None
    # sy-659 (#471): the checkpoint run_id, when checkpointing is active, is
    # surfaced in JSON output so agents resuming a run have the handle without
    # scraping stderr. Stays None for non-checkpointed runs (the common case).
    run_id: str | None = None
    preloaded_results: list[PanelistResult] = []
    resumed_config_note: str | None = None
    active_personas = personas  # may be filtered below on resume
    if wants_checkpoint:
        root_path = Path(checkpoint_root) if checkpoint_root else default_checkpoint_root()
        run_config_dict = _build_run_config_fingerprint(
            personas=personas,
            questions=questions,
            model=model,
            persona_models=persona_models,
            temperature=temperature,
            top_p=top_p,
            seed=seed,
            response_schema=response_schema,
            extract_schema=extract_schema,
            template_vars=template_vars,
        )
        if resume_id:
            try:
                ckpt = load_checkpoint(resume_id, root_path)
            except CheckpointNotFoundError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            except CheckpointError as exc:
                print(f"Error: checkpoint load failed: {exc}", file=sys.stderr)
                return 1
            allow_drift = bool(getattr(args, "allow_drift", False))
            try:
                ensure_config_matches(ckpt, run_config_dict)
            except CheckpointDriftError as exc:
                if allow_drift:
                    print(
                        f"Warning: {exc}\nWarning: --allow-drift active; "
                        f"this resumed run mixes panelists answered under "
                        f"different config and is statistically inconsistent.",
                        file=sys.stderr,
                    )
                else:
                    print(f"Error: {exc}", file=sys.stderr)
                    print(
                        "Hint: pass --allow-drift to continue anyway (produces a statistically inconsistent run).",
                        file=sys.stderr,
                    )
                    return 1
            # Reconstitute completed panelists as PanelistResult so the
            # downstream output pipeline (cost/metadata/synthesis) sees
            # a single unified list across the original + resumed slices.
            preloaded_results = [_panelist_result_from_dict(r) for r in ckpt.completed]
            skip = {pr.persona_name for pr in preloaded_results}
            active_personas = [p for p in personas if p.get("name", "Anonymous") not in skip]
            preloaded_cost_raw = ckpt.usage.get("provider_reported_cost") if isinstance(ckpt.usage, dict) else None
            cost_note = f", spent ${float(preloaded_cost_raw):.4f}" if preloaded_cost_raw else ""
            resumed_config_note = (
                f"resumed run {resume_id}: {len(preloaded_results)} of "
                f"{len(personas)} panelists already complete, "
                f"{len(active_personas)} remaining{cost_note}"
            )
            print(resumed_config_note, file=sys.stderr)
            run_id = resume_id
            directory = checkpoint_dir_for(run_id, root_path)
        else:
            run_id = new_run_id()
            directory = checkpoint_dir_for(run_id, root_path)
            print(
                f"Checkpointing enabled: run_id={run_id} dir={directory} every={checkpoint_every}",
                file=sys.stderr,
            )

        preloaded_usage_dict = ZERO_USAGE.to_dict()
        for pr in preloaded_results:
            preloaded_usage_dict = _merge_usage_dicts(preloaded_usage_dict, pr.usage.to_dict())

        force_overwrite = bool(getattr(args, "force_overwrite", False))
        try:
            checkpoint_writer = CheckpointWriter(
                run_id=run_id,
                directory=directory,
                config=run_config_dict,
                all_personas=[p.get("name", "Anonymous") for p in personas],
                every=checkpoint_every,
                preloaded_completed=[_panelist_result_to_ckpt_dict(pr, model) for pr in preloaded_results],
                preloaded_usage=preloaded_usage_dict,
                cli_args=_build_resume_cli_args(args),
                resume_existing=bool(resume_id),
                force_overwrite=force_overwrite,
            )
        except CheckpointCollisionError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            print(
                "Hint: pass --force-overwrite to replace the existing checkpoint, "
                "or pass --resume <run-id> to continue from it.",
                file=sys.stderr,
            )
            return 1
        except CheckpointLockError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        checkpoint_writer.install_signal_handlers()

    # sp-56pb: initialized pre-branch so the aggregator block after the
    # blend vs. standard if/else can reference it unconditionally.
    sigint_halted = False
    # hq-hjq8: pre-initialize so the post-branch aggregator code (banners,
    # JSON ``extra`` keys) can reference it on the blend path too. Only the
    # standard ``else`` branch instantiates a real budget; blend bypasses
    # the orchestrator entirely so there is nothing to gate.
    question_budget: QuestionFailureBudget | None = None

    if blend_mode and model_spec:
        # ── Blend mode: run full panel once per model, then blend ────
        from althing.ensemble import blend_distributions, ensemble_run

        ensemble_models = [m for m, _w in model_spec]
        ensemble = ensemble_run(
            personas,
            questions,
            ensemble_models,
            client,
            system_prompt_fn=system_prompt_fn,
            question_prompt_fn=build_question_prompt,
            response_schema=response_schema,
            extract_schema=extract_schema,
            temperature=temperature,
            top_p=top_p,
            seed=seed,
            attachment_bank=instrument.attachments or None,
        )
        # Build weights dict from the model spec
        blend_weights = {m: w for m, w in model_spec}
        blend_result = blend_distributions(ensemble, weights=blend_weights, questions=questions)

        # sy-546: detect blend members that contributed ZERO usable responses
        # (e.g. a slug that 404'd on every call slipped past pre-flight, or a
        # member died mid-run). The blend silently degrades to the survivors;
        # emit a loud, top-level warning stating the new N so the operator
        # isn't fooled into treating it as a full-strength blend.
        dropped_models = _blend_dropped_models(ensemble)
        if dropped_models:
            surviving = [m for m in ensemble_models if m not in dropped_models]
            blend_drop_warning = (
                f"BLEND DEGRADED: {len(dropped_models)} of {len(ensemble_models)} model(s) "
                f"produced no usable responses ({', '.join(dropped_models)}). "
                f"The blend dropped to {len(surviving)} model(s): {', '.join(surviving) or 'none'}. "
                "Distributions and synthesis reflect only the surviving model(s). "
                "Re-run with corrected --models (or --require-all-models to abort instead)."
            )
            logger.warning(blend_drop_warning)
            print(f"\nWarning: {blend_drop_warning}\n", file=sys.stderr)

        # Flatten all panelist results across models for output + synthesis
        panelist_results = [pr for mr in ensemble.model_results for pr in mr.panelist_results]
    else:
        # Show a live progress bar when the user is watching a TTY in text mode.
        _show_progress = fmt is OutputFormat.TEXT and sys.stdout.isatty() and bool(active_personas)
        progress: PanelProgressBar | None = PanelProgressBar(len(active_personas), model) if _show_progress else None

        def _on_complete(pr: PanelistResult) -> None:
            if checkpoint_writer is not None:
                record = _panelist_result_to_ckpt_dict(pr, model)
                checkpoint_writer.record_completed(record, pr.usage.to_dict())
            if progress is not None:
                progress.update(pr.usage)

        # sp-utnk: instantiate the gate against the panelists actually being
        # dispatched in this invocation. On a fresh run this is all personas;
        # on --resume it excludes preloaded panelists so the projection
        # reflects the spend still to come, not the full historical run.
        if max_cost_usd is not None and active_personas:
            cost_gate = CostGate(max_cost_usd=max_cost_usd, total_panelists=len(active_personas))

        # sp-xw2z6o: per-question failure budget. Sized against the panelists
        # being dispatched (same as the cost gate) so the fractional form
        # tracks the actual run, not the resumed historical total.
        if question_budget_value is not None and active_personas:
            question_budget = QuestionFailureBudget(
                budget=question_budget_value,
                total_panelists=len(active_personas),
            )

        try:
            panelist_results, _registry, _sessions = run_panel_parallel(
                client=client,
                personas=active_personas,
                questions=questions,
                model=model,
                system_prompt_fn=system_prompt_fn,
                question_prompt_fn=build_question_prompt,
                response_schema=response_schema,
                extract_schema=extract_schema,
                temperature=temperature,
                top_p=top_p,
                seed=seed,
                persona_models=persona_models,
                max_workers=max_concurrent,
                convergence_tracker=convergence_tracker,
                on_panelist_complete=_on_complete if (checkpoint_writer is not None or progress is not None) else None,
                cost_gate=cost_gate,
                question_budget=question_budget,
                attachment_bank=instrument.attachments or None,
                allow_empty_attachments=getattr(args, "allow_empty_attachments", False),
            )
        except RunAbortedError as abort_exc:
            # sp-56pb: SIGINT path. Surface whatever panelists finished
            # before the signal landed as a valid partial JSON envelope;
            # exit non-zero so automation detects the abort without parsing
            # banner text.
            sigint_halted = abort_exc.reason == "sigint"
            panelist_results = abort_exc.results
            _registry = abort_exc.registry
            _sessions = abort_exc.sessions
            print(
                "panel run interrupted (SIGINT); emitting partial result for "
                f"{len(panelist_results)}/{len(active_personas)} panelist(s)",
                file=sys.stderr,
            )
        finally:
            if progress is not None:
                progress.close()
            if checkpoint_writer is not None:
                try:
                    checkpoint_writer.flush(force=True)
                finally:
                    try:
                        checkpoint_writer.remove_signal_handlers()
                    finally:
                        checkpoint_writer.close()

        # sp-hsk3: merge preloaded (resumed) results with fresh ones, in
        # the original persona order. Preloaded results are guaranteed to
        # be disjoint from `panelist_results` because we filtered their
        # names out of `active_personas`.
        if preloaded_results:
            by_name: dict[str, PanelistResult] = {pr.persona_name: pr for pr in preloaded_results}
            for pr in panelist_results:
                by_name[pr.persona_name] = pr
            panelist_results = [
                by_name[p.get("name", "Anonymous")] for p in personas if p.get("name", "Anonymous") in by_name
            ]

    # Build output results and aggregate panelist usage
    results: list[dict[str, Any]] = []
    panelist_usage = ZERO_USAGE

    for pr in panelist_results:
        # Use per-panelist model for accurate cost tracking
        pr_model = pr.model or model
        pricing, is_estimated = lookup_pricing(pr_model)
        persona_cost = estimate_cost(pr.usage, pricing)
        result_dict: dict[str, Any] = {
            "persona": pr.persona_name,
            "responses": pr.responses,
            "usage": pr.usage.to_dict(),
            "cost": persona_cost.format_usd(),
            "error": pr.error,
        }
        if pr.model:
            result_dict["model"] = pr.model
        results.append(result_dict)
        panelist_usage = panelist_usage + pr.usage

    # sp-atvc: bucket panelist usage/cost by actual model so multi-model
    # runs (persona_models routing, --blend, ensemble) price each token
    # at the rate its provider charged, instead of applying the primary
    # model's rate to every bucket.
    panelist_per_model_usage, panelist_per_model_cost = aggregate_per_model(panelist_results, model)
    multi_model_run = len(panelist_per_model_usage) > 1

    # ── Failure analysis (sp-2hg) ──────────────────────────────────────
    # Count panelist-question pairs and determine how many errored. A pair
    # is considered errored if its response dict is flagged ``error: True``
    # OR if the panelist wrapper itself failed (no responses recorded).
    failure_stats = _analyze_failures(panelist_results, questions)
    strict = getattr(args, "strict", False)
    threshold = getattr(args, "failure_threshold", 0.5)
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = 0.5
    run_invalid = failure_stats["total_pairs"] > 0 and failure_stats["failure_rate"] > threshold
    strict_violation = strict and failure_stats["errored_pairs"] > 0

    # sp-efip: separate total-failure short-circuit. _analyze_failures
    # can report total_pairs==0 when every panelist bailed before
    # recording any response (e.g. runtime init threw) — in that case
    # the failure_rate math falls to 0 and run_invalid stays False.
    # Regardless of the pair-rate, if no panelist produced usable data
    # the run is invalid and the banner must name the failing model(s)
    # and first upstream error.
    total_failure = detect_total_failure(panelist_results)
    if total_failure is not None:
        run_invalid = True

    # sp-bjt4: detect the "polite refusal" failure mode — panelists returned
    # clean responses but uniformly flagged the question as unanswerable
    # because the source material never made it into the prompt. Without
    # this check, ``run_invalid`` stays False and the failure is only
    # narrated in the synthesis ``surprises`` field, so CI gates (which
    # key on ``run_invalid``) miss it silently.
    missing_input_stats = _detect_missing_input_refusals(panelist_results)
    missing_input_invalid = (
        missing_input_stats["considered"] > 0 and missing_input_stats["refusal_rate"] >= _MISSING_INPUT_THRESHOLD
    )
    if missing_input_invalid:
        run_invalid = True

    # sp-utnk: mid-run cost gate tripped. The orchestrator has already
    # cancelled pending futures, so ``panelist_results`` is a valid
    # prefix. Mark the run invalid and carry the gate snapshot so
    # consumers (CI, MCP) can key on ``cost_exceeded`` without parsing
    # banner text. Skip synthesis on a halted partial — synthesizing a
    # deliberately-truncated panel would burn more budget without
    # delivering a trustworthy result.
    cost_gate_halted = cost_gate is not None and cost_gate.should_halt()
    if cost_gate_halted:
        run_invalid = True

    # sp-56pb: SIGINT aborts the run-panel loop. Whatever panelists
    # already landed are preserved as the partial prefix — mark the run
    # invalid so consumers don't mistake the truncated output for a
    # successful run, and skip synthesis for the same reason we skip it
    # on a cost-halt (truncated panel ≠ trustworthy synthesis input).
    if sigint_halted:
        run_invalid = True

    # Compute panelist costs (needed before synthesis for cost estimate).
    # For multi-model runs, sum the per-model costs so the total reflects
    # each provider's real rate; single-model runs keep the legacy path.
    pricing, is_estimated = lookup_pricing(model)
    if multi_model_run:
        panelist_cost_est = CostEstimate()
        for _m, _c in panelist_per_model_cost.items():
            panelist_cost_est = panelist_cost_est + _c
    else:
        panelist_cost_est = estimate_cost(panelist_usage, pricing)

    # Synthesis step (unless --no-synthesis)
    skip_synthesis = getattr(args, "no_synthesis", False)
    if run_invalid or strict_violation:
        skip_synthesis = True  # don't synthesize garbage
    if sigint_halted:
        # sp-56pb: SIGINT halts the panel loop; synthesizing over a
        # user-interrupted partial wastes spend and produces a result
        # the operator never asked for.
        skip_synthesis = True
    synthesis_result = None
    synthesis_error_payload: dict[str, Any] | None = None

    if not skip_synthesis:
        synthesis_model = getattr(args, "synthesis_model", None)
        custom_prompt = getattr(args, "synthesis_prompt", None)
        synthesis_temperature = getattr(args, "synthesis_temperature", None)
        requested_strategy = getattr(args, "synthesis_strategy", "auto") or "auto"
        # sp-rcvr: the recovery ladder may only fall back to map-reduce when
        # the operator did NOT explicitly pin the single strategy (a custom
        # synthesis prompt also pins single — map/reduce prompts are not
        # overridable). Capture this before requested_strategy is rewritten.
        recovery_map_reduce_allowed = requested_strategy != STRATEGY_SINGLE and custom_prompt is None
        # sp-kkzz: --synthesis-prompt only applies to the single-pass call.
        # Map/reduce prompts are not overridable (yet) — warn and force single.
        if custom_prompt is not None and requested_strategy == STRATEGY_MAP_REDUCE:
            print(
                "warning: --synthesis-prompt is incompatible with "
                "--synthesis-strategy=map-reduce; forcing strategy=single. "
                "Future work: --synthesis-map-prompt / --synthesis-reduce-prompt.",
                file=sys.stderr,
            )
            requested_strategy = STRATEGY_SINGLE
        elif custom_prompt is not None and requested_strategy == "auto":
            requested_strategy = STRATEGY_SINGLE
        # synthesis_temperature overrides panelist temperature for synthesis
        effective_synth_temp = synthesis_temperature if synthesis_temperature is not None else temperature
        effective_synth_model = synthesis_model or model
        # sp-exu6: resolve the strategy FIRST. The single-pass overflow
        # pre-flight only applies when the resolved strategy is ``single``
        # — otherwise ``--synthesis-strategy=auto`` would reject large
        # panels instead of routing them through map-reduce (sp-avmm ×
        # sp-9rzu interaction). Map-reduce has its own per-map overflow
        # guard inside ``synthesize_panel_mapreduce``.
        resolved_strategy = select_strategy(
            requested_strategy,
            effective_synth_model,
            panelist_results,
            questions,
            prompt=custom_prompt,
        )
        overflow = None
        if resolved_strategy == STRATEGY_SINGLE:
            overflow = detect_synthesis_context_overflow(
                panelist_results,
                questions,
                synthesis_model=effective_synth_model,
                custom_prompt=custom_prompt,
            )
        if overflow is not None:
            actionable = format_synthesis_overflow_message(overflow)
            print(
                f"Error: synthesis pre-flight rejected: {actionable}",
                file=sys.stderr,
            )
            synthesis_error_payload = build_synthesis_error_payload(
                None,
                error_type="synthesis_context_overflow",
                message=actionable,
                suggested_fix=(
                    "Rerun with --synthesis-model gemini-2.5-flash-lite (1M context) "
                    "or gemini-2.5-pro (1M context), --synthesis-strategy=map-reduce, "
                    "or reduce panel size."
                ),
                diagnostic=overflow,
            )
        else:
            try:
                if resolved_strategy == STRATEGY_MAP_REDUCE:
                    synthesis_result = synthesize_panel_mapreduce(
                        client,
                        panelist_results,
                        questions,
                        model=synthesis_model,
                        panelist_model=model,
                        panelist_cost=panelist_cost_est,
                        temperature=effective_synth_temp,
                        top_p=top_p,
                        seed=seed,
                        personas=personas,
                        auto_escalate=getattr(args, "synthesis_auto_escalate", False),
                    )
                else:
                    synthesis_result = synthesize_panel(
                        client,
                        panelist_results,
                        questions,
                        model=synthesis_model,
                        panelist_model=model,
                        custom_prompt=custom_prompt,
                        panelist_cost=panelist_cost_est,
                        temperature=effective_synth_temp,
                        top_p=top_p,
                        seed=seed,
                    )
            except MapChunkOverflowError as exc:
                # sp-exu6: per-map chunk overflow — distinct from the
                # single-pass pre-flight (sp-avmm) so consumers can tell
                # "this one question is too big" apart from "the whole
                # panel is too big".
                logger.error("map-reduce per-chunk overflow: %s", exc)
                print(
                    f"Error: synthesis pre-flight rejected: {exc}",
                    file=sys.stderr,
                )
                synthesis_error_payload = build_synthesis_error_payload(
                    None,
                    error_type="synthesis_map_chunk_overflow",
                    message=str(exc),
                    suggested_fix=(
                        "A single question's responses exceed the synthesis "
                        "model's context window. Rerun with --synthesis-model "
                        "gemini-2.5-flash-lite (1M context) or gemini-2.5-pro "
                        "(1M context), or reduce panel size."
                    ),
                    diagnostic=exc.diagnostic,
                )
            except Exception as exc:
                # sp-rcvr: synthesis recovery ladder — classify the failure,
                # retry transients once, fall back to map-reduce on (suspected)
                # context overflow, and reroute away from a failing OpenRouter
                # downstream, before failing loud (sp-avmm) with a guided
                # `panel synthesize` recovery command.
                from althing._runners import _sanitize_api_error
                from althing.synthesis_recovery import (
                    RecoveryContext,
                    SynthesisRecoveryError,
                    recover_synthesis_failure,
                )

                recovery_exc: BaseException = exc
                if resolved_strategy == STRATEGY_SINGLE:
                    ctx = RecoveryContext(
                        client=client,
                        panelist_results=panelist_results,
                        questions=questions,
                        synthesis_model=synthesis_model,
                        panelist_model=model,
                        custom_prompt=custom_prompt,
                        temperature=effective_synth_temp,
                        top_p=top_p,
                        seed=seed,
                        personas=personas,
                        allow_map_reduce=recovery_map_reduce_allowed,
                        auto_escalate=getattr(args, "synthesis_auto_escalate", False),
                    )
                    try:
                        synthesis_result = recover_synthesis_failure(exc, ctx)
                    except SynthesisRecoveryError as rexc:
                        recovery_exc = rexc

                if synthesis_result is None:
                    sanitized = _sanitize_api_error(recovery_exc)
                    logger.error("synthesis failed: %s", sanitized)
                    print(f"Error: synthesis call failed: {sanitized}", file=sys.stderr)
                    if isinstance(recovery_exc, SynthesisRecoveryError):
                        suggested = (
                            f"Recover without re-running panelists: `{recovery_exc.suggested_command}` "
                            "(requires --save; large-context in-family model). "
                            "Also check provider credentials and model availability."
                        )
                        diagnostic: dict[str, Any] | None = {"recovery_rungs": recovery_exc.rungs}
                    else:
                        suggested = (
                            "Check provider credentials and model availability;"
                            " if context-related, rerun with a larger-context synthesis model"
                            " (e.g. gemini-2.5-flash-lite or gemini-2.5-pro)"
                            " or --synthesis-strategy=map-reduce."
                        )
                        diagnostic = None
                    synthesis_error_payload = build_synthesis_error_payload(
                        recovery_exc,
                        error_type="synthesis_api_error",
                        message=f"Synthesis call failed: {sanitized}",
                        suggested_fix=suggested,
                        diagnostic=diagnostic,
                    )

    # sy-549: a synthesis that returned a *fallback* result (judge exhausted
    # its schema-adherence retries, or returned a partial schema) is NOT a
    # healthy synthesis. Previously such a result was persisted as if it
    # succeeded, hiding the failure behind mostly-empty fields. Surface a
    # loud warning + a machine-detectable error payload.
    if synthesis_result is not None and getattr(synthesis_result, "is_fallback", False):
        synth_err = synthesis_result.error or "synthesis judge did not return a valid structured result"
        logger.warning("synthesis produced a fallback result: %s", synth_err)
        print(f"Error: synthesis incomplete: {synth_err}", file=sys.stderr)
        synthesis_error_payload = build_synthesis_error_payload(
            None,
            error_type="synthesis_fallback",
            message=f"Synthesis incomplete: {synth_err}",
            suggested_fix=(
                "The synthesis judge returned an empty/partial result after all retries. "
                "Rerun with a higher-quality --synthesis-model, or re-synthesize the saved "
                "result with `althing panel synthesize <id> --synthesis-model ...`."
            ),
        )

    if synthesis_result:
        total_usage = panelist_usage + synthesis_result.usage
        total_cost_est = panelist_cost_est + synthesis_result.cost
    else:
        total_usage = panelist_usage
        total_cost_est = panelist_cost_est

    timer.stop()
    synthesis_dict = synthesis_result.to_dict() if synthesis_result else None

    panelist_per_model_meta: dict[str, tuple[TokenUsage, CostEstimate]] | None = None
    if multi_model_run:
        panelist_per_model_meta = {
            _m: (panelist_per_model_usage[_m], panelist_per_model_cost[_m]) for _m in panelist_per_model_usage
        }

    metadata = build_metadata(
        panelist_model=model,
        synthesis_model=getattr(args, "synthesis_model", None),
        panelist_usage=panelist_usage,
        panelist_cost=panelist_cost_est,
        synthesis_usage=synthesis_result.usage if synthesis_result else None,
        synthesis_cost=synthesis_result.cost if synthesis_result else None,
        total_usage=total_usage,
        total_cost=total_cost_est,
        persona_count=len(personas),
        question_count=len(questions),
        timer=timer,
        template_vars=template_vars or None,
        panelist_per_model=panelist_per_model_meta,
    )

    # sp-kkzz: when map-reduce ran, expose the per-call cost breakdown
    # under metadata.cost.synthesis so downstream consumers can attribute
    # spend to the map vs reduce phases without re-deriving it.
    if synthesis_result is not None and synthesis_result.strategy == STRATEGY_MAP_REDUCE:
        cost_section = metadata.setdefault("cost", {})
        cost_section["synthesis"] = {
            "strategy": STRATEGY_MAP_REDUCE,
            "map_calls": synthesis_result.map_cost_breakdown or [],
            "reduce_call": synthesis_result.reduce_cost_breakdown or {},
            "total_cost_usd": round(synthesis_result.cost.total_cost, 6),
        }

    # sp-prof: inject profile info into metadata for synthbench
    if profile is not None:
        metadata["profile"] = profile.name
        metadata["profile_hash"] = profile.config_hash()

    # Output results
    banner = _build_invalid_banner(
        failure_stats,
        threshold,
        strict=strict,
        strict_violation=strict_violation,
        missing_input_stats=missing_input_stats,
        missing_input_invalid=missing_input_invalid,
        total_failure=total_failure,
    )
    # sp-utnk: if the cost gate tripped, the failure-stats banner may not
    # render (no errored pairs, no refusals). Synthesize a dedicated line
    # so TEXT-mode operators and stderr-grepping CI both see the halt.
    if cost_gate_halted and cost_gate is not None:
        snap = cost_gate.snapshot()
        cost_banner = (
            "⚠️  PANEL RUN HALTED: cost ceiling exceeded — "
            f"projected ${snap['projected_total_usd']:.4f} > "
            f"max ${snap['max_cost_usd']:.4f} after "
            f"{snap['completed']}/{snap['total_panelists']} panelists "
            f"(running ${snap['running_cost_usd']:.4f})"
        )
        banner = f"{banner}\n{cost_banner}" if banner else cost_banner

    # sp-xw2z6o: surface mid-run question disables. The run still completed
    # (these are not panel-invalidating events on their own), but the
    # operator must see which questions were short-circuited so they can
    # diagnose the prompt or schema issue.
    if question_budget is not None and question_budget.disabled_questions():
        budget_lines = ["⚠️  Disabled mid-run by --question-failure-budget:"]
        for entry in question_budget.disabled_details():
            qi = entry["question_index"]
            qtext = entry.get("question_text") or f"<question #{qi}>"
            qtext_short = qtext if len(qtext) <= 80 else qtext[:77] + "..."
            failures = entry["failures_at_disable"]
            total = entry["total_panelists"]
            budget_lines.append(
                f"  Q{qi + 1}: {qtext_short!r} — {failures}/{total} panelists "
                f"failed before threshold ({entry['threshold_count']}) reached"
            )
        budget_banner = "\n".join(budget_lines)
        banner = f"{banner}\n{budget_banner}" if banner else budget_banner

    # ── sp-ezz: pre-build the convergence report + snapshot raw model
    # distributions BEFORE the text/json branch split, so a single block
    # at the bottom of the function can submit to SynthBench regardless
    # of output mode. Snapshotting before close() is required because
    # the tracker's internal state is the source of truth for
    # ``model_distribution`` in the SynthBench payload.
    convergence_report: dict[str, Any] | None = None
    convergence_model_distributions: dict[str, dict[str, float]] = {}
    if convergence_tracker is not None:
        orch_meta: dict[str, Any] = {
            "primary_model": model,
            "mixed_models": bool(persona_models),
        }
        if persona_models:
            orch_meta["distinct_model_count"] = len({m for m in persona_models.values()})
        convergence_report = convergence_tracker.build_report(
            baseline=convergence_baseline_payload,
            calibration_spec=calibrate_against_spec,
            extractor_label=extractor_label,
            auto_derived=auto_derived,
            orchestrator=orch_meta,
        )
        convergence_model_distributions = convergence_tracker.cumulative_distributions()
        if convergence_baseline_error:
            convergence_report["human_baseline_error"] = convergence_baseline_error
        # sp-ezz P0 fix: a --calibrate-against baseline describes ONE survey
        # question, so build_report binds calibration to a single tracked
        # question. Surface that decision loudly — excluded questions carry
        # no calibration JSD and are never uploaded to SynthBench.
        if calibrate_against_spec is not None and "calibration_question" in convergence_report:
            bound_question = convergence_report.get("calibration_question")
            excluded_questions = convergence_report.get("calibration_excluded_questions") or []
            if bound_question is None:
                print(
                    f"Warning: --calibrate-against {calibrate_against_spec} did not match any "
                    f"tracked question; no calibration JSD was computed and nothing will be "
                    f"submitted to SynthBench. Tracked questions: "
                    f"{', '.join(repr(k) for k in convergence_report.get('tracked_questions') or [])}",
                    file=sys.stderr,
                )
            elif excluded_questions:
                print(
                    f"Warning: --calibrate-against {calibrate_against_spec} is bound to question "
                    f"{bound_question!r}; {len(excluded_questions)} other tracked question(s) were "
                    f"excluded from calibration and any SynthBench submission: "
                    f"{', '.join(repr(k) for k in excluded_questions)}",
                    file=sys.stderr,
                )
        convergence_tracker.close()

    # ── sy-659 (#471): persist --save BEFORE the text/json branch split ──
    # The saved-result handle (result_id) and its absolute path must land in
    # the JSON object agents parse from stdout, so the write happens here —
    # ahead of emit() — rather than at the end of the function. The
    # human-facing "Result saved" hint still prints after the branch (below)
    # to preserve stderr-after-stdout ordering for TEXT-mode operators.
    saved_result_id: str | None = None
    saved_result_path: str | None = None
    if getattr(args, "save", False):
        from althing.mcp.data import _results_dir, save_panel_result

        # Determine instrument name (pack name or filename stem)
        inst_name = None
        inst_arg = getattr(args, "instrument", None)
        if inst_arg:
            inst_path = Path(inst_arg)
            if inst_path.exists():
                inst_name = inst_path.stem
            else:
                inst_name = inst_arg  # pack name

        all_models: list[str] | None = None
        if persona_models:
            all_models = sorted(set(persona_models.values()))
        elif model_spec:
            all_models = [m for m, _w in model_spec]

        saved_result_id = save_panel_result(
            results=results,
            model=model,
            total_usage=total_usage.to_dict(),
            total_cost=total_cost_est.format_usd(),
            persona_count=len(personas),
            question_count=len(questions),
            instrument_name=inst_name,
            # sy-547: persist the authored question defs (text +
            # response_schema) so poll-summary / analyze recognize enum/scale
            # questions instead of falling back to kind=text. Without this the
            # saved result carried only the question text echoed on each
            # response, dropping the schema kind.
            questions=_question_meta_for_save(questions),
            models=all_models,
            synthesis=synthesis_dict,
            metadata=metadata,
        )
        saved_result_path = str(_results_dir() / f"{saved_result_id}.json")

    if fmt is OutputFormat.TEXT:
        if banner:
            print(banner, file=sys.stderr)
        path_line = _format_path(_degenerate_path(instrument))
        if path_line:
            print(f"path: {path_line}")
        for r in results:
            print(f"\n{'=' * 60}")
            print(f"Persona: {r['persona']}")
            print(f"{'=' * 60}")
            if r.get("error"):
                print(f"  ERROR: {r['error']}")
            for resp in r["responses"]:
                if resp.get("skipped_by_condition"):
                    print(f"  [follow-up] Q: {resp['question']}")
                    print("  [follow-up] A: (skipped — condition not met)")
                    print()
                    continue
                if resp.get("skipped_by_budget"):
                    print(f"  Q: {resp['question']}")
                    print("  A: (skipped — disabled mid-run by --question-failure-budget)")
                    print()
                    continue
                prefix = "  [follow-up] " if resp.get("follow_up") else "  "
                print(f"{prefix}Q: {resp['question']}")
                print(f"{prefix}A: {resp['response']}")
                print()
            print(f"  Cost: {r['cost']}")

        # Synthesis output
        if synthesis_dict:
            print(f"\n{'=' * 60}")
            print("SYNTHESIS")
            print(f"{'=' * 60}")
            print(f"\n  Summary: {synthesis_dict['summary']}")
            if synthesis_dict.get("themes"):
                print("\n  Themes:")
                for t in synthesis_dict["themes"]:
                    print(f"    - {t}")
            if synthesis_dict.get("agreements"):
                print("\n  Agreements:")
                for a in synthesis_dict["agreements"]:
                    print(f"    - {a}")
            if synthesis_dict.get("disagreements"):
                print("\n  Disagreements:")
                for d in synthesis_dict["disagreements"]:
                    print(f"    - {d}")
            if synthesis_dict.get("surprises"):
                print("\n  Surprises:")
                for s in synthesis_dict["surprises"]:
                    print(f"    - {s}")
            print(f"\n  Recommendation: {synthesis_dict['recommendation']}")
            print(f"  Synthesis cost: {synthesis_dict['cost']}")

        # Blend output
        if blend_result:
            print(f"\n{'=' * 60}")
            print("BLENDED DISTRIBUTIONS")
            print(f"{'=' * 60}")
            print(f"  Models: {', '.join(blend_result.models)}")
            print(f"  Weights: {', '.join(f'{m}={w:.2f}' for m, w in blend_result.weights.items())}")
            for bq in blend_result.questions:
                print(f"\n  Q{bq.question_index + 1}: {bq.question_text}")
                print(f"  Responses: {bq.response_count}")
                if bq.distribution:
                    sorted_dist = sorted(bq.distribution.items(), key=lambda x: x[1], reverse=True)
                    for option, prob in sorted_dist:
                        print(f"    {option}: {prob:.1%}")

        # Cost summaries
        print(f"\n{'=' * 60}")
        print(
            format_summary("Panelist cost", panelist_usage, panelist_cost_est, model=model, is_estimated=is_estimated)
        )
        if synthesis_result:
            _synth_pricing, synth_est = lookup_pricing(synthesis_result.model)
            print(
                format_summary(
                    "Synthesis cost",
                    synthesis_result.usage,
                    synthesis_result.cost,
                    model=synthesis_result.model,
                    is_estimated=synth_est,
                )
            )
        print(format_summary("Total", total_usage, total_cost_est, model=model, is_estimated=is_estimated))
        if total_cost_est.total_cost == 0 and failure_stats["errored_pairs"] > 0:
            print(
                "  \u26a0\ufe0f  no-cost = no-data (every request errored; "
                "cost line reflects that no tokens were billed)",
                file=sys.stderr,
            )
        skipped_fu = failure_stats.get("skipped_follow_ups", 0)
        if skipped_fu:
            print(f"  ({skipped_fu} follow-up(s) skipped by condition)")
        if banner:
            # Repeat the banner at the bottom so a scrolled terminal still
            # surfaces it — this is the critical fix for sp-2hg.
            print(banner, file=sys.stderr)
        # sp-yaru: even in TEXT mode, render a compact convergence summary
        # so operators watching stdout can see "converged at n=473" without
        # re-running with --output-format json.
        if convergence_report is not None:
            report = convergence_report
            print("\n" + "=" * 60)
            print("CONVERGENCE")
            print("=" * 60)
            overall = report.get("overall_converged_at")
            if overall is None:
                print(f"  overall: not yet converged (final n={report['final_n']})")
            else:
                print(f"  overall converged_at: n={overall}")
            if report.get("auto_stopped"):
                print(f"  auto-stopped at n={report['final_n']}")
            for qkey, qdata in report.get("per_question", {}).items():
                ca = qdata.get("converged_at")
                ca_str = f"n={ca}" if ca else "pending"
                print(f"  {qkey}: converged_at={ca_str}, support={qdata.get('support_size', 0)}")
                skew = qdata.get("skew_vs_uniform")
                if skew:
                    print(f"    vs uniform: Cramer's V={skew.get('cramers_v')}, p={skew.get('p_value')}")
                    interp = skew.get("lead_interpretation") or ""
                    if interp:
                        print(f"      {interp}")
            if convergence_baseline_payload:
                hb_n = convergence_baseline_payload.get("converged_at")
                if hb_n is not None:
                    print(f"  human baseline: converged_at=n={hb_n}")

        # gh-r4: terse end-of-run summary so a default (text) run states
        # panel size, error count, recorded cost, synthesis status, and
        # where the results live — previously synthesis status and result
        # location were only discoverable via --save/--output-format json.
        if synthesis_dict:
            synthesis_status = f"completed (cost: {synthesis_dict['cost']})"
        elif getattr(args, "no_synthesis", False):
            synthesis_status = "skipped (--no-synthesis)"
        elif skip_synthesis:
            synthesis_status = "skipped (run halted or invalid)"
        else:
            synthesis_status = "FAILED — see stderr above; recover with: althing panel synthesize <result-id>"
        print(f"\n{'=' * 60}")
        print("RUN SUMMARY")
        print(f"{'=' * 60}")
        print(
            f"  Personas:  {len(personas)}    Questions: {len(questions)}    Errors: {failure_stats['errored_pairs']}"
        )
        print(f"  Cost:      {total_cost_est.format_usd()} (recorded)")
        print(f"  Synthesis: {synthesis_status}")
        if saved_result_path is not None:
            print(f"  Results:   {saved_result_path}")
        else:
            print("  Results:   not saved — re-run with --save to persist (then: althing report <result-id>)")
    else:
        # sp-efip: mirror the TEXT-mode behaviour and print the fatal
        # banner to stderr even when JSON/NDJSON is the primary output.
        # Operators grep stderr for "PANEL RUN INVALID" in CI and the
        # banner is where the failing model + upstream error surface.
        if banner:
            print(banner, file=sys.stderr)

        # sp-0h9x: always populate per_model_results + cost_breakdown, even
        # for single-model panels. mayor's ensemble audits use persona_models
        # (mixed-model mode) rather than the ``models=[...]`` ensemble path,
        # so these fields were silently None in 0.9.5 despite sp-gl9 claiming
        # to ship them. A single-entry rollup still eliminates the
        # None-vs-dict branch for consumers.
        from althing.ensemble import build_mixed_model_rollup

        def _cli_panelist_formatter(pr: PanelistResult, panel_model: str) -> dict[str, Any]:
            pr_pricing, _ = lookup_pricing(pr.model or panel_model)
            pr_cost = estimate_cost(pr.usage, pr_pricing)
            out: dict[str, Any] = {
                "persona": pr.persona_name,
                "responses": pr.responses,
                "usage": pr.usage.to_dict(),
                "cost": pr_cost.format_usd(),
                "error": pr.error,
            }
            if pr.model:
                out["model"] = pr.model
            return out

        per_model_results, cost_breakdown = build_mixed_model_rollup(
            panelist_results,
            default_model=model,
            panelist_formatter=_cli_panelist_formatter,
        )

        # sp-nn8k: surface DEFAULT_PRICING fallbacks loudly. Contributing
        # models are the per-panelist buckets we priced above plus the
        # synthesis model when present.
        synth_model_for_warning = synthesis_result.model if synthesis_result else None
        cost_fallback_warnings = build_cost_fallback_warnings(
            [*panelist_per_model_usage.keys(), synth_model_for_warning]
        )

        extra: dict[str, Any] = _build_rounds_shape(
            instrument=instrument,
            results=results,
            synthesis_dict=synthesis_dict,
            panelist_cost=panelist_cost_est,
            panelist_usage=panelist_usage,
            total_usage=total_usage,
            total_cost=total_cost_est,
            model=model,
            persona_count=len(personas),
            question_count=len(questions),
            metadata=metadata,
            per_model_results=per_model_results,
            cost_breakdown=cost_breakdown,
            cost_fallback_warnings=cost_fallback_warnings,
        )
        extra["parameters"] = _build_params_metadata(args, temperature, top_p)
        # Surface failure stats + run validity in structured output so
        # downstream consumers (MCP, CI) can gate on it without parsing text.
        extra["failure_stats"] = failure_stats
        extra["missing_input_stats"] = missing_input_stats
        extra["run_invalid"] = bool(run_invalid or strict_violation)
        # sp-utnk: surface cost-gate outcome for CI/MCP consumers.
        if cost_gate_halted and cost_gate is not None:
            extra["cost_exceeded"] = True
            extra["halted_at_panelist"] = cost_gate.completed
            extra["cost_gate"] = cost_gate.snapshot()
            extra["abort_reason"] = "cost_exceeded"
        # sp-xw2z6o: surface per-question disables for CI/MCP consumers.
        # Always include the snapshot when the budget was active (even with
        # no disables) so dashboards can distinguish "feature off" from
        # "feature on, no disables" without sniffing CLI args.
        if question_budget is not None:
            extra["question_failure_budget"] = question_budget.snapshot()
            disabled_indices = question_budget.disabled_questions()
            if disabled_indices:
                extra["disabled_questions"] = [
                    {
                        "question_index": entry["question_index"],
                        "question_text": entry.get("question_text"),
                        "failures_at_disable": entry["failures_at_disable"],
                        "threshold_count": entry["threshold_count"],
                        "threshold_fraction": entry.get("threshold_fraction"),
                        "total_panelists": entry["total_panelists"],
                    }
                    for entry in question_budget.disabled_details()
                ]
        if total_failure is not None:
            # sp-efip: carry the structured total-failure diagnostic so
            # CI/MCP consumers can detect the "every panelist failed"
            # case without parsing banner text.
            extra["total_failure"] = total_failure
            # sp-56pb: specific abort_reason so downstream tooling can
            # distinguish total-failure from other invalid runs without
            # parsing diagnostic text. When every sampled error points at
            # the LLM client's rate-limit budget running out, classify
            # the abort as rate-limit-exhausted so operators know to
            # raise --rate-limit-rps or --max-retries instead of chasing
            # a model issue. Cost-exceeded takes precedence (it got in
            # first above) because a halted prefix is a more specific
            # explanation than "every panelist failed".
            if "abort_reason" not in extra:
                extra["abort_reason"] = _classify_total_failure_abort_reason(total_failure)
        # sp-56pb: SIGINT is a first-class abort path. Surface a
        # top-level ``abort_reason`` so MCP/CI consumers can key on it
        # without scraping stderr. If a prior clause (cost gate, total
        # failure) already claimed abort_reason we leave that value
        # alone — those are the *cause* of the halt; SIGINT arrived on
        # top of it.
        if sigint_halted and "abort_reason" not in extra:
            extra["abort_reason"] = "sigint"
            extra["halted_at_panelist"] = len(panelist_results)
        if synthesis_error_payload is not None:
            # sp-avmm: top-level synthesis_error so JSON/NDJSON consumers
            # see the failure without walking into the synthesis dict.
            # A synthesis-stage failure does NOT invalidate the run — the
            # panelist responses above are complete and usable. Degrade to
            # synthesis: null/fallback + a warning; recovery is a cheap
            # re-synthesize of the saved result, not a full re-run.
            extra["synthesis_error"] = synthesis_error_payload
            warnings_list = extra.setdefault("warnings", [])
            if isinstance(warnings_list, list):
                warnings_list.append(
                    "synthesis_failed: "
                    f"{synthesis_error_payload.get('message', 'synthesis failed')}"
                    " — panelist responses are complete; recover with"
                    " `althing panel synthesize <result-id>`"
                )
        if missing_input_invalid:
            # sp-bjt4: surface as a top-level warning so MCP / CI consumers
            # see the condition even if they don't special-case
            # missing_input_stats.
            warnings_list = extra.setdefault("warnings", [])
            if isinstance(warnings_list, list):
                warnings_list.append(
                    "missing_input_refusals: "
                    f"{missing_input_stats['refusing']}/{missing_input_stats['considered']}"
                    f" panelists reported missing or unavailable input"
                    f" ({missing_input_stats['refusal_rate']:.0%})"
                )
        if assignment_warnings:
            warnings_list = extra.setdefault("warnings", [])
            if isinstance(warnings_list, list):
                warnings_list.extend(assignment_warnings)
        # sy-546: carry the blend-degraded warning into the JSON envelope so
        # CI / MCP consumers detect the dropped member(s) without scraping
        # stderr, and expose the count explicitly.
        if blend_drop_warning is not None:
            warnings_list = extra.setdefault("warnings", [])
            if isinstance(warnings_list, list):
                warnings_list.append(blend_drop_warning)
            extra["blend_degraded"] = True
        # sp-g270: surface --personas-merge name-collision drops so JSON
        # consumers can assert panel size matches expectations. Always
        # present (as []) when --personas-merge was used so downstream
        # checks can rely on the key, but omitted when no merge happened
        # to avoid noise in the common single-pack case.
        if merge_used:
            extra["personas_merge_warnings"] = personas_merge_warnings
        # sp-zdul: surface the deterministic persona→model assignment so
        # JSON consumers (dashboards, analyze pipelines) can record which
        # model answered which persona without re-deriving from the spec.
        if persona_models:
            extra["model_assignment"] = dict(persona_models)
        # sp-yaru: surface convergence telemetry for large runs. Report
        # is built once above the text/json branch split (sp-ezz) so the
        # SynthBench submission block can reuse it.
        if convergence_report is not None:
            extra["convergence"] = convergence_report
            dw = convergence_report.get("diversity_warnings") or []
            if dw:
                warnings_list = extra.setdefault("warnings", [])
                warnings_list.extend(dw)
        # Include blend distributions when --blend is active
        if blend_result:
            extra["blend"] = {
                "models": blend_result.models,
                "weights": blend_result.weights,
                "questions": [
                    {
                        "index": bq.question_index,
                        "question": bq.question_text,
                        "distribution": bq.distribution,
                        "per_model": bq.per_model,
                        "response_count": bq.response_count,
                    }
                    for bq in blend_result.questions
                ],
            }
        if run_invalid or strict_violation:
            extra["message"] = banner.replace("\n", " ").strip() if banner else "PANEL RUN INVALID"
        # sy-659 (#471): surface the saved-result handle + path so agents
        # running with --save --output-format json get a stable follow-up
        # handle from stdout instead of scraping the stderr "Result saved"
        # line. run_id is the checkpoint identity (present only when
        # checkpointing was active).
        if saved_result_id is not None:
            extra["result_id"] = saved_result_id
            extra["saved_path"] = saved_result_path
        if run_id is not None:
            extra["run_id"] = run_id
        emit(fmt, message=extra.get("message", "Panel complete"), extra=extra)

    # ── sp-ezz: opt-in SynthBench submission ─────────────────────────
    # Runs only when --submit-to-synthbench is set (parse-time validation
    # already guarantees --calibrate-against and SYNTHBENCH_API_KEY are
    # present). Submission failures are warned-but-non-fatal so a slow or
    # rejecting SynthBench cannot turn a successful panel run into a
    # failed CLI exit. Skipped entirely on invalid runs (the submitter
    # also re-checks ``run_invalid`` in ``is_submittable``).
    if submit_to_synthbench and not (run_invalid or strict_violation):
        from althing.synthbench_submit import submit_panel_result

        # Build a minimal panel_extra view; the submitter only consults
        # ``convergence`` and ``run_invalid``, so we do not need the full
        # JSON-mode ``extra`` dict (which is not built in TEXT mode).
        submit_extra: dict[str, Any] = {
            "convergence": convergence_report,
            "run_invalid": False,
        }
        instrument_name_for_submit: str | None = None
        instrument_arg = getattr(args, "instrument", None)
        if instrument_arg:
            inst_path = Path(instrument_arg)
            instrument_name_for_submit = inst_path.stem if inst_path.exists() else instrument_arg
        persona_pack_name = getattr(args, "personas", None)
        if persona_pack_name:
            personas_path = Path(persona_pack_name)
            if personas_path.exists():
                persona_pack_name = personas_path.stem
        sb_result = submit_panel_result(
            panel_extra=submit_extra,
            calibration_spec=calibrate_against_spec,
            baseline_payload=convergence_baseline_payload,
            model_distributions=convergence_model_distributions,
            panelist_model=model,
            instrument_name=instrument_name_for_submit,
            persona_pack_name=persona_pack_name,
            skip_consent=bool(getattr(args, "yes", False)),
        )
        if sb_result.accepted:
            link = sb_result.leaderboard_url or "(no leaderboard URL returned)"
            print(f"Submitted to SynthBench: {link}", file=sys.stderr)
        else:
            print(
                f"Warning: SynthBench submission not accepted ({sb_result.status}): {sb_result.error}",
                file=sys.stderr,
            )

    # ── sp-7vp: auto-save results with --save ─────────────────────────
    # sy-659 (#471): the write itself now happens before the text/json branch
    # split (so JSON output can carry result_id + saved_path); here we only
    # print the human-facing follow-up hint to stderr, keeping it after the
    # stdout body for readable terminal ordering.
    if saved_result_id is not None:
        _print_saved_result_hint(saved_result_id)

    # sp-2hg: exit non-zero when the run is invalid so automation (CI,
    # refinery, wrapper scripts) can detect silent-failure scenarios.
    if strict_violation:
        return 3
    if run_invalid:
        return 2
    return 0


def _multi_round_flag_errors(
    args: argparse.Namespace,
    instrument: Instrument,
    *,
    ensemble_mode: bool,
    blend_mode: bool,
) -> list[str]:
    """Collect loud-refusal messages for panel-run flags the multi-round engine does not support.

    P1-2 support matrix (also documented in README "Adaptive Research"):
    the multi-round router engine (``run_multi_round_panel``) supports
    model selection (``--model``, weighted ``--models``, per-persona YAML
    overrides, ``--best-model-for``), generation params
    (``--temperature`` / ``--top-p`` / ``--seed``), schemas (``--schema``
    / ``--extract-schema``), synthesis knobs (``--synthesis-model`` /
    ``--synthesis-prompt`` / ``--synthesis-temperature``), templating
    (``--var`` / ``--vars-file`` / ``--prompt-template``), persona
    merging, ``--save``, ``--strict`` / ``--failure-threshold``,
    ``--max-concurrent`` / ``--rate-limit-rps``, and ``--dry-run``.

    Everything returned from here is only wired into the single-round
    orchestrator path today. Rather than silently degrading (the pre-fix
    behaviour was worse: silently flattening the whole DAG), each
    combination fails loudly before any LLM spend. Returns one message
    per offending flag group; empty list means the combination is safe.
    """
    errors: list[str] = []
    unsupported = "is not supported with multi-round (branching) instruments yet"
    hint = "run a single-round instrument or drop the flag"

    if blend_mode:
        errors.append(f"--blend {unsupported}; {hint}.")
    if ensemble_mode:
        errors.append(
            f"--models ensemble comparison (comma-separated spec without weights) {unsupported}; "
            f"use a single --model, a weighted --models 'model:weight,...' spec, or run a "
            f"single-round instrument."
        )
    if getattr(args, "variants", None) is not None:
        errors.append(f"--variants (robustness perturbation) {unsupported}; {hint}.")

    checkpoint_flags = [
        flag
        for flag, dest in (
            ("--resume", "resume"),
            ("--checkpoint-dir", "checkpoint_dir"),
            ("--checkpoint-every", "checkpoint_every"),
            ("--allow-drift", "allow_drift"),
            ("--force-overwrite", "force_overwrite"),
        )
        if getattr(args, dest, None)
    ]
    if checkpoint_flags:
        errors.append(f"{'/'.join(checkpoint_flags)} (checkpointing/resume) {unsupported}; {hint}.")

    if getattr(args, "max_cost", None) is not None:
        errors.append(f"--max-cost (mid-run cost gate) {unsupported}; {hint}.")
    if getattr(args, "question_failure_budget", None) is not None:
        errors.append(f"--question-failure-budget {unsupported}; {hint}.")

    convergence_flags = [
        flag
        for flag, dest in (
            ("--convergence-check-every", "convergence_check_every"),
            ("--convergence-log", "convergence_log"),
            ("--convergence-eps", "convergence_eps"),
            ("--convergence-min-n", "convergence_min_n"),
            ("--convergence-m", "convergence_m"),
            ("--convergence-baseline", "convergence_baseline"),
        )
        if getattr(args, dest, None) is not None
    ]
    if getattr(args, "auto_stop", False):
        convergence_flags.append("--auto-stop")
    if convergence_flags:
        errors.append(f"{'/'.join(convergence_flags)} (convergence tracking) {unsupported}; {hint}.")

    if getattr(args, "calibrate_against", None) is not None:
        errors.append(f"--calibrate-against (inline calibration) {unsupported}; {hint}.")
    if getattr(args, "submit_to_synthbench", False):
        errors.append(f"--submit-to-synthbench {unsupported}; {hint}.")

    # --no-synthesis is fine for LINEAR multi-round instruments (routing
    # falls through depends_on / positional order), but branching route_when
    # predicates evaluate against each round's synthesis — without it every
    # route would silently fall to a halt after round 1.
    if getattr(args, "no_synthesis", False) and any(r.route_when for r in instrument.rounds):
        errors.append(
            "--no-synthesis is not supported with branching (route_when) instruments: "
            "routing predicates evaluate each round's synthesis, so skipping synthesis "
            "would break routing. Drop --no-synthesis or run a non-branching instrument."
        )

    if (getattr(args, "synthesis_strategy", None) or "auto") == STRATEGY_MAP_REDUCE:
        errors.append(
            f"--synthesis-strategy=map-reduce {unsupported} (multi-round synthesis is "
            f"always single-pass); drop the flag or run a single-round instrument."
        )
    if getattr(args, "synthesis_auto_escalate", False):
        errors.append(f"--synthesis-auto-escalate (map-reduce only) {unsupported}; {hint}.")

    if getattr(args, "allow_empty_attachments", False):
        errors.append(f"--allow-empty-attachments {unsupported}; {hint}.")

    return errors


def _handle_panel_run_multi_round(
    *,
    args: argparse.Namespace,
    fmt: OutputFormat,
    client: LLMClient,
    timer: PanelTimer,
    personas: list[dict[str, Any]],
    instrument: Instrument,
    model: str,
    system_prompt_fn: Any,
    response_schema: dict[str, Any] | None,
    extract_schema: dict[str, Any] | None,
    temperature: float | None,
    top_p: float | None,
    seed: int | None,
    persona_models: dict[str, str] | None,
    template_vars: dict[str, str],
    personas_merge_warnings: list[dict[str, Any]],
    merge_used: bool,
    profile: Any,
    max_concurrent: int | None,
    request_id: str,
) -> int:
    """Run a multi-round (v2 linear / v3 branching) instrument via the real engine.

    Drives :func:`althing._runners.run_multi_round_sync` — the same
    machinery behind the MCP server's ``run_panel`` and the SDK's
    ``run_panel(instrument=...)`` — and emits the rounds-shaped envelope
    with the REAL routing ``path`` (the route_when decisions that fired)
    in text, json, and ndjson output modes. Exit codes mirror the
    single-round path: 0 success, 2 invalid run, 3 --strict violation.
    """
    from althing._runners import (
        _sanitize_api_error,
        format_panelist_result,
        run_multi_round_sync,
    )

    synthesis_enabled = not getattr(args, "no_synthesis", False)
    synthesis_model = getattr(args, "synthesis_model", None)

    logger.info(
        "[%s] multi-round dispatch: %d declared rounds (branching=%s)",
        request_id,
        len(instrument.rounds),
        any(r.route_when for r in instrument.rounds),
    )

    try:
        mr = run_multi_round_sync(
            client,
            personas,
            instrument,
            model,
            response_schema,
            synthesis=synthesis_enabled,
            synthesis_model=synthesis_model,
            synthesis_prompt=getattr(args, "synthesis_prompt", None),
            temperature=temperature,
            top_p=top_p,
            seed=seed,
            persona_models=persona_models,
            extract_schema=extract_schema,
            synthesis_temperature=getattr(args, "synthesis_temperature", None),
            system_prompt_fn=system_prompt_fn,
            max_workers=max_concurrent,
        )
    except RunAbortedError:
        # SIGINT mid-DAG: partial multi-round results only cover the round
        # in flight; checkpointing is refused for multi-round runs, so there
        # is no partial envelope to emit. Fail loud instead of fabricating.
        print(
            "panel run interrupted (SIGINT) during a multi-round instrument; "
            "no partial result is emitted (checkpointing is not supported for "
            "multi-round runs yet).",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        sanitized = _sanitize_api_error(exc)
        logger.error("multi-round panel run failed: %s", sanitized, exc_info=True)
        print(f"Error: multi-round panel run failed: {sanitized}", file=sys.stderr)
        return 1

    timer.stop()

    # ── Per-round payload (mirrors the MCP _run_panel_async_instrument shape) ──
    rounds_by_name = {r.name: r for r in instrument.rounds}
    rounds_payload: list[dict[str, Any]] = []
    executed_questions: list[dict[str, Any]] = []
    for rr in mr.rounds:
        declared = rounds_by_name.get(rr.name)
        round_questions = declared.questions if declared is not None else []
        executed_questions.extend(round_questions)
        rounds_payload.append(
            {
                "name": rr.name,
                "results": [format_panelist_result(pr, model) for pr in rr.panelist_results],
                "synthesis": rr.synthesis.to_dict() if hasattr(rr.synthesis, "to_dict") else None,
                "usage": rr.usage.to_dict(),
            }
        )
    # Back-compat flat view: the terminal round's panelist list (same
    # convention as the MCP/SDK surfaces).
    flat_results = rounds_payload[-1]["results"] if rounds_payload else []
    all_panelist_results = [pr for rr in mr.rounds for pr in rr.panelist_results]

    # ── Failure analysis (sp-2hg / sp-efip parity with the single-round path) ──
    failure_stats = _analyze_multi_round_failures(mr, rounds_by_name)
    strict = getattr(args, "strict", False)
    try:
        threshold = float(getattr(args, "failure_threshold", 0.5))
    except (TypeError, ValueError):
        threshold = 0.5
    run_invalid = failure_stats["total_pairs"] > 0 and failure_stats["failure_rate"] > threshold
    strict_violation = strict and failure_stats["errored_pairs"] > 0

    total_failure = detect_total_failure(all_panelist_results)
    if total_failure is not None:
        run_invalid = True

    missing_input_stats = _detect_missing_input_refusals(all_panelist_results)
    missing_input_invalid = (
        missing_input_stats["considered"] > 0 and missing_input_stats["refusal_rate"] >= _MISSING_INPUT_THRESHOLD
    )
    if missing_input_invalid:
        run_invalid = True

    # sy-549 parity: a fallback final synthesis is not a healthy synthesis.
    synthesis_error_payload: dict[str, Any] | None = None
    if mr.final_synthesis is not None and getattr(mr.final_synthesis, "is_fallback", False):
        synth_err = mr.final_synthesis.error or "synthesis judge did not return a valid structured result"
        logger.warning("final synthesis produced a fallback result: %s", synth_err)
        print(f"Error: synthesis incomplete: {synth_err}", file=sys.stderr)
        synthesis_error_payload = build_synthesis_error_payload(
            None,
            error_type="synthesis_fallback",
            message=f"Synthesis incomplete: {synth_err}",
            suggested_fix=(
                "The synthesis judge returned an empty/partial result after all retries. "
                "Rerun with a higher-quality --synthesis-model."
            ),
        )

    # ── Cost / usage aggregation ────────────────────────────────────────
    panelist_usage = ZERO_USAGE
    for pr in all_panelist_results:
        panelist_usage = panelist_usage + pr.usage

    panelist_per_model_usage, panelist_per_model_cost = aggregate_per_model(all_panelist_results, model)
    multi_model_run = len(panelist_per_model_usage) > 1
    pricing, is_estimated = lookup_pricing(model)
    if multi_model_run:
        panelist_cost_est = CostEstimate()
        for _c in panelist_per_model_cost.values():
            panelist_cost_est = panelist_cost_est + _c
    else:
        panelist_cost_est = estimate_cost(panelist_usage, pricing)

    # Synthesis spend covers every per-round synthesis PLUS the final
    # reduce, so total_cost accounts for the full multi-round bill.
    synthesis_usage = ZERO_USAGE
    synthesis_cost = CostEstimate()
    synthesis_ran = False
    synth_objects = [rr.synthesis for rr in mr.rounds]
    if mr.final_synthesis is not None:
        synth_objects.append(mr.final_synthesis)
    for synth in synth_objects:
        s_usage = getattr(synth, "usage", None)
        if s_usage is not None:
            synthesis_usage = synthesis_usage + s_usage
            synthesis_ran = True
        s_cost = getattr(synth, "cost", None)
        if s_cost is not None:
            synthesis_cost = synthesis_cost + s_cost

    total_usage = mr.usage
    total_cost_est = panelist_cost_est + synthesis_cost

    final_synth_dict = (
        mr.final_synthesis.to_dict()
        if mr.final_synthesis is not None and hasattr(mr.final_synthesis, "to_dict")
        else None
    )
    effective_synth_model = (
        getattr(mr.final_synthesis, "model", None) if mr.final_synthesis is not None else None
    ) or synthesis_model

    panelist_per_model_meta: dict[str, tuple[TokenUsage, CostEstimate]] | None = None
    if multi_model_run:
        panelist_per_model_meta = {
            _m: (panelist_per_model_usage[_m], panelist_per_model_cost[_m]) for _m in panelist_per_model_usage
        }

    metadata = build_metadata(
        panelist_model=model,
        synthesis_model=effective_synth_model,
        panelist_usage=panelist_usage,
        panelist_cost=panelist_cost_est,
        synthesis_usage=synthesis_usage if synthesis_ran else None,
        synthesis_cost=synthesis_cost if synthesis_ran else None,
        total_usage=total_usage,
        total_cost=total_cost_est,
        persona_count=len(personas),
        question_count=len(executed_questions),
        timer=timer,
        template_vars=template_vars or None,
        panelist_per_model=panelist_per_model_meta,
    )
    if profile is not None:
        metadata["profile"] = profile.name
        metadata["profile_hash"] = profile.config_hash()

    # sp-nn8k: surface DEFAULT_PRICING fallbacks loudly.
    cost_fallback_warnings = build_cost_fallback_warnings([*panelist_per_model_usage.keys(), effective_synth_model])

    banner = _build_invalid_banner(
        failure_stats,
        threshold,
        strict=strict,
        strict_violation=strict_violation,
        missing_input_stats=missing_input_stats,
        missing_input_invalid=missing_input_invalid,
        total_failure=total_failure,
    )

    # ── sy-659 parity: persist --save BEFORE the text/json branch split ──
    saved_result_id: str | None = None
    saved_result_path: str | None = None
    if getattr(args, "save", False):
        from althing.mcp.data import _results_dir, save_panel_result

        inst_name: str | None = None
        inst_arg = getattr(args, "instrument", None)
        if inst_arg:
            inst_path = Path(inst_arg)
            inst_name = inst_path.stem if inst_path.exists() else inst_arg

        all_models: list[str] | None = None
        if persona_models:
            all_models = sorted(set(persona_models.values()))

        # sy-oyl convention (mirrors the SDK): persist every declared
        # round's question defs so analyze/poll-summary see the schema
        # kinds, while flat ``results`` carries the terminal round.
        flat_questions = [q for r in instrument.rounds for q in r.questions]
        saved_result_id = save_panel_result(
            results=flat_results,
            model=model,
            total_usage=total_usage.to_dict(),
            total_cost=total_cost_est.format_usd(),
            persona_count=len(personas),
            question_count=len(executed_questions),
            instrument_name=inst_name,
            questions=_question_meta_for_save(flat_questions),
            models=all_models,
            synthesis=final_synth_dict,
            metadata=metadata,
        )
        saved_result_path = str(_results_dir() / f"{saved_result_id}.json")

    if fmt is OutputFormat.TEXT:
        if banner:
            print(banner, file=sys.stderr)
        # REAL routing path — the route_when decisions that fired, not the
        # _degenerate_path fabrication the flattened run used to print.
        path_line = _format_path(mr.path)
        if path_line:
            print(f"path: {path_line}")
        if mr.terminal_round:
            print(f"terminal round: {mr.terminal_round}")

        for rp in rounds_payload:
            print(f"\n{'=' * 60}")
            print(f"ROUND: {rp['name']}")
            print(f"{'=' * 60}")
            for r in rp["results"]:
                print(f"\nPersona: {r['persona']}")
                if r.get("error"):
                    print(f"  ERROR: {r['error']}")
                for resp in r["responses"]:
                    if resp.get("skipped_by_condition"):
                        print(f"  [follow-up] Q: {resp['question']}")
                        print("  [follow-up] A: (skipped — condition not met)")
                        print()
                        continue
                    prefix = "  [follow-up] " if resp.get("follow_up") else "  "
                    print(f"{prefix}Q: {resp['question']}")
                    print(f"{prefix}A: {resp['response']}")
                    print()
                print(f"  Cost: {r['cost']}")

        if final_synth_dict and "synthesis_error" not in final_synth_dict:
            print(f"\n{'=' * 60}")
            print("SYNTHESIS")
            print(f"{'=' * 60}")
            print(f"\n  Summary: {final_synth_dict['summary']}")
            if final_synth_dict.get("themes"):
                print("\n  Themes:")
                for t in final_synth_dict["themes"]:
                    print(f"    - {t}")
            if final_synth_dict.get("agreements"):
                print("\n  Agreements:")
                for a in final_synth_dict["agreements"]:
                    print(f"    - {a}")
            if final_synth_dict.get("disagreements"):
                print("\n  Disagreements:")
                for d in final_synth_dict["disagreements"]:
                    print(f"    - {d}")
            if final_synth_dict.get("surprises"):
                print("\n  Surprises:")
                for s in final_synth_dict["surprises"]:
                    print(f"    - {s}")
            print(f"\n  Recommendation: {final_synth_dict['recommendation']}")
            print(f"  Synthesis cost: {final_synth_dict['cost']}")

        print(f"\n{'=' * 60}")
        print(
            format_summary("Panelist cost", panelist_usage, panelist_cost_est, model=model, is_estimated=is_estimated)
        )
        if synthesis_ran:
            synth_model_disp = effective_synth_model or model
            _synth_pricing, synth_est = lookup_pricing(synth_model_disp)
            print(
                format_summary(
                    "Synthesis cost",
                    synthesis_usage,
                    synthesis_cost,
                    model=synth_model_disp,
                    is_estimated=synth_est,
                )
            )
        print(format_summary("Total", total_usage, total_cost_est, model=model, is_estimated=is_estimated))
        for w in mr.warnings:
            print(f"Warning: {w}", file=sys.stderr)
        for w in cost_fallback_warnings:
            print(f"Warning: {w}", file=sys.stderr)
        if banner:
            print(banner, file=sys.stderr)
    else:
        if banner:
            print(banner, file=sys.stderr)

        from althing.ensemble import build_mixed_model_rollup

        terminal_prs = mr.rounds[-1].panelist_results if mr.rounds else []
        per_model_results, cost_breakdown = build_mixed_model_rollup(
            terminal_prs,
            default_model=model,
            panelist_formatter=format_panelist_result,
        )

        extra: dict[str, Any] = _build_rounds_shape(
            instrument=instrument,
            results=flat_results,
            synthesis_dict=final_synth_dict,
            panelist_cost=panelist_cost_est,
            panelist_usage=panelist_usage,
            total_usage=total_usage,
            total_cost=total_cost_est,
            model=model,
            persona_count=len(personas),
            question_count=len(executed_questions),
            metadata=metadata,
            per_model_results=per_model_results,
            cost_breakdown=cost_breakdown,
            cost_fallback_warnings=cost_fallback_warnings,
            rounds_override=rounds_payload,
            path=mr.path,
            terminal_round=mr.terminal_round,
            run_warnings=list(mr.warnings),
        )
        extra["parameters"] = _build_params_metadata(args, temperature, top_p)
        extra["failure_stats"] = failure_stats
        extra["missing_input_stats"] = missing_input_stats
        extra["run_invalid"] = bool(run_invalid or strict_violation)
        if total_failure is not None:
            extra["total_failure"] = total_failure
            extra["abort_reason"] = _classify_total_failure_abort_reason(total_failure)
        if synthesis_error_payload is not None:
            # Synthesis-stage failure degrades gracefully: the per-round
            # panelist responses are complete, so surface the payload plus
            # a warning instead of invalidating the run.
            extra["synthesis_error"] = synthesis_error_payload
            warnings_list = extra.setdefault("warnings", [])
            if isinstance(warnings_list, list):
                warnings_list.append(
                    "synthesis_failed: "
                    f"{synthesis_error_payload.get('message', 'synthesis failed')}"
                    " — panelist responses are complete"
                )
        if missing_input_invalid:
            warnings_list = extra.setdefault("warnings", [])
            if isinstance(warnings_list, list):
                warnings_list.append(
                    "missing_input_refusals: "
                    f"{missing_input_stats['refusing']}/{missing_input_stats['considered']}"
                    f" panelists reported missing or unavailable input"
                    f" ({missing_input_stats['refusal_rate']:.0%})"
                )
        if merge_used:
            extra["personas_merge_warnings"] = personas_merge_warnings
        if persona_models:
            extra["model_assignment"] = dict(persona_models)
        if run_invalid or strict_violation:
            extra["message"] = banner.replace("\n", " ").strip() if banner else "PANEL RUN INVALID"
        if saved_result_id is not None:
            extra["result_id"] = saved_result_id
            extra["saved_path"] = saved_result_path
        emit(fmt, message=extra.get("message", "Panel complete"), extra=extra)

    if saved_result_id is not None:
        _print_saved_result_hint(saved_result_id)

    if strict_violation:
        return 3
    if run_invalid:
        return 2
    return 0


def _analyze_multi_round_failures(
    mr: Any,
    rounds_by_name: dict[str, Any],
) -> dict[str, Any]:
    """Sum :func:`_analyze_failures` stats across each executed round.

    Per-round accounting keeps the pair math honest: a persona is only
    accountable for the questions of rounds that actually executed, not
    the flattened union of every declared branch.
    """
    totals: dict[str, Any] = {
        "total_pairs": 0,
        "errored_pairs": 0,
        "failure_rate": 0.0,
        "failed_panelists": 0,
        "errored_personas": [],
        "skipped_follow_ups": 0,
        "skipped_by_budget": 0,
    }
    errored_personas: set[str] = set()
    for rr in mr.rounds:
        declared = rounds_by_name.get(rr.name)
        round_stats = _analyze_failures(
            rr.panelist_results,
            declared.questions if declared is not None else [],
        )
        totals["total_pairs"] += round_stats["total_pairs"]
        totals["errored_pairs"] += round_stats["errored_pairs"]
        totals["failed_panelists"] += round_stats["failed_panelists"]
        totals["skipped_follow_ups"] += round_stats["skipped_follow_ups"]
        totals["skipped_by_budget"] += round_stats["skipped_by_budget"]
        errored_personas.update(round_stats["errored_personas"])
    totals["errored_personas"] = sorted(errored_personas)
    if totals["total_pairs"] > 0:
        totals["failure_rate"] = totals["errored_pairs"] / totals["total_pairs"]
    return totals


# sp-56pb: substrings that mark a panelist failure as rooted in rate-limit
# exhaustion. The LLM client raises ``LLMError`` with category
# ``RETRIES_EXHAUSTED`` after the rate-limit retry budget is spent; the
# stringified form leaks "retries exhausted" and/or "rate limit" into
# the panelist error message. Matching on substrings keeps us resilient
# to future wording changes as long as one of these tokens survives.
_RATE_LIMIT_ABORT_MARKERS = (
    "retries_exhausted",
    "retries exhausted",
    "rate_limit",
    "rate limit",
    "429",
)


def _classify_total_failure_abort_reason(diagnostic: dict[str, Any]) -> str:
    """Map a total-failure diagnostic to a specific ``abort_reason`` tag.

    Returns ``"rate_limit_exhausted"`` when every sampled panelist error
    name-checks a rate-limit marker, otherwise ``"total_failure"``. The
    caller surfaces this at the top level of the JSON output so consumers
    can treat "the whole panel rate-limited out" distinctly from other
    total failures without having to parse the diagnostic envelope.
    """
    sample_errors = diagnostic.get("sample_errors") or []
    if not sample_errors:
        return "total_failure"
    for entry in sample_errors:
        # ``sample_errors`` is a list of (persona, error_message) tuples
        # on construction, but JSON round-trip turns them into lists.
        if isinstance(entry, (tuple, list)) and len(entry) >= 2:
            msg = str(entry[1]).lower()
        else:
            msg = str(entry).lower()
        if not any(marker in msg for marker in _RATE_LIMIT_ABORT_MARKERS):
            return "total_failure"
    return "rate_limit_exhausted"


def _analyze_failures(
    panelist_results: list[Any],
    questions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Count errored panelist-question pairs across a run.

    A pair errors if:
      - the panelist-level wrapper threw (``pr.error`` set), in which
        case *every* authored question is counted as an error, even
        those the panelist never reached, OR
      - an individual response dict was flagged ``error: True`` (the
        orchestrator sets this when a per-question call raises).

    Follow-ups skipped by condition (``skipped_by_condition: True``) are
    excluded from both the numerator and denominator — they are intentional
    non-answers, not failures.

    Primary questions skipped by the per-question failure budget
    (``skipped_by_budget: True``, sp-xw2z6o) are also excluded — they are
    short-circuits triggered by the budget feature, not missing answers,
    so they must not inflate ``failure_rate`` or appear as 0% response
    rate downstream.

    Returns a dict with ``total_pairs``, ``errored_pairs``,
    ``failure_rate`` (0-1), ``failed_panelists`` (panelist-level
    failures), ``errored_personas`` (names of affected personas),
    ``skipped_follow_ups`` (total follow-ups skipped by condition), and
    ``skipped_by_budget`` (primary questions short-circuited by the
    per-question failure budget).
    """
    total_questions = len(questions) if questions else 0
    total_pairs = 0
    errored_pairs = 0
    failed_panelists = 0
    errored_personas: set[str] = set()
    skipped_follow_ups = 0
    skipped_by_budget = 0

    for pr in panelist_results:
        if getattr(pr, "error", None):
            # Whole panelist exploded — every authored question counts as
            # an error for the purposes of the failure-rate calculation.
            failed_panelists += 1
            errored_pairs += total_questions
            total_pairs += total_questions
            errored_personas.add(getattr(pr, "persona_name", "unknown"))
            continue

        pair_count = 0
        err_count = 0
        budget_skipped_for_panelist = 0
        for resp in getattr(pr, "responses", []) or []:
            if isinstance(resp, dict) and resp.get("follow_up"):
                # Follow-ups are not counted as primary QA pairs — they
                # are second-order and would double-count toward the rate.
                if resp.get("skipped_by_condition"):
                    skipped_follow_ups += 1
                continue
            # sp-xw2z6o: budget-skipped primary questions don't count as
            # failed pairs OR as missing pairs. They're an intentional
            # mid-run short-circuit; surface separately and exclude from
            # the failure-rate denominator.
            if isinstance(resp, dict) and resp.get("skipped_by_budget"):
                skipped_by_budget += 1
                budget_skipped_for_panelist += 1
                continue
            pair_count += 1
            if isinstance(resp, dict) and resp.get("error"):
                err_count += 1
        # If the panelist never produced any primary responses (e.g. a
        # structured-output path that bailed before recording), treat the
        # shortfall as errored against the authored question count. The
        # shortfall is computed against the questions that were *not*
        # skipped by budget — a budget skip is an intentional non-answer,
        # not a missing one, so it must not feed the shortfall counter.
        if total_questions:
            authored_for_panelist = total_questions - budget_skipped_for_panelist
            if pair_count < authored_for_panelist:
                shortfall = authored_for_panelist - pair_count
                err_count += shortfall
                pair_count += shortfall
        total_pairs += pair_count
        errored_pairs += err_count
        if err_count > 0:
            errored_personas.add(getattr(pr, "persona_name", "unknown"))

    failure_rate = (errored_pairs / total_pairs) if total_pairs > 0 else 0.0
    return {
        "total_pairs": total_pairs,
        "errored_pairs": errored_pairs,
        "failure_rate": failure_rate,
        "failed_panelists": failed_panelists,
        "errored_personas": sorted(errored_personas),
        "skipped_follow_ups": skipped_follow_ups,
        "skipped_by_budget": skipped_by_budget,
    }


# sp-bjt4: patterns that signal a panelist is refusing because the prompt
# arrived with empty/malformed source material (e.g. a landing-page audit
# rendered with a blank {page_html} section). These fire *after* a clean
# panelist run, so they cannot be caught by _analyze_failures — the model
# replied politely with "I don't see…" and no error flag was set. Kept as
# ordered tuples of (pattern, description) to keep the list auditable.
_MISSING_INPUT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:don't|do not|can't|cannot|can not|don't actually|do not actually)\s+(?:see|find|have)\s+"
        r"(?:the|any|a|an)\s+(?:actual\s+)?"
        r"(?:content|text|page|document|article|example|examples|material|information|"
        r"details|data|image|screenshot|section|excerpt|passage|transcript|copy)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bno\s+(?:actual\s+)?"
        r"(?:content|text|input|page|document|material|information|article|copy|examples?|excerpt|passage|transcript)\s+"
        r"(?:was|has\s+been|is|to)\s+"
        r"(?:provided|shared|given|included|attached|review|analyze|evaluate)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:haven't|have not|hasn't|has not|wasn't|was not)\s+(?:been\s+|actually\s+)?"
        r"(?:shared|provided|given|included|attached|pasted)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bnothing\s+(?:here\s+)?to\s+"
        r"(?:review|analyze|evaluate|assess|comment\s+on|respond\s+to|discuss|work\s+with|go\s+on)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:the\s+)?(?:content|text|page|document|material|copy|excerpt|passage)\s+"
        r"(?:you|that|which)\s+"
        r"(?:mentioned|referenced|described|referred\s+to|linked|pointed\s+to)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:missing|absent|empty|blank)\s+"
        r"(?:content|input|text|page|document|material|information|body|section)",
        re.IGNORECASE,
    ),
)

# Threshold is a module constant rather than a CLI flag — the bead specifies
# ≥50% as the heuristic and adding a flag would invite misuse (set to 100%
# and the signal disappears). Tune here if real-world data demands it.
_MISSING_INPUT_THRESHOLD: float = 0.5


def _response_flags_missing_input(text: str) -> bool:
    """Return True iff *text* matches any missing-input refusal pattern."""
    if not text:
        return False
    return any(p.search(text) for p in _MISSING_INPUT_PATTERNS)


def _detect_missing_input_refusals(
    panelist_results: list[Any],
) -> dict[str, Any]:
    """Count panelists whose primary responses flag missing/unavailable input.

    A panelist is counted as *refusing* if at least one primary (non-follow-up)
    response matches a missing-input pattern. Wholesale-errored panelists are
    excluded from the denominator — their failure is already captured by
    ``_analyze_failures`` and double-counting would muddy the rate.

    Returns a dict with ``considered`` (panelists included in denominator),
    ``refusing`` (panelists with at least one flagged response),
    ``refusal_rate`` (0-1), and ``refusing_personas`` (sorted names).
    """
    considered = 0
    refusing = 0
    refusing_personas: set[str] = set()

    for pr in panelist_results:
        if getattr(pr, "error", None):
            continue
        responses = getattr(pr, "responses", []) or []
        if not responses:
            continue
        considered += 1
        for resp in responses:
            if not isinstance(resp, dict) or resp.get("follow_up"):
                continue
            answer = resp.get("response", "")
            if isinstance(answer, dict):
                answer = json.dumps(answer)
            if _response_flags_missing_input(str(answer)):
                refusing += 1
                refusing_personas.add(getattr(pr, "persona_name", "unknown"))
                break

    rate = (refusing / considered) if considered > 0 else 0.0
    return {
        "considered": considered,
        "refusing": refusing,
        "refusal_rate": rate,
        "refusing_personas": sorted(refusing_personas),
    }


def _build_total_failure_banner(
    total_failure: dict[str, Any],
    stats: dict[str, Any] | None = None,
) -> str:
    """Render the sp-efip banner for a wholesale "every panelist failed" run.

    Names the model(s) exercised and the first sample error so the user
    sees the upstream failure (e.g. "OpenRouter API error 400: ...") at
    the top of stderr — not buried inside a JSON blob or an aggregate
    failure-rate line. Still prints the errored/total pair count when
    available, so the aggregate sp-2hg diagnostic stays intact.
    """
    bar = "!" * 70
    lines = [bar]
    headline = (
        f"PANEL RUN INVALID: every panelist failed"
        f" ({total_failure.get('panelists', 0)} panelist(s), no usable Q/A pairs)"
    )
    if stats and stats.get("total_pairs"):
        headline += f" — {stats['errored_pairs']}/{stats['total_pairs']} pairs errored"
    lines.append(headline + ".")
    models = total_failure.get("models") or []
    if models:
        lines.append(f"  Failing model(s): {', '.join(models)}")
    sample_errors = total_failure.get("sample_errors") or []
    for persona, err in sample_errors:
        snippet = err.strip().splitlines()[0] if err else ""
        if len(snippet) > 240:
            snippet = snippet[:237] + "..."
        lines.append(f"  {persona}: {snippet}")
    lines.append("No synthesis was performed — the run produced no usable data.")
    lines.append("Check the model name, provider credentials, and upstream status.")
    lines.append(bar)
    return "\n".join(lines)


def _build_invalid_banner(
    stats: dict[str, Any],
    threshold: float,
    *,
    strict: bool,
    strict_violation: bool,
    missing_input_stats: dict[str, Any] | None = None,
    missing_input_invalid: bool = False,
    total_failure: dict[str, Any] | None = None,
) -> str:
    """Render the fatal banner printed to stderr on an invalid run.

    Returns an empty string when the run is valid. The banner is
    intentionally loud — the entire point of sp-2hg is that a user
    skimming output cannot miss that the panel was not executed
    successfully.
    """
    total = stats["total_pairs"]
    errored = stats["errored_pairs"]
    rate = stats["failure_rate"] if total > 0 else 0.0
    over_threshold = total > 0 and rate > threshold
    if not (over_threshold or strict_violation or missing_input_invalid or total_failure):
        return ""

    # sp-efip: total-wipeout banner takes precedence so the user sees
    # the failing model and first upstream error at the top, not buried
    # under aggregate failure-rate language.
    if total_failure is not None:
        return _build_total_failure_banner(total_failure, stats)

    bar = "!" * 70
    lines = [bar]
    if over_threshold:
        lines.append(
            f"PANEL RUN INVALID: {errored}/{total} panelist-question pairs"
            f" errored ({rate:.0%} > threshold {threshold:.0%})."
        )
    elif strict_violation:
        lines.append(f"PANEL RUN INVALID (--strict): {errored}/{total} panelist-question pairs errored.")
    elif missing_input_invalid and missing_input_stats:
        considered = missing_input_stats["considered"]
        refusing = missing_input_stats["refusing"]
        mi_rate = missing_input_stats["refusal_rate"]
        lines.append(
            f"PANEL RUN INVALID: {refusing}/{considered} panelist(s) reported"
            f" missing or unavailable input ({mi_rate:.0%} >= threshold"
            f" {_MISSING_INPUT_THRESHOLD:.0%})."
        )
        lines.append(
            "The prompt likely arrived with an empty or malformed section (e.g. an unrendered template fragment)."
        )
    lines.append("No synthesis was performed — the run produced no usable data.")
    if stats.get("failed_panelists"):
        lines.append(f"  {stats['failed_panelists']} panelist(s) failed wholesale (no responses recorded).")
    if stats.get("errored_personas"):
        shown = ", ".join(stats["errored_personas"][:4])
        extra = len(stats["errored_personas"]) - 4
        if extra > 0:
            shown += f", +{extra} more"
        lines.append(f"  Affected personas: {shown}")
    if missing_input_invalid and missing_input_stats and missing_input_stats.get("refusing_personas"):
        shown = ", ".join(missing_input_stats["refusing_personas"][:4])
        extra = len(missing_input_stats["refusing_personas"]) - 4
        if extra > 0:
            shown += f", +{extra} more"
        lines.append(f"  Personas reporting missing input: {shown}")
    if missing_input_invalid and not (over_threshold or strict_violation):
        lines.append("Check that every --var / {placeholder} rendered with real content,")
        lines.append("then re-run once the prompt is whole.")
    else:
        lines.append("Check provider status (e.g. rate-limit / 503), run with a different")
        lines.append("--model, or re-run once the upstream provider is healthy.")
    lines.append(bar)
    return "\n".join(lines)


def _build_rounds_shape(
    *,
    instrument: Instrument,
    results: list[dict[str, Any]],
    synthesis_dict: dict[str, Any] | None,
    panelist_cost: Any,
    total_usage: TokenUsage,
    total_cost: Any,
    model: str,
    persona_count: int,
    question_count: int,
    panelist_usage: TokenUsage | None = None,
    metadata: dict[str, Any] | None = None,
    per_model_results: dict[str, Any] | None = None,
    cost_breakdown: dict[str, Any] | None = None,
    cost_fallback_warnings: list[str] | None = None,
    rounds_override: list[dict[str, Any]] | None = None,
    path: list[dict[str, Any]] | None = None,
    terminal_round: str | None = None,
    run_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build the rounds-shaped panel output payload.

    Single-round runs surface as one round entry whose ``name`` is the
    instrument's only round (``"default"`` for v1). Multi-round/branching
    runs (P1-2) pass ``rounds_override`` — one entry per EXECUTED round —
    plus the REAL router-emitted ``path`` and ``terminal_round``, matching
    the envelope the MCP/SDK surfaces produce. Per-round ``synthesis`` is
    ``null`` for the single-round case — the final synthesis goes at the
    top level.

    ``run_warnings``, when provided, replaces the parser-only
    ``instrument.warnings`` default — the multi-round engine's warnings
    already include the parser warnings plus any runtime routing issues.

    ``per_model_results`` and ``cost_breakdown``, when provided, surface
    the sp-0h9x rollup shape alongside the primary ``model`` field — a
    superset of the mixed-model artifact that mayor's ensemble audits
    previously had to reconstruct by iterating ``rounds[].results[]``.

    ``cost_fallback_warnings`` (sp-nn8k) is appended to ``warnings`` for
    every model that was priced via ``DEFAULT_PRICING`` fallback; the
    top-level ``cost_is_estimated`` boolean mirrors ``bool(...)`` of that
    list so programmatic consumers can gate on it without string-matching
    the warning text.
    """
    round_name = instrument.rounds[0].name if instrument.rounds else "default"
    warnings = list(run_warnings) if run_warnings is not None else list(getattr(instrument, "warnings", []) or [])
    fallback_warnings = list(cost_fallback_warnings or [])
    warnings.extend(fallback_warnings)
    rounds_payload = (
        rounds_override
        if rounds_override is not None
        else [
            {
                "name": round_name,
                "results": results,
                "synthesis": None,
            }
        ]
    )
    result: dict[str, Any] = {
        "rounds": rounds_payload,
        "path": list(path) if path is not None else [],
        "warnings": warnings,
        "cost_is_estimated": bool(fallback_warnings),
        "synthesis": synthesis_dict,
        "panelist_cost": panelist_cost.format_usd(),
        "panelist_usage": (panelist_usage.to_dict() if panelist_usage is not None else None),
        "total_usage": total_usage.to_dict(),
        "total_cost": total_cost.format_usd(),
        "model": model,
        "persona_count": persona_count,
        "question_count": question_count,
        "per_model_results": per_model_results,
        "cost_breakdown": cost_breakdown,
    }
    if rounds_override is not None:
        # Multi-round envelope always names the terminal round (parity with
        # the MCP/SDK shape); omitted for legacy single-round output.
        result["terminal_round"] = terminal_round
    if metadata is not None:
        result["metadata"] = metadata
    return result


def handle_pack_list(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """List persona packs.

    Default: bundled + local packs with origin column. With ``--registry``:
    packs from the cached althing registry (id, name, ref, version
    columns). On empty cache + fetch fail, prints an advisory line and exits 0.
    """
    if getattr(args, "registry", False):
        return _handle_pack_list_registry(fmt)

    from althing.mcp.data import list_persona_packs

    packs = list_persona_packs()

    if fmt is OutputFormat.TEXT:
        if not packs:
            print("No persona packs found.")
        else:
            for p in packs:
                origin = "bundled" if p.get("builtin") else "local"
                print(f"  {p['id']}  {p['name']}  ({p['persona_count']} personas)  [{origin}]")
    else:
        emit(fmt, message="Persona packs", extra={"packs": packs})

    return 0


def _handle_pack_list_registry(fmt: OutputFormat) -> int:
    """Registry branch of ``pack list``."""
    from althing.registry import cache_path, fetch_registry

    had_cache = cache_path().exists()
    registry = fetch_registry()
    raw_packs = registry.get("packs") or []
    entries = [e for e in raw_packs if isinstance(e, dict) and e.get("id")]

    if not entries and not had_cache and not cache_path().exists():
        # Empty cache + fetch fail (load_registry returned EMPTY_REGISTRY
        # without persisting a cache). Advisory, exit 0.
        if fmt is OutputFormat.TEXT:
            print("registry unavailable, try again later")
        else:
            emit(fmt, message="registry unavailable, try again later", extra={"packs": []})
        return 0

    rows = sorted(
        (
            {
                "id": str(e.get("id", "")),
                "name": str(e.get("name", "")),
                "ref": str(e.get("ref") or "main"),
                "version": str(e.get("version") or ""),
            }
            for e in entries
        ),
        key=lambda r: r["id"],
    )

    if fmt is OutputFormat.TEXT:
        if not rows:
            print("No packs in registry.")
        else:
            for r in rows:
                print(f"  {r['id']}  {r['name']}  ({r['ref']})  {r['version']}".rstrip())
    else:
        emit(fmt, message="Registry packs", extra={"packs": rows})

    return 0


def handle_pack_search(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Search packs by substring.

    Default: searches bundled + local packs (id, name, description).
    With ``--registry``: searches the remote althing registry instead.
    """
    if getattr(args, "registry", False):
        return _handle_pack_search_registry(args, fmt)
    return _handle_pack_search_local(args, fmt)


def _handle_pack_search_local(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Search bundled + user-saved packs by substring."""
    from althing.mcp.data import list_persona_packs

    term = (args.term or "").lower()
    packs = list_persona_packs()

    matches: list[dict[str, Any]] = []
    for p in packs:
        haystack = " ".join([str(p.get("id", "")), str(p.get("name", "")), str(p.get("description", ""))]).lower()
        if term in haystack:
            matches.append(
                {
                    "id": str(p["id"]),
                    "name": str(p["name"]),
                    "persona_count": p["persona_count"],
                    "origin": "bundled" if p.get("builtin") else "local",
                }
            )

    if fmt is OutputFormat.TEXT:
        if not matches:
            print(f"No packs match '{args.term}'.")
        else:
            for m in matches:
                print(f"  {m['id']}  {m['name']}  ({m['persona_count']} personas)  [{m['origin']}]")
    else:
        emit(fmt, message="Pack search", extra={"term": args.term, "matches": matches})

    return 0


def _handle_pack_search_registry(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Search remote registry packs by substring across id, name, description, tags."""
    from althing.registry import cache_path, fetch_registry

    term = (args.term or "").lower()

    had_cache = cache_path().exists()
    registry = fetch_registry()
    raw_packs = registry.get("packs") or []
    entries = [e for e in raw_packs if isinstance(e, dict) and e.get("id")]

    if not entries and not had_cache and not cache_path().exists():
        if fmt is OutputFormat.TEXT:
            print("registry unavailable, try again later")
        else:
            emit(fmt, message="registry unavailable, try again later", extra={"matches": []})
        return 0

    matches: list[dict[str, Any]] = []
    for e in entries:
        haystack_parts = [
            str(e.get("id", "")),
            str(e.get("name", "")),
            str(e.get("description", "")),
        ]
        tags = e.get("tags") or ()
        if isinstance(tags, (list, tuple)):
            haystack_parts.extend(str(t) for t in tags)
        haystack = " ".join(haystack_parts).lower()
        if term in haystack:
            matches.append(
                {
                    "id": str(e.get("id", "")),
                    "name": str(e.get("name", "")),
                    "ref": str(e.get("ref") or "main"),
                    "version": str(e.get("version") or ""),
                    "description": str(e.get("description", "")),
                }
            )

    matches.sort(key=lambda r: r["id"])

    if fmt is OutputFormat.TEXT:
        if not matches:
            print(f"No packs match '{args.term}'.")
        else:
            for r in matches:
                desc = f"  {r['description']}" if r["description"] else ""
                print(f"  {r['id']}  {r['name']}  ({r['ref']})  {r['version']}{desc}".rstrip())
    else:
        emit(fmt, message="Registry search", extra={"term": args.term, "matches": matches})

    return 0


def handle_pack_import(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Import a persona pack from a local file or remote source.

    Routes on ``args.source``:

    - ``gh:user/repo[@ref][:path]`` / ``https://…`` → remote fetch path with
      registry consultation, collision checks, and ``--unverified`` gating.
    - A bare registry id that isn't a local file → resolved against the
      registry to its ``gh:`` source and imported via the remote path
      (#520). This is the command ``panel run --personas <id>`` points
      users at, so it must install the pack rather than echo itself back.
    - Anything else → local file path (existing behavior, unchanged).
    """
    source = args.source
    if source.startswith("gh:") or source.startswith("http://") or source.startswith("https://"):
        return _handle_pack_import_remote(args, fmt, source)
    if not Path(source).exists() and _looks_like_pack_id(source):
        gh_source = _resolve_registry_id_source(source)
        if gh_source is not None:
            # Keep the installed pack id equal to the registry id so the
            # follow-up `--personas <id>` resolves, unless --id overrides.
            if not getattr(args, "pack_id", None):
                args.pack_id = source
            return _handle_pack_import_remote(args, fmt, gh_source)
    return _handle_pack_import_local(args, fmt, source)


def _handle_pack_import_local(args: argparse.Namespace, fmt: OutputFormat, source: str) -> int:
    """Local-path branch of ``pack import`` (unchanged behavior)."""
    from althing.mcp.data import PackValidationError, save_persona_pack

    try:
        personas = _load_personas(source)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error loading file: {exc}", file=sys.stderr)
        return 1

    # Determine pack name: explicit flag > YAML 'name' key > filename stem
    pack_name = args.name
    if not pack_name:
        data = _load_yaml(source)
        if isinstance(data, dict):
            pack_name = data.get("name")
    if not pack_name:
        pack_name = Path(source).stem

    try:
        result = save_persona_pack(pack_name, personas, pack_id=args.pack_id)
    except PackValidationError as exc:
        print(f"Validation error: {exc}", file=sys.stderr)
        return 1

    if fmt is OutputFormat.TEXT:
        print(f"Imported pack '{result['name']}' ({result['persona_count']} personas) as {result['id']}")
    else:
        emit(fmt, message="Pack imported", extra=result)

    return 0


def handle_pack_save(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Save a local YAML file as a named persona pack in the local registry.

    Validates the YAML before saving and refuses to install a pack whose
    derived ID collides with a bundled pack name.  Use ``--id`` to choose a
    different ID when a collision is unavoidable.
    """
    from althing.mcp.data import PackValidationError, _bundled_packs, save_persona_pack

    source = args.file
    try:
        personas = _load_personas(source)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error loading file: {exc}", file=sys.stderr)
        return 1

    # Determine pack name: explicit flag > YAML 'name' key > filename stem
    pack_name = args.name
    if not pack_name:
        data = _load_yaml(source)
        if isinstance(data, dict):
            pack_name = data.get("name")
    if not pack_name:
        pack_name = Path(source).stem

    pack_id = args.pack_id if getattr(args, "pack_id", None) else _slugify_pack_id(str(pack_name))

    if pack_id in _bundled_packs():
        print(
            f"Error: '{pack_id}' conflicts with a bundled pack. Use --id to choose a different ID.",
            file=sys.stderr,
        )
        return 1

    try:
        result = save_persona_pack(str(pack_name), personas, pack_id=pack_id)
    except PackValidationError as exc:
        print(f"Validation error: {exc}", file=sys.stderr)
        return 1

    if fmt is OutputFormat.TEXT:
        print(f"Saved pack '{result['name']}' ({result['persona_count']} personas) as {result['id']}")
    else:
        emit(fmt, message="Pack saved", extra=result)

    return 0


def handle_pack_uninstall(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Uninstall a user-saved persona pack from the local registry.

    Refuses to uninstall bundled packs.
    """
    from althing.mcp.data import uninstall_persona_pack

    pack_id = args.pack_id
    try:
        uninstall_persona_pack(pack_id)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if fmt is OutputFormat.TEXT:
        print(f"Uninstalled pack '{pack_id}'.")
    else:
        emit(fmt, message="Pack uninstalled", extra={"pack_id": pack_id})
    return 0


def _slugify_pack_id(value: str) -> str:
    """Best-effort slug: lowercase, non-alphanumeric → ``-``, trim ``-``s."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "pack"


def _default_pack_id_from_source(source: str) -> str:
    """Derive a fallback pack id from a ``gh:``/``https://…`` source."""
    from althing.registry import parse_gh_source

    if source.startswith("gh:"):
        try:
            parsed = parse_gh_source(source)
        except ValueError:
            return _slugify_pack_id(source[3:] or "pack")
        return _slugify_pack_id(parsed.repo)

    # https URL: use the path stem, falling back to the host.
    from urllib.parse import urlparse

    parts = urlparse(source)
    tail = Path(parts.path).stem or parts.netloc or "pack"
    return _slugify_pack_id(tail)


_PACK_FILE_SUFFIXES = {".yaml", ".yml", ".json"}


def _looks_like_pack_id(source: str) -> bool:
    """Return True when *source* looks like a bare registry/pack id (a slug).

    Pack ids are slugs (``icp-demo``, ``pricing-discovery``); anything
    with a path separator or a recognized data-file extension is a
    filesystem path and must NOT trigger a registry network probe. This
    keeps ``pack import ./some/file.yaml`` on the local branch.
    """
    if not source or "/" in source or "\\" in source:
        return False
    return Path(source).suffix.lower() not in _PACK_FILE_SUFFIXES


def _resolve_registry_id_source(pack_id: str) -> str | None:
    """Resolve a bare registry id to a ``gh:`` source spec, or ``None``.

    This is what lets ``pack import <registry-id>`` actually install the
    pack that ``panel run --personas <registry-id>`` points users at —
    rather than echoing the same self-referential command back (#520).

    Returns ``None`` when *pack_id* isn't a known registry pack or the
    registry can't be consulted (offline, malformed entry). Best-effort:
    any exception degrades to ``None`` so a network blip never crashes
    the command — the caller then falls back to local-path import.
    """
    try:
        from althing.registry import resolve_pack

        entry = resolve_pack(pack_id)
    except Exception:
        return None
    if entry is None:
        return None
    ref = entry.ref or "main"
    return f"gh:{entry.repo}@{ref}:{entry.path}"


def _source_in_registry(source: str) -> bool:
    """Return True when *source* is listed in the cached registry.

    Only ``gh:`` sources can be matched against the registry — http(s)
    URLs have no repo/ref to key on, so they are always treated as
    unregistered (forcing the ``--unverified`` path).
    """
    from althing.registry import fetch_registry, parse_gh_source

    if not source.startswith("gh:"):
        return False
    try:
        parsed = parse_gh_source(source)
    except ValueError:
        return False

    repo_slug = f"{parsed.user}/{parsed.repo}"
    try:
        registry = fetch_registry()
    except Exception:
        return False

    for entry in registry.get("packs", []) or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("repo") != repo_slug:
            continue
        entry_ref = entry.get("ref") or "main"
        if entry_ref == parsed.ref:
            return True
    return False


def _handle_pack_import_remote(
    args: argparse.Namespace,
    fmt: OutputFormat,
    source: str,
) -> int:
    """Remote-source branch of ``pack import`` (``gh:`` / ``https://…``)."""
    import hashlib

    import httpx

    from althing.mcp.data import (
        PackValidationError,
        _bundled_packs,
        _packs_dir,
        save_persona_pack,
        validate_persona_pack,
    )
    from althing.registry import resolve_source

    # 1. Resolve to a raw URL.
    try:
        url = resolve_source(source)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # 2. Fetch the YAML body.
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        print(f"Error fetching {url}: {exc}", file=sys.stderr)
        return 1

    if resp.status_code == 404:
        print(
            f"Error: pack not found at {url} (HTTP 404).\n  Repo may be private; GITHUB_TOKEN support planned.",
            file=sys.stderr,
        )
        return 1
    if resp.status_code != 200:
        print(f"Error: HTTP {resp.status_code} fetching {url}", file=sys.stderr)
        return 1

    content = resp.text

    # 3. Parse YAML.
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        print(f"Error: invalid YAML at {url}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print(
            f"Error: pack YAML at {url} must be a mapping, got {type(data).__name__}",
            file=sys.stderr,
        )
        return 1

    # 4. Validate personas (same bar as local import).
    personas = data.get("personas")
    if personas is None and isinstance(data, list):
        personas = data
    try:
        personas = validate_persona_pack(personas if personas is not None else [])
    except PackValidationError as exc:
        print(f"Validation error: {exc}", file=sys.stderr)
        return 1

    # 5. Determine target pack_id: explicit flag > YAML 'id' > derived.
    if args.pack_id:
        pack_id = args.pack_id
    elif isinstance(data.get("id"), str) and data["id"].strip():
        pack_id = data["id"]
    else:
        pack_id = _default_pack_id_from_source(source)

    # 6. Collision checks.
    if pack_id in _bundled_packs():
        print(
            f"Error: pack id '{pack_id}' collides with a bundled pack.\n"
            f"  Re-run with --id <new-id> to import under a different name.",
            file=sys.stderr,
        )
        return 1

    saved_path = _packs_dir() / f"{pack_id}.yaml"
    if saved_path.exists() and not args.force:
        print(
            f"Error: pack id '{pack_id}' already exists as a user-saved pack.\n"
            f"  Re-run with --force to overwrite, or --id <new-id> for a new copy.",
            file=sys.stderr,
        )
        return 1

    # 7. Registry consultation.
    is_registered = _source_in_registry(source)
    if not is_registered and not args.unverified:
        print(
            f"Error: pack '{source}' is not in the althing registry.\n  Rerun with --unverified to import anyway.",
            file=sys.stderr,
        )
        return 1
    if is_registered and args.unverified:
        print(
            f"Note: '{source}' is already in the althing registry; --unverified flag unnecessary.",
            file=sys.stderr,
        )

    # 8. Resolve pack name (explicit flag > YAML 'name' > pack_id).
    pack_name = args.name
    if not pack_name:
        yaml_name = data.get("name")
        if isinstance(yaml_name, str) and yaml_name.strip():
            pack_name = yaml_name
    if not pack_name:
        pack_name = pack_id

    # 9. Save.
    version = data.get("version") if isinstance(data.get("version"), str) else None
    try:
        result = save_persona_pack(
            pack_name,
            personas,
            pack_id=pack_id,
            version=version,
        )
    except (PackValidationError, ValueError) as exc:
        print(f"Validation error: {exc}", file=sys.stderr)
        return 1

    # 10. Section-4 warning block for unverified imports.
    if not is_registered and args.unverified:
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        print(
            f"! Pack '{source}' is not in the althing registry.\n"
            f"  Source:      {url}\n"
            f"  Checksum:    sha256:{checksum}\n"
            f"  Imported as: {pack_id}\n"
            f"  Use 'pack search' to browse registered packs.",
            file=sys.stderr,
        )

    if fmt is OutputFormat.TEXT:
        print(f"Imported pack '{result['name']}' ({result['persona_count']} personas) as {result['id']}")
    else:
        emit(fmt, message="Pack imported", extra=result)

    return 0


def handle_pack_export(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Export a saved persona pack to stdout or a file."""
    from althing.mcp.data import get_persona_pack

    try:
        pack = get_persona_pack(args.pack_id)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Build clean export data (without internal 'id' field)
    export_data = {k: v for k, v in pack.items() if k != "id"}

    content = yaml.dump(export_data, default_flow_style=False)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        if fmt is OutputFormat.TEXT:
            print(f"Exported pack '{pack.get('name', args.pack_id)}' to {args.output}")
        else:
            emit(fmt, message="Pack exported", extra={"path": args.output, "pack_id": args.pack_id})
    else:
        if fmt is OutputFormat.TEXT:
            print(content, end="")
        else:
            emit(fmt, message="Pack exported", extra={"pack_id": args.pack_id, "data": export_data})

    return 0


def handle_pack_show(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Print a saved persona pack's YAML to stdout.

    sp-oem: API parity alias for ``instruments show``. Internally
    delegates to :func:`handle_pack_export` with ``output=None`` so
    behaviour stays identical to ``pack export <id>`` (no file arg).
    """
    args.output = None
    return handle_pack_export(args, fmt)


def handle_pack_inspect(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Human-readable pack summary with wrapped long fields (GH #311).

    Text mode word-wraps long prose to the terminal width unless
    ``--full`` is set (preserves newlines, no wrapping). JSON/NDJSON
    emit the raw pack payload plus ``full`` under ``inspect``.
    """
    from althing.mcp.data import get_persona_pack

    try:
        pack = get_persona_pack(args.pack_id)
    except FileNotFoundError as exc:
        msg = f"Error: {exc}"
        if fmt is OutputFormat.TEXT:
            print(msg, file=sys.stderr)
        else:
            emit(fmt, message=msg, extra={"error": "not_found", "pack_id": args.pack_id})
        return 1

    full = bool(getattr(args, "full", False))
    personas = pack.get("personas")
    if not isinstance(personas, list):
        personas = []

    pid = pack.get("id", args.pack_id)

    if fmt is not OutputFormat.TEXT:
        emit(
            fmt,
            message="Pack inspect",
            extra={
                "inspect": {
                    "pack_id": pid,
                    "full": full,
                    "name": pack.get("name", ""),
                    "version": pack.get("version", ""),
                    "author": pack.get("author", ""),
                    "description": pack.get("description", ""),
                    "persona_count": len(personas),
                    "personas": personas,
                }
            },
        )
        return 0

    wrap_w = terminal_columns()
    lines: list[str] = []
    lines.append(f"Pack: {pid}")
    lines.append(f"  Name: {pack.get('name', '')}")
    lines.append(f"  Version: {pack.get('version', '')}")
    lines.append(f"  Author: {pack.get('author', '')}")
    lines.append("  Description:")
    lines.extend(
        format_prose_for_inspect(
            str(pack.get("description") or ""),
            wrap_width=wrap_w,
            full=full,
        )
    )
    lines.append(f"  Personas: {len(personas)}")
    for i, persona in enumerate(personas, start=1):
        if not isinstance(persona, dict):
            lines.append("")
            lines.append(f"  [{i}] (invalid persona entry — expected mapping)")
            continue
        lines.append("")
        lines.append(f"  [{i}] {persona.get('name', '(unnamed)')}")
        age = persona.get("age")
        if age is not None:
            lines.append(f"      Age: {age}")
        occ = persona.get("occupation")
        if occ:
            lines.append("      Occupation:")
            lines.extend(
                format_prose_for_inspect(
                    str(occ),
                    wrap_width=wrap_w,
                    full=full,
                    indent="        ",
                )
            )
        lines.append("      Background:")
        lines.extend(
            format_prose_for_inspect(
                str(persona.get("background") or ""),
                wrap_width=wrap_w,
                full=full,
                indent="        ",
            )
        )
        traits = persona.get("personality_traits")
        if traits is not None:
            if isinstance(traits, list):
                tstr = ", ".join(str(t) for t in traits if t is not None)
            else:
                tstr = str(traits)
            lines.append("      Traits:")
            lines.extend(
                format_prose_for_inspect(
                    tstr,
                    wrap_width=wrap_w,
                    full=full,
                    indent="        ",
                )
            )

    print("\n".join(lines))
    return 0


def handle_pack_diff(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Compare two persona packs side-by-side (GH #308).

    Accepts built-in pack names, user-saved pack IDs, or YAML file paths
    for either side. Prints a human-readable summary by default; pass
    ``--format json`` for CI-friendly structured output.
    """
    try:
        pack_a, pack_a_id = load_pack(args.pack_a)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, yaml.YAMLError) as exc:
        print(f"Error reading {args.pack_a!r}: {exc}", file=sys.stderr)
        return 1

    try:
        pack_b, pack_b_id = load_pack(args.pack_b)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, yaml.YAMLError) as exc:
        print(f"Error reading {args.pack_b!r}: {exc}", file=sys.stderr)
        return 1

    diff = compute_pack_diff(pack_a, pack_b, pack_a_id=pack_a_id, pack_b_id=pack_b_id)

    diff_format = getattr(args, "diff_format", "text")
    if diff_format == "json" or fmt is not OutputFormat.TEXT:
        payload = {
            "pack_a": {
                "id": diff.pack_a_id,
                "name": diff.pack_a_name,
                "composition": _composition_to_dict(diff.composition_a),
            },
            "pack_b": {
                "id": diff.pack_b_id,
                "name": diff.pack_b_name,
                "composition": _composition_to_dict(diff.composition_b),
            },
            "added": diff.added,
            "removed": diff.removed,
            "unchanged": diff.unchanged,
            "changed": [
                {
                    "name": c.name,
                    "fields": _changed_fields_to_dict(c, trait_delta(c)),
                }
                for c in diff.changed
            ],
        }
        if diff_format == "json" and fmt is OutputFormat.TEXT:
            print(json.dumps(payload, indent=2))
        else:
            emit(fmt, message="pack_diff", extra=payload)
        return 0

    _print_pack_diff_text(diff)
    return 0


def _composition_to_dict(c: CompositionStats) -> dict[str, Any]:
    return {
        "persona_count": c.persona_count,
        "age_min": c.age_min,
        "age_max": c.age_max,
        "age_mean": c.age_mean,
        "gender_split": c.gender_split,
        "role_distribution": c.role_distribution,
    }


def _changed_fields_to_dict(
    change: PersonaChange,
    trait_added_removed: tuple[list[str], list[str]],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    added_traits, removed_traits = trait_added_removed
    for key, vals in change.changed.items():
        if key == "personality_traits":
            out[key] = {
                "a": vals.get("a"),
                "b": vals.get("b"),
                "added": added_traits,
                "removed": removed_traits,
            }
        else:
            out[key] = {"a": vals.get("a"), "b": vals.get("b")}
    return out


def _print_pack_diff_text(diff: PackDiff) -> None:
    a_count = diff.composition_a.persona_count
    b_count = diff.composition_b.persona_count
    print(f"Pack A: {diff.pack_a_id}  ({diff.pack_a_name}, {a_count} personas)")
    print(f"Pack B: {diff.pack_b_id}  ({diff.pack_b_name}, {b_count} personas)")

    print()
    print("── Composition " + "─" * 37)
    print(_compose_line("Personas", a_count, b_count))
    print(_compose_line("Age range", _age_range_str(diff.composition_a), _age_range_str(diff.composition_b)))
    print(_compose_line("Age mean", diff.composition_a.age_mean or "—", diff.composition_b.age_mean or "—"))
    if diff.composition_a.gender_split or diff.composition_b.gender_split:
        print(
            _compose_line(
                "Gender",
                _bucket_str(diff.composition_a.gender_split) or "—",
                _bucket_str(diff.composition_b.gender_split) or "—",
            )
        )
    if diff.composition_a.role_distribution or diff.composition_b.role_distribution:
        print()
        print("── Role distribution " + "─" * 31)
        print(f"  A: {_bucket_str(diff.composition_a.role_distribution) or '(none)'}")
        print(f"  B: {_bucket_str(diff.composition_b.role_distribution) or '(none)'}")

    print()
    print("── Personas " + "─" * 40)
    print(f"  Added in B:    {len(diff.added)}")
    if diff.added:
        for name in diff.added:
            print(f"    + {name}")
    print(f"  Removed in B:  {len(diff.removed)}")
    if diff.removed:
        for name in diff.removed:
            print(f"    - {name}")
    print(f"  Unchanged:     {len(diff.unchanged)}")
    print(f"  Changed:       {len(diff.changed)}")

    if diff.changed:
        print()
        print("── Changed personas " + "─" * 32)
        for change in diff.changed:
            print(f"\n  {change.name}")
            for key, vals in change.changed.items():
                if key == "personality_traits":
                    added, removed = trait_delta(change)
                    if added:
                        print(f"    + traits: {', '.join(added)}")
                    if removed:
                        print(f"    - traits: {', '.join(removed)}")
                elif key == "background":
                    a_text = vals.get("a") or ""
                    b_text = vals.get("b") or ""
                    a_excerpt = _excerpt(str(a_text), 60)
                    b_excerpt = _excerpt(str(b_text), 60)
                    print(f"    {key}:")
                    print(f"      A: {a_excerpt}")
                    print(f"      B: {b_excerpt}")
                else:
                    print(f"    {key}: {vals.get('a')!r} → {vals.get('b')!r}")


def _age_range_str(c: CompositionStats) -> str:
    if c.age_min is None or c.age_max is None:
        return "—"
    return f"{c.age_min}-{c.age_max}"


def _bucket_str(buckets: dict[str, int]) -> str:
    if not buckets:
        return ""
    items = sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))
    return " ".join(f"{k}({v})" for k, v in items)


def _compose_line(label: str, a: object, b: object) -> str:
    eq = "=" if str(a) == str(b) else "→"
    return f"  {label:<14} {a!s:<22} {eq}  {b}"


def _excerpt(text: str, n: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= n:
        return collapsed or "(empty)"
    return collapsed[:n] + "…"


def handle_pack_generate(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Generate a persona pack using an LLM."""
    from althing.llm.models import InputMessage, TextBlock
    from althing.mcp.data import PackValidationError, save_persona_pack
    from althing.structured.output import StructuredOutputConfig, StructuredOutputEngine

    count = args.count
    if count < 1 or count > 50:
        print("Error: --count must be between 1 and 50.", file=sys.stderr)
        return 1

    model = _resolve_model(args)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "personas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Full name."},
                        "age": {"type": "integer", "description": "Age in years."},
                        "occupation": {"type": "string", "description": "Job title or role."},
                        "background": {
                            "type": "string",
                            "description": "One-paragraph professional and personal background.",
                        },
                        "personality_traits": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "3-5 personality traits relevant to their product usage.",
                        },
                    },
                    "required": ["name", "age", "occupation", "background", "personality_traits"],
                },
            },
        },
        "required": ["personas"],
    }

    prompt = (
        f"Generate exactly {count} diverse, realistic personas for a synthetic focus group.\n\n"
        f"Product/service: {args.product}\n"
        f"Target audience: {args.audience}\n\n"
        "Requirements:\n"
        f"- Generate exactly {count} personas\n"
        "- Each persona needs: name, age, occupation, background (one paragraph), "
        "and 3-5 personality_traits\n"
        "- Personas should be diverse in age, occupation, background, and perspective\n"
        "- Backgrounds should be specific and relevant to the product/audience context\n"
        "- Personality traits should reflect how they'd engage with the product\n"
        "- Names should reflect diverse cultural backgrounds"
    )

    client = LLMClient()
    engine = StructuredOutputEngine(client)
    config = StructuredOutputConfig(
        schema=schema,
        tool_name="generate_personas",
        tool_description="Generate a list of diverse personas for a synthetic focus group.",
    )

    try:
        result = engine.extract(
            model=model,
            max_tokens=4096,
            messages=[InputMessage(role="user", content=[TextBlock(text=prompt)])],
            config=config,
            temperature=1.0,
        )
    except Exception as exc:
        print(f"Error calling LLM: {exc}", file=sys.stderr)
        return 1

    if result.is_fallback:
        print(f"Error: LLM did not produce valid structured output: {result.error}", file=sys.stderr)
        return 1

    personas = result.data.get("personas", [])
    if not personas:
        print("Error: LLM returned no personas.", file=sys.stderr)
        return 1

    pack_name = args.name or f"{args.product[:60]} personas"

    try:
        saved = save_persona_pack(pack_name, personas, pack_id=args.pack_id)
    except PackValidationError as exc:
        print(f"Validation error: {exc}", file=sys.stderr)
        return 1

    if fmt is OutputFormat.TEXT:
        print(f"Generated pack '{saved['name']}' ({saved['persona_count']} personas) as {saved['id']}")
        print(f"Saved to: {saved['path']}")
    else:
        emit(fmt, message="Pack generated", extra=saved)

    return 0


# ---------------------------------------------------------------------------
# Pack calibrate (sp-sghl)
# ---------------------------------------------------------------------------


def _render_calibration_question(question_text: str, options: list[str]) -> str:
    """Render the panelist-facing prompt: the real question + its real options.

    ``build_question_prompt`` shows only a question's ``text`` to the panel, so
    the answer choices must be embedded in the text itself for the panelist to
    see them. Returns the bare question when no options are supplied.
    """
    if not options:
        return question_text
    rendered = "\n".join(f"- {opt}" for opt in options)
    return f"{question_text}\n\nAnswer with exactly one of the following options:\n{rendered}"


def _build_calibration_instrument(baseline: dict[str, Any], against: str) -> dict[str, Any]:
    """Build the single-question instrument for a calibration panel.

    Uses the **real** survey question text and answer options from the
    SynthBench baseline. When the baseline carries no question text (an older
    SynthBench that predates the ``question_text``/``options`` fields, or a
    gated dataset that withholds the wording), this **refuses** — it raises
    rather than fabricate a placeholder prompt. Fabricating and then scoring
    the panel against the real ``human_distribution`` would measure a question
    the personas never actually saw (cross-repo P0-3).

    The real option labels are attached as an ``enum`` ``response_schema`` so
    the question is a bounded (tracked) calibration item and answers are
    coerced to the same category space as the baseline's ``human_distribution``
    (SynthBench guarantees ``options`` align with the distribution keys).
    """
    question_text = baseline.get("question_text") or baseline.get("prompt")
    if not question_text or not str(question_text).strip():
        raise RuntimeError(
            f"SynthBench baseline for {against!r} did not include question text; "
            "refusing to fabricate a placeholder prompt and score panelists "
            "against real human ground truth. Upgrade SynthBench to a version "
            "that exposes question_text/options, or pick a full-tier dataset "
            "(e.g. gss, ntia) whose question text is redistributable."
        )
    question_text = str(question_text).strip()

    raw_options = baseline.get("options")
    options: list[str] = []
    if isinstance(raw_options, list):
        options = [str(o).strip() for o in raw_options if isinstance(o, str) and str(o).strip()]

    question: dict[str, Any] = {"text": _render_calibration_question(question_text, options)}
    if options:
        question["response_schema"] = {"type": "enum", "options": options}

    return {"instrument": {"version": 1, "questions": [question]}}


def _run_calibration_panel(
    *,
    pack_yaml_path: str,
    against: str,
    n: int,
    samples_per_question: int,
    models: str | None,
) -> dict[str, Any]:
    """Drive the actual calibration run and return a dict the YAML writer consumes.

    Shells out to ``althing panel run`` so this command composes from the
    same orchestrator the user would invoke manually. Tests monkeypatch this
    function, so the real subprocess path is exercised only at integration
    time.

    The returned dict has keys: ``jsd``, ``extractor``, ``models`` (list of
    ``model:weight`` strings), ``panelist_cost_usd``, and optionally
    ``alignment_error``.
    """
    import contextlib
    import subprocess
    import tempfile

    # The instrument used here is a single question whose text + answer
    # options come from the SynthBench baseline payload. If the baseline does
    # not carry the real question text we REFUSE (see
    # ``_build_calibration_instrument``) rather than fabricate a placeholder
    # and score the panel against real human ground truth. The panelists'
    # answers are bounded to the real option labels (which SynthBench aligns
    # with the ``human_distribution`` keys) so the panel distribution and the
    # human distribution share a category space and the JSD is meaningful.
    try:
        baseline = load_synthbench_baseline(against)
    except SynthbenchUnavailableError as exc:
        raise RuntimeError(str(exc)) from exc

    instrument_payload = _build_calibration_instrument(baseline, against)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as inst_fh:
        yaml.safe_dump(instrument_payload, inst_fh, sort_keys=False)
        instrument_path = inst_fh.name

    try:
        cmd = [
            sys.executable,
            "-m",
            "althing",
            "--output-format",
            "json",
            "panel",
            "run",
            "--personas",
            pack_yaml_path,
            "--instrument",
            instrument_path,
            "--n",
            str(n),
            "--samples-per-question",
            str(samples_per_question),
            "--calibrate-against",
            against,
        ]
        if models:
            cmd.extend(["--models", models])

        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(instrument_path)

    if proc.returncode != 0:
        raise RuntimeError(f"panel run for calibration failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}")

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse panel run JSON output: {exc}") from exc

    convergence = payload.get("convergence") or {}
    per_question = convergence.get("per_question") or {}
    if not per_question:
        raise RuntimeError("panel run produced no convergence.per_question — calibration JSD unavailable")
    # We expect exactly one tracked question (instrument has one).
    first_key = next(iter(per_question))
    calib = (per_question[first_key] or {}).get("calibration") or {}
    if "jsd" not in calib:
        raise RuntimeError("panel run convergence report has no calibration.jsd — was --calibrate-against rejected?")

    panelist_cost_usd = 0.0
    cost_section = payload.get("total_cost") or payload.get("cost") or {}
    if isinstance(cost_section, dict):
        panelist_cost_usd = float(
            cost_section.get("usd") or cost_section.get("total_usd") or cost_section.get("cost_usd") or 0.0
        )

    cramers_v_raw = calib.get("cramers_v")
    cramers_v: float | None
    if cramers_v_raw is None:
        cramers_v = None
    else:
        try:
            cramers_v = float(cramers_v_raw)
        except (TypeError, ValueError):
            cramers_v = None

    return {
        "jsd": float(calib["jsd"]),
        "cramers_v": cramers_v,
        "extractor": calib.get("extractor") or "pick_one:auto-derived",
        "models": _split_models_arg(models) if models else _default_models_list(payload),
        "panelist_cost_usd": panelist_cost_usd,
        "alignment_error": calib.get("alignment_error"),
        "lead_interpretation": calib.get("lead_interpretation"),
    }


def _split_models_arg(models: str | None) -> list[str]:
    """Split a comma-separated --models argument into a list."""
    if not models:
        return []
    return [m.strip() for m in models.split(",") if m.strip()]


def _calibration_effect_bucket(v: float) -> str:
    """Cohen-style label for a Cramer's V effect size (GH-313)."""
    if v < 0.1:
        return "negligible"
    if v < 0.3:
        return "small"
    if v < 0.5:
        return "medium"
    return "large"


def _default_models_list(payload: dict[str, Any]) -> list[str]:
    """Best-effort recovery of the default model used when --models was not set."""
    model = payload.get("model")
    if isinstance(model, str) and model:
        return [model]
    metadata = payload.get("metadata") or {}
    model_meta = metadata.get("model")
    if isinstance(model_meta, str) and model_meta:
        return [model_meta]
    return []


def handle_pack_calibrate(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Calibrate a persona pack against a SynthBench baseline (sp-sghl).

    Orchestrates a panel run with ``--calibrate-against`` and writes the
    resulting JSD into the pack YAML's ``calibration:`` list. Re-running
    against the same dataset+question replaces the prior entry.
    """
    from althing import calibration as calib_mod
    from althing.__version__ import __version__

    pack_yaml_path = args.pack_yaml
    against = args.against
    output_path = args.output or pack_yaml_path
    dry_run = bool(args.dry_run)
    yes = bool(args.yes)
    debug = bool(getattr(args, "debug", False))

    try:
        # ── Validate --against spec + allowlist before any work ───────────
        dataset, sep, question = against.partition(":")
        if not sep or not dataset or not question:
            print(
                "Error: --against requires DATASET:QUESTION (colon-separated, both non-empty).",
                file=sys.stderr,
            )
            return 2
        if dataset not in _INLINE_CALIBRATION_ALLOWED and os.environ.get("SYNTHBENCH_ALLOW_GATED") != "1":
            allowed = ", ".join(sorted(_INLINE_CALIBRATION_ALLOWED))
            print(
                f"Error: --against only supports inline-publishable datasets ({allowed}). "
                f"For gated datasets use post-hoc calibration.",
                file=sys.stderr,
            )
            return 2

        # ── Load + validate the pack YAML ────────────────────────────────
        if not Path(pack_yaml_path).exists():
            print(f"Error: file not found: {pack_yaml_path}", file=sys.stderr)
            return 2
        try:
            raw_text, parsed = calib_mod.load_pack_yaml(pack_yaml_path)
        except (OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

        # ── Run the calibration panel ────────────────────────────────────
        try:
            run_result = _run_calibration_panel(
                pack_yaml_path=pack_yaml_path,
                against=against,
                n=int(args.n),
                samples_per_question=int(args.samples_per_question),
                models=args.models,
            )
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        # ── Build the calibration entry ──────────────────────────────────
        run_cramers_v = run_result.get("cramers_v")
        entry = calib_mod.CalibrationEntry(
            dataset=dataset,
            question=question,
            jsd=round(float(run_result["jsd"]), 6),
            n=int(args.n),
            samples_per_question=int(args.samples_per_question),
            models=list(run_result.get("models") or []),
            extractor=str(run_result.get("extractor") or "pick_one:auto-derived"),
            panelist_cost_usd=round(float(run_result.get("panelist_cost_usd") or 0.0), 4),
            calibrated_at=calib_mod.now_iso_utc(),
            althing_version=str(__version__),
            alignment_error=run_result.get("alignment_error"),
            cramers_v=round(float(run_cramers_v), 6) if run_cramers_v is not None else None,
        )
        new_dict = entry.to_yaml_dict()
        new_yaml = calib_mod.update_pack_calibration_text(raw_text, parsed, new_dict)

        # ── Dry-run: print, don't write ──────────────────────────────────
        if dry_run:
            if fmt is OutputFormat.TEXT:
                print(f"# DRY RUN — would write to {output_path}")
                print(new_yaml, end="")
            else:
                emit(
                    fmt,
                    message="Dry-run preview",
                    extra={
                        "pack_yaml": pack_yaml_path,
                        "output": output_path,
                        "calibration_entry": new_dict,
                        "rendered_yaml": new_yaml,
                    },
                )
            return 0

        # ── Confirm overwrite when output path already exists ────────────
        out_exists = Path(output_path).exists()
        if out_exists and not yes and sys.stdin.isatty():
            prompt = f"Overwrite {output_path}? [y/N]: "
            try:
                answer = input(prompt).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.", file=sys.stderr)
                return 1
            if answer not in {"y", "yes"}:
                print("Aborted.", file=sys.stderr)
                return 1

        try:
            Path(output_path).write_text(new_yaml, encoding="utf-8")
        except OSError as exc:
            print(f"Error: failed to write {output_path}: {exc}", file=sys.stderr)
            return 1

        if fmt is OutputFormat.TEXT:
            # GH-313: lead with effect size (Cramer's V) so the reader's first
            # impression is "how different are these distributions?" rather
            # than "what's the JSD scalar?". JSD is retained as the secondary
            # check on distributional shape.
            lead = run_result.get("lead_interpretation")
            if entry.cramers_v is not None:
                effect_label = _calibration_effect_bucket(entry.cramers_v)
                summary = (
                    f"Wrote calibration entry ({dataset}:{question}): "
                    f"Cramer's V={entry.cramers_v:.3f} ({effect_label}); "
                    f"JSD={entry.jsd} → {output_path}"
                )
            else:
                summary = f"Wrote calibration entry ({dataset}:{question}, JSD {entry.jsd}) → {output_path}"
            print(summary)
            if isinstance(lead, str) and lead:
                print(f"  {lead}")
        else:
            emit(
                fmt,
                message="Pack calibrated",
                extra={
                    "pack_yaml": pack_yaml_path,
                    "output": output_path,
                    "calibration_entry": new_dict,
                },
            )
        return 0
    except Exception as exc:
        if debug:
            raise
        print(
            f"Error: unexpected failure: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


def handle_mcp_serve(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Start the MCP server on stdio transport.

    sy-xyn: guard the import so a base install (no ``[mcp]`` extra)
    surfaces an actionable error instead of a raw Python traceback. This
    matters most when the server is launched indirectly by an editor
    host (Claude Code, Cursor) — hosts only relay stderr, and a
    traceback there reads as a generic "server failed to start" with no
    fix path. Probe first via :func:`mcp_install.mcp_extra_available`
    (added in #512) and fall back to a defensive ``ModuleNotFoundError``
    catch for partial-install edge cases.
    """
    from althing.cli import mcp_install as helper

    if not helper.mcp_extra_available():
        print(f"Error: {helper.MISSING_MCP_EXTRA_MESSAGE}", file=sys.stderr)
        return 1

    try:
        from althing.mcp.server import serve
    except ModuleNotFoundError as exc:
        # Defensive fallback: the find_spec probe above catches the
        # common case (extra wasn't installed). This handles a partial
        # install where ``mcp`` itself is importable but a submodule
        # isn't — e.g. a wheel pinned to a pre-FastMCP version. Same
        # actionable copy so the fix path stays one-line.
        if exc.name and exc.name.startswith("mcp"):
            print(
                f"Error: {helper.MISSING_MCP_EXTRA_MESSAGE} (import error: {exc.name})",
                file=sys.stderr,
            )
            return 1
        raise

    serve()
    return 0


_MCP_INSTALL_KEY_NOTE = (
    "No API key was written to the config. Sampling-capable hosts "
    "(Claude Code, Claude Desktop, Cursor, Windsurf) can run panels with "
    "no key at all; otherwise set a provider key (e.g. ANTHROPIC_API_KEY) "
    "in your environment or run `althing login` — see the README's "
    "'LLM Provider Support' section."
)


def _emit_mcp_install_result(
    result: Any,
    fmt: OutputFormat,
    dry_run: bool,
    host: Any | None,
) -> None:
    """Render a single InstallResult (shared by explicit and auto mode)."""
    from althing.cli import mcp_install as helper

    if fmt is OutputFormat.JSON:
        # JSON mode already carries the resulting-config snapshot in the
        # payload (see helper.format_json); a single line on stdout
        # preserves the existing machine-readable contract.
        print(helper.format_json(result))
        return
    if dry_run:
        # sy-0k2 / gh-495: surface the actual config payload so dry-run
        # output is usable by agents and shell pipelines. Human prose
        # goes to stderr; stdout is pretty-printed JSON of the resulting
        # config so callers can pipe through `jq`, diff, or write it to
        # the target themselves.
        print(helper.format_text(result), file=sys.stderr)
        if result.warnings:
            for warning in result.warnings:
                print(f"Warning: {warning}", file=sys.stderr)
        rendered = helper.format_resulting_config(result)
        if rendered is not None:
            print(rendered)
        return
    print(helper.format_text(result))
    if result.warnings:
        for warning in result.warnings:
            print(f"Warning: {warning}", file=sys.stderr)
    if result.action in ("installed", "updated", "removed"):
        # synthbench#262: the host only re-reads its MCP config on launch.
        label = host.label if host is not None else "your MCP host"
        print(f"Restart {label} to pick up the server.")


def handle_mcp_install(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Register althing as an MCP server in a host's JSON config (sy-skf).

    sy-xyn: refuse by default to write a host config that points at a
    ``althing mcp-serve`` command which will fail at runtime because
    the ``mcp`` extra isn't installed in this env. ``--uninstall`` is
    always allowed (so users can clean up a broken config) and
    ``--allow-missing-extra`` opts out for cross-machine workflows.

    synthbench#262: ``--host`` selects a named host (claude-code,
    claude-desktop, cursor, windsurf, zed — Zed uses the
    ``context_servers`` schema) or ``auto`` to detect installed hosts and
    confirm each write. ``althing mcp uninstall`` routes here too with
    ``args.uninstall`` pre-set.
    """
    from althing.cli import mcp_install as helper

    name = getattr(args, "name", None) or helper.DEFAULT_SERVER_NAME
    dry_run = bool(getattr(args, "dry_run", False))
    allow_missing_extra = bool(getattr(args, "allow_missing_extra", False))
    is_uninstall = bool(getattr(args, "uninstall", False))
    host_arg = getattr(args, "host", None)
    target_arg = getattr(args, "target", None)
    scope = getattr(args, "scope", "user") or "user"
    assume_yes = bool(getattr(args, "yes", False))

    # sy-xyn: pre-flight extra check. Skip for uninstall (always safe)
    # and for callers who explicitly opted out via --allow-missing-extra.
    # The probe (added in #512 as mcp_extra_available) tells us whether
    # writing this config would point the host at a `althing mcp-serve`
    # that crashes at launch.
    if not is_uninstall and not allow_missing_extra and not helper.mcp_extra_available():
        print(
            f"Error: refusing to install MCP config — {helper.MISSING_MCP_EXTRA_MESSAGE}. "
            "Pass --allow-missing-extra to write the config anyway "
            "(useful when the host runs althing in a different env).",
            file=sys.stderr,
        )
        return 1

    # Build the worklist of (host-or-None, config-path) pairs.
    worklist: list[tuple[Any | None, Path]] = []
    if target_arg:
        # Explicit path override: host-agnostic, mcpServers schema.
        worklist.append((None, Path(target_arg).expanduser()))
    elif host_arg == "auto":
        detected = helper.detect_hosts()
        if not detected:
            print(
                "Error: no known MCP host configs detected (looked for "
                "Claude Code, Claude Desktop, Cursor, Windsurf, Zed user "
                "configs). Pass --host <name> to create one explicitly, or "
                "--target PATH for any other host.",
                file=sys.stderr,
            )
            return 1
        verb = "Remove althing from" if is_uninstall else "Install althing into"
        print(
            "Detected MCP host configs: " + ", ".join(f"{h.label} ({p})" for h, p in detected),
            file=sys.stderr,
        )
        for host, path in detected:
            if assume_yes or dry_run:
                worklist.append((host, path))
                continue
            if not sys.stdin.isatty():
                print(
                    "Error: --host auto needs confirmation for each detected "
                    "host but stdin is not a TTY. Pass --yes to accept all, "
                    "or pick one host with --host <name>.",
                    file=sys.stderr,
                )
                return 1
            answer = input(f"{verb} {path} ({host.label})? [y/N] ").strip().lower()
            if answer in ("y", "yes"):
                worklist.append((host, path))
        if not worklist:
            print("Nothing to do.", file=sys.stderr)
            return 0
    elif host_arg:
        host = helper.HOSTS[host_arg]
        try:
            path = helper.host_config_path(host, scope)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        worklist.append((host, path))
    else:
        # Legacy default: Claude Code, honoring --scope (user → ~/.claude.json,
        # project → ./.mcp.json).
        worklist.append((helper.HOSTS["claude-code"], helper.resolve_target(scope, None)))

    env: dict[str, str] = {}
    command = ""
    if not is_uninstall:
        try:
            env = helper.parse_env_pairs(getattr(args, "mcp_env", None))
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        command = helper.resolve_command(getattr(args, "mcp_command_override", None))

    wrote_install = False
    for host, target in worklist:
        config_key = host.config_key if host is not None else "mcpServers"
        entry_extra = host.entry_extra if host is not None else ()
        try:
            if is_uninstall:
                result = helper.uninstall(
                    target=target,
                    name=name,
                    dry_run=dry_run,
                    config_key=config_key,
                )
            else:
                result = helper.install(
                    target=target,
                    name=name,
                    command=command,
                    env=env,
                    force=bool(getattr(args, "force", False)),
                    dry_run=dry_run,
                    config_key=config_key,
                    entry_extra=entry_extra,
                )
        except FileExistsError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"Error: failed to parse {target}: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"Error: failed to write {target}: {exc}", file=sys.stderr)
            return 1

        _emit_mcp_install_result(result, fmt, dry_run, host)
        if result.action in ("installed", "updated"):
            wrote_install = True

    # synthbench#262: never bake secrets in by default — when the entry was
    # written without an env block, point at `althing login` instead.
    if wrote_install and not env and fmt is not OutputFormat.JSON:
        print(f"Note: {_MCP_INSTALL_KEY_NOTE}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# install-skills (sy-65k74)
# ---------------------------------------------------------------------------

_INSTALL_SKILLS_COMMANDS = ["althing-poll.md"]
_INSTALL_SKILLS_SKILLS = [
    "concept-test",
    "focus-group",
    "name-test",
    "pricing-probe",
    "survey-prescreen",
    "althing",
]


def handle_install_skills(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Copy bundled slash commands and skills into a Claude Code target directory."""
    from importlib.resources import files as _resource_files

    target = Path(getattr(args, "target", None) or Path.home() / ".claude").expanduser()
    pkg = _resource_files("althing.agent_assets")

    installed: list[str] = []
    errors: list[str] = []

    commands_dst = target / "commands"
    commands_dst.mkdir(parents=True, exist_ok=True)
    for name in _INSTALL_SKILLS_COMMANDS:
        src = pkg / "commands" / name
        dst = commands_dst / name
        try:
            dst.write_bytes(src.read_bytes())
            installed.append(f"commands/{name}")
        except Exception as exc:
            errors.append(f"commands/{name}: {exc}")

    skills_dst = target / "skills"
    for skill in _INSTALL_SKILLS_SKILLS:
        skill_dir = skills_dst / skill
        skill_dir.mkdir(parents=True, exist_ok=True)
        src = pkg / "skills" / skill / "SKILL.md"
        dst = skill_dir / "SKILL.md"
        try:
            dst.write_bytes(src.read_bytes())
            installed.append(f"skills/{skill}/SKILL.md")
        except Exception as exc:
            errors.append(f"skills/{skill}/SKILL.md: {exc}")

    if fmt is OutputFormat.JSON:
        payload: dict[str, Any] = {"target": str(target), "installed": installed}
        if errors:
            payload["errors"] = errors
        print(json.dumps(payload))
    else:
        if installed:
            print(f"Installed {len(installed)} file(s) to {target}:")
            for f in installed:
                print(f"  {f}")
        if errors:
            for e in errors:
                print(f"ERROR: {e}", file=sys.stderr)

    return 1 if errors else 0


# ---------------------------------------------------------------------------
# Analyze subgroup (GH-341)
# ---------------------------------------------------------------------------


def _load_persona_pack(pack_ref: str) -> list[dict[str, Any]]:
    """Load personas from a pack ID or YAML file path."""
    import yaml

    p = Path(pack_ref).expanduser()
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "personas" in data:
            return data["personas"]
        raise ValueError(f"Invalid personas file {pack_ref!r}: expected list or dict with 'personas' key")

    from althing.mcp.data import get_persona_pack

    try:
        pack_data = get_persona_pack(pack_ref)
        return pack_data.get("personas", [])
    except (FileNotFoundError, KeyError):
        raise FileNotFoundError(f"Persona pack not found: {pack_ref!r}") from None


def handle_analyze_subgroup(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Subgroup breakdown: group panel responses by a persona field (GH-341)."""
    import json as _json

    from althing.analysis.subgroup_cli import (
        analyze_subgroup,
        format_subgroup_json,
        format_subgroup_text,
    )

    result_ref = args.result
    by_raw: str = args.by
    fields = [f.strip() for f in by_raw.split(",") if f.strip()]
    personas_ref = getattr(args, "personas", None)
    output_fmt = getattr(args, "output", "text") or "text"

    # Load panel result
    path = Path(result_ref).expanduser()
    if path.exists() and path.suffix == ".json":
        data = _json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("id", path.stem)
    else:
        from althing.mcp.data import get_panel_result

        try:
            data = get_panel_result(result_ref)
        except FileNotFoundError:
            print(f"Error: panel result not found: {result_ref}", file=sys.stderr)
            return 1

    personas_list: list[dict[str, Any]] = []
    if personas_ref:
        try:
            personas_list = _load_persona_pack(personas_ref)
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    analysis = analyze_subgroup(data, personas_list, fields)

    if not personas_ref and any("No panelist names matched" in w for w in analysis.global_warnings):
        print(
            "Error: pass --personas <pack_or_file> to supply persona attributes for --by analysis.",
            file=sys.stderr,
        )
        return 1

    if output_fmt == "json":
        print(_json.dumps(format_subgroup_json(analysis), indent=2))
    else:
        print(format_subgroup_text(analysis))
    return 0


# Cost subcommands (sy-kmw1)
# ---------------------------------------------------------------------------


def handle_cost_summary(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Aggregate cost across saved panel runs and print a summary."""
    from althing import cost_summary as cs

    runs_dir_arg = getattr(args, "runs_dir", None)
    runs_dir = Path(runs_dir_arg).expanduser() if runs_dir_arg else cs.default_runs_dir()

    since_arg = getattr(args, "since", None)
    since: datetime | None = None
    if since_arg:
        try:
            since = cs.parse_since(since_arg)
        except ValueError as exc:
            print(f"Error: invalid --since value {since_arg!r}: {exc}", file=sys.stderr)
            return 1

    parsed: list[cs.RunInfo] = []
    for path in cs.discover_run_files(runs_dir):
        info = cs.parse_run(path)
        if info is not None:
            parsed.append(info)
    report = cs.aggregate_runs(parsed, since=since)

    group_by = getattr(args, "by", "model") or "model"
    cost_format = getattr(args, "cost_format", "text") or "text"

    if cost_format == "json":
        print(json.dumps(cs.to_json_payload(report, group_by=group_by), indent=2))
    else:
        print(cs.format_text(report, group_by=group_by))
    return 0


def handle_cost_show(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Per-run cost breakdown for one saved result (gh-r4).

    Thin alias over the cost section of ``panel inspect`` — same result
    resolution (ID or JSON path), same :class:`InspectReport` data, but
    only the cost/usage fields. ``cost summary`` aggregates across runs;
    this answers "what did run X cost?" without wading through the full
    inspect output.
    """
    from althing.analysis.inspect import build_inspect_report

    result_ref = args.result
    path = Path(result_ref)
    if path.exists() and path.suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            msg = f"Error: not valid JSON: {result_ref}: {exc}"
            if fmt is OutputFormat.TEXT:
                print(msg, file=sys.stderr)
            else:
                emit(fmt, message=msg, extra={"error": "invalid_json"})
            return 1
        data.setdefault("id", path.stem)
    else:
        from althing.mcp.data import get_panel_result

        try:
            data = get_panel_result(result_ref)
        except FileNotFoundError:
            msg = f"Error: panel result not found: {result_ref}"
            if fmt is OutputFormat.TEXT:
                print(msg, file=sys.stderr)
            else:
                emit(fmt, message=msg, extra={"error": "not_found"})
            return 1

    if not isinstance(data, dict):
        msg = "Error: panel result is not a JSON object"
        if fmt is OutputFormat.TEXT:
            print(msg, file=sys.stderr)
        else:
            emit(fmt, message=msg, extra={"error": "invalid_shape"})
        return 1

    report = build_inspect_report(data)
    cost_payload: dict[str, Any] = {
        "result_id": report.result_id,
        "total_cost": report.total_cost,
        "panelist_cost": report.panelist_cost,
        "total_tokens": report.total_tokens,
        "per_model": [
            {
                "model": m.model,
                "total_tokens": m.total_tokens,
                "input_tokens": m.input_tokens,
                "output_tokens": m.output_tokens,
                "cost_usd": m.cost_usd,
            }
            for m in report.model_rollup
        ],
        "synthesis": {
            "ran": report.synthesis.ran,
            "model": report.synthesis.model,
            "cost": report.synthesis.cost,
        },
    }

    if fmt is OutputFormat.TEXT:
        print(f"Cost for {report.result_id or result_ref}")
        print(f"  Total cost:    {report.total_cost or '(not recorded)'}")
        if report.panelist_cost:
            print(f"  Panelist cost: {report.panelist_cost}")
        if report.synthesis.ran:
            synth_line = report.synthesis.cost or "(not recorded)"
            if report.synthesis.model:
                synth_line += f" ({report.synthesis.model})"
            print(f"  Synthesis:     {synth_line}")
        else:
            print("  Synthesis:     not run")
        print(f"  Total tokens:  {report.total_tokens}")
        if report.model_rollup:
            print("  Per model:")
            for m in report.model_rollup:
                cost_str = f"${m.cost_usd:.4f}" if m.cost_usd is not None else "(unknown)"
                print(
                    f"    {m.model}: {cost_str} "
                    f"(tokens={m.total_tokens} input={m.input_tokens} output={m.output_tokens})"
                )
    else:
        emit(fmt, message="Cost show", extra={"cost": cost_payload})

    return 0


# ---------------------------------------------------------------------------
# Instruments subcommands (sp-xsu)
# ---------------------------------------------------------------------------


def handle_instruments_list(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """List installed instrument packs."""
    from althing.mcp.data import list_instrument_packs

    packs = list_instrument_packs()

    if fmt is OutputFormat.TEXT:
        if not packs:
            print("No instrument packs installed.")
            print("Install one with: althing instruments install <file.yaml>")
        else:
            for p in packs:
                version = f" v{p['version']}" if p.get("version") else ""
                desc = f" — {p['description']}" if p.get("description") else ""
                print(f"  {p['id']}{version}{desc}")
    else:
        emit(fmt, message="Instrument packs", extra={"packs": packs})
    return 0


def handle_instruments_install(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Install an instrument pack from a YAML file (or bundled name)."""
    from althing.mcp.data import load_instrument_pack, save_instrument_pack

    source = args.source
    data: dict | None = None

    if Path(source).exists():
        try:
            data = _load_yaml(source)
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error loading file: {exc}", file=sys.stderr)
            return 1
    else:
        # Try loading as a bundled pack name.
        try:
            data = load_instrument_pack(source)
        except FileNotFoundError:
            from althing.mcp.data import list_instrument_packs

            bundled = [p["id"] for p in list_instrument_packs() if p.get("source") == "bundled"]
            hint = ", ".join(bundled) if bundled else "(none)"
            print(
                f"Error: '{source}' is not a file path or known bundled pack.\n"
                f"Available bundled packs: {hint}\n"
                f"Or pass a YAML file path.",
                file=sys.stderr,
            )
            return 1

    if not isinstance(data, dict):
        print("Error: instrument pack file must be a YAML mapping", file=sys.stderr)
        return 1

    # Validate by parsing — bad instruments must fail before install.
    raw = data.get("instrument", data)
    raw.setdefault("version", 1)
    try:
        parse_instrument(raw)
    except InstrumentError as exc:
        print(f"Error: instrument validation failed: {exc}", file=sys.stderr)
        return 1

    pack_name = args.name or data.get("name") or Path(source).stem
    meta = save_instrument_pack(pack_name, data)

    if fmt is OutputFormat.TEXT:
        print(f"Installed instrument pack '{meta['id']}' -> {meta['path']}")
    else:
        emit(fmt, message="Instrument pack installed", extra=meta)
    return 0


def handle_instruments_show(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Print an installed instrument pack's contents."""
    from althing.mcp.data import load_instrument_pack

    try:
        data = load_instrument_pack(args.name)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if fmt is OutputFormat.TEXT:
        print(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), end="")
    else:
        emit(fmt, message="Instrument pack", extra={"name": args.name, "data": data})
    return 0


def handle_instruments_graph(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Render the round DAG for an instrument file or installed pack name."""
    try:
        instrument = _load_instrument(args.source)
    except (FileNotFoundError, ValueError, InstrumentError) as exc:
        print(f"Error loading instrument: {exc}", file=sys.stderr)
        return 1

    for w in instrument.warnings:
        logger.warning("instrument: %s", w)

    rendered = (
        _render_mermaid(instrument) if getattr(args, "format", "text") == "mermaid" else _render_text_dag(instrument)
    )

    if fmt is OutputFormat.TEXT:
        print(rendered)
    else:
        emit(
            fmt,
            message="Instrument graph",
            extra={
                "rounds": [r.name for r in instrument.rounds],
                "graph": rendered,
                "warnings": instrument.warnings,
            },
        )
    return 0


def handle_domains_list(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """List bundled domain prompt templates."""
    from althing.domains import list_domain_templates

    domains = list_domain_templates()

    if fmt is OutputFormat.TEXT:
        if not domains:
            print("No domain templates registered.")
            return 0
        width = max(len(d["name"]) for d in domains)
        for d in domains:
            print(f"  {d['name']:<{width}}  {d['description']}")
    else:
        emit(fmt, message="Domain templates", extra={"domains": domains})
    return 0


def handle_domains_inspect(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Print a domain template's full prompt body and metadata."""
    from althing.domains import DomainNotFoundError, get_domain_template

    try:
        template = get_domain_template(args.name)
    except DomainNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if fmt is OutputFormat.TEXT:
        print(f"name:        {args.name}")
        print(f"label:       {template.get('name', '')}")
        print(f"description: {template.get('description', '')}")
        print()
        print("template:")
        body = template.get("template", "")
        for line in body.splitlines() or [""]:
            print(f"  {line}")
    else:
        emit(
            fmt,
            message="Domain template",
            extra={"name": args.name, "template": template},
        )
    return 0


def _render_text_dag(instrument: Instrument) -> str:
    """Plain-text node + edge listing of an instrument's round DAG."""

    lines: list[str] = []
    lines.append(f"# instrument v{instrument.version}")
    lines.append(f"# {len(instrument.rounds)} round(s)")
    lines.append("")
    for r in instrument.rounds:
        lines.append(f"[{r.name}] ({len(r.questions)} question(s))")
        if r.depends_on:
            lines.append(f"  depends_on: {r.depends_on}")
        if r.route_when:
            for entry in r.route_when:
                if "if" in entry:
                    pred = entry["if"]
                    lines.append(
                        f"  if {pred.get('field')} {pred.get('op')} {pred.get('value')!r} -> {entry.get('goto')}"
                    )
                elif "else" in entry:
                    lines.append(f"  else -> {entry['else']}")
    return "\n".join(lines)


def _render_mermaid(instrument: Instrument) -> str:
    """Mermaid flowchart of an instrument's round DAG."""
    from althing.instrument import END_SENTINEL

    lines = ["flowchart TD"]
    for r in instrument.rounds:
        lines.append(f"    {r.name}([{r.name}])")

    # Collect __end__ targets so we can declare the terminal node once
    # if any route lands there. The terminal sentinel renders as a
    # double-circle stadium so it's visually distinct from regular rounds.
    has_end_target = False

    for r in instrument.rounds:
        if r.depends_on:
            lines.append(f"    {r.depends_on} --> {r.name}")
        if r.route_when:
            for entry in r.route_when:
                if "if" in entry:
                    pred = entry["if"]
                    label = f"{pred.get('field')} {pred.get('op')} {pred.get('value')}"
                    target = entry.get("goto")
                    if target == END_SENTINEL:
                        has_end_target = True
                    lines.append(f"    {r.name} -->|{label}| {target}")
                elif "else" in entry:
                    target = entry["else"]
                    if target == END_SENTINEL:
                        has_end_target = True
                    lines.append(f"    {r.name} -->|else| {target}")

    if has_end_target:
        lines.append(f"    {END_SENTINEL}(((end)))")
    return "\n".join(lines)


def _degenerate_path(instrument: Instrument) -> list[dict[str, Any]]:
    """Synthesize a path log for single-round runs.

    Multi-round (v2/v3) instruments now execute through
    ``run_multi_round_panel`` (P1-2) and render the REAL router-emitted
    path, so this fallback only ever sees single-round instruments —
    where the "path" is trivially ``round -> __end__``. F3-E still wants
    the path line rendered for that case.
    """
    from althing.instrument import END_SENTINEL

    rounds = list(instrument.rounds)
    if not rounds:
        return []
    path: list[dict[str, Any]] = []
    for i, r in enumerate(rounds):
        nxt = rounds[i + 1].name if i + 1 < len(rounds) else END_SENTINEL
        path.append({"round": r.name, "branch": "linear", "next": nxt})
    return path


def _build_params_metadata(
    args: argparse.Namespace,
    temperature: float | None,
    top_p: float | None,
) -> dict[str, Any]:
    """Build a metadata dict of generation parameters for output."""
    params: dict[str, Any] = {}
    if temperature is not None:
        params["temperature"] = temperature
    if top_p is not None:
        params["top_p"] = top_p
    seed = getattr(args, "seed", None)
    if seed is not None:
        params["seed"] = seed
    synthesis_temperature = getattr(args, "synthesis_temperature", None)
    if synthesis_temperature is not None:
        params["synthesis_temperature"] = synthesis_temperature
    prompt_template = getattr(args, "prompt_template", None)
    if prompt_template is not None:
        params["prompt_template"] = prompt_template
    return params


def handle_panel_inspect(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Inspect a saved panel result: metadata, per-model rollup, failures (sp-76gm).

    No LLM calls. Loads a result by ID or path (same resolution as
    ``panel synthesize``) and prints a compact summary. JSON/NDJSON
    modes emit the full structured :class:`InspectReport` dict under
    the ``extra`` key.
    """
    from althing.analysis.inspect import build_inspect_report, format_inspect_text

    result_ref = args.result
    path = Path(result_ref)
    if path.exists() and path.suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            msg = f"Error: not valid JSON: {result_ref}: {exc}"
            if fmt is OutputFormat.TEXT:
                print(msg, file=sys.stderr)
            else:
                emit(fmt, message=msg, extra={"error": "invalid_json"})
            return 1
        data.setdefault("id", path.stem)
    else:
        from althing.mcp.data import get_panel_result

        try:
            data = get_panel_result(result_ref)
        except FileNotFoundError:
            msg = f"Error: panel result not found: {result_ref}"
            if fmt is OutputFormat.TEXT:
                print(msg, file=sys.stderr)
            else:
                emit(fmt, message=msg, extra={"error": "not_found"})
            return 1

    if not isinstance(data, dict):
        msg = "Error: panel result is not a JSON object"
        if fmt is OutputFormat.TEXT:
            print(msg, file=sys.stderr)
        else:
            emit(fmt, message=msg, extra={"error": "invalid_shape"})
        return 1

    report = build_inspect_report(data)

    if fmt is OutputFormat.TEXT:
        print(format_inspect_text(report))
    else:
        emit(fmt, message="Panel inspect", extra={"inspect": report.to_dict()})

    return 0


def handle_panel_synthesize(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Re-synthesize a saved panel result (sp-5on.5).

    Loads a saved panel result, reconstructs the panelist results and
    question list, invokes :func:`synthesize_panel` with the requested
    model/prompt, and writes a sidecar file
    ``<result_id>.synthesis-<ts>.json`` next to the original result.
    """
    from datetime import datetime, timezone

    from althing.orchestrator import PanelistResult

    # sp-rcvr: the global --model flag has no effect on this subcommand —
    # the synthesis model comes from --synthesis-model (falling back to the
    # saved panelist model). Silently ignoring it cost operators real
    # debugging time; warn and point at the right flag.
    if getattr(args, "model", None):
        print(
            f"warning: global --model {args.model!r} is ignored by 'panel synthesize'; "
            f"use --synthesis-model {args.model} to pick the synthesis model.",
            file=sys.stderr,
        )

    result_ref = args.result
    path = Path(result_ref)
    if path.exists() and path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("id", path.stem)
    else:
        from althing.mcp.data import get_panel_result

        try:
            data = get_panel_result(result_ref)
        except FileNotFoundError:
            print(f"Error: panel result not found: {result_ref}", file=sys.stderr)
            return 1

    saved_results = data.get("results") or []
    if not saved_results:
        print(f"Error: no panelist results found in {result_ref}", file=sys.stderr)
        return 1

    panelist_results: list[PanelistResult] = []
    for r in saved_results:
        usage_dict = r.get("usage") or {}
        usage = TokenUsage.from_dict(usage_dict) if usage_dict else ZERO_USAGE
        panelist_results.append(
            PanelistResult(
                persona_name=r.get("persona", "Anonymous"),
                responses=r.get("responses") or [],
                usage=usage,
                error=r.get("error"),
                model=r.get("model"),
            )
        )

    saved_questions = data.get("questions")
    if saved_questions:
        questions = saved_questions
    else:
        seen: list[dict[str, Any]] = []
        seen_texts: set[str] = set()
        for pr in panelist_results:
            for resp in pr.responses:
                if resp.get("follow_up"):
                    continue
                q_text = resp.get("question", "")
                if q_text and q_text not in seen_texts:
                    seen.append({"text": q_text})
                    seen_texts.add(q_text)
        questions = seen

    panelist_model = data.get("model")
    client = LLMClient()
    # sp-avmm: pre-flight size check for the re-synthesize path too, so a
    # saved panel cannot silently overflow a smaller-context synthesis model
    # picked via --synthesis-model on the rerun.
    synth_model_for_check = getattr(args, "synthesis_model", None) or panelist_model
    custom_prompt = getattr(args, "synthesis_prompt", None)
    overflow = detect_synthesis_context_overflow(
        panelist_results,
        questions,
        synthesis_model=synth_model_for_check,
        custom_prompt=custom_prompt,
    )
    if overflow is not None:
        actionable = format_synthesis_overflow_message(overflow)
        payload = build_synthesis_error_payload(
            None,
            error_type="synthesis_context_overflow",
            message=actionable,
            suggested_fix=(
                "Rerun with --synthesis-model gemini-2.5-flash-lite (1M context) "
                "or gemini-2.5-pro (1M context), or reduce panel size."
            ),
            diagnostic=overflow,
        )
        print(f"Error: synthesis pre-flight rejected: {actionable}", file=sys.stderr)
        if fmt is not OutputFormat.TEXT:
            emit(
                fmt,
                message=actionable,
                extra={"run_invalid": True, "synthesis_error": payload},
            )
        return 2
    source_id = data.get("id") or path.stem
    try:
        synth = synthesize_panel(
            client,
            panelist_results,
            questions,
            model=getattr(args, "synthesis_model", None),
            panelist_model=panelist_model,
            custom_prompt=custom_prompt,
        )
    except Exception as exc:
        # sp-rcvr: run the synthesis recovery ladder before failing loud
        # (sp-avmm) — classify, retry transients once, map-reduce on
        # (suspected) downstream context overflow, reroute away from a
        # failing OpenRouter downstream.
        from althing._runners import _sanitize_api_error
        from althing.synthesis_recovery import (
            RecoveryContext,
            SynthesisRecoveryError,
            recover_synthesis_failure,
        )

        saved_personas = data.get("personas")
        ctx = RecoveryContext(
            client=client,
            panelist_results=panelist_results,
            questions=questions,
            synthesis_model=getattr(args, "synthesis_model", None),
            panelist_model=panelist_model,
            custom_prompt=custom_prompt,
            personas=saved_personas if isinstance(saved_personas, list) else None,
            allow_map_reduce=custom_prompt is None,
            result_id=source_id,
        )
        try:
            synth = recover_synthesis_failure(exc, ctx)
        except SynthesisRecoveryError as rexc:
            sanitized = _sanitize_api_error(rexc)
            payload = build_synthesis_error_payload(
                rexc,
                error_type="synthesis_api_error",
                message=f"Synthesis call failed: {sanitized}",
                suggested_fix=(
                    f"Retry with a large-context in-family model: `{rexc.suggested_command}`. "
                    "Also check provider credentials and model availability."
                ),
                diagnostic={"recovery_rungs": rexc.rungs},
            )
            print(f"Error: synthesis failed: {sanitized}", file=sys.stderr)
            if fmt is not OutputFormat.TEXT:
                emit(
                    fmt,
                    message=f"Synthesis call failed: {sanitized}",
                    extra={"run_invalid": True, "synthesis_error": payload},
                )
            return 2

    synthesis_dict = synth.to_dict()

    from althing.mcp.data import save_panel_synthesis

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    sidecar_name = save_panel_synthesis(
        source_id,
        ts,
        {
            "source_result_id": source_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "synthesis_model": synth.model,
            "synthesis_prompt_override": getattr(args, "synthesis_prompt", None) is not None,
            "synthesis": synthesis_dict,
        },
    )

    # sy-549: a fallback synthesis (judge exhausted retries / partial schema)
    # is persisted for auditability but is NOT a healthy result — surface it
    # loudly and exit non-zero so MCP / CI consumers don't treat the empty
    # synthesis as success.
    if synth.is_fallback:
        from althing.synthesis_recovery import format_recovery_command

        synth_err = synth.error or "synthesis judge did not return a valid structured result"
        payload = build_synthesis_error_payload(
            None,
            error_type="synthesis_fallback",
            message=f"Synthesis incomplete: {synth_err}",
            suggested_fix=(
                "The synthesis judge returned an empty/partial result after all retries. "
                f"Rerun with a higher-quality synthesis model, e.g. `{format_recovery_command(source_id, synth.model)}`."
            ),
        )
        print(f"Error: synthesis incomplete: {synth_err}", file=sys.stderr)
        print(f"Saved (incomplete) synthesis: {sidecar_name}", file=sys.stderr)
        if fmt is not OutputFormat.TEXT:
            emit(
                fmt,
                message=f"Synthesis incomplete: {synth_err}",
                extra={
                    "run_invalid": True,
                    "synthesis_error": payload,
                    "source_result_id": source_id,
                    "synthesis": synthesis_dict,
                    "saved_as": sidecar_name,
                },
            )
        return 2

    if fmt is OutputFormat.TEXT:
        print(f"{'=' * 60}")
        print("SYNTHESIS")
        print(f"{'=' * 60}")
        print(f"\n  Summary: {synthesis_dict['summary']}")
        if synthesis_dict.get("themes"):
            print("\n  Themes:")
            for t in synthesis_dict["themes"]:
                print(f"    - {t}")
        if synthesis_dict.get("agreements"):
            print("\n  Agreements:")
            for a in synthesis_dict["agreements"]:
                print(f"    - {a}")
        if synthesis_dict.get("disagreements"):
            print("\n  Disagreements:")
            for d in synthesis_dict["disagreements"]:
                print(f"    - {d}")
        if synthesis_dict.get("surprises"):
            print("\n  Surprises:")
            for s in synthesis_dict["surprises"]:
                print(f"    - {s}")
        print(f"\n  Recommendation: {synthesis_dict['recommendation']}")
        print(f"  Synthesis cost: {synthesis_dict['cost']}")
        print(f"\nSaved synthesis: {sidecar_name}", file=sys.stderr)
    else:
        emit(
            fmt,
            message="Panel synthesis complete",
            extra={
                "source_result_id": source_id,
                "synthesis": synthesis_dict,
                "saved_as": sidecar_name,
            },
        )

    return 0


def handle_analyze(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Run statistical analysis on a saved panel result."""
    import json as _json

    from althing.analyze import (
        analysis_to_dict,
        analyze_panel_result,
        format_csv,
        format_csv_responses,
        format_text,
        parse_response_csv_columns,
    )

    result_ref = args.result
    output_mode = getattr(args, "output", "text")

    # Load the panel result — either by ID or file path
    path = Path(result_ref)
    if path.exists() and path.suffix == ".json":
        data = _json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("id", path.stem)
    else:
        from althing.mcp.data import get_panel_result

        try:
            data = get_panel_result(result_ref)
        except FileNotFoundError:
            print(f"Error: panel result not found: {result_ref}", file=sys.stderr)
            return 1

    if output_mode == "responses-csv":
        try:
            cols = parse_response_csv_columns(getattr(args, "columns", None))
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        # ``end=""`` because the writer already emits CRLFs after every row.
        print(format_csv_responses(data, cols), end="")
        return 0

    analysis = analyze_panel_result(data)

    if output_mode == "json":
        print(_json.dumps(analysis_to_dict(analysis), indent=2))
    elif output_mode == "csv":
        print(format_csv(analysis))
    else:
        print(format_text(analysis))

    return 0


def handle_report(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Render a saved panel result as a Markdown report (sp-viz-layer T4).

    Resolves the RESULT (ID or ``.json`` path), walks it through
    :func:`build_inspect_report`, and emits Markdown via
    :func:`render_markdown`. Destination is stdout by default; ``--output
    PATH`` writes to disk (``-`` explicitly means stdout).

    Error parity with :func:`handle_panel_inspect`: a :exc:`ReportLoadError`
    prints to stderr in TEXT mode and is ``emit``-ted as a JSON/NDJSON
    error payload otherwise, returning exit code 1.

    S-gate OQ3: in JSON/NDJSON modes with ``--output FILE``, the handler is
    silent — the file IS the output, no status line on stdout. TEXT mode
    emits a stderr status line when writing to a file so humans see
    confirmation without polluting stdout.
    """
    from althing.analysis.inspect import build_inspect_report
    from althing.reporting import ReportLoadError, load_panel_json, render_markdown

    result_ref = args.result

    try:
        data = load_panel_json(result_ref)
    except ReportLoadError as err:
        msg = f"Error: {err}"
        if fmt is OutputFormat.TEXT:
            print(msg, file=sys.stderr)
        else:
            emit(fmt, message=msg, extra={"error": err.code})
        return 1

    resolved_path: Path | None = None
    candidate = Path(result_ref)
    if result_ref.endswith(".json") and candidate.exists():
        resolved_path = candidate

    report = build_inspect_report(data)
    md = render_markdown(
        report,
        data,
        source_path=str(resolved_path) if resolved_path is not None else None,
    )

    output_arg = getattr(args, "output", None)
    if output_arg is None or output_arg == "-":
        sys.stdout.write(md)
        return 0

    out_path = Path(output_arg)
    out_path.write_text(md, encoding="utf-8")

    if fmt is OutputFormat.TEXT:
        print(f"Report written: {out_path} ({len(md)} bytes)", file=sys.stderr)
    # JSON/NDJSON: silent per S-gate OQ3 — the file is the output.

    return 0


def handle_poll_summary(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Render the deterministic structured-response summary for a saved result.

    Loads the result (ID or path), runs :func:`build_poll_summary`, then
    emits either a human-friendly text block (default) or the full JSON
    payload. ``--segment-by`` is split on commas; empty string disables
    segment splits.

    Exits 1 with a typed error message when the result can't be loaded
    (mirrors ``handle_report``'s ``ReportLoadError`` parity). Exits 0
    even when the summary is empty — an enum-free result is a valid
    finding, not an error.

    sy-4yd.
    """
    from althing.poll_summary import build_poll_summary
    from althing.reporting import ReportLoadError, load_panel_json

    try:
        data = load_panel_json(args.result)
    except ReportLoadError as err:
        msg = f"Error: {err}"
        if fmt is OutputFormat.TEXT:
            print(msg, file=sys.stderr)
        else:
            emit(fmt, message=msg, extra={"error": err.code})
        return 1

    raw_segment = getattr(args, "segment_by", None)
    if raw_segment is None:
        segment_by = None  # use defaults
    else:
        # Empty string => disable explicitly; otherwise split on commas
        # and strip whitespace so `--segment-by "occupation, tier"` works.
        segment_by = [s.strip() for s in raw_segment.split(",") if s.strip()]

    summary = build_poll_summary(
        data,
        segment_by=segment_by,
        choice_field=getattr(args, "choice_field", None),
        confidence_field=getattr(args, "confidence_field", None),
    )
    out_format = getattr(args, "poll_summary_format", "text")

    if out_format == "json" or fmt is not OutputFormat.TEXT:
        payload = summary.to_dict()
        if fmt is OutputFormat.TEXT:
            # Direct JSON output for `--format json` regardless of the
            # top-level --output-format. Lets a user pipe to jq without
            # also picking the global JSON envelope.
            print(json.dumps(payload, indent=2))
        else:
            emit(fmt, message="poll_summary", extra=payload)
        return 0

    # Text rendering. Concise — the consumer is either a human eyeballing
    # the panel or an agent scanning for the headline. Detailed payload
    # always available via --format json.
    lines: list[str] = []
    lines.append(f"Poll summary — {summary.persona_count} panelist{'s' if summary.persona_count != 1 else ''}")
    if not summary.questions:
        lines.append("  (no structured questions in this result — see `althing report` for the markdown report)")

    for qs in summary.questions:
        lines.append("")
        title = qs.question[:80] + ("…" if len(qs.question) > 80 else "")
        lines.append(f"  Q{qs.question_index}: {title}  [kind={qs.kind}, n={qs.n}]")
        if qs.kind == "enum":
            if qs.winner:
                lines.append(f"    winner: {qs.winner}")
            if qs.first_choice_counts:
                for opt, count in qs.first_choice_counts.items():
                    pct = (count / qs.n * 100) if qs.n else 0
                    lines.append(f"      {opt}: {count} ({pct:.0f}%)")
            if qs.second_choice_counts:
                lines.append("    second choices:")
                for opt, count in qs.second_choice_counts.items():
                    lines.append(f"      {opt}: {count}")
            if qs.weighted_scores:
                lines.append("    weighted scores (avg rating per choice):")
                for opt, score in qs.weighted_scores.items():
                    lines.append(f"      {opt}: {score:.2f}")
            # sy-bn7: surface the overall confidence average for enum
            # polls that carry a numeric score field. Distinct from
            # weighted_scores (per-option) — answers "how confident was
            # the panel overall?".
            if qs.metric_average is not None:
                lines.append(f"    overall confidence: {qs.metric_average:.2f}")
        elif qs.kind == "scale":
            if qs.metric_average is not None:
                lines.append(f"    average: {qs.metric_average:.2f}")
            if qs.metric_distribution:
                hist = ", ".join(f"{k}x{v}" for k, v in qs.metric_distribution.items())
                lines.append(f"    distribution: {hist}")
        if qs.n_unparseable:
            lines.append(f"    ({qs.n_unparseable} unparseable response{'s' if qs.n_unparseable != 1 else ''})")

    if summary.segment_splits:
        lines.append("")
        lines.append("  segment splits:")
        for qi, attrs in summary.segment_splits.items():
            for attr, rows in attrs.items():
                lines.append(f"    Q{qi} by {attr}:")
                for value, choices in rows.items():
                    choice_str = ", ".join(f"{opt}={cnt}" for opt, cnt in choices.items())
                    lines.append(f"      {value}: {choice_str}")

    if summary.top_objections:
        lines.append("")
        lines.append("  top objections:")
        for obj in summary.top_objections:
            lines.append(f"    - {obj}")

    if summary.recommended_next_test:
        lines.append("")
        lines.append(f"  recommended next test: {summary.recommended_next_test}")

    if summary.warnings:
        lines.append("")
        for w in summary.warnings:
            print(f"  warning: {w}", file=sys.stderr)

    print("\n".join(lines))
    return 0


def _format_path(path: list[dict[str, Any]]) -> str:
    """Render an executed branching path as a single human line.

    Example: ``exploration -> probe[themes contains pricing] -> probe_pricing -> validation``
    """
    if not path:
        return ""
    parts: list[str] = []
    for i, entry in enumerate(path):
        if i == 0:
            parts.append(entry["round"])
        branch = entry.get("branch", "")
        nxt = entry.get("next", "")
        if branch and branch not in ("linear",):
            parts.append(f"[{branch}] -> {nxt}")
        else:
            parts.append(f"-> {nxt}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Credential commands (sp-lve)
# ---------------------------------------------------------------------------


_PROVIDER_ENV_VAR: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "xai": "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def _read_api_key_interactive() -> str:
    """Read an API key from stdin.

    Uses :func:`getpass.getpass` when attached to a TTY so the key
    doesn't echo; falls back to a plain ``input`` read when stdin is
    piped (CI, scripts) so ``echo sk-... | althing login`` still
    works.
    """
    import getpass

    if sys.stdin.isatty():
        return getpass.getpass("API key: ").strip()
    return sys.stdin.readline().strip()


def _provider_for_env_var(env_var: str) -> str | None:
    """Return the canonical CLI ``--provider`` name for ``env_var``.

    Used to render the "did you mean --provider X?" hint when a key
    matches a different provider's distinctive prefix. Returns the first
    matching alias when more than one CLI name maps to the same env var
    (e.g. both ``gemini`` and ``google`` map to ``GOOGLE_API_KEY``).
    """
    for name, var in _PROVIDER_ENV_VAR.items():
        if var == env_var:
            return name
    return None


def _article(label: str) -> str:
    """Return ``a`` or ``an`` for the given provider label.

    Treats labels starting with a vowel — and ``xAI``, since the ``x`` is
    pronounced ``ex`` — as taking ``an``. Used so login error messages
    read as ``an Anthropic API key`` rather than ``a Anthropic API key``.
    """
    first = label[:1].lower()
    return "an" if first in "aeiou" or first == "x" else "a"


def handle_login(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Store an API key for the selected provider.

    Reads the key from ``--api-key`` or stdin (hidden on TTY) and writes
    it to the on-disk credential store. Validates that the provider
    name maps to a recognised env var so typos don't silently persist,
    and that the key's prefix matches the provider's convention so an
    obvious mismatch (e.g. an OpenAI ``sk-proj-`` key passed to
    ``--provider anthropic``) fails loudly at ``login`` time rather than
    much later as an opaque 401 from the API (sy-bybx).
    """
    from althing.credentials import (
        KNOWN_CREDENTIAL_ENV_VARS,
        PROVIDER_KEY_PREFIXES,
        PROVIDER_LABELS,
        detect_provider_from_key,
        save_credential,
    )

    provider = getattr(args, "provider", None) or "anthropic"
    env_var = _PROVIDER_ENV_VAR.get(provider)
    if env_var is None or env_var not in KNOWN_CREDENTIAL_ENV_VARS:
        msg = f"Unknown provider: {provider!r}"
        if fmt is OutputFormat.TEXT:
            print(msg, file=sys.stderr)
        else:
            emit(fmt, message=msg, extra={"error": "unknown_provider"})
        return 2

    key = (getattr(args, "api_key", None) or "").strip()
    if not key:
        key = _read_api_key_interactive()
    if not key:
        msg = "No API key provided."
        if fmt is OutputFormat.TEXT:
            print(msg, file=sys.stderr)
        else:
            emit(fmt, message=msg, extra={"error": "empty_key"})
        return 2

    label = PROVIDER_LABELS.get(env_var, provider)
    required = PROVIDER_KEY_PREFIXES.get(env_var, ())
    detected = detect_provider_from_key(key)
    prefix_mismatch = bool(required) and not any(key.startswith(p) for p in required)
    cross_provider = detected is not None and detected != env_var

    if prefix_mismatch or cross_provider:
        if prefix_mismatch:
            expected = " or ".join(repr(p) for p in required)
            msg = f"that doesn't look like {_article(label)} {label} API key (expected {expected} prefix)."
        else:
            detected_label = PROVIDER_LABELS.get(detected, detected) if detected else "another provider's"
            msg = f"that key looks like {_article(detected_label)} {detected_label} API key, not {_article(label)} {label} key."
        if cross_provider:
            hint_provider = _provider_for_env_var(detected) if detected else None
            if hint_provider:
                msg += f"\nhint: did you mean --provider {hint_provider}?"
        if fmt is OutputFormat.TEXT:
            print(f"error: {msg}", file=sys.stderr)
        else:
            emit(
                fmt,
                message="invalid_key_prefix",
                extra={
                    "error": "invalid_key_prefix",
                    "provider": provider,
                    "env_var": env_var,
                    "expected_prefixes": list(required),
                    "detected_env_var": detected,
                },
            )
        return 2

    path = save_credential(env_var, key)
    if fmt is OutputFormat.TEXT:
        print(f"Stored {label} key ({env_var}) in {path}")
    else:
        emit(
            fmt,
            message="credential_stored",
            extra={"provider": provider, "env_var": env_var, "path": str(path)},
        )
    return 0


def handle_logout(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Remove one or all stored API keys."""
    from althing.credentials import (
        KNOWN_CREDENTIAL_ENV_VARS,
        delete_credential,
        load_credentials,
    )

    provider = getattr(args, "provider", None) or "anthropic"

    if provider == "all":
        stored = list(load_credentials().keys())
        removed = [env for env in stored if delete_credential(env)]
        if fmt is OutputFormat.TEXT:
            if removed:
                print(f"Removed {len(removed)} stored credential(s): {', '.join(removed)}")
            else:
                print("No stored credentials to remove.")
        else:
            emit(fmt, message="credentials_removed", extra={"removed": removed})
        return 0

    env_var = _PROVIDER_ENV_VAR.get(provider)
    if env_var is None or env_var not in KNOWN_CREDENTIAL_ENV_VARS:
        msg = f"Unknown provider: {provider!r}"
        if fmt is OutputFormat.TEXT:
            print(msg, file=sys.stderr)
        else:
            emit(fmt, message=msg, extra={"error": "unknown_provider"})
        return 2

    removed = delete_credential(env_var)
    if fmt is OutputFormat.TEXT:
        if removed:
            print(f"Removed stored credential: {env_var}")
        else:
            print(f"No stored credential for {env_var}")
    else:
        emit(
            fmt,
            message="credential_removed" if removed else "credential_absent",
            extra={"provider": provider, "env_var": env_var, "removed": removed},
        )
    return 0


def handle_whoami(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Report which providers have credentials available (env or stored)."""
    import os

    from althing.credentials import (
        KNOWN_CREDENTIAL_ENV_VARS,
        PROVIDER_LABELS,
        credentials_path,
        load_credentials,
    )

    stored = load_credentials()
    rows: list[dict[str, Any]] = []
    for env_var in KNOWN_CREDENTIAL_ENV_VARS:
        env_set = bool(os.environ.get(env_var, "").strip())
        stored_set = env_var in stored
        source: str | None = None
        if env_set:
            source = "env"
        elif stored_set:
            source = "stored"
        rows.append(
            {
                "provider": PROVIDER_LABELS.get(env_var, env_var),
                "env_var": env_var,
                "available": env_set or stored_set,
                "source": source,
            }
        )

    if fmt is OutputFormat.TEXT:
        any_available = any(r["available"] for r in rows)
        if not any_available:
            print("No provider credentials found.")
            print("Run `althing login` or export an API key env var.")
        else:
            print(f"Credential store: {credentials_path()}")
            for row in rows:
                if not row["available"]:
                    continue
                print(f"  [{row['source']:<6}] {row['env_var']:<20} — {row['provider']}")
        return 0

    emit(
        fmt,
        message="credentials_status",
        extra={"credentials_path": str(credentials_path()), "providers": rows},
    )
    return 0


def handle_doctor(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Preflight check for CI/scripts: sane runtime and credentials, no secret material in output."""
    import os
    import sys
    from pathlib import Path

    from althing import __version__
    from althing.credentials import (
        KNOWN_CREDENTIAL_ENV_VARS,
        PROVIDER_LABELS,
        credentials_path,
        has_credential,
        load_credentials,
    )

    verbose = bool(getattr(args, "verbose", False))
    # sy-e28: agents and clean-install CI smoke tests need to validate
    # package health without provisioning a provider key. --install-only
    # drops the credential check from the exit-code gate but keeps it in
    # the report so the output still tells you whether you can actually
    # run a panel.
    install_only = bool(getattr(args, "install_only", False))

    # --- Python runtime
    py_version_str = sys.version.split()[0]
    py_ok = sys.version_info[:2] >= (3, 10)

    # --- Required + optional dependencies
    dep_errors: list[str] = []
    optional_notes: list[str] = []
    try:
        import httpx  # noqa: F401
    except ImportError:
        dep_errors.append("missing dependency: httpx")
    try:
        import yaml  # noqa: F401
    except ImportError:
        dep_errors.append("missing dependency: pyyaml")
    try:
        import mcp  # noqa: F401

        mcp_installed = True
    except ImportError:
        mcp_installed = False
        optional_notes.append("optional: mcp not installed (`pip install althing[mcp]`).")

    # --- Provider credentials (per-provider, env-vs-stored, never values)
    def _credential_row(env_var: str) -> dict[str, Any]:
        env_set = bool(os.environ.get(env_var, "").strip())
        disk = load_credentials()
        stored_set = env_var in disk
        source: str | None = None
        if env_set:
            source = "env"
        elif stored_set:
            source = "stored"
        return {
            "provider": PROVIDER_LABELS.get(env_var, env_var),
            "env_var": env_var,
            "available": has_credential(env_var),
            "source": source,
        }

    provider_rows = [_credential_row(ev) for ev in KNOWN_CREDENTIAL_ENV_VARS]
    any_provider = any(r["available"] for r in provider_rows)

    # --- Checkpoint root (writable + existing-run count). Non-destructive:
    # walk up to the first existing ancestor and probe os.access for write.
    ckpt_root = default_checkpoint_root()
    ckpt_writable = False
    ckpt_existing_runs: int | None = None
    ckpt_error: str | None = None
    if ckpt_root.exists():
        ckpt_writable = os.access(ckpt_root, os.W_OK)
        try:
            ckpt_existing_runs = sum(1 for p in ckpt_root.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError as exc:
            ckpt_error = str(exc)
    else:
        # Find the first existing ancestor; doctor must not create directories
        # as a side effect (CI cleanliness, repeatable runs).
        probe = ckpt_root
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        ckpt_writable = os.access(probe, os.W_OK)
        ckpt_existing_runs = 0

    # --- Pack registry (bundled persona + instrument packs load OK)
    bundled_persona_count = 0
    bundled_instrument_count = 0
    pack_load_errors: list[str] = []
    try:
        from althing.mcp.data import (
            _bundled_instrument_packs,
            _bundled_packs,
        )

        bundled_persona_count = len(_bundled_packs())
        bundled_instrument_count = len(_bundled_instrument_packs())
        if bundled_persona_count == 0:
            pack_load_errors.append("no bundled persona packs loaded")
        if bundled_instrument_count == 0:
            pack_load_errors.append("no bundled instrument packs loaded")
    except Exception as exc:
        pack_load_errors.append(f"pack registry load failed: {exc}")
    packs_ok = not pack_load_errors

    # sy-e28: install_ok is the same gate as the old checks_ok minus the
    # credential check. checks_ok stays "fully runnable" by default so
    # the existing CI/agent contract holds; --install-only drops the
    # credential check from checks_ok so the exit code reflects install
    # health alone.
    install_ok = py_ok and not dep_errors and ckpt_writable and packs_ok
    checks_ok = install_ok and (any_provider or install_only)

    extra: dict[str, Any] = {
        "althing_version": __version__,
        "python_version": py_version_str,
        "python_ok": py_ok,
        "credentials_path": str(credentials_path()),
        "providers": provider_rows,
        "dependencies_ok": not dep_errors,
        "dependency_errors": dep_errors,
        "optional_notes": optional_notes,
        "mcp_installed": mcp_installed,
        "credential_configured": any_provider,
        "checkpoint_root": str(ckpt_root),
        "checkpoint_root_writable": ckpt_writable,
        "checkpoint_existing_runs": ckpt_existing_runs,
        "checkpoint_error": ckpt_error,
        "bundled_persona_packs": bundled_persona_count,
        "bundled_instrument_packs": bundled_instrument_count,
        "pack_load_errors": pack_load_errors,
        "packs_ok": packs_ok,
        "install_only": install_only,
        "install_ok": install_ok,
        "checks_ok": checks_ok,
    }

    rc = 0 if checks_ok else 1

    if fmt is OutputFormat.TEXT:
        # Unicode glyphs match the GH-310 spec example. UTF-8 is Python's
        # default stdout encoding on Linux/Mac and on Windows since 3.6.
        ok_mark = "✓"
        bad_mark = "✗"
        warn_mark = "!"

        # Header line: althing + python
        print(f"althing {__version__}")
        py_marker = ok_mark if py_ok else bad_mark
        py_suffix = "(>= 3.10)" if py_ok else "(need >= 3.10)"
        py_stream = sys.stdout if py_ok else sys.stderr
        print(f"  {py_marker} python: {py_version_str} {py_suffix}", file=py_stream)

        # Required deps
        if dep_errors:
            for msg in dep_errors:
                print(f"  {bad_mark} {msg}", file=sys.stderr)
        else:
            print(f"  {ok_mark} required deps: httpx, pyyaml")

        # Optional: mcp
        if mcp_installed:
            print(f"  {ok_mark} optional: mcp installed")
        else:
            print(f"  {warn_mark} optional: mcp not installed (`pip install althing[mcp]`)")

        # Credentials (one line per provider with a key). In --install-only
        # mode credential absence is reported as a warning, not an error,
        # since the user has explicitly asked to validate install health
        # only.
        if any_provider:
            for row in provider_rows:
                if row["available"]:
                    print(f"  {ok_mark} {row['env_var']}: {row['source']} ({row['provider']})")
                elif verbose:
                    print(f"  {warn_mark} {row['env_var']}: not set ({row['provider']})")
            if verbose:
                print(f"     credential store: {credentials_path()}")
        elif install_only:
            print(
                f"  {warn_mark} credentials: none configured "
                "(install-only mode — run `althing login` before running a panel)."
            )
        else:
            print(
                f"  {bad_mark} No LLM credentials found (env or stored). Run `althing login`.",
                file=sys.stderr,
            )

        # Checkpoint root
        display = str(ckpt_root)
        home = str(Path.home())
        if not verbose and display.startswith(home):
            display = "~" + display[len(home) :]
        if ckpt_writable:
            runs_label = ""
            if ckpt_existing_runs is not None:
                noun = "run" if ckpt_existing_runs == 1 else "runs"
                runs_label = f", {ckpt_existing_runs} existing {noun}"
            print(f"  {ok_mark} checkpoint root: {display} (writable{runs_label})")
        else:
            err_msg = f" — {ckpt_error}" if ckpt_error else ""
            print(
                f"  {bad_mark} checkpoint root: {display} not writable{err_msg}",
                file=sys.stderr,
            )

        # Pack registry
        if packs_ok:
            print(
                f"  {ok_mark} packs: {bundled_persona_count} persona, {bundled_instrument_count} instrument (bundled)"
            )
        else:
            for msg in pack_load_errors:
                print(f"  {bad_mark} {msg}", file=sys.stderr)

        # Tally — in --install-only mode missing credentials count as a
        # warning rather than an error so the printed summary matches the
        # exit code.
        credentials_error_count = 0 if (any_provider or install_only) else 1
        credentials_warning_count = 1 if (install_only and not any_provider) else 0
        n_err = (
            (0 if py_ok else 1)
            + len(dep_errors)
            + credentials_error_count
            + (0 if ckpt_writable else 1)
            + len(pack_load_errors)
        )
        n_warn = len(optional_notes) + credentials_warning_count
        if rc == 0:
            tail = f"{n_warn} warning{'s' if n_warn != 1 else ''}, 0 errors." if n_warn else "0 errors."
            print(tail)
        else:
            print(
                f"{n_err} error{'s' if n_err != 1 else ''}, {n_warn} warning{'s' if n_warn != 1 else ''}.",
                file=sys.stderr,
            )
        return rc

    emit(
        fmt,
        message="doctor_report",
        extra=extra,
    )
    return rc


def _print_saved_result_hint(result_id: str) -> None:
    """Print the canonical follow-up commands for a freshly saved result.

    Emitted to stderr (like the prior ``Result saved`` line) so JSON/NDJSON
    consumers reading stdout stay unpolluted, while a human or agent gets an
    obvious next step instead of having to scrape prose or search the
    filesystem for the artifact (#525).
    """
    print(f"Result saved: {result_id}", file=sys.stderr)
    print("Next:", file=sys.stderr)
    print(f"  althing report {result_id}          # full Markdown report (incl. cost rollup)", file=sys.stderr)
    print(f"  althing results show {result_id}    # raw saved result", file=sys.stderr)
    print("  althing results list                # all saved results", file=sys.stderr)
    print(f"  althing cost show {result_id}       # this run's cost breakdown", file=sys.stderr)
    print("  althing cost summary                # cost rollup across saved runs", file=sys.stderr)


def handle_runs_prune(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Prune stale checkpoint run directories from the checkpoint root."""
    from pathlib import Path

    root = Path(args.root) if getattr(args, "root", None) else None

    older_than = None
    if getattr(args, "older_than", None):
        try:
            older_than = parse_duration(args.older_than)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    keep = getattr(args, "keep", None)
    dry_run: bool = getattr(args, "dry_run", False)

    if older_than is None and keep is None:
        print("error: specify at least one of --older-than or --keep", file=sys.stderr)
        return 1

    try:
        pruned = prune_runs(root=root, older_than=older_than, keep=keep, dry_run=dry_run)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if fmt is OutputFormat.TEXT:
        checkpoint_root = root or default_checkpoint_root()
        prefix = "[dry-run] would prune" if dry_run else "pruned"
        if not pruned:
            print(f"Nothing to prune under {checkpoint_root}")
        else:
            for run_id in pruned:
                print(f"{prefix}: {run_id}")
            noun = "run" if len(pruned) == 1 else "runs"
            suffix = " (no files deleted)" if dry_run else ""
            print(f"\n{len(pruned)} {noun}{suffix}")
        return 0

    emit(
        fmt,
        message="runs_pruned",
        extra={
            "dry_run": dry_run,
            "pruned": pruned,
            "count": len(pruned),
        },
    )
    return 0


def handle_runs_list(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """List checkpoint run directories and their status."""
    from pathlib import Path

    root = Path(args.root) if getattr(args, "root", None) else None

    runs = list_runs(root=root)

    if fmt is OutputFormat.TEXT:
        checkpoint_root = root or default_checkpoint_root()
        if not runs:
            print(f"No runs found under {checkpoint_root}")
            # #525: checkpoints and saved results are distinct stores.
            # Point users who ran a panel with --save (which writes to the
            # results store, not here) at the right list command instead of
            # leaving them to conclude their artifact vanished.
            print("(Saved --save results live elsewhere; try: althing results list)")
            return 0
        for run in runs:
            if run.get("malformed"):
                print(f"  {run['run_id']}  [malformed]")
            else:
                status = "in-progress" if run["in_progress"] else ("aborted" if run["abort_reason"] else "complete")
                print(f"  {run['run_id']}  [{status}]  updated={run['updated_at']}")
        return 0

    emit(fmt, message="runs_list", extra={"runs": runs, "count": len(runs)})
    return 0


def handle_results_list(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """List saved panel results (the ``--save`` artifact store).

    #525: ``runs list`` only sees the checkpoint store, leaving ``--save``
    artifacts undiscoverable without a filesystem search. This lists the
    results store directly so a freshly saved result is rediscoverable by
    its stable ID — the handle that ``report`` and ``results show`` accept.
    """
    from althing.mcp.data import list_panel_results

    results = list_panel_results()

    if fmt is OutputFormat.TEXT:
        if not results:
            from althing.mcp.data import _results_dir

            print(f"No saved results found under {_results_dir()}")
            return 0
        for r in results:
            extras = []
            if r.get("instrument_name"):
                extras.append(f"instrument={r['instrument_name']}")
            extras.append(f"personas={r.get('persona_count', 0)}")
            extras.append(f"questions={r.get('question_count', 0)}")
            suffix = "  " + "  ".join(extras) if extras else ""
            print(f"  {r['id']}  [{r.get('model', '?')}]  {r.get('created_at', '')}{suffix}")
        return 0

    emit(fmt, message="results_list", extra={"results": results, "count": len(results)})
    return 0


def handle_results_show(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Show a saved panel result by its stable ID (or path).

    Resolves the handle to the saved JSON, surfacing the canonical
    ``saved_path`` so agents never have to guess where ``--save`` wrote the
    artifact. JSON/NDJSON modes emit the full result envelope plus
    ``saved_path``; text mode prints a compact provenance-aware summary and
    the follow-up ``report`` command (#525).
    """
    from althing.mcp.data import _results_dir, get_panel_result

    result_ref = args.result

    # Accept a path ending in .json (parity with report/inspect) or a bare ID.
    candidate = Path(result_ref)
    if result_ref.endswith(".json") and candidate.exists():
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            msg = f"Error: not valid JSON: {result_ref}: {exc}"
            if fmt is OutputFormat.TEXT:
                print(msg, file=sys.stderr)
            else:
                emit(fmt, message=msg, extra={"error": "invalid_json"})
            return 1
        data.setdefault("id", candidate.stem)
        saved_path = str(candidate)
    else:
        try:
            data = get_panel_result(result_ref)
        except FileNotFoundError:
            msg = f"Error: saved result not found: {result_ref}"
            if fmt is OutputFormat.TEXT:
                print(msg, file=sys.stderr)
            else:
                emit(fmt, message=msg, extra={"error": "not_found"})
            return 1
        saved_path = str(_results_dir() / f"{data.get('id', result_ref)}.json")

    if fmt is not OutputFormat.TEXT:
        emit(fmt, message="result_show", extra={**data, "saved_path": saved_path})
        return 0

    rid = data.get("id", result_ref)
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    version = metadata.get("version") if isinstance(metadata.get("version"), dict) else {}
    cost_meta = metadata.get("cost") if isinstance(metadata.get("cost"), dict) else {}

    print(f"Result: {rid}")
    print(f"  saved_path:         {saved_path}")
    print(f"  created_at:         {data.get('created_at', '(unknown)')}")
    print(f"  model:              {data.get('model', '(unknown)')}")
    if data.get("models"):
        print(f"  models:             {', '.join(str(m) for m in data['models'])}")
    if data.get("instrument_name"):
        print(f"  instrument:         {data['instrument_name']}")
    print(f"  personas:           {data.get('persona_count', 0)}")
    print(f"  questions:          {data.get('question_count', 0)}")
    if data.get("total_cost"):
        print(f"  total_cost:         {data['total_cost']}")
    print(f"  althing_version: {version.get('althing', '(unknown)')}")
    print(f"  python_version:     {version.get('python', '(unknown)')}")
    print(f"  config_hash:        {metadata.get('config_hash', '(not recorded)')}")
    print(f"  pricing_snapshot:   {cost_meta.get('pricing_snapshot_date', '(unknown)')}")
    print(f"\nReport: althing report {rid}")
    return 0


def handle_runs_diff(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Compare two saved panel results statistically."""
    from althing.diff import (
        compute_diff,
        load_result,
    )

    try:
        result_a = load_result(args.result_a)
    except FileNotFoundError:
        print(f"error: result not found: {args.result_a}", file=sys.stderr)
        return 1

    try:
        result_b = load_result(args.result_b)
    except FileNotFoundError:
        print(f"error: result not found: {args.result_b}", file=sys.stderr)
        return 1

    diff = compute_diff(result_a, result_b)
    m = diff.metadata

    if fmt is OutputFormat.TEXT:
        _print_runs_diff_text(diff)
        return 0

    # JSON / NDJSON output
    cat_out = [
        {
            "question_key": q.question_key,
            "question_text": q.question_text,
            "type": "categorical",
            "jsd": round(q.jsd, 4),
            "cramers_v_a": round(q.cramers_v_a, 4) if q.cramers_v_a is not None else None,
            "cramers_v_b": round(q.cramers_v_b, 4) if q.cramers_v_b is not None else None,
            "distribution_a": q.distribution_a,
            "distribution_b": q.distribution_b,
        }
        for q in diff.categorical_questions
    ]
    text_out = [
        {
            "question_key": q.question_key,
            "question_text": q.question_text,
            "type": "text",
            "top_themes_a": q.top_themes_a,
            "top_themes_b": q.top_themes_b,
            "new_themes": q.new_themes,
            "dropped_themes": q.dropped_themes,
        }
        for q in diff.text_questions
    ]
    emit(
        fmt,
        message="runs_diff",
        extra={
            "result_a": m.result_a_id,
            "result_b": m.result_b_id,
            "metadata": {
                "created_at_a": m.created_at_a,
                "created_at_b": m.created_at_b,
                "model_a": m.model_a,
                "model_b": m.model_b,
                "persona_count_a": m.persona_count_a,
                "persona_count_b": m.persona_count_b,
                "question_count_a": m.question_count_a,
                "question_count_b": m.question_count_b,
                "cost_a": m.cost_a,
                "cost_b": m.cost_b,
                "usage_a": m.usage_a,
                "usage_b": m.usage_b,
            },
            "categorical_questions": cat_out,
            "text_questions": text_out,
        },
    )
    return 0


def _print_runs_diff_text(diff: RunDiff) -> None:
    """Print a human-readable diff to stdout."""
    m = diff.metadata

    def _field(label: str, a: object, b: object) -> str:
        eq = "=" if str(a) == str(b) else "→"
        return f"  {label:<16} {a!s:<18} {eq}  {b}"

    print(f"Run A: {m.result_a_id}  ({m.created_at_a})")
    print(f"Run B: {m.result_b_id}  ({m.created_at_b})")
    print()
    print("── Metadata " + "─" * 40)
    print(_field("Model", m.model_a, m.model_b))
    print(_field("Personas", m.persona_count_a, m.persona_count_b))
    print(_field("Questions", m.question_count_a, m.question_count_b))
    print(_field("Cost", m.cost_a or "—", m.cost_b or "—"))

    if not diff.categorical_questions and not diff.text_questions:
        print()
        print("(No question-level data available for comparison.)")
        return

    if diff.categorical_questions:
        print()
        print("── Categorical questions " + "─" * 27)
        for q in diff.categorical_questions:
            _print_categorical_diff(q)

    if diff.text_questions:
        print()
        print("── Text questions " + "─" * 34)
        for q in diff.text_questions:
            _print_text_diff(q)


def _pct_bar(dist: dict[str, int]) -> str:
    total = sum(dist.values()) or 1
    parts = [f"{k}={round(100 * v / total)}%" for k, v in sorted(dist.items())]
    return "  ".join(parts)


def _print_categorical_diff(q: CategoricalQuestionDiff) -> None:
    text = q.question_text[:72] + ("…" if len(q.question_text) > 72 else "")
    print(f"\n  [{q.question_key}] {text}")

    if q.jsd < 0.05:
        shift = "stable"
    elif q.jsd < 0.15:
        shift = "small shift"
    elif q.jsd < 0.30:
        shift = "moderate shift"
    else:
        shift = "large shift"
    print(f"    JSD: {q.jsd:.3f}  ({shift})")

    if q.distribution_a:
        print(f"    Run A: {_pct_bar(q.distribution_a)}")
    else:
        print("    Run A: (no data)")
    if q.distribution_b:
        print(f"    Run B: {_pct_bar(q.distribution_b)}")
    else:
        print("    Run B: (no data)")


def _print_text_diff(q: TextQuestionDiff) -> None:
    text = q.question_text[:72] + ("…" if len(q.question_text) > 72 else "")
    print(f"\n  [{q.question_key}] {text}")
    if q.top_themes_a:
        print(f"    Themes A: {', '.join(q.top_themes_a[:8])}")
    if q.top_themes_b:
        print(f"    Themes B: {', '.join(q.top_themes_b[:8])}")
    if q.new_themes:
        print(f"    New in B: {', '.join(q.new_themes)}")
    if q.dropped_themes:
        print(f"    Gone in B: {', '.join(q.dropped_themes)}")


# ---------------------------------------------------------------------------
# plugin lint (sy-0rr)
# ---------------------------------------------------------------------------


def handle_plugin_lint(args: argparse.Namespace, fmt: OutputFormat) -> int:
    """Lint one plugin directory or every installed plugin (--all)."""
    from althing.plugins.lint import lint_plugin

    strict = bool(getattr(args, "strict", False))
    lint_all = bool(getattr(args, "lint_all", False))
    path = getattr(args, "path", None)

    if lint_all and path:
        print(
            "Error: pass either a plugin path or --all, not both.",
            file=sys.stderr,
        )
        return 1
    if not lint_all and not path:
        print(
            "Error: provide a plugin path or pass --all.",
            file=sys.stderr,
        )
        return 1

    targets: list[Path] = []
    if lint_all:
        config_dir = Path(getattr(args, "plugin_config_dir", None) or Path.home() / ".althing").expanduser()
        plugins_dir = config_dir / "plugins"
        if not plugins_dir.exists():
            if fmt is OutputFormat.TEXT:
                print(f"No plugins installed under {plugins_dir}.")
            else:
                emit(
                    fmt,
                    message="no plugins installed",
                    extra={"plugins_dir": str(plugins_dir), "reports": []},
                )
            return 0
        targets = sorted(p for p in plugins_dir.iterdir() if p.is_dir())
        if not targets:
            if fmt is OutputFormat.TEXT:
                print(f"No plugins installed under {plugins_dir}.")
            else:
                emit(
                    fmt,
                    message="no plugins installed",
                    extra={"plugins_dir": str(plugins_dir), "reports": []},
                )
            return 0
    else:
        targets = [Path(path)]

    reports: list[LintReport] = [lint_plugin(t) for t in targets]
    failed = [r for r in reports if not r.passed(strict=strict)]

    if fmt is OutputFormat.TEXT:
        for report in reports:
            _print_lint_report(report, strict=strict)
        _print_lint_summary(reports, strict=strict)
    else:
        payload = {
            "strict": strict,
            "reports": [r.to_dict() for r in reports],
            "failed": len(failed),
            "passed": len(reports) - len(failed),
        }
        message = "plugin lint passed" if not failed else "plugin lint failed"
        emit(fmt, message=message, extra=payload)

    return 1 if failed else 0


def _print_lint_report(report: LintReport, *, strict: bool) -> None:
    label = report.plugin_name or report.plugin_path
    status = "ok" if report.passed(strict=strict) else "fail"
    print(f"--- {label} [{status}]")
    if not report.issues:
        print("  ✓ no issues")
        return
    for issue in report.issues:
        print(f"  {issue.format()}")


def _print_lint_summary(reports: list[LintReport], *, strict: bool) -> None:
    failed = sum(1 for r in reports if not r.passed(strict=strict))
    if failed == 0:
        suffix = "" if len(reports) == 1 else "s"
        print(f"\n{len(reports)} plugin{suffix} lint OK.")
    else:
        print(f"\n{failed} plugin lint failed.")
