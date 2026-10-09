"""MCP server implementation for althing.

Exposes 12 tools, 4 resource URI patterns, and 3 prompt templates.
Uses stdio transport. Default model is haiku for MCP mode.

Tools:
    run_prompt             - Send a single prompt to an LLM (no personas)
    run_panel              - Run a full synthetic focus group panel
    run_quick_poll         - Quick single-question poll across personas
    extend_panel           - Append one ad-hoc round to a saved panel result
    list_persona_packs     - List saved persona packs
    get_persona_pack       - Get a specific persona pack
    save_persona_pack      - Save a persona pack
    list_instrument_packs  - List installed instrument packs
    get_instrument_pack    - Get an installed instrument pack
    save_instrument_pack   - Save (install) an instrument pack
    list_panel_results     - List saved panel results
    get_panel_result       - Get a specific panel result

Resources (URI patterns):
    persona-pack://{pack_id}         - A specific persona pack
    persona-pack://                  - List all persona packs
    panel-result://{result_id}       - A specific panel result
    panel-result://                  - List all panel results

Prompts:
    focus_group   - Run a focus group discussion
    name_test     - Test product/feature names with personas
    concept_test  - Test a concept or idea with personas
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import InputRequiredResult

from althing import __version__ as _althing_version
from althing._runners import (
    EXTRACT_SCHEMA_REGISTRY,
    MAX_PERSONAS,
    MAX_QUESTIONS,
    PANELIST_TIMEOUT,
    PanelTotalFailureError,
    build_synthesis_error_payload,
    detect_total_failure,
)
from althing._runners import (
    compute_variant_data as _compute_variant_data,  # re-exported for back-compat
)
from althing._runners import (
    format_panelist_result as _format_panelist_result,
)
from althing._runners import (
    resolve_extract_schema as _resolve_extract_schema,
)
from althing._runners import (
    run_multi_round_sync as _run_multi_round_sync,
)
from althing._runners import (
    run_panel_sync as _run_panel_sync,
)
from althing.cost import (
    ZERO_USAGE,
    CostEstimate,
    CostGate,
    aggregate_per_model,
    build_cost_fallback_warnings,
    estimate_cost,
    lookup_pricing,
    resolve_cost,
)
from althing.cost import TokenUsage as CostTokenUsage
from althing.instrument import Instrument, InstrumentError, parse_instrument
from althing.llm import fast_default as _fast_default
from althing.llm.client import LLMClient
from althing.llm.models import (
    CompletionRequest,
    ImageBlock,
    InlineSource,
    InputMessage,
    TextBlock,
    URLSource,
)
from althing.mcp.compat import W_DECISION_MISSING, apply_legacy_grace
from althing.mcp.data import (
    get_panel_result as _data_get_panel_result,
)
from althing.mcp.data import (
    get_persona_pack as _data_get_persona_pack,
)
from althing.mcp.data import (
    list_instrument_packs as _data_list_instrument_packs,
)
from althing.mcp.data import (
    list_panel_results as _data_list_panel_results,
)
from althing.mcp.data import (
    list_persona_packs as _data_list_persona_packs,
)
from althing.mcp.data import (
    load_instrument_pack as _data_load_instrument_pack,
)
from althing.mcp.data import (
    load_panel_sessions,
    save_panel_result,
    save_panel_sessions,
    update_panel_result,
)
from althing.mcp.data import (
    save_instrument_pack as _data_save_instrument_pack,
)
from althing.mcp.data import (
    save_persona_pack as _data_save_persona_pack,
)
from althing.mcp.sampling import (
    SAMPLING_MAX_PERSONAS,
    SAMPLING_MAX_QUESTIONS,
    SAMPLING_MAX_TOKENS_DEFAULT,
    SampleSpec,
    SamplingBridge,
    SamplingSuspend,
)
from althing.mcp.sampling import (
    decide_mode as _decide_sampling_mode,
)
from althing.mcp.sampling import (
    sample_text as _sample_text,  # noqa: F401 — re-exported for back-compat
)
from althing.metadata import PanelTimer, build_metadata
from althing.orchestrator import (
    MultiRoundResult,
    PanelistResult,
    PanelState,
    run_panel_parallel,
)
from althing.persistence import Session
from althing.prompts import build_question_prompt, persona_system_prompt
from althing.structured.retry import exhausted_retry_outcome
from althing.structured.validate import ErrorEnvelope, apply_response_gate, validate_request
from althing.structured.verdict import build_panel_verdict
from althing.synthesis import synthesize_panel

logger = logging.getLogger(__name__)

# Default model for MCP mode — used as the terminal fallback when no
# provider credentials are present in the environment. Prefer
# :func:`_resolve_mcp_default_model` at call sites so users with a
# non-Anthropic key (OpenRouter, Gemini, xAI, OpenAI) aren't silently
# routed into the Anthropic provider and a misleading missing-key error.
MCP_DEFAULT_MODEL = "haiku"

# Preference chain for the MCP default model. Mirrors the CLI's
# _DEFAULT_MODEL_PREFERENCE and sdk._DEFAULT_MODEL_PREFERENCE, but picks
# the cheap/fast model per provider since MCP is optimised for
# iterative use (whereas the CLI defaults to workhorse models).
_MCP_DEFAULT_MODEL_PREFERENCE: list[tuple[str, str]] = [
    ("ANTHROPIC_API_KEY", "haiku"),
    ("OPENAI_API_KEY", "gpt-4o-mini"),
    ("GEMINI_API_KEY", "gemini-2.5-flash"),
    ("GOOGLE_API_KEY", "gemini-2.5-flash"),
    ("XAI_API_KEY", "grok-3"),
    ("OPENROUTER_API_KEY", "openrouter/auto"),
]


def _resolve_mcp_default_model() -> str:
    """Pick a cheap/fast default model based on available provider creds.

    Walks :data:`_MCP_DEFAULT_MODEL_PREFERENCE` and returns the first
    alias whose credential is available via env OR the on-disk store
    written by ``althing login``. Falls back to
    :data:`MCP_DEFAULT_MODEL` when nothing is set so the LLM client's
    missing-credentials error is the one the user sees.
    """
    from althing.credentials import has_credential

    for env_var, alias in _MCP_DEFAULT_MODEL_PREFERENCE:
        if has_credential(env_var):
            return alias
    return MCP_DEFAULT_MODEL


# Persona-count threshold above which the auto-resolved default model is
# swapped for a known-fast equivalent. Mirrors GH#462 / sy-2ag: a 20-persona
# ``run_panel`` under ``openrouter/auto`` hung >15 min because OR routed
# every persona to an expensive workhorse; pinning haiku-4-5 cut the same
# run to 25–40 s. Most entries in :data:`_MCP_DEFAULT_MODEL_PREFERENCE`
# are already fast (haiku, gpt-4o-mini, gemini-2.5-flash) — only
# ``openrouter/auto`` needs the swap today.
#
# synthbench#261: the policy now lives in the shared
# :mod:`althing.llm.fast_default` module so the CLI and SDK apply the
# same swap; the names below are kept as re-exports for compatibility.
LARGE_PANEL_PERSONA_THRESHOLD = _fast_default.LARGE_PANEL_PERSONA_THRESHOLD

# Slow auto-resolved default → fast equivalent for large panels. Keyed on
# the alias returned by :func:`_resolve_mcp_default_model`; aliases that
# are already fast (or whose routing the user controls) are absent.
_LARGE_PANEL_FAST_MODEL_SWAP: dict[str, str] = _fast_default.FAST_MODEL_SWAP


def _resolve_mcp_default_model_for_panel(persona_count: int) -> str:
    """Resolve the default model, preferring fast equivalents for big panels.

    Wraps :func:`_resolve_mcp_default_model` and, when *persona_count*
    is at or above :data:`LARGE_PANEL_PERSONA_THRESHOLD`, swaps the
    resolved alias through :data:`_LARGE_PANEL_FAST_MODEL_SWAP` so a
    20-persona ``run_panel`` under an OpenRouter-only environment
    doesn't stall on ``openrouter/auto`` (sy-2ag / GH#462).

    The swap only applies when the caller has *not* supplied an explicit
    ``model`` argument — call sites guard on ``model is None`` before
    reaching this function. Explicit choices are honored verbatim so a
    user who deliberately asked for ``openrouter/auto`` still gets it.
    """
    base = _resolve_mcp_default_model()
    model, swapped_from = _fast_default.fast_default_for_panel(base, persona_count)
    if swapped_from is not None:
        logger.info(
            "auto-fast-model: persona_count=%d >= %d, swapping default %s → %s (sy-2ag)",
            persona_count,
            LARGE_PANEL_PERSONA_THRESHOLD,
            swapped_from,
            model,
        )
    return model


def _serialize_content_block(block: Any) -> dict[str, Any]:
    """Render a althing ContentBlock as a JSON-friendly dict.

    Used by tools that surface multimodal sampling output (T6 / hq-l0lw)
    to MCP callers — the tool payload must be JSON-serializable, but our
    ContentBlock dataclasses are not. Only the multimodal types the
    sampling pathway can actually return today (TextBlock, ImageBlock)
    are emitted; future block types should grow a branch here.
    """
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ImageBlock):
        if isinstance(block.source, InlineSource):
            source: dict[str, Any] = {
                "type": "base64",
                "media_type": block.media_type,
                "data": block.source.data,
            }
        elif isinstance(block.source, URLSource):
            source = {"type": "url", "url": block.source.url}
        else:
            source = {"type": "file", "file_id": getattr(block.source, "file_id", "")}
        out: dict[str, Any] = {"type": "image", "source": source}
        if block.cache_control is not None:
            out["cache_control"] = block.cache_control
        return out
    return {"type": "unknown"}


def _looks_like_weighted_model_spec(value: str) -> bool:
    """Return True when *value* matches the CLI's ``name:weight`` spec shape.

    The CLI's ``--models`` flag accepts ``haiku:0.25,gpt-4o-mini:0.25``
    via :func:`althing.cli.commands.parse_models_spec`. The MCP
    surface does not parse that grammar — each string is treated as a
    raw model alias — so ``"haiku:0.25"`` previously routed to a
    nonexistent model and silently produced an empty panel (sp-2rj8).

    Detection heuristic: a single trailing ``:<float>`` after the last
    colon. Legitimate model identifiers that embed colons (``ollama:``
    / ``local:`` prefixes, OpenRouter ``:free`` / ``:beta`` suffixes)
    are preserved because their tail is non-numeric.
    """
    if ":" not in value:
        return False
    if value.startswith(("ollama:", "local:")):
        return False
    tail = value.rsplit(":", 1)[1].strip()
    if not tail:
        return False
    try:
        float(tail)
    except ValueError:
        return False
    return True


def _reject_weighted_model_spec(
    *,
    model: str | None = None,
    models: list[str] | None = None,
    synthesis_model: str | None = None,
    persona_models: dict[str, str] | None = None,
) -> str | None:
    """Return a JSON error string if any model argument uses weighted spec syntax.

    Returns ``None`` when every value is a plain alias. The check covers
    all model-accepting parameters in the MCP surface so the failure is
    caught uniformly at the boundary instead of surfacing as provider
    400s downstream.
    """
    offenders: list[str] = []

    def _check(val: Any) -> None:
        if isinstance(val, str) and _looks_like_weighted_model_spec(val):
            offenders.append(val)

    _check(model)
    _check(synthesis_model)
    if models:
        for m in models:
            _check(m)
    if persona_models:
        for m in persona_models.values():
            _check(m)

    if not offenders:
        return None

    quoted = ", ".join(f"'{o}'" for o in offenders)
    return json.dumps(
        {
            "error": (
                f"Weighted model spec is not supported via MCP (got {quoted}). "
                'Pass model aliases only, e.g. ["haiku", "gpt-4o-mini"]; '
                "weights default to equal across the ensemble."
            )
        }
    )


def _invalid_tool_arg(message: str, *, field_path: str | None = None) -> str:
    """Build a typed ``INVALID_TOOL_ARG`` envelope for an MCP boundary error.

    Mirrors the v1.0.0 ErrorEnvelope shape (``error_code``, ``message``,
    optional ``field_path``, ``schema_version``, ``retry_safe``) and also
    carries a top-level ``error`` field so existing callers that read the
    plain-text message keep working. ``retry_safe`` is False: replaying an
    identical malformed request fails identically (fix the request instead).
    """
    env = ErrorEnvelope(
        error_code="INVALID_TOOL_ARG",
        message=message,
        field_path=field_path,
        schema_version="1.0.0",
        retry_safe=False,
    ).to_dict()
    env["error"] = message
    return json.dumps(env)


def _validate_max_cost(max_cost: float | None) -> str | None:
    """Boundary validation for the ``max_cost`` tool argument (GH#576).

    Returns a typed ``INVALID_TOOL_ARG`` envelope string when the value is
    unusable, or ``None`` when it is absent or valid. Mirrors the CLI's
    ``--max-cost must be > 0`` check so both surfaces refuse the same way.
    """
    if max_cost is None:
        return None
    try:
        value = float(max_cost)
    except (TypeError, ValueError):
        return _invalid_tool_arg(
            f"'max_cost' must be a number (USD ceiling), got {max_cost!r}.",
            field_path="max_cost",
        )
    if value <= 0:
        return _invalid_tool_arg(
            f"'max_cost' must be > 0 (a positive USD ceiling), got {max_cost!r}.",
            field_path="max_cost",
        )
    return None


def _attach_cost_gate_halt(
    result: dict[str, Any],
    *,
    cost_gate: CostGate,
    personas: list[dict[str, Any]],
    completed_names: list[str],
    result_id: str | None,
    tool: str,
    resume_hint: str | None = None,
) -> None:
    """Mark a response envelope as a cost-gate partial (GH#576).

    Mirrors the CLI's ``--max-cost`` trip envelope (sp-utnk): the run is
    flagged ``run_invalid`` with ``cost_exceeded: true``, a specific
    ``abort_reason``, the panelist index at halt, and the machine-readable
    ``cost_gate`` snapshot (spend so far, ceiling, projection). On top of
    the CLI shape, an agent-legible ``resume`` block states exactly how to
    continue: the completed prefix is already persisted under
    ``result_id`` (the MCP analog of the CLI's checkpoint file), and the
    remaining personas can be re-run in a follow-up call.
    """
    snap = cost_gate.snapshot()
    completed_set = set(completed_names)
    remaining = [p.get("name", "Anonymous") for p in personas if p.get("name", "Anonymous") not in completed_set]
    result["run_invalid"] = True
    result["cost_exceeded"] = True
    result["abort_reason"] = "cost_exceeded"
    result["halted_at_panelist"] = cost_gate.completed
    result["cost_gate"] = snap
    if resume_hint is None:
        resume_hint = (
            f"To finish the panel, call {tool} again with only remaining_personas "
            "and a raised (or omitted) max_cost, then combine with the persisted partial. "
            "Synthesis was skipped on this partial to avoid spending past the cap."
        )
    resume: dict[str, Any] = {
        "completed_panelists": list(completed_names),
        "remaining_personas": remaining,
        "how_to_resume": (
            f"The completed prefix ({len(completed_names)} panelist(s), "
            f"${snap['running_cost_usd']:.4f} spent of the ${snap['max_cost_usd']:.4f} cap) "
            + (
                f"is persisted under result_id {result_id!r} — fetch it with get_panel_result({result_id!r}). "
                if result_id
                else "is returned in this envelope. "
            )
            + resume_hint
        ),
    }
    if result_id:
        resume["partial_result_id"] = result_id
    result["resume"] = resume
    warnings_list = result.setdefault("warnings", [])
    if isinstance(warnings_list, list):
        warnings_list.append(
            "cost_exceeded: projected total "
            f"${snap['projected_total_usd']:.4f} > max ${snap['max_cost_usd']:.4f} after "
            f"{snap['completed']}/{snap['total_panelists']} panelists "
            f"(running ${snap['running_cost_usd']:.4f}); pending panelists were cancelled "
            "and synthesis was skipped"
        )


def _panel_timeout_envelope(
    *,
    personas: int,
    model: str,
    questions: int | None = None,
    rounds: int | None = None,
    variants: int = 0,
) -> str:
    """Build a clear timeout envelope so MCP clients see *why* the call failed.

    Without this, an :class:`asyncio.TimeoutError` raised by ``wait_for``
    bubbles up to FastMCP with an empty ``str(exc)`` and the client sees
    ``Error executing tool run_panel:`` with no context (hq-6j40).
    """
    if rounds is not None:
        budget_s = PANELIST_TIMEOUT * max(personas, 1) * max(rounds, 1)
        shape = f"personas={personas} rounds={rounds}"
    else:
        budget_s = PANELIST_TIMEOUT * max(personas, 1) * (1 + variants)
        shape = f"personas={personas} questions={questions} variants={variants}"
    msg = (
        f"Panel run timed out after {budget_s}s ({shape}, model={model!r}). "
        "The provider did not respond within the per-panelist budget. "
        "Retry with fewer personas/questions, a faster model, or check provider health."
    )
    env = {
        "error": msg,
        "error_code": "PANEL_TIMEOUT",
        "schema_version": "1.0.0",
        "retry_safe": True,
        "timeout_seconds": budget_s,
    }
    return json.dumps(env, indent=2)


# Cap on how many valid ids an INVALID_TOOL_ARG envelope enumerates, so a
# store with hundreds of saved results doesn't itself bloat the error.
_MAX_ENUMERATED_IDS = 20


def _unknown_id_envelope(
    kind: str,
    given: str,
    field_path: str,
    valid_ids: list[str],
) -> str:
    """Typed ``INVALID_TOOL_ARG`` for an unknown pack/result/instrument id.

    Turns the raw ``FileNotFoundError`` the data layer raises for a bad
    ``pack_id`` / ``instrument_pack`` / ``result_id`` into the same typed
    envelope the rest of the MCP boundary emits, naming the valid ids when
    that is cheap to enumerate (a store with more than
    :data:`_MAX_ENUMERATED_IDS` entries is truncated with a ``+N more``
    tail so the error stays small).
    """
    if valid_ids:
        shown = valid_ids[:_MAX_ENUMERATED_IDS]
        listing = ", ".join(repr(v) for v in shown)
        if len(valid_ids) > _MAX_ENUMERATED_IDS:
            listing += f", … (+{len(valid_ids) - _MAX_ENUMERATED_IDS} more)"
        valid_clause = f" Valid {kind}s: {listing}."
    else:
        valid_clause = f" No {kind}s are currently available."
    return _invalid_tool_arg(f"Unknown {kind} {given!r}.{valid_clause}", field_path=field_path)


def _resolve_persona_pack_or_error(pack_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Load a persona pack, returning ``(pack, None)`` or ``(None, error_json)``.

    An unknown ``pack_id`` becomes a typed ``INVALID_TOOL_ARG`` envelope
    naming the installed packs, instead of the raw ``FileNotFoundError``
    FastMCP would otherwise surface as a generic tool error.
    """
    try:
        return _data_get_persona_pack(pack_id), None
    except FileNotFoundError:
        valid = [str(p.get("id")) for p in _data_list_persona_packs() if p.get("id")]
        return None, _unknown_id_envelope("persona pack", pack_id, "pack_id", valid)


def _resolve_instrument_pack_or_error(name: str) -> tuple[dict[str, Any] | None, str | None]:
    """Load an instrument pack, returning ``(body, None)`` or ``(None, error_json)``."""
    try:
        return _data_load_instrument_pack(name), None
    except FileNotFoundError:
        valid = [str(p.get("id")) for p in _data_list_instrument_packs() if p.get("id")]
        return None, _unknown_id_envelope("instrument pack", name, "instrument_pack", valid)


def _substitute_instrument_vars_or_error(
    instrument_obj: Instrument,
    template_vars: dict[str, str] | None,
) -> str | None:
    """Substitute ``vars`` into *instrument_obj*, or return a typed error.

    MCP twin of the CLI's ``--var`` / ``--vars-file`` flow (GH#562): reuses
    the same shared engine (:mod:`althing.templates`) for substitution
    and the same fail-fast guard (sp-6yi). If any ``{placeholder}`` in the
    instrument's question text lacks a value — including when ``vars`` is
    omitted entirely — the run aborts with a typed ``INVALID_TOOL_ARG``
    envelope naming the missing keys, instead of silently sending literal
    braces to panelists and corrupting the run.

    Returns ``None`` on success (mutating *instrument_obj* in place when
    ``vars`` is non-empty), or the JSON error envelope string.
    """
    from althing.templates import apply_vars_to_instrument, find_unresolved_in_instrument

    tv = {str(k): str(v) for k, v in (template_vars or {}).items()}
    unresolved = find_unresolved_in_instrument(instrument_obj, tv)
    if unresolved:
        keys = ", ".join(unresolved)
        example = json.dumps({k: "..." for k in unresolved})
        return _invalid_tool_arg(
            f"Instrument has unsubstituted placeholders: {keys}. "
            f"Pass a value for each via the vars parameter, e.g. vars={example}. "
            f"(CLI equivalent: --var {unresolved[0]}=...)",
            field_path="vars",
        )
    if tv:
        apply_vars_to_instrument(instrument_obj, tv)
    return None


def _resolve_panel_result_or_error(result_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Load a saved panel result, returning ``(result, None)`` or ``(None, error_json)``."""
    try:
        return _data_get_panel_result(result_id), None
    except FileNotFoundError:
        valid = [str(r.get("id")) for r in _data_list_panel_results() if r.get("id")]
        return None, _unknown_id_envelope("panel result", result_id, "result_id", valid)


def _total_failure_envelope(exc: PanelTotalFailureError) -> str:
    """Serialize the typed total-failure envelope ``run_panel`` returns.

    Shared by ``run_quick_poll`` and ``extend_panel`` so a knowingly-bad
    model alias produces the same ``run_invalid`` / ``total_failure``
    shape everywhere instead of a raw ``PanelTotalFailureError`` bubbling
    up as a generic FastMCP "Error executing tool".
    """
    return json.dumps(
        {
            "error": str(exc),
            "run_invalid": True,
            "total_failure": exc.diagnostic,
        },
        indent=2,
    )


def _dereference_per_model_transcripts(
    per_model_results: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Strip the duplicated panelist transcript from a NON-ensemble rollup.

    :func:`althing.ensemble.build_mixed_model_rollup` embeds a full
    formatted copy of every panelist's responses under
    ``per_model_results[model]["results"]`` — a byte-for-byte duplicate of
    the canonical ``rounds[].results`` transcript (each canonical row
    already carries its ``model`` tag, so per-model views are recoverable
    by filtering). We keep the per-model ``usage`` / ``cost`` breakdown and
    add a cheap ``result_count`` + ``personas`` reference so consumers can
    still slice by model without the server serialising the transcript two
    or three times.

    The ensemble path (``models=[...]``) is deliberately NOT routed through
    here: there each model ran the panel independently, so its ``results``
    block is a unique, unpersisted transcript rather than a copy.
    """
    compact: dict[str, dict[str, Any]] = {}
    for model_name, entry in per_model_results.items():
        rows = entry.get("results") or []
        new_entry = {k: v for k, v in entry.items() if k != "results"}
        new_entry["result_count"] = len(rows)
        new_entry["personas"] = [r.get("persona") for r in rows if isinstance(r, dict)]
        compact[model_name] = new_entry
    return compact


def _apply_detail(result: dict[str, Any], detail: str) -> dict[str, Any]:
    """Honor the ``detail`` selector on a *persisted* panel-run envelope.

    ``detail="full"`` returns the envelope untouched. ``detail="summary"``
    drops the per-panelist transcripts — the top-level ``results`` mirror
    and each ``rounds[].results`` list — to protect the agent's context
    window, keeping synthesis, ``panel_verdict``, ``poll_summary``,
    ``metadata``, ``per_model_results`` (usage/cost only), costs,
    ``warnings``, ``path`` and ``terminal_round``. The dropped transcript
    stays retrievable via ``get_panel_result`` / the
    ``panel-result://{result_id}`` resource (also on
    ``panel_verdict.full_transcript_uri``); the envelope carries
    ``detail: "summary"`` plus a ``transcript_uri`` pointer and per-round
    ``result_count`` so the omission is self-describing.

    Only applied where the transcript is recoverable (persisted BYOK
    runs). Sampling responses carry no ``result_id`` and are never
    persisted, so their transcripts are returned in full regardless.
    Typed error envelopes (``error_code``) pass through unchanged.
    """
    if not isinstance(result, dict) or detail != "summary" or "error_code" in result:
        return result
    result["detail"] = "summary"
    if "results" in result:
        result.pop("results", None)
        result["results_omitted"] = True
    rounds = result.get("rounds")
    if isinstance(rounds, list):
        for rd in rounds:
            if isinstance(rd, dict) and "results" in rd:
                rd["result_count"] = len(rd.get("results") or [])
                rd.pop("results", None)
    rid = result.get("result_id") or result.get("id")
    if rid:
        result["transcript_uri"] = f"panel-result://{rid}"
    return result


def _normalize_models_param(
    *,
    model: str | None,
    models: list[str] | None,
) -> tuple[str | None, list[str] | None] | str:
    """Normalize ``model`` / ``models`` at the MCP boundary.

    Returns either the normalized ``(model, models)`` tuple or a
    JSON-serialised typed error envelope.

    Rules:
    * Both ``model`` and a non-empty ``models`` set → mutually-exclusive
      error.
    * ``models=[]`` → empty-list error (caller almost certainly meant to
      omit the parameter).
    * ``len(models) == 1`` → forgiving promote: ``model = models[0]`` and
      ``models = None`` so the call routes through the single-model path
      instead of the (length-2-only) ensemble path. Without this promotion
      the caller's chosen model would be silently swapped for the default
      and the request would be billed against the wrong provider.
    * ``len(models) >= 2`` → ensemble path, unchanged.
    * ``models is None`` → pass-through.
    """
    if models is not None:
        if not isinstance(models, list):
            return _invalid_tool_arg(
                f"'models' must be a list of model aliases (got {type(models).__name__}).",
                field_path="models",
            )
        if len(models) == 0:
            return _invalid_tool_arg(
                "'models' must contain at least one model alias, or be omitted.",
                field_path="models",
            )
        if model is not None and len(models) >= 1:
            return _invalid_tool_arg(
                "'model' and 'models' are mutually exclusive — pass one or the other, not both.",
                field_path="models",
            )
        if len(models) == 1:
            return (models[0], None)
    return (model, models)


def _grace_nudge(tool: str) -> str:
    """Response-side warning emitted when AC-4 synthesized the placeholder."""
    return (
        f"{W_DECISION_MISSING}: 'decision_being_informed' was not provided; "
        f"synthesized 'unspecified-legacy-call' under the v1.0.x grace window. "
        f"Supply the decision this {tool} call informs (12-280 chars, single line) — "
        f"v1.1.0 (or ALTHING_SCHEMA_MIN>=1.1.0) hard-rejects the call with MISSING_DECISION."
    )


def _resolve_decision_contract(
    tool: str,
    decision_being_informed: str | None,
) -> tuple[str | None, list[str], str | None]:
    """AC-3/AC-4 request path: grace shim → validator-core.

    Returns ``(decision, warnings, error_json)``:

    * ``decision`` — the caller-supplied value, or the AC-4 placeholder
      ``"unspecified-legacy-call"`` when the field was omitted under the
      v1.0.x grace window. ``None`` only alongside a non-``None``
      ``error_json``.
    * ``warnings`` — the response-side ``W_DECISION_MISSING`` nudge when
      the placeholder was synthesized; empty otherwise.
    * ``error_json`` — JSON-serialised typed error envelope when the
      request violates the contract; ``None`` when it conforms.

    An *omitted* field (``None``) is the AC-4 grace path: the shim
    synthesizes the placeholder, or — under ``ALTHING_SCHEMA_MIN>=1.1.0``
    — leaves it absent so the validator returns ``MISSING_DECISION``.
    A field that was *provided* (even empty/whitespace) skips the shim and
    goes straight to the validator-core: an explicit-but-empty value is a
    caller bug worth a typed error, not legacy traffic to be masked.
    """
    warnings: list[str] = []
    decision = decision_being_informed
    if decision is None:
        graced = apply_legacy_grace(tool, {})
        decision = graced.get("decision_being_informed")
        if decision is not None:
            warnings.append(_grace_nudge(tool))
    payload: dict[str, Any] = {}
    if decision is not None:
        payload["decision_being_informed"] = decision
    err = validate_request(tool, payload)
    if err is not None:
        env = err.to_dict()
        env["error"] = err.message
        return None, [], json.dumps(env)
    return decision, warnings, None


def _persist_stamped_sessions(
    result_id: str,
    sessions: dict[str, Any] | None,
    decision_being_informed: str | None,
) -> None:
    """AC-7: stamp panel sessions with the decision and persist them.

    Only genuine :class:`~althing.persistence.Session` objects are
    stamped/saved — test doubles and legacy sentinels are skipped. Failure
    to persist sessions is non-fatal (the panel result itself is already
    saved); it is logged loudly instead of failing the response.
    """
    if not sessions:
        return
    real: dict[str, Session] = {}
    for name, sess in sessions.items():
        if isinstance(sess, Session):
            if decision_being_informed is not None:
                sess.decision_being_informed = decision_being_informed
            real[name] = sess
    if not real:
        return
    try:
        save_panel_sessions(result_id, real)
    except OSError:
        logger.warning("failed to persist stamped sessions for %s (non-fatal)", result_id, exc_info=True)


def _derive_headline(
    synthesis_dict: dict[str, Any] | None,
    persona_count: int,
    question_count: int,
) -> str:
    """Pick the verdict headline: synthesis recommendation > summary > fallback."""
    if isinstance(synthesis_dict, dict):
        for key in ("recommendation", "summary"):
            val = synthesis_dict.get(key)
            if isinstance(val, str) and val.strip():
                line = val.strip().splitlines()[0].strip()
                if line:
                    return line
    return f"Panel completed: {persona_count} personas x {question_count} question(s)."


def _derive_convergence_dissent(
    poll_summary: dict[str, Any] | None,
) -> tuple[float | None, int]:
    """Derive (convergence, dissent_count) from the deterministic poll summary.

    Uses the first enum-kind question's first-choice distribution: the
    modal share is the agreement score and everyone outside the modal
    bucket is a dissenter. Free-text-only panels have no comparable
    measure — ``(None, 0)`` (the verdict assembler renders ``None`` as
    ``0.0`` and raises no ``low_convergence`` flag).
    """
    if not isinstance(poll_summary, dict):
        return None, 0
    for q in poll_summary.get("questions") or []:
        if not isinstance(q, dict) or q.get("kind") != "enum":
            continue
        counts = q.get("first_choice_counts")
        if not isinstance(counts, dict) or not counts:
            continue
        try:
            values = [int(v) for v in counts.values()]
        except (TypeError, ValueError):
            continue
        total = sum(values)
        if total <= 0:
            continue
        top = max(values)
        return top / total, max(0, total - top)
    return None, 0


_VERBATIM_MAX_CHARS = 240


def _collect_verbatims(result_dicts: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Deterministic ``{persona_id, quote}`` selection for the verdict.

    Takes each panelist's first non-empty free-text response, in panelist
    order, deduped by persona, capped at three. Structured (dict-shaped)
    responses carry no quotable prose and are skipped — the schema allows
    0-3 verbatims.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for rd in result_dicts:
        if len(out) >= 3:
            break
        persona = str(rd.get("persona", ""))
        if not persona or persona in seen:
            continue
        for resp in rd.get("responses") or []:
            if not isinstance(resp, dict):
                continue
            text = resp.get("response")
            if isinstance(text, str) and text.strip():
                out.append({"persona_id": persona, "quote": text.strip()[:_VERBATIM_MAX_CHARS]})
                seen.add(persona)
                break
    return out


def _structured_retry_exhausted(result_dicts: list[dict[str, Any]]) -> bool:
    """True when any schema-forced response exhausted the 3-strike retry.

    Scoped to ``structured`` responses (``response_schema`` tool-forcing):
    a fallback there means the primary artifact is untrustworthy. Post-hoc
    ``extract_schema`` fallbacks are auxiliary annotations over intact
    free text and surface via ``extraction_is_fallback`` instead.
    """
    for rd in result_dicts:
        for resp in rd.get("responses") or []:
            if isinstance(resp, dict) and resp.get("structured") and resp.get("is_fallback"):
                return True
    return False


def _finalize_contract_response(
    result: dict[str, Any],
    *,
    decision_being_informed: str | None,
    decision_warnings: list[str] | tuple[str, ...] = (),
    panelist_results: list[PanelistResult],
    personas: list[dict[str, Any]],
    result_dicts: list[dict[str, Any]],
    synthesis_dict: dict[str, Any] | None,
    poll_summary: dict[str, Any] | None,
    result_id: str,
) -> dict[str, Any]:
    """AC-6/AC-8: attach the v1.0.0 ``panel_verdict`` to a success envelope.

    Builds the verdict from the run's post-synthesis state and returns the
    envelope carrying ``panel_verdict`` + top-level ``schema_version`` so
    the AC-9 gate validates it on egress. When the structured-output
    3-strike retry exhausted (schema drift), the AC-8 contract pivot
    applies: with ``ALTHING_DRIFT_DEGRADE`` on, the degraded verdict
    (``schema_drift`` warn flag) ships inside the normal envelope; with it
    off (v1.0.0 default), the whole envelope is replaced by the typed
    ``SCHEMA_DRIFT`` error (``retry_safe=True``).

    ``decision_being_informed=None`` (direct legacy callers) returns the
    envelope untouched — the contract fields ride only on decision-scoped
    runs.
    """
    if decision_being_informed is None:
        return result

    if decision_warnings:
        result["warnings"] = [*list(result.get("warnings") or []), *decision_warnings]

    drift = _structured_retry_exhausted(result_dicts)
    convergence, dissent = _derive_convergence_dissent(poll_summary)
    panel_state = PanelState(
        panelist_results=panelist_results,
        personas=personas,
        convergence=convergence,
        schema_drift=drift,
    )
    transcript_uri = f"panel-result://{result_id}"
    verdict = build_panel_verdict(
        decision_being_informed=decision_being_informed,
        panel_state=panel_state,
        headline=_derive_headline(
            synthesis_dict,
            int(result.get("persona_count") or len(personas)),
            int(result.get("question_count") or 0),
        ),
        full_transcript_uri=transcript_uri,
        top_3_verbatims=_collect_verbatims(result_dicts),
        dissent_count=dissent,
    )

    if drift:
        outcome = exhausted_retry_outcome(
            partial_artifact=verdict,
            decision_being_informed=decision_being_informed,
            full_transcript_uri=transcript_uri,
        )
        if isinstance(outcome, ErrorEnvelope):
            env = outcome.to_dict()
            env["error"] = outcome.message
            return env
        verdict = outcome

    result["panel_verdict"] = verdict
    result["schema_version"] = "1.0.0"
    return result


# Re-export for backward compatibility — callers patch these names.
__all__ = [
    "EXTRACT_SCHEMA_REGISTRY",
    "LARGE_PANEL_PERSONA_THRESHOLD",
    "MAX_PERSONAS",
    "MAX_QUESTIONS",
    "MCP_DEFAULT_MODEL",
    "PANELIST_TIMEOUT",
    "SAMPLING_MAX_PERSONAS",
    "SAMPLING_MAX_QUESTIONS",
    "_compute_variant_data",
    "mcp",
    "serve",
]


mcp = MCPServer(
    "althing",
    instructions=(
        "Synthetic focus group server. Run panels of AI personas to get "
        "structured qualitative feedback on products, concepts, and names."
    ),
    # SDK 2.0's public ``version=`` replaces the pre-2.0 hack of assigning
    # ``mcp._mcp_server.version`` — without it the low-level server falls
    # back to ``importlib.metadata.version("mcp")`` and leaks the SDK
    # version into serverInfo.
    version=_althing_version,
)


# Minimal default persona set for ``run_quick_poll`` — three diverse
# voices so the first-run story works without hand-crafting personas.
# Kept intentionally small: sampling mode caps at SAMPLING_MAX_PERSONAS
# and we want the BYOK path to stay cheap by default too.
DEFAULT_QUICK_POLL_PERSONAS: list[dict[str, Any]] = [
    {
        "name": "Alex Rivera",
        "age": 29,
        "occupation": "Software Engineer",
        "background": "Early-career developer at a mid-sized SaaS company.",
        "personality_traits": ["analytical", "curious", "pragmatic"],
    },
    {
        "name": "Jordan Park",
        "age": 42,
        "occupation": "Small Business Owner",
        "background": "Runs an independent retail shop; values clarity and ROI.",
        "personality_traits": ["practical", "skeptical", "value-driven"],
    },
    {
        "name": "Sam Okafor",
        "age": 35,
        "occupation": "Marketing Manager",
        "background": "Leads growth at a consumer brand; follows trends closely.",
        "personality_traits": ["creative", "social", "brand-aware"],
    },
]

# Shared LLM client — reused across tool calls to avoid rebuilding the
# provider cache on every invocation.  Thread-safe by design (see LLMClient).
# Lazy-initialised so that module import in test/CI contexts doesn't trigger
# provider resolution before patches or env vars are set up.
_shared_client: LLMClient | None = None


def _get_shared_client() -> LLMClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = LLMClient()
    return _shared_client


# ---------------------------------------------------------------------------
# Internal panel runner (bridges threads to async)
# ---------------------------------------------------------------------------


def _server_run_panel_sync(
    personas: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    model: str,
    response_schema: dict[str, Any] | None = None,
    *,
    synthesis: bool = True,
    synthesis_model: str | None = None,
    synthesis_prompt: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    persona_models: dict[str, str] | None = None,
    extract_schema: dict[str, Any] | None = None,
    synthesis_temperature: float | None = None,
    variants: int = 0,
    sessions_out: dict[str, Any] | None = None,
    cost_gate: CostGate | None = None,
) -> tuple[
    list[PanelistResult], list[dict[str, Any]], CostTokenUsage, Any, dict[str, Any] | None, dict[str, Any] | None
]:
    """Thin shim around :func:`althing._runners.run_panel_sync` using the shared client."""
    return _run_panel_sync(
        _get_shared_client(),
        personas,
        questions,
        model,
        response_schema,
        synthesis=synthesis,
        synthesis_model=synthesis_model,
        synthesis_prompt=synthesis_prompt,
        temperature=temperature,
        top_p=top_p,
        persona_models=persona_models,
        extract_schema=extract_schema,
        synthesis_temperature=synthesis_temperature,
        variants=variants,
        sessions_out=sessions_out,
        cost_gate=cost_gate,
    )


def _server_run_multi_round_sync(
    personas: list[dict[str, Any]],
    instrument: Instrument,
    model: str,
    response_schema: dict[str, Any] | None,
    *,
    synthesis: bool,
    synthesis_model: str | None,
    synthesis_prompt: str | None,
    temperature: float | None = None,
    top_p: float | None = None,
    persona_models: dict[str, str] | None = None,
    extract_schema: dict[str, Any] | None = None,
    synthesis_temperature: float | None = None,
) -> MultiRoundResult:
    """Thin shim around :func:`althing._runners.run_multi_round_sync` using the shared client."""
    return _run_multi_round_sync(
        _get_shared_client(),
        personas,
        instrument,
        model,
        response_schema,
        synthesis=synthesis,
        synthesis_model=synthesis_model,
        synthesis_prompt=synthesis_prompt,
        temperature=temperature,
        top_p=top_p,
        persona_models=persona_models,
        extract_schema=extract_schema,
        synthesis_temperature=synthesis_temperature,
    )


def _run_ensemble_sync(
    personas: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    models: list[str],
    response_schema: dict[str, Any] | None = None,
    extract_schema: dict[str, Any] | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
) -> dict[str, Any]:
    """Run the same panel with each model and return comparative results."""
    from althing._runners import format_total_failure_message
    from althing.ensemble import build_ensemble_output, ensemble_run

    client = LLMClient()
    ens = ensemble_run(
        personas,
        questions,
        models,
        client,
        system_prompt_fn=persona_system_prompt,
        question_prompt_fn=build_question_prompt,
        response_schema=response_schema,
        extract_schema=extract_schema,
        temperature=temperature,
        top_p=top_p,
    )

    # sp-efip: fail loud when every panelist of every model failed.
    # Without this, ensemble runs with a knowingly-bad model name
    # returned a well-shaped result containing 0-token panelists.
    ensemble_panelists: list[PanelistResult] = []
    for mr in ens.model_results:
        ensemble_panelists.extend(mr.panelist_results)
    ensemble_failure = detect_total_failure(ensemble_panelists)
    if ensemble_failure is not None:
        raise PanelTotalFailureError(
            format_total_failure_message(ensemble_failure),
            diagnostic=ensemble_failure,
        )

    return build_ensemble_output(ens, panelist_formatter=_format_panelist_result)


async def _run_panel_async_instrument(
    personas: list[dict[str, Any]],
    instrument: Instrument,
    model: str,
    ctx: Context,
    response_schema: dict[str, Any] | None = None,
    *,
    synthesis: bool = True,
    synthesis_model: str | None = None,
    synthesis_prompt: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    persona_models: dict[str, str] | None = None,
    extract_schema: dict[str, Any] | None = None,
    synthesis_temperature: float | None = None,
    decision_being_informed: str | None = None,
    decision_warnings: list[str] | tuple[str, ...] = (),
    detail: str = "summary",
) -> dict[str, Any]:
    """Run a (possibly branching) instrument and return v3-shaped response.

    ``detail`` selects the transcript verbosity of the returned envelope:
    ``"full"`` keeps every panelist row; ``"summary"`` (the default for
    ``run_panel``) drops the transcripts via :func:`_apply_detail` — see
    that helper for the retained/omitted field split.
    """
    total = len(personas)
    timer = PanelTimer()
    await ctx.report_progress(0, total)

    mr: MultiRoundResult = await asyncio.wait_for(
        asyncio.to_thread(
            _server_run_multi_round_sync,
            personas,
            instrument,
            model,
            response_schema,
            synthesis=synthesis,
            synthesis_model=synthesis_model,
            synthesis_prompt=synthesis_prompt,
            temperature=temperature,
            top_p=top_p,
            persona_models=persona_models,
            extract_schema=extract_schema,
            synthesis_temperature=synthesis_temperature,
        ),
        # Multi-round can chain N rounds; budget per panelist scales with rounds.
        timeout=PANELIST_TIMEOUT * max(total, 1) * max(len(instrument.rounds), 1),
    )

    await ctx.report_progress(total, total)

    rounds_payload: list[dict[str, Any]] = []
    flat_results: list[dict[str, Any]] = []
    total_question_count = 0
    for rr in mr.rounds:
        round_dict_results = [_format_panelist_result(pr, model) for pr in rr.panelist_results]
        questions_for_round = next((r.questions for r in instrument.rounds if r.name == rr.name), [])
        total_question_count += len(questions_for_round)
        rounds_payload.append(
            {
                "name": rr.name,
                "results": round_dict_results,
                "synthesis": rr.synthesis.to_dict() if hasattr(rr.synthesis, "to_dict") else None,
                "usage": rr.usage.to_dict(),
            }
        )
        # Flat results for back-compat / persistence: use the *last* round per persona.
        flat_results = round_dict_results

    pricing, _ = lookup_pricing(model)

    timer.stop()

    final_synth_dict = (
        mr.final_synthesis.to_dict()
        if mr.final_synthesis is not None and hasattr(mr.final_synthesis, "to_dict")
        else None
    )

    # sp-atvc: aggregate panelist usage/cost per actual model across all
    # rounds so multi-model instrument runs get accurate per-model cost
    # instead of pricing every bucket at the default model's rate.
    all_panelist_results: list[Any] = [pr for rr in mr.rounds for pr in rr.panelist_results]
    per_model_usage, per_model_cost = aggregate_per_model(all_panelist_results, model)
    multi_model_run = len(per_model_usage) > 1

    panelist_usage = ZERO_USAGE
    for rr in mr.rounds:
        for pr in rr.panelist_results:
            panelist_usage = panelist_usage + pr.usage

    if multi_model_run:
        panelist_cost_est = CostEstimate()
        for _c in per_model_cost.values():
            panelist_cost_est = panelist_cost_est + _c
    else:
        panelist_cost_est = estimate_cost(panelist_usage, pricing)

    synthesis_usage_for_meta: CostTokenUsage | None = None
    synthesis_cost_for_meta = None
    if mr.final_synthesis is not None and hasattr(mr.final_synthesis, "usage"):
        synthesis_usage_for_meta = mr.final_synthesis.usage
        synth_model = getattr(mr.final_synthesis, "model", model)
        synth_pricing, _ = lookup_pricing(synth_model)
        synthesis_cost_for_meta = estimate_cost(synthesis_usage_for_meta, synth_pricing)

    # Derive the run's reported total_cost from the accurate components
    # instead of the single-model rollup that mr.usage assumes.
    total_cost = panelist_cost_est + (synthesis_cost_for_meta or CostEstimate())

    panelist_per_model_meta = (
        {_m: (per_model_usage[_m], per_model_cost[_m]) for _m in per_model_usage} if multi_model_run else None
    )
    inst_metadata = build_metadata(
        panelist_model=model,
        synthesis_model=getattr(mr.final_synthesis, "model", None) if mr.final_synthesis else None,
        panelist_usage=panelist_usage,
        panelist_cost=panelist_cost_est,
        synthesis_usage=synthesis_usage_for_meta,
        synthesis_cost=synthesis_cost_for_meta,
        total_usage=mr.usage,
        total_cost=total_cost,
        persona_count=len(personas),
        question_count=total_question_count,
        timer=timer,
        panelist_per_model=panelist_per_model_meta,
    )

    result_id = save_panel_result(
        results=flat_results,
        model=model,
        total_usage=mr.usage.to_dict(),
        total_cost=total_cost.format_usd(),
        persona_count=len(personas),
        question_count=total_question_count,
        synthesis=final_synth_dict,
        metadata=inst_metadata,
        decision_being_informed=decision_being_informed,
    )
    # AC-7: persist decision-stamped panelist transcripts alongside the result.
    _persist_stamped_sessions(result_id, mr.sessions, decision_being_informed)

    # sp-0h9x: mirror the ensemble path's per-model rollup onto every
    # non-ensemble panel result. The terminal round's panelist list is the
    # canonical "one row per persona" view, so grouping those by model
    # matches the flat ``results`` field that back-compat consumers read.
    from althing.ensemble import build_mixed_model_rollup

    terminal_prs = mr.rounds[-1].panelist_results if mr.rounds else []
    per_model_results, cost_breakdown = build_mixed_model_rollup(
        terminal_prs,
        default_model=model,
        panelist_formatter=lambda pr, m: _format_panelist_result(pr, m),
    )
    # Drop the per-model transcript copy; ``rounds[].results`` is canonical.
    per_model_results = _dereference_per_model_transcripts(per_model_results)

    # sp-nn8k: warn loudly when any contributing model was priced via
    # DEFAULT_PRICING fallback instead of an explicit tier. Candidates are
    # every model we actually priced above plus the synthesis model.
    synth_model_name = getattr(mr.final_synthesis, "model", None) if mr.final_synthesis else None
    cost_warnings = build_cost_fallback_warnings([*per_model_usage.keys(), synth_model_name])
    merged_warnings = list(mr.warnings) + cost_warnings

    # sy-4yd: attach the deterministic structured-response summary so
    # MCP callers can read vote counts / weighted scores / objections
    # from the same envelope without a second tool call. Built from the
    # final result shape (same as the SDK) so it survives readback.
    from althing.poll_summary import build_poll_summary

    poll_summary_obj = build_poll_summary(
        {"results": flat_results, "rounds": rounds_payload, "synthesis": final_synth_dict},
        personas=personas,
    )
    poll_summary_payload = (
        poll_summary_obj.to_dict()
        if (poll_summary_obj.questions or poll_summary_obj.top_objections or poll_summary_obj.recommended_next_test)
        else None
    )

    result = {
        "result_id": result_id,
        "model": model,
        "persona_count": len(personas),
        "question_count": total_question_count,
        "rounds": rounds_payload,
        "path": mr.path,
        "terminal_round": mr.terminal_round,
        "warnings": merged_warnings,
        "cost_is_estimated": bool(cost_warnings),
        "synthesis": final_synth_dict,
        "total_cost": total_cost.format_usd(),
        "total_usage": mr.usage.to_dict(),
        # Back-compat: ``results`` mirrors the terminal round's flat panelist
        # list so v1/v2 callers see the same shape they did pre-0.5.0.
        "results": flat_results,
        "per_model_results": per_model_results,
        "cost_breakdown": cost_breakdown,
        "metadata": inst_metadata,
        "poll_summary": poll_summary_payload,
    }

    # v1.0.0 contract fields. Drift detection scans every executed round's
    # results (not just the terminal round) so an early-round 3-strike
    # exhaustion still triggers the AC-8 pivot.
    all_round_result_dicts = [rd for rp in rounds_payload for rd in rp["results"]]
    finalized = _finalize_contract_response(
        result,
        decision_being_informed=decision_being_informed,
        decision_warnings=decision_warnings,
        panelist_results=terminal_prs,
        personas=personas,
        result_dicts=all_round_result_dicts,
        synthesis_dict=final_synth_dict,
        poll_summary=poll_summary_payload,
        result_id=result_id,
    )
    return _apply_detail(finalized, detail)


async def _run_panel_async(
    personas: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    model: str,
    ctx: Context,
    response_schema: dict[str, Any] | None = None,
    *,
    synthesis: bool = True,
    synthesis_model: str | None = None,
    synthesis_prompt: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    persona_models: dict[str, str] | None = None,
    extract_schema: dict[str, Any] | None = None,
    synthesis_temperature: float | None = None,
    variants: int = 0,
    decision_being_informed: str | None = None,
    decision_warnings: list[str] | tuple[str, ...] = (),
    detail: str = "summary",
    max_cost: float | None = None,
) -> dict[str, Any]:
    """Run panel via asyncio.to_thread with progress notifications.

    ``decision_being_informed`` / ``decision_warnings`` come from
    :func:`_resolve_decision_contract`. When a decision is present (real or
    AC-4 placeholder) the run persists it in the saved result, stamps the
    panelist transcripts (AC-7), and attaches the ``panel_verdict`` +
    ``schema_version`` contract fields to the envelope (AC-6/AC-8/AC-9).

    ``detail`` selects the transcript verbosity: ``"full"`` keeps every
    panelist row; ``"summary"`` (the default) drops the top-level
    ``results`` mirror and ``rounds[].results`` via :func:`_apply_detail`
    so a large BYOK panel doesn't flood the caller's context.

    ``max_cost`` (GH#576) arms a :class:`~althing.cost.CostGate` sized
    to the dispatched personas — the exact machinery behind the CLI's
    ``--max-cost``. On a trip, the returned envelope is a valid partial
    with ``run_invalid``, ``cost_exceeded``, ``abort_reason:
    "cost_exceeded"``, the ``cost_gate`` snapshot, and a ``resume`` block
    (see :func:`_attach_cost_gate_halt`).
    """
    total = len(personas)
    timer = PanelTimer()
    await ctx.report_progress(0, total)

    # GH#576: arm the projected-total cost gate before dispatch. Sized to
    # the personas actually being dispatched, same as the CLI wiring.
    cost_gate: CostGate | None = None
    if max_cost is not None:
        cost_gate = CostGate(max_cost_usd=max_cost, total_panelists=total)

    # Run the blocking panel execution in a thread
    run_sessions: dict[str, Any] = {}
    (
        panelist_results_full,
        result_dicts,
        panelist_usage,
        panelist_cost,
        synthesis_dict,
        variant_data,
    ) = await asyncio.wait_for(
        asyncio.to_thread(
            _server_run_panel_sync,
            personas,
            questions,
            model,
            response_schema,
            synthesis=synthesis,
            synthesis_model=synthesis_model,
            synthesis_prompt=synthesis_prompt,
            temperature=temperature,
            top_p=top_p,
            persona_models=persona_models,
            extract_schema=extract_schema,
            synthesis_temperature=synthesis_temperature,
            variants=variants,
            sessions_out=run_sessions,
            cost_gate=cost_gate,
        ),
        timeout=PANELIST_TIMEOUT * total * (1 + variants),
    )

    await ctx.report_progress(total, total)

    # sp-atvc: re-price panelist usage per actual model when panelists
    # were dispatched across multiple providers (persona_models routing).
    # Without this, total_cost prices every token at the default model's
    # rate and metadata.cost.per_model hides the cheaper/dearer providers.
    per_model_usage, per_model_cost = aggregate_per_model(panelist_results_full, model)
    multi_model_run = len(per_model_usage) > 1
    if multi_model_run:
        panelist_cost = CostEstimate()
        for _c in per_model_cost.values():
            panelist_cost = panelist_cost + _c

    # Compute total cost (panelist + synthesis)
    synthesis_usage_obj: CostTokenUsage | None = None
    synthesis_cost_obj = None
    # sp-avmm: synthesis_dict may be an error envelope (no "usage" key) when
    # the pre-flight check or the API call failed. Guard the cost arithmetic
    # so we do not KeyError before we get a chance to surface the error.
    if synthesis_dict and "usage" in synthesis_dict:
        synthesis_usage_obj = CostTokenUsage.from_dict(synthesis_dict["usage"])
        synthesis_pricing, _ = lookup_pricing(synthesis_dict.get("model"))
        synthesis_cost_obj = estimate_cost(synthesis_usage_obj, synthesis_pricing)
        total_usage = panelist_usage + synthesis_usage_obj
        total_cost = panelist_cost + synthesis_cost_obj
    else:
        total_usage = panelist_usage
        total_cost = panelist_cost

    timer.stop()
    panelist_per_model_meta = (
        {_m: (per_model_usage[_m], per_model_cost[_m]) for _m in per_model_usage} if multi_model_run else None
    )
    metadata = build_metadata(
        panelist_model=model,
        synthesis_model=synthesis_dict.get("model") if synthesis_dict else None,
        panelist_usage=panelist_usage,
        panelist_cost=panelist_cost,
        synthesis_usage=synthesis_usage_obj,
        synthesis_cost=synthesis_cost_obj,
        total_usage=total_usage,
        total_cost=total_cost,
        persona_count=len(personas),
        question_count=len(questions),
        timer=timer,
        panelist_per_model=panelist_per_model_meta,
    )

    # Save result
    variant_count = variant_data["variant_count"] if variant_data else 0
    result_id = save_panel_result(
        results=result_dicts,
        model=model,
        total_usage=total_usage.to_dict(),
        total_cost=total_cost.format_usd(),
        persona_count=len(personas),
        question_count=len(questions),
        variant_count=variant_count,
        synthesis=synthesis_dict,
        metadata=metadata,
        decision_being_informed=decision_being_informed,
    )
    # AC-7: persist decision-stamped panelist transcripts alongside the result.
    _persist_stamped_sessions(result_id, run_sessions, decision_being_informed)

    # sp-0h9x: emit per_model_results + cost_breakdown so downstream
    # consumers see the same rollup shape as the ensemble path, even on
    # single-model and mixed-model (persona_models) panels.
    from althing.ensemble import build_mixed_model_rollup

    per_model_results, cost_breakdown = build_mixed_model_rollup(
        panelist_results_full,
        default_model=model,
        panelist_formatter=lambda pr, m: _format_panelist_result(pr, m),
    )
    # Drop the per-model transcript copy; ``rounds[].results`` is canonical.
    per_model_results = _dereference_per_model_transcripts(per_model_results)

    # sp-nn8k: surface DEFAULT_PRICING fallback loudly so estimated totals
    # don't blend into billed ones silently.
    synth_model_name = synthesis_dict.get("model") if synthesis_dict else None
    cost_warnings = build_cost_fallback_warnings([*per_model_usage.keys(), synth_model_name])

    # sy-4yd: deterministic poll summary on the single-round envelope too.
    from althing.poll_summary import build_poll_summary

    poll_summary_obj = build_poll_summary(
        {"results": result_dicts, "questions": questions, "synthesis": synthesis_dict},
        personas=personas,
    )
    poll_summary_payload = (
        poll_summary_obj.to_dict()
        if (poll_summary_obj.questions or poll_summary_obj.top_objections or poll_summary_obj.recommended_next_test)
        else None
    )

    result: dict[str, Any] = {
        "result_id": result_id,
        "model": model,
        "persona_count": len(personas),
        "question_count": len(questions),
        "panelist_cost": panelist_cost.format_usd(),
        "synthesis": synthesis_dict,
        "total_cost": total_cost.format_usd(),
        "total_usage": total_usage.to_dict(),
        "rounds": [
            {
                "name": "default",
                "results": result_dicts,
                "synthesis": None,
            }
        ],
        "path": [],
        # Envelope uniformity (sy-envlp): the flat-questions path now emits
        # the same top-level ``results`` mirror + ``terminal_round`` keys the
        # instrument and sampling paths already carry, so an agent keying on
        # ``results`` doesn't KeyError on a plain-questions BYOK run. Both
        # honor ``detail`` — the mirror is dropped under ``summary``.
        "terminal_round": "default",
        "results": result_dicts,
        "warnings": list(cost_warnings),
        "cost_is_estimated": bool(cost_warnings),
        "per_model_results": per_model_results,
        "cost_breakdown": cost_breakdown,
        "metadata": metadata,
        "poll_summary": poll_summary_payload,
    }

    # sp-avmm: synthesis failure must surface loudly at the envelope
    # top-level. Without this, MCP callers see synthesis: {synthesis_error:
    # …} buried inside the result and cannot spot the failure without
    # inspecting the nested payload. It does NOT set run_invalid — the
    # panelist responses are complete and usable; degrade to a warning so
    # callers can re-synthesize instead of discarding the run.
    if synthesis_dict and isinstance(synthesis_dict.get("synthesis_error"), dict):
        result["synthesis_error"] = synthesis_dict["synthesis_error"]
        result["warnings"].append(
            "synthesis_failed: "
            f"{synthesis_dict['synthesis_error'].get('message', 'synthesis failed')}"
            " — panelist responses are complete"
        )

    # GH#576: cost gate tripped mid-run — the orchestrator cancelled the
    # pending panelists and ``result_dicts`` is a valid completed prefix.
    # Surface the same typed partial-result markers the CLI emits, plus an
    # agent-legible resume block.
    if cost_gate is not None and cost_gate.should_halt():
        _attach_cost_gate_halt(
            result,
            cost_gate=cost_gate,
            personas=personas,
            completed_names=[pr.persona_name for pr in panelist_results_full],
            result_id=result_id,
            tool="run_panel",
        )

    if variant_data:
        result["robustness_scores"] = variant_data["robustness_scores"]
        result["per_persona_robustness"] = variant_data["per_persona_robustness"]
        result["variant_count"] = variant_data["variant_count"]

    # v1.0.0 contract fields (panel_verdict, schema_version, warnings nudge).
    finalized = _finalize_contract_response(
        result,
        decision_being_informed=decision_being_informed,
        decision_warnings=decision_warnings,
        panelist_results=panelist_results_full,
        personas=personas,
        result_dicts=result_dicts,
        synthesis_dict=synthesis_dict,
        poll_summary=poll_summary_payload,
        result_id=result_id,
    )
    return _apply_detail(finalized, detail)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def run_prompt(
    prompt: str,
    model: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    use_sampling: bool | None = None,
    accept_multimodal_sampling: bool = False,
    ctx: Context = None,
) -> str | InputRequiredResult:
    """Send a single prompt to an LLM and get a response. No personas required.

    The simplest tool — ask a quick research question without constructing
    personas or running a full panel.

    Two execution modes:

    * **BYOK** (bring-your-own-key): calls a provider directly using env
      credentials (``ANTHROPIC_API_KEY``, etc.). Supports the full model
      list and per-call cost accounting.
    * **Sampling**: when no creds are set and the invoking MCP client
      (Claude Desktop, Claude Code, Cursor, Windsurf) advertises the
      ``sampling`` capability, althing asks the client to run the
      completion itself. Model is whatever the host agent is using, and
      token cost is charged to the host agent's subscription rather
      than reported here.

    Args:
        prompt: The question or prompt to send.
        model: LLM model to use. Defaults to haiku. Ignored in sampling
            mode (the host agent picks its own model).
        temperature: Sampling temperature (0.0-1.0). Controls randomness.
        top_p: Nucleus sampling threshold (0.0-1.0). Alternative to
            temperature. Ignored in sampling mode.
        use_sampling: Explicit mode override. ``True`` forces sampling
            (error if unsupported), ``False`` forces BYOK. ``None``
            auto-picks based on creds + client capability.
        accept_multimodal_sampling: Opt into preserving image/document
            blocks the MCP host returns from sampling (T6 / hq-l0lw).
            Default-off — the host's image content is silently dropped
            and only text is surfaced, preserving the contract callers
            relied on before this flag landed. Multimodal turns can
            cost ~10x a text-only turn, so this is per-call opt-in.
    """
    spec_error = _reject_weighted_model_spec(model=model)
    if spec_error is not None:
        return spec_error
    model = model or _resolve_mcp_default_model()
    decision = _decide_sampling_mode(ctx, use_sampling=use_sampling, model=model)
    logger.info("run_prompt: mode=%s model=%s prompt_len=%d", decision.mode, model, len(prompt))

    if decision.mode == "error":
        return json.dumps({"error": decision.error})

    if decision.mode == "sampling":
        bridge = SamplingBridge(ctx)
        try:
            gathered = await bridge.gather(
                {"prompt": SampleSpec(prompt=prompt, max_tokens=4096, temperature=temperature)},
                accept_multimodal=accept_multimodal_sampling,
            )
        except SamplingSuspend as suspend:
            # 2026-07-28+ client: sampling rides the InputRequiredResult
            # retry loop instead of a mid-call server→client request.
            return suspend.to_input_required()
        sample = gathered["prompt"]
        # sp-k2ed4a: surface host-side truncation so callers see why a
        # prompt response is short, not just a generic empty result.
        warnings = [sample["warning"]] if sample.get("truncated") and sample.get("warning") else []
        payload: dict[str, Any] = {
            "response": sample["text"],
            "model": sample["model"],
            "mode": "sampling",
            "usage": None,
            "cost": None,
            "hint": decision.hint,
            "warnings": warnings,
        }
        if accept_multimodal_sampling:
            blocks = sample.get("content_blocks") or []
            payload["content_blocks"] = [_serialize_content_block(b) for b in blocks]
        return json.dumps(payload, indent=2)

    client = _get_shared_client()
    request = CompletionRequest(
        model=model,
        max_tokens=4096,
        messages=[InputMessage(role="user", content=[TextBlock(text=prompt)])],
        temperature=temperature,
        top_p=top_p,
    )
    response = await asyncio.to_thread(client.send, request)
    usage = CostTokenUsage(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cache_creation_input_tokens=response.usage.cache_write_tokens,
        cache_read_input_tokens=response.usage.cache_read_tokens,
        provider_reported_cost=response.usage.provider_reported_cost,
        reasoning_tokens=response.usage.reasoning_tokens,
        cached_tokens=response.usage.cached_tokens,
    )
    cost = resolve_cost(usage, model)
    return json.dumps(
        {
            "response": response.text,
            "model": response.model,
            "mode": "byok",
            "usage": usage.to_dict(),
            "cost": cost.format_usd(),
        },
        indent=2,
    )


@mcp.tool()
async def run_panel(
    questions: list[dict[str, Any]] | None = None,
    personas: list[dict[str, Any]] | None = None,
    pack_id: str | None = None,
    instrument: dict[str, Any] | None = None,
    instrument_pack: str | None = None,
    vars: dict[str, str] | None = None,
    model: str | None = None,
    response_schema: dict[str, Any] | None = None,
    synthesis: bool = True,
    synthesis_model: str | None = None,
    synthesis_prompt: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    persona_models: dict[str, str] | None = None,
    extract_schema: str | dict[str, Any] | None = None,
    models: list[str] | None = None,
    synthesis_temperature: float | None = None,
    variants: int | None = None,
    max_cost: float | None = None,
    use_sampling: bool | None = None,
    accept_multimodal_sampling: bool = False,
    decision_being_informed: str | None = None,
    detail: str = "summary",
    ctx: Context = None,
) -> str | InputRequiredResult:
    """Run a full synthetic focus group panel.

    Each persona answers all questions independently in parallel.
    After responses are collected, a synthesis step aggregates findings
    into themes, agreements, disagreements, and recommendations.
    Results are saved and can be retrieved later.

    Three input modes for the question stream:

    1. Inline ``questions`` list — single round, v1-equivalent.
    2. Inline ``instrument`` dict — a v1/v2/v3 instrument body. v3
       instruments with ``route_when`` clauses run as a branching
       multi-round panel.
    3. ``instrument_pack`` name — load an installed instrument pack
       from ``$SYNTH_PANEL_DATA_DIR/packs/instruments/<name>.yaml``.

    Response shape: every successful run returns ``rounds`` (list of
    per-round panelist results + synthesis), ``path`` (the executed
    routing decisions: ``[{round, branch, next}]``), ``warnings``
    (parser + runtime warnings), and ``terminal_round`` (the round
    whose synthesis fed final synthesis). For v1/v2 instruments and
    raw ``questions`` input, ``path`` has length 1 or N (linear) and
    ``warnings`` is empty in the typical case — the shape is uniform
    across versions so callers don't need to special-case.

    Every non-ensemble run also includes ``per_model_results`` and
    ``cost_breakdown`` (``{by_model, total}``). To avoid re-serialising
    the transcript, the non-ensemble ``per_model_results[model]`` carries
    ``{usage, cost, result_count, personas}`` — the per-model spend plus a
    cheap reference list — NOT a second copy of every panelist's
    responses; the canonical transcript is ``rounds[].results``, whose
    rows are already ``model``-tagged for filtering. (The ``models=[...]``
    ensemble path still returns ``{results, cost, usage}`` per model,
    because there each model's transcript is unique and unpersisted.)
    Single-model panels produce a one-entry dict; ``persona_models`` runs
    produce one entry per distinct model.

    Response verbosity is controlled by ``detail`` (see below): the
    default ``"summary"`` omits the per-panelist transcripts to protect
    the caller's context window; pass ``detail="full"`` (or fetch
    ``get_panel_result``) for the complete rows.

    Args:
        questions: Flat list of question dicts (v1-equivalent). Each
            should have a ``text`` key. Ignored when ``instrument`` or
            ``instrument_pack`` is provided.
        personas: Inline persona definitions. Each persona is a JSON
            object with the following recognized fields (additional
            fields are preserved and remain available to custom
            prompt templates):

            * ``name`` (str, **required**) — persona's display name.
            * ``age`` (int, optional) — persona's age.
            * ``occupation`` (str, optional) — job title or role.
            * ``background`` (str, optional) — paragraph-sized bio
              giving context (company, tenure, constraints, etc.).
            * ``personality_traits`` (list[str], optional) — short
              trait adjectives, e.g. ``["analytical", "skeptical"]``.

            Example::

                [
                  {
                    "name": "Sarah Chen",
                    "age": 34,
                    "occupation": "Product Manager",
                    "background": "8 years in tech at a mid-size SaaS company; manages a team of 5.",
                    "personality_traits": ["analytical", "pragmatic", "detail-oriented"]
                  },
                  {
                    "name": "Marcus Johnson",
                    "age": 52,
                    "occupation": "Small Business Owner",
                    "background": "Runs a family restaurant chain; values simplicity over features.",
                    "personality_traits": ["practical", "skeptical of technology"]
                  }
                ]
        pack_id: ID of a saved persona pack. Merged with inline
            personas (inline first). At least one of ``personas`` or
            ``pack_id`` must be provided.
        instrument: Raw instrument body (the value under the
            top-level ``instrument:`` key in YAML). Takes precedence
            over ``questions``.
        instrument_pack: Name of an installed instrument pack.
            Takes precedence over both ``instrument`` and ``questions``.
        vars: Template variables substituted into the resolved
            instrument's question text (MCP equivalent of the CLI's
            ``--var`` / ``--vars-file``). Bundled packs ship with
            ``{placeholder}`` tokens — e.g. ``pricing-discovery`` needs
            ``{"problem": "..."}`` and ``name-test`` needs
            ``{"candidates": "Name A, Name B"}``. Applies to both
            ``instrument_pack`` and inline ``instrument`` inputs.

            **Fail-fast guard:** if any ``{placeholder}`` in the
            instrument remains unsubstituted after ``vars`` is applied —
            including when ``vars`` is omitted entirely — the call
            returns a typed ``INVALID_TOOL_ARG`` envelope naming the
            missing keys instead of sending literal braces to panelists.
            Passing ``vars`` with plain ``questions`` (no instrument) is
            also a typed error, since inline questions are sent verbatim.
        model: LLM model to use. Defaults to a cheap/fast model chosen
            from the configured provider credentials (haiku for Anthropic,
            gpt-4o-mini for OpenAI, etc.). When the auto-resolved default
            would be ``openrouter/auto`` *and* the panel has at least
            :data:`LARGE_PANEL_PERSONA_THRESHOLD` personas, the default is
            swapped for ``openrouter/anthropic/claude-haiku-4.5`` to avoid
            the multi-minute stalls observed under ``openrouter/auto`` on
            large panels (sy-2ag / GH#462). Explicit values are honored
            verbatim.
        response_schema: Optional JSON Schema for structured output. When
            provided, each panelist's responses are extracted as structured
            JSON matching this schema instead of free text.
        synthesis: Whether to run synthesis after collecting responses.
            Defaults to true.
        synthesis_model: Model to use for synthesis. Defaults to panelist model.
        synthesis_prompt: Custom synthesis prompt. Replaces the default.
        temperature: Sampling temperature (0.0-1.0) for panelist responses.
        top_p: Nucleus sampling threshold (0.0-1.0) for panelist responses.
        persona_models: Per-persona model overrides. Maps persona name to
            model alias (e.g. {"Sarah Chen": "sonnet", "Mike": "haiku"}).
            This is the MCP equivalent of the CLI's ``--models``
            weighted-assignment feature: the caller pre-computes the
            persona→model map (deterministic, no rounding ambiguity) and
            passes it directly. Persona names not in the map fall back to
            ``model``.
        extract_schema: Schema for post-hoc structured extraction from
            free-text responses. Pass a built-in name ("sentiment",
            "themes", "rating") or an inline JSON Schema dict.
        models: List of model names for multi-model ensemble. When
            provided (length ≥ 2), the panel is run once per model and
            results are compared — every persona answers every model.
            Mutually exclusive with ``model``. Unlike the CLI's weighted
            ``--models`` spec (which splits personas across models), this
            MCP ``models`` list is ensemble-only; use ``persona_models``
            for the CLI-style split-assignment behavior.

            Plain aliases only — the CLI's weighted ``"haiku:0.25"``
            syntax is rejected at this boundary; weights default to
            equal across the ensemble. The ensemble response replaces
            the single-model shape with:

            * ``per_model_results`` — ``{model: {results, cost, usage}}``
              where ``results`` is the formatted panelist list for that
              model, ``cost`` is a formatted USD string, and ``usage``
              is the token bucket dict for the model's run.
            * ``cost_breakdown`` — ``{by_model: {model: "$X"}, total: "$Z"}``.
            * ``models`` — the input model list.
            * ``total_usage`` — summed token buckets across all models.
        synthesis_temperature: Sampling temperature for the synthesis step.
            Independent of the panelist temperature.
        variants: Number of persona variants to generate per persona for
            robustness analysis. When > 0, each persona is perturbed K times
            and all variants run through the same questions. Results include
            robustness_scores and per_persona_robustness. Default: no variants.
        max_cost: Hard ceiling on total panel spend, in USD (the MCP
            analog of the CLI's ``--max-cost``, GH#576). After each
            panelist completes, ``running_cost / completed_n * total_n``
            is compared against the ceiling; if the projected total
            exceeds it the run soft-halts — the in-flight panelists
            finish, no new panelists start, synthesis is skipped — and
            the response is a valid *partial* envelope with
            ``run_invalid: true``, ``cost_exceeded: true``,
            ``abort_reason: "cost_exceeded"``, ``halted_at_panelist``, a
            machine-readable ``cost_gate`` snapshot (spend so far, cap,
            projection), and a ``resume`` block naming the persisted
            partial ``result_id``, the completed panelists, and the
            remaining personas to re-run. Composes with
            ``detail="summary"`` and the ``panel_verdict`` envelope.
            Must be > 0. BYOK inline-``questions`` runs only: typed
            ``INVALID_TOOL_ARG`` when combined with sampling mode (the
            host agent pays; no server-side cost accounting), ``models``
            ensembles, ``variants``, or ``instrument`` /
            ``instrument_pack`` inputs (parity with the CLI, which
            refuses ``--max-cost`` on the multi-round engine). Default:
            no ceiling.
        use_sampling: Explicit mode override. ``True`` forces sampling
            (error if unsupported or if limits exceeded), ``False`` forces
            BYOK. ``None`` auto-picks based on creds + client capability.
            Sampling mode is capped at :data:`SAMPLING_MAX_PERSONAS`
            personas by :data:`SAMPLING_MAX_QUESTIONS` questions; larger
            panels require BYOK. Ensemble mode (``models``) and v3
            branching are BYOK-only.
        decision_being_informed: Required v1.0.0 contract field — the
            decision this panel will inform, in 12-280 characters
            (trimmed), single line, UTF-8. Echoed verbatim into the
            verdict's ``meta`` and stamped on every transcript row.
            Validation failures return a typed ``MISSING_DECISION`` /
            ``DECISION_TOO_LONG`` / ``INVALID_TOOL_ARG`` error envelope.
            Omitting the field is tolerated during the v1.0.x grace
            window (the placeholder ``"unspecified-legacy-call"`` is
            synthesized and a ``W_DECISION_MISSING`` warning is returned);
            under ``ALTHING_SCHEMA_MIN>=1.1.0`` omission is a hard
            ``MISSING_DECISION`` reject.
        detail: Transcript verbosity of the response envelope, one of
            ``"summary"`` (default) or ``"full"``. ``"summary"`` returns
            synthesis, ``panel_verdict``, ``poll_summary``, ``metadata``,
            ``result_id``, costs, ``per_model_results`` (usage/cost),
            ``warnings``, ``path`` and ``terminal_round`` but DROPS the
            per-panelist transcripts (the top-level ``results`` mirror and
            each ``rounds[].results`` list) to protect the agent's context
            window — with caps of MAX_PERSONAS x MAX_QUESTIONS a full panel
            can serialise megabytes. The dropped transcript stays
            retrievable via ``get_panel_result(result_id)`` or the
            ``panel-result://{result_id}`` resource (also surfaced on
            ``panel_verdict.full_transcript_uri`` and the envelope's
            ``transcript_uri``). ``"full"`` returns every panelist row.
            Applies to persisted BYOK runs; sampling responses (no
            ``result_id``, never persisted) always return full transcripts.
    """
    if detail not in ("summary", "full"):
        return _invalid_tool_arg(
            f"'detail' must be 'summary' or 'full' (got {detail!r}).",
            field_path="detail",
        )
    decision_being_informed, decision_warnings, decision_error = _resolve_decision_contract(
        "run_panel", decision_being_informed
    )
    if decision_error is not None:
        return decision_error
    spec_error = _reject_weighted_model_spec(
        model=model,
        models=models,
        synthesis_model=synthesis_model,
        persona_models=persona_models,
    )
    if spec_error is not None:
        return spec_error
    normalized = _normalize_models_param(model=model, models=models)
    if isinstance(normalized, str):
        return normalized
    model, models = normalized
    model_was_explicit = model is not None
    variants_k = variants or 0
    if variants_k < 0 or variants_k > 20:
        return json.dumps({"error": "variants must be between 0 and 20."})

    # ── GH#576: max_cost boundary checks ─────────────────────────────────
    # The gate wires into the single-round BYOK orchestrator only (exact
    # parity with the CLI's --max-cost support matrix); every unsupported
    # combination refuses loudly before any LLM spend.
    max_cost_error = _validate_max_cost(max_cost)
    if max_cost_error is not None:
        return max_cost_error
    if max_cost is not None:
        if models and len(models) >= 2:
            return _invalid_tool_arg(
                "max_cost is not supported with multi-model ensembles (models=[...]); "
                "run each model separately with its own max_cost, or drop max_cost.",
                field_path="max_cost",
            )
        if instrument is not None or instrument_pack is not None:
            return _invalid_tool_arg(
                "max_cost is not supported with instrument / instrument_pack inputs "
                "(they dispatch through the multi-round engine, which the cost gate "
                "does not cover — same refusal as the CLI's --max-cost). Pass the "
                "instrument's questions inline via 'questions', or drop max_cost.",
                field_path="max_cost",
            )
        if variants_k > 0:
            return _invalid_tool_arg(
                "max_cost is not supported together with variants (robustness "
                "perturbation): the gate's projection is sized to the base panel "
                "and variant expansion would skew it. Run the variant sweep "
                "ungated, or pilot ungated variants on a smaller panel first.",
                field_path="max_cost",
            )

    # Resolve extract_schema name → dict before threading to orchestrator.
    try:
        resolved_extract_schema = _resolve_extract_schema(extract_schema)
    except (ValueError, TypeError) as exc:
        return json.dumps({"error": str(exc)})
    merged = list(personas) if personas else []
    if pack_id is not None:
        pack, pack_error = _resolve_persona_pack_or_error(pack_id)
        if pack_error is not None:
            return pack_error
        assert pack is not None
        merged.extend(pack.get("personas", []))
    if not merged:
        return json.dumps({"error": "No personas provided. Supply personas and/or pack_id."})

    # Default-model resolution is deferred until after merging so the
    # auto-fast swap (sy-2ag) can see the true persona count.
    if not model_was_explicit:
        model = _resolve_mcp_default_model_for_panel(len(merged))
    logger.info("run_panel: model=%s synthesis=%s variants=%d", model, synthesis, variants_k)

    # Validate personas: must be dicts with "name"
    for i, p in enumerate(merged):
        if not isinstance(p, dict):
            return json.dumps({"error": f"Persona at index {i} must be a dict, got {type(p).__name__}."})
        if "name" not in p or not str(p["name"]).strip():
            return json.dumps({"error": f"Persona at index {i} is missing required field 'name'."})

    if len(merged) > MAX_PERSONAS:
        return json.dumps({"error": f"Too many personas ({len(merged)}). Maximum is {MAX_PERSONAS}."})

    # Validate questions if provided directly (not via instrument)
    if questions is not None and instrument is None and instrument_pack is None:
        for i, q in enumerate(questions):
            if not isinstance(q, dict):
                return json.dumps({"error": f"Question at index {i} must be a dict, got {type(q).__name__}."})
            if "text" not in q or not str(q["text"]).strip():
                return json.dumps({"error": f"Question at index {i} is missing required field 'text'."})
        if len(questions) > MAX_QUESTIONS:
            return json.dumps({"error": f"Too many questions ({len(questions)}). Maximum is {MAX_QUESTIONS}."})

    # ── GH#562: resolve the instrument once, up front, and substitute vars ─
    # All three execution paths (sampling, ensemble, BYOK) previously
    # re-resolved the instrument independently and none substituted template
    # variables, so a pack like `pricing-discovery` sent literal `{problem}`
    # to panelists. Resolve + parse + substitute here so every path sees the
    # same rendered instrument, and fail fast (typed INVALID_TOOL_ARG) on
    # any unsubstituted {placeholder} — even when `vars` is omitted.
    # InstrumentError → clean JSON so caller-side typos in attachment
    # payloads (hq-jviv) surface across the wire instead of crashing the
    # tool with a generic ToolError.
    instrument_obj: Instrument | None = None
    if instrument_pack is not None or instrument is not None:
        if instrument_pack is not None:
            pack_body, ip_error = _resolve_instrument_pack_or_error(instrument_pack)
            if ip_error is not None:
                return ip_error
            assert pack_body is not None
            raw = pack_body.get("instrument", pack_body)
        else:
            assert instrument is not None
            raw = instrument.get("instrument", instrument)
        try:
            instrument_obj = parse_instrument(raw)
        except InstrumentError as exc:
            return json.dumps({"error": str(exc)})
        vars_error = _substitute_instrument_vars_or_error(instrument_obj, vars)
        if vars_error is not None:
            return vars_error
    elif vars:
        return _invalid_tool_arg(
            "The vars parameter only applies to instrument / instrument_pack "
            "inputs; inline questions are sent to panelists verbatim. Write "
            "the values directly into the question text instead.",
            field_path="vars",
        )

    # ── Sampling fallback: route through MCP sampling when no BYOK creds ─
    # Ensemble mode is BYOK-only (sampling host exposes only one model),
    # so we only consult the decision in the non-ensemble branch.
    if not (models and len(models) >= 2):
        decision = _decide_sampling_mode(ctx, use_sampling=use_sampling, model=model)
        if decision.mode == "error":
            return json.dumps({"error": decision.error})
        if decision.mode == "sampling":
            # GH#576: sampling mode has no server-side cost accounting
            # (tokens bill to the host agent's subscription), so a USD
            # ceiling cannot be enforced. Refuse loudly rather than
            # silently running unbounded.
            if max_cost is not None:
                return _invalid_tool_arg(
                    "max_cost requires BYOK cost accounting; this call resolved to "
                    "sampling mode, where token spend bills to the host agent and "
                    "the server sees no per-panelist costs. Set a provider API key "
                    "(e.g. ANTHROPIC_API_KEY) / pass use_sampling=false, or drop "
                    "max_cost.",
                    field_path="max_cost",
                )
            # Resolve question stream for sampling — no v3 branching.
            # The instrument itself was resolved + vars-substituted above.
            sampling_questions: list[dict[str, Any]]
            if instrument_obj is not None:
                if len(instrument_obj.rounds) > 1:
                    source = "pack" if instrument_pack is not None else "instrument"
                    return json.dumps(
                        {
                            "error": (
                                "Sampling mode does not support v3 branching "
                                "instruments (multiple rounds). Set a provider "
                                "API key (e.g. ANTHROPIC_API_KEY) to run this "
                                f"{source} under BYOK."
                            )
                        }
                    )
                sampling_questions = [{"text": q["text"]} for q in instrument_obj.questions]
            elif questions:
                sampling_questions = questions
            else:
                return json.dumps({"error": "No questions or instrument provided."})

            if len(merged) > SAMPLING_MAX_PERSONAS:
                return json.dumps(
                    {
                        "error": (
                            f"Sampling mode is capped at {SAMPLING_MAX_PERSONAS} personas "
                            f"to protect the host agent's context window (got {len(merged)}). "
                            f"Set ANTHROPIC_API_KEY (or another provider key) in your "
                            f"environment to run larger panels via BYOK."
                        )
                    }
                )
            if len(sampling_questions) > SAMPLING_MAX_QUESTIONS:
                return json.dumps(
                    {
                        "error": (
                            f"Sampling mode is capped at {SAMPLING_MAX_QUESTIONS} questions "
                            f"(got {len(sampling_questions)}). Set a provider API key to "
                            f"run larger panels via BYOK."
                        )
                    }
                )
            if variants_k > 0:
                return json.dumps(
                    {
                        "error": (
                            "Sampling mode does not support persona variants. "
                            "Set a provider API key to use robustness analysis."
                        )
                    }
                )

            try:
                sampling_result = await _run_panel_sampling(
                    ctx,
                    personas=merged,
                    questions=sampling_questions,
                    synthesis=synthesis,
                    synthesis_prompt=synthesis_prompt,
                    temperature=temperature,
                    hint=decision.hint,
                    accept_multimodal=accept_multimodal_sampling,
                    decision_being_informed=decision_being_informed,
                )
            except SamplingSuspend as suspend:
                # 2026-07-28+ client: sampling rides the InputRequiredResult
                # retry loop instead of mid-call server→client requests.
                return suspend.to_input_required()
            return json.dumps(apply_response_gate(sampling_result), indent=2)

    # ── Ensemble mode: run with each model, compare across models ────────
    if models and len(models) >= 2:
        if not questions and instrument_obj is None:
            return json.dumps({"error": "Ensemble mode requires questions or instrument."})
        ens_questions = questions or []
        if not ens_questions and instrument_obj is not None:
            # Instruments: extract flat questions for ensemble (v1/v2 only).
            # instrument_obj is already parsed + vars-substituted above.
            ens_questions = [{"text": q["text"]} for q in instrument_obj.questions]
        try:
            ens_result = await asyncio.to_thread(
                _run_ensemble_sync,
                merged,
                ens_questions,
                models,
                response_schema,
                extract_schema,
                temperature,
                top_p,
            )
        except PanelTotalFailureError as exc:
            logger.error("run_panel ensemble: total failure: %s", exc)
            return json.dumps(
                {
                    "error": str(exc),
                    "run_invalid": True,
                    "total_failure": exc.diagnostic,
                },
                indent=2,
            )
        # Ensemble responses are comparative (one run per model, nothing
        # persisted) so they carry no panel_verdict; the decision is still
        # echoed and the AC-4 nudge surfaced.
        if decision_being_informed is not None:
            ens_result["decision_being_informed"] = decision_being_informed
        if decision_warnings:
            existing_warnings = ens_result.get("warnings")
            if isinstance(existing_warnings, list):
                existing_warnings.extend(decision_warnings)
            else:
                ens_result["warnings"] = list(decision_warnings)
        return json.dumps(apply_response_gate(ens_result), indent=2)

    # Instrument source (pack > inline instrument > questions) was resolved
    # and vars-substituted in the GH#562 block above.
    if instrument_obj is not None:
        try:
            result = await _run_panel_async_instrument(
                merged,
                instrument_obj,
                model,
                ctx,
                response_schema,
                synthesis=synthesis,
                synthesis_model=synthesis_model,
                synthesis_prompt=synthesis_prompt,
                temperature=temperature,
                top_p=top_p,
                persona_models=persona_models,
                extract_schema=resolved_extract_schema,
                synthesis_temperature=synthesis_temperature,
                decision_being_informed=decision_being_informed,
                decision_warnings=decision_warnings,
                detail=detail,
            )
        except PanelTotalFailureError as exc:
            logger.error("run_panel instrument: total failure: %s", exc)
            return json.dumps(
                {
                    "error": str(exc),
                    "run_invalid": True,
                    "total_failure": exc.diagnostic,
                },
                indent=2,
            )
        except asyncio.TimeoutError:
            logger.error(
                "run_panel instrument: timed out (personas=%d rounds=%d model=%s)",
                len(merged),
                len(instrument_obj.rounds),
                model,
            )
            return _panel_timeout_envelope(
                personas=len(merged),
                rounds=len(instrument_obj.rounds),
                model=model,
            )
        return json.dumps(apply_response_gate(result), indent=2)

    if not questions:
        return json.dumps({"error": "No questions or instrument provided."})

    try:
        result = await _run_panel_async(
            merged,
            questions,
            model,
            ctx,
            response_schema,
            synthesis=synthesis,
            synthesis_model=synthesis_model,
            synthesis_prompt=synthesis_prompt,
            temperature=temperature,
            top_p=top_p,
            persona_models=persona_models,
            extract_schema=resolved_extract_schema,
            synthesis_temperature=synthesis_temperature,
            variants=variants_k,
            decision_being_informed=decision_being_informed,
            decision_warnings=decision_warnings,
            detail=detail,
            max_cost=max_cost,
        )
    except PanelTotalFailureError as exc:
        logger.error("run_panel: total failure: %s", exc)
        return json.dumps(
            {
                "error": str(exc),
                "run_invalid": True,
                "total_failure": exc.diagnostic,
            },
            indent=2,
        )
    except asyncio.TimeoutError:
        logger.error(
            "run_panel: timed out (personas=%d questions=%d model=%s variants=%d)",
            len(merged),
            len(questions),
            model,
            variants_k,
        )
        return _panel_timeout_envelope(
            personas=len(merged),
            questions=len(questions),
            variants=variants_k,
            model=model,
        )
    return json.dumps(apply_response_gate(result), indent=2)


@mcp.tool()
async def run_quick_poll(
    question: str,
    personas: list[dict[str, Any]] | None = None,
    pack_id: str | None = None,
    model: str | None = None,
    response_schema: dict[str, Any] | None = None,
    synthesis: bool = True,
    synthesis_model: str | None = None,
    synthesis_prompt: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    max_cost: float | None = None,
    use_sampling: bool | None = None,
    accept_multimodal_sampling: bool = False,
    decision_being_informed: str | None = None,
    detail: str = "summary",
    ctx: Context = None,
) -> str | InputRequiredResult:
    """Quick single-question poll across personas.

    A simplified version of run_panel for quick feedback on one question.
    Includes synthesis by default.

    When no provider credentials are set and the invoking MCP client
    advertises the ``sampling`` capability, the poll runs in
    **sampling mode**: althing asks the host agent to run each
    persona's completion using its own LLM access, so the user can run
    their first poll with zero configuration. Sampling mode is capped
    at :data:`SAMPLING_MAX_PERSONAS` personas to keep the host agent's
    context footprint small — larger panels require BYOK credentials.

    Args:
        question: The question to ask all personas.
        personas: List of persona definitions. Optional — when omitted
            (and no ``pack_id`` is given), a small built-in pack of
            diverse personas is used so the tool works with zero
            configuration. Merged with ``pack_id`` (inline personas
            first). Each persona is a JSON object with the following
            recognized fields (additional fields are preserved and
            remain available to custom prompt templates):

            * ``name`` (str, **required**) — persona's display name.
            * ``age`` (int, optional) — persona's age.
            * ``occupation`` (str, optional) — job title or role.
            * ``background`` (str, optional) — paragraph-sized bio
              giving context (company, tenure, constraints, etc.).
            * ``personality_traits`` (list[str], optional) — short
              trait adjectives, e.g. ``["analytical", "skeptical"]``.

            Example::

                [
                  {
                    "name": "Alex Rivera",
                    "age": 29,
                    "occupation": "Software Engineer",
                    "background": "Early-career developer at a mid-sized SaaS company.",
                    "personality_traits": ["analytical", "curious", "pragmatic"]
                  }
                ]
        pack_id: ID of a saved persona pack (bundled or user-saved),
            resolved the same way ``run_panel`` resolves it. Merged with
            inline ``personas`` (inline first). An unknown ``pack_id``
            returns a typed ``INVALID_TOOL_ARG`` envelope naming the
            installed packs. When both ``personas`` and ``pack_id`` are
            omitted the built-in diverse persona set is used.
        model: LLM model to use. Defaults to a cheap/fast model chosen
            from configured provider credentials. When the auto-resolved
            default would be ``openrouter/auto`` and the poll runs against
            at least :data:`LARGE_PANEL_PERSONA_THRESHOLD` personas, the
            default is swapped for ``openrouter/anthropic/claude-haiku-4.5``
            (sy-2ag / GH#462). Ignored in sampling mode (the host agent
            picks its own model).
        response_schema: Optional JSON Schema for structured output. When
            provided, responses are extracted as structured JSON matching
            this schema instead of free text. Not supported in sampling
            mode — raw text is returned instead.
        synthesis: Whether to run synthesis after collecting responses.
            Defaults to true. In sampling mode synthesis is also
            performed via the host agent.
        synthesis_model: Model to use for synthesis. Defaults to panelist model.
        synthesis_prompt: Custom synthesis prompt. Replaces the default.
        temperature: Sampling temperature (0.0-1.0). Controls randomness.
        top_p: Nucleus sampling threshold (0.0-1.0). Alternative to temperature.
        max_cost: Hard ceiling on total poll spend, in USD (the MCP
            analog of the CLI's ``--max-cost``, GH#576). Same semantics
            as ``run_panel``'s ``max_cost``: soft-halt when the
            projected total exceeds the ceiling — the in-flight
            panelists finish, no new panelists start, synthesis is
            skipped — and the response is a valid partial envelope
            carrying ``run_invalid: true``, ``cost_exceeded: true``,
            ``abort_reason: "cost_exceeded"``, ``halted_at_panelist``,
            the ``cost_gate`` snapshot, and a ``resume`` block. Must be
            > 0. BYOK only — typed ``INVALID_TOOL_ARG`` when the call
            resolves to sampling mode (the host agent pays; no
            server-side cost accounting). Default: no ceiling.
        use_sampling: Explicit mode override. ``True`` forces sampling
            (error if unsupported), ``False`` forces BYOK. ``None``
            auto-picks based on creds + client capability.
        decision_being_informed: Required v1.0.0 contract field — the
            decision this poll will inform, in 12-280 characters
            (trimmed), single line, UTF-8. Validation failures return a
            typed ``MISSING_DECISION`` / ``DECISION_TOO_LONG`` /
            ``INVALID_TOOL_ARG`` error envelope. Omission is tolerated
            during the v1.0.x grace window (placeholder + warning);
            ``ALTHING_SCHEMA_MIN>=1.1.0`` makes it a hard reject.
        detail: Transcript verbosity of the BYOK response envelope, one of
            ``"summary"`` (default) or ``"full"``. ``"summary"`` drops the
            per-panelist transcripts (retrievable via
            ``get_panel_result(result_id)``); ``"full"`` returns them. See
            ``run_panel``'s ``detail`` for the retained/omitted split.
            Sampling responses always return full transcripts.
    """
    if detail not in ("summary", "full"):
        return _invalid_tool_arg(
            f"'detail' must be 'summary' or 'full' (got {detail!r}).",
            field_path="detail",
        )
    decision_being_informed, decision_warnings, decision_error = _resolve_decision_contract(
        "run_quick_poll", decision_being_informed
    )
    if decision_error is not None:
        return decision_error
    spec_error = _reject_weighted_model_spec(
        model=model,
        synthesis_model=synthesis_model,
    )
    if spec_error is not None:
        return spec_error
    model_was_explicit = model is not None

    # GH#576: max_cost boundary check (same rule as run_panel / the CLI).
    max_cost_error = _validate_max_cost(max_cost)
    if max_cost_error is not None:
        return max_cost_error

    if not question or not question.strip():
        return json.dumps({"error": "Question text must be a non-empty string."})

    # Resolve personas: inline first, then pack_id, else the built-in
    # diverse persona set (preserves the zero-config first-run story).
    merged_personas = list(personas) if personas else []
    if pack_id is not None:
        pack, pack_error = _resolve_persona_pack_or_error(pack_id)
        if pack_error is not None:
            return pack_error
        assert pack is not None
        merged_personas.extend(pack.get("personas", []))
    if not merged_personas:
        merged_personas = [dict(p) for p in DEFAULT_QUICK_POLL_PERSONAS]
    personas = merged_personas

    # Validate personas: must be dicts with "name"
    for i, p in enumerate(personas):
        if not isinstance(p, dict):
            return json.dumps({"error": f"Persona at index {i} must be a dict, got {type(p).__name__}."})
        if "name" not in p or not str(p["name"]).strip():
            return json.dumps({"error": f"Persona at index {i} is missing required field 'name'."})

    if len(personas) > MAX_PERSONAS:
        return json.dumps({"error": f"Too many personas ({len(personas)}). Maximum is {MAX_PERSONAS}."})

    # Resolve default model *after* persona resolution so the auto-fast
    # swap (sy-2ag) can see the real persona count.
    if not model_was_explicit:
        model = _resolve_mcp_default_model_for_panel(len(personas))

    decision = _decide_sampling_mode(ctx, use_sampling=use_sampling, model=model)
    logger.info("run_quick_poll: mode=%s model=%s personas=%d", decision.mode, model, len(personas))

    if decision.mode == "error":
        return json.dumps({"error": decision.error})

    if decision.mode == "sampling":
        # GH#576: no server-side cost accounting in sampling mode — a USD
        # ceiling cannot be enforced. Refuse loudly (parity with run_panel).
        if max_cost is not None:
            return _invalid_tool_arg(
                "max_cost requires BYOK cost accounting; this call resolved to "
                "sampling mode, where token spend bills to the host agent and "
                "the server sees no per-panelist costs. Set a provider API key "
                "(e.g. ANTHROPIC_API_KEY) / pass use_sampling=false, or drop "
                "max_cost.",
                field_path="max_cost",
            )
        if len(personas) > SAMPLING_MAX_PERSONAS:
            return json.dumps(
                {
                    "error": (
                        f"Sampling mode is capped at {SAMPLING_MAX_PERSONAS} personas "
                        f"to protect the host agent's context window (got "
                        f"{len(personas)}). Set ANTHROPIC_API_KEY (or another "
                        f"provider key) in your environment to run larger panels "
                        f"via BYOK."
                    )
                }
            )
        try:
            result = await _run_quick_poll_sampling(
                ctx,
                question=question,
                personas=personas,
                synthesis=synthesis,
                synthesis_prompt=synthesis_prompt,
                temperature=temperature,
                hint=decision.hint,
                accept_multimodal=accept_multimodal_sampling,
                decision_being_informed=decision_being_informed,
            )
        except SamplingSuspend as suspend:
            # 2026-07-28+ client: sampling rides the InputRequiredResult
            # retry loop instead of mid-call server→client requests.
            return suspend.to_input_required()
        return json.dumps(apply_response_gate(result), indent=2)

    questions = [{"text": question}]
    try:
        result = await _run_panel_async(
            personas,
            questions,
            model,
            ctx,
            response_schema,
            synthesis=synthesis,
            synthesis_model=synthesis_model,
            synthesis_prompt=synthesis_prompt,
            temperature=temperature,
            top_p=top_p,
            decision_being_informed=decision_being_informed,
            decision_warnings=decision_warnings,
            detail=detail,
            max_cost=max_cost,
        )
    except PanelTotalFailureError as exc:
        # Same typed envelope run_panel returns — a knowingly-bad model
        # alias must not surface as a generic FastMCP tool error (hq-6j40).
        logger.error("run_quick_poll: total failure: %s", exc)
        return _total_failure_envelope(exc)
    except asyncio.TimeoutError:
        logger.error(
            "run_quick_poll: timed out (personas=%d model=%s)",
            len(personas),
            model,
        )
        return _panel_timeout_envelope(
            personas=len(personas),
            questions=1,
            variants=0,
            model=model,
        )
    if "error_code" not in result:
        result["mode"] = "byok"
    return json.dumps(apply_response_gate(result), indent=2)


async def _run_panel_sampling(
    ctx: Context,
    *,
    personas: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    synthesis: bool,
    synthesis_prompt: str | None,
    temperature: float | None,
    hint: str | None,
    accept_multimodal: bool = False,
    decision_being_informed: str | None = None,
) -> dict[str, Any]:
    """Run a full panel via MCP sampling.

    Mirrors :func:`_run_panel_async`'s shape for the fields callers
    depend on (``results``, ``rounds``, ``synthesis``, ``persona_count``,
    ``question_count``, ``path``, ``warnings``) so downstream tooling
    doesn't have to special-case sampling-mode output. BYOK-only fields
    (``usage``, ``cost``, ``metadata``) are ``None`` — the host agent
    absorbs token cost.

    Serial across personas on legacy connections (host agents rate-limit
    sampling) but each persona answers all questions in a single sampling
    call to keep round-trips small. On 2026-07-28+ connections the
    per-persona requests are batched into one ``InputRequiredResult``
    round (SEP-2322) via :class:`SamplingBridge`; the synthesis call is a
    second round.
    """
    from althing.prompts import SYNTHESIS_PROMPT

    await ctx.report_progress(0, len(personas))
    bridge = SamplingBridge(ctx)
    panelist_entries: list[dict[str, Any]] = []
    host_model: str | None = None

    question_texts = [str(q["text"]) for q in questions]
    joined_questions = "\n\n".join(f"Q{i + 1}: {t}" for i, t in enumerate(question_texts))
    user_prompt = (
        "Answer each of the following questions in order. "
        "Label each answer with the matching Q number.\n\n" + joined_questions
    )

    persona_specs = {
        f"persona_{i}": SampleSpec(
            prompt=user_prompt,
            system_prompt=persona_system_prompt(persona),
            max_tokens=SAMPLING_MAX_TOKENS_DEFAULT,
            temperature=temperature,
        )
        for i, persona in enumerate(personas)
    }
    samples = await bridge.gather(
        persona_specs,
        accept_multimodal=accept_multimodal,
        on_progress=ctx.report_progress,
    )

    # sp-k2ed4a: collect host-side truncation warnings so the run summary
    # can flag turns that were silently cut off by the host's max_tokens
    # ceiling (otherwise indistinguishable from generic schema-fail).
    sampling_warnings: list[str] = []
    for i, persona in enumerate(personas):
        sample = samples[f"persona_{i}"]
        host_model = sample["model"]
        if sample.get("truncated") and sample.get("warning"):
            persona_name = persona.get("name", f"persona_{i}")
            sampling_warnings.append(f"panelist '{persona_name}': {sample['warning']}")
        # Surface the full answer string against every question so the
        # output matches BYOK shape: one response entry per question.
        responses = [{"question": q_text, "answer": sample["text"]} for q_text in question_texts]
        panelist_entries.append(
            {
                "persona": persona,
                "responses": responses,
                "model": sample["model"],
                "usage": None,
            }
        )

    synthesis_block: dict[str, Any] | None = None
    if synthesis and panelist_entries:
        synth_prompt = synthesis_prompt or SYNTHESIS_PROMPT
        rendered_panel = "\n\n".join(
            "Panelist: {name}\n{responses}".format(
                name=entry["persona"].get("name", "anon"),
                responses="\n".join(f"Q: {r['question']}\nA: {r['answer']}" for r in entry["responses"]),
            )
            for entry in panelist_entries
        )
        synth = (
            await bridge.gather(
                {
                    "synthesis": SampleSpec(
                        prompt=rendered_panel,
                        system_prompt=synth_prompt,
                        max_tokens=SAMPLING_MAX_TOKENS_DEFAULT,
                        temperature=temperature,
                    )
                }
            )
        )["synthesis"]
        if synth.get("truncated") and synth.get("warning"):
            sampling_warnings.append(f"synthesis: {synth['warning']}")
        synthesis_block = {
            "summary": synth["text"],
            "model": synth["model"],
            "usage": None,
        }

    out: dict[str, Any] = {
        "mode": "sampling",
        "hint": hint,
        "model": host_model,
        "persona_count": len(personas),
        "question_count": len(questions),
        "results": panelist_entries,
        "rounds": [
            {
                "name": "default",
                "results": panelist_entries,
                "synthesis": synthesis_block,
            }
        ],
        "synthesis": synthesis_block,
        "path": [],
        "warnings": sampling_warnings,
        "usage": None,
        "cost": None,
        "metadata": None,
    }
    # Sampling runs are not persisted (no result_id / transcript), so no
    # panel_verdict is emitted here — the decision is echoed for the audit
    # join instead. See docs/response-contract.md for the caveat.
    if decision_being_informed is not None:
        out["decision_being_informed"] = decision_being_informed
    return out


async def _run_quick_poll_sampling(
    ctx: Context,
    *,
    question: str,
    personas: list[dict[str, Any]],
    synthesis: bool,
    synthesis_prompt: str | None,
    temperature: float | None,
    hint: str | None,
    accept_multimodal: bool = False,
    decision_being_informed: str | None = None,
) -> dict[str, Any]:
    """Run a quick poll via MCP sampling.

    One sampling request per persona — fulfilled serially via
    ``create_message`` on legacy connections (host agents generally
    rate-limit sampling), or batched into a single ``InputRequiredResult``
    round on 2026-07-28+ (SEP-2322) — plus one synthesis call when
    enabled. The result shape deliberately mirrors the BYOK
    :func:`_run_panel_async` output for the fields callers care about
    (``results``, ``synthesis``, ``rounds``, ``persona_count``,
    ``question_count``) so downstream tooling works uniformly across
    modes. Fields that only make sense in BYOK (``usage``, ``cost``,
    ``metadata``) are ``None``.
    """
    from althing.prompts import SYNTHESIS_PROMPT

    await ctx.report_progress(0, len(personas))
    bridge = SamplingBridge(ctx)
    panelist_entries: list[dict[str, Any]] = []
    host_model: str | None = None
    sampling_warnings: list[str] = []

    user_prompt = build_question_prompt({"text": question})
    persona_specs = {
        f"persona_{i}": SampleSpec(
            prompt=user_prompt,
            system_prompt=persona_system_prompt(persona),
            max_tokens=2048,
            temperature=temperature,
        )
        for i, persona in enumerate(personas)
    }
    samples = await bridge.gather(
        persona_specs,
        accept_multimodal=accept_multimodal,
        on_progress=ctx.report_progress,
    )

    for i, persona in enumerate(personas):
        sample = samples[f"persona_{i}"]
        host_model = sample["model"]
        if sample.get("truncated") and sample.get("warning"):
            persona_name = persona.get("name", f"persona_{i}")
            sampling_warnings.append(f"panelist '{persona_name}': {sample['warning']}")
        panelist_entries.append(
            {
                "persona": persona,
                "responses": [
                    {
                        "question": question,
                        "answer": sample["text"],
                    }
                ],
                "model": sample["model"],
                "usage": None,
            }
        )

    synthesis_block: dict[str, Any] | None = None
    if synthesis and panelist_entries:
        synth_prompt = synthesis_prompt or SYNTHESIS_PROMPT
        rendered_panel = "\n\n".join(
            f"Panelist: {entry['persona'].get('name', 'anon')}\nQ: {question}\nA: {entry['responses'][0]['answer']}"
            for entry in panelist_entries
        )
        synth = (
            await bridge.gather(
                {
                    "synthesis": SampleSpec(
                        prompt=rendered_panel,
                        system_prompt=synth_prompt,
                        max_tokens=2048,
                        temperature=temperature,
                    )
                },
                accept_multimodal=accept_multimodal,
            )
        )["synthesis"]
        if synth.get("truncated") and synth.get("warning"):
            sampling_warnings.append(f"synthesis: {synth['warning']}")
        synthesis_block = {
            "summary": synth["text"],
            "model": synth["model"],
            "usage": None,
        }

    out: dict[str, Any] = {
        "mode": "sampling",
        "hint": hint,
        "model": host_model,
        "persona_count": len(personas),
        "question_count": 1,
        "results": panelist_entries,
        "rounds": [
            {
                "name": "default",
                "results": panelist_entries,
                "synthesis": synthesis_block,
            }
        ],
        "synthesis": synthesis_block,
        "path": [],
        "warnings": sampling_warnings,
        "usage": None,
        "cost": None,
        "metadata": None,
    }
    # Sampling runs are not persisted (no result_id / transcript), so no
    # panel_verdict is emitted here — the decision is echoed for the audit
    # join instead. See docs/response-contract.md for the caveat.
    if decision_being_informed is not None:
        out["decision_being_informed"] = decision_being_informed
    return out


@mcp.tool()
async def list_persona_packs() -> str:
    """List all saved persona packs.

    Returns metadata for each pack including ID, name, and persona count.
    """
    packs = _data_list_persona_packs()
    return json.dumps(packs, indent=2)


@mcp.tool()
async def get_persona_pack(pack_id: str) -> str:
    """Get a specific persona pack by ID.

    Args:
        pack_id: The ID of the persona pack to retrieve.
    """
    pack = _data_get_persona_pack(pack_id)
    return json.dumps(pack, indent=2)


@mcp.tool()
async def save_persona_pack(
    name: str,
    personas: list[dict[str, Any]],
    pack_id: str | None = None,
) -> str:
    """Save a persona pack for reuse.

    Args:
        name: Human-readable name for the pack.
        personas: List of persona definitions.
        pack_id: Optional ID. Auto-generated if not provided.
    """
    result = _data_save_persona_pack(name, personas, pack_id)
    return json.dumps(result, indent=2)


@mcp.tool()
async def list_instrument_packs() -> str:
    """List installed instrument packs.

    Instrument packs live as single ``<name>.yaml`` files under
    ``$SYNTH_PANEL_DATA_DIR/packs/instruments/`` and carry the four
    shared manifest fields (name, version, description, author) at
    the top level alongside the instrument body.
    """
    return json.dumps(_data_list_instrument_packs(), indent=2)


@mcp.tool()
async def get_instrument_pack(name: str) -> str:
    """Load an installed instrument pack by name.

    Args:
        name: The pack name (filename stem under packs/instruments/).
    """
    return json.dumps(_data_load_instrument_pack(name), indent=2)


@mcp.tool()
async def save_instrument_pack(
    name: str,
    content: dict[str, Any],
) -> str:
    """Install an instrument pack to the local instrument-pack directory.

    The instrument body is validated via the parser before being
    written: a malformed v1/v2/v3 instrument fails fast and is never
    written to disk. Manifest fields (name, version, description,
    author) are expected at the top level of ``content`` alongside
    either ``instrument:`` or the instrument keys directly.

    Args:
        name: Pack name. Becomes ``<name>.yaml`` on disk.
        content: Full pack body — manifest fields plus the
            instrument definition.
    """
    raw = content.get("instrument", content)
    parse_instrument(raw)  # validate before write
    return json.dumps(_data_save_instrument_pack(name, content), indent=2)


@mcp.tool()
async def extend_panel(
    result_id: str,
    questions: list[dict[str, Any]],
    model: str | None = None,
    synthesis: bool = True,
    synthesis_model: str | None = None,
    synthesis_prompt: str | None = None,
    max_cost: float | None = None,
    accept_multimodal_sampling: bool = False,
    decision_being_informed: str | None = None,
    ctx: Context = None,
) -> str:
    """Append a single ad-hoc round to a saved panel result.

    ``extend_panel`` always appends ONE improvised round on top of an
    existing result, reusing each panelist's saved session so the
    follow-up sees full conversational context. It is **not** a way
    to re-enter the authored v3 DAG: the original instrument's
    ``route_when`` clauses are not consulted, no routing decision is
    made, and the result's ``path`` is extended by exactly one entry
    tagged as an extension. If you want branching, run a fresh
    ``run_panel`` with a v3 instrument instead.

    The pre-extend snapshot is preserved alongside the result file
    (``<result_id>.pre-extend.json``) so the operation is reversible.

    Args:
        result_id: ID of a previously saved panel result. An unknown id
            returns a typed ``INVALID_TOOL_ARG`` envelope naming the
            available result ids (not a raw tool error).
        questions: One or more questions for the ad-hoc round. They
            run as a single round, in order, against the same
            personas as the original run.
        model: LLM model to use for the new round. Defaults to haiku.
        synthesis: Whether to synthesize the new round.
        synthesis_model: Synthesis model. Defaults to panelist model.
        synthesis_prompt: Custom synthesis prompt for the new round.
        max_cost: Hard ceiling on the extension round's spend, in USD
            (the MCP analog of the CLI's ``--max-cost``, GH#576). Same
            soft-halt semantics as ``run_panel``: the in-flight
            panelists finish, no new panelists start, the round's
            synthesis is skipped, and the response is a valid partial
            envelope with ``run_invalid: true``, ``cost_exceeded:
            true``, ``abort_reason: "cost_exceeded"``,
            ``halted_at_panelist``, the ``cost_gate`` snapshot, and a
            ``resume`` block naming the panelists still to be asked.
            The partial extension round IS persisted onto the result
            (and the pre-extend snapshot remains for rollback). Must
            be > 0. Default: no ceiling.
        decision_being_informed: Required v1.0.0 contract field — the
            decision this extension informs, in 12-280 characters
            (trimmed), single line, UTF-8. Validation failures return a
            typed ``MISSING_DECISION`` / ``DECISION_TOO_LONG`` /
            ``INVALID_TOOL_ARG`` error envelope. Omission is tolerated
            during the v1.0.x grace window (placeholder + warning);
            ``ALTHING_SCHEMA_MIN>=1.1.0`` makes it a hard reject.
    """
    decision_being_informed, decision_warnings, decision_error = _resolve_decision_contract(
        "extend_panel", decision_being_informed
    )
    if decision_error is not None:
        return decision_error
    spec_error = _reject_weighted_model_spec(
        model=model,
        synthesis_model=synthesis_model,
    )
    if spec_error is not None:
        return spec_error
    # GH#576: max_cost boundary check (same rule as run_panel / the CLI).
    max_cost_error = _validate_max_cost(max_cost)
    if max_cost_error is not None:
        return max_cost_error
    model = model or _resolve_mcp_default_model()
    logger.info("extend_panel: result_id=%s questions=%d model=%s", result_id, len(questions), model)
    existing, result_error = _resolve_panel_result_or_error(result_id)
    if result_error is not None:
        return result_error
    assert existing is not None

    # Reuse the original personas (recovered from saved sessions if possible).
    # A missing sessions dir raises FileNotFoundError — surface it as the
    # same typed INVALID_TOOL_ARG envelope rather than a raw tool error.
    try:
        sessions = load_panel_sessions(result_id)
    except FileNotFoundError:
        return _invalid_tool_arg(
            f"Panel result {result_id!r} has no saved panelist sessions to extend; "
            "extend_panel can only follow up on a run that persisted its transcripts.",
            field_path="result_id",
        )
    personas: list[dict[str, Any]] = [{"name": name} for name in sessions]
    if not personas:
        return json.dumps({"error": f"No sessions found for result {result_id}"})

    if ctx is not None:
        await ctx.report_progress(0, len(personas))

    # GH#576: arm the projected-total cost gate for the extension round.
    # Sized to the panelists being re-asked (one per saved session), same
    # machinery as the CLI's --max-cost. Passed conditionally so existing
    # monkeypatched doubles with explicit signatures keep working.
    cost_gate: CostGate | None = None
    if max_cost is not None:
        cost_gate = CostGate(max_cost_usd=max_cost, total_panelists=len(personas))
    _gate_kwargs: dict[str, Any] = {"cost_gate": cost_gate} if cost_gate is not None else {}

    def _go() -> tuple[list[PanelistResult], dict[str, Any], Any, dict[str, Any] | None]:
        client = _get_shared_client()
        results, _registry, out_sessions = run_panel_parallel(
            client=client,
            personas=personas,
            questions=questions,
            model=model,
            system_prompt_fn=persona_system_prompt,
            question_prompt_fn=build_question_prompt,
            sessions=sessions,
            **_gate_kwargs,
        )
        synth = None
        synth_error: dict[str, Any] | None = None
        # GH#576: a cost-halted extension is a deliberately-truncated
        # partial; synthesizing it would spend past the cap for an
        # untrustworthy summary (same rationale as the CLI / run_panel).
        run_synthesis = synthesis and not (cost_gate is not None and cost_gate.should_halt())
        if run_synthesis:
            try:
                synth = synthesize_panel(
                    client,
                    results,
                    questions,
                    model=synthesis_model,
                    panelist_model=model,
                    custom_prompt=synthesis_prompt,
                )
            except Exception as exc:
                # sp-0ozi: surface the failure in the tool response envelope so
                # MCP callers can distinguish "synthesis threw" from "synthesis
                # skipped" or "synthesis produced empty output". Stays
                # non-fatal — panelist results are still returned.
                logger.error("extend_panel synthesis failed (non-fatal)", exc_info=True)
                synth = None
                synth_error = build_synthesis_error_payload(
                    exc,
                    error_type="synthesis_api_error",
                    message=f"Synthesis call failed: {exc.__class__.__name__}: {exc}",
                    suggested_fix=(
                        "Check provider credentials and model availability;"
                        " retry extend_panel once the underlying issue is resolved."
                    ),
                )
        return results, out_sessions, synth, synth_error

    try:
        panelist_results, extended_sessions, synth, synthesis_error = await asyncio.wait_for(
            asyncio.to_thread(_go),
            timeout=PANELIST_TIMEOUT * len(personas),
        )
    except PanelTotalFailureError as exc:
        # Parity with run_panel: a bad alias / provider outage returns the
        # typed run_invalid envelope, not a generic FastMCP tool error.
        logger.error("extend_panel: total failure: %s", exc)
        return _total_failure_envelope(exc)
    except asyncio.TimeoutError:
        logger.error(
            "extend_panel: timed out (personas=%d questions=%d model=%s)",
            len(personas),
            len(questions),
            model,
        )
        return _panel_timeout_envelope(
            personas=len(personas),
            rounds=1,
            model=model,
        )

    # ``run_panel_parallel`` returns error-tagged rows rather than raising on
    # a total wipeout, so detect it here and emit the same typed envelope
    # run_panel does (a knowingly-bad model alias produces 0-token panelists).
    failure = detect_total_failure(panelist_results)
    if failure is not None:
        from althing._runners import format_total_failure_message

        logger.error("extend_panel: total failure detected: %s", failure)
        return _total_failure_envelope(
            PanelTotalFailureError(format_total_failure_message(failure), diagnostic=failure)
        )

    if ctx is not None:
        await ctx.report_progress(len(personas), len(personas))

    new_round_results = [_format_panelist_result(pr, model) for pr in panelist_results]

    # Append the ad-hoc round to the existing result and persist.
    rounds = existing.get("rounds") or []
    new_round: dict[str, Any] = {
        "name": f"extension-{len(rounds) + 1}",
        "results": new_round_results,
        "synthesis": synth.to_dict() if synth is not None and hasattr(synth, "to_dict") else None,
        "extension": True,
    }
    if decision_being_informed is not None:
        # The extension's own decision is recorded on the round it produced;
        # the top-level field (if any) keeps describing the original run.
        new_round["decision_being_informed"] = decision_being_informed
    if synthesis_error is not None:
        new_round["synthesis_error"] = synthesis_error
    rounds = [*list(rounds), new_round]
    path = list(existing.get("path") or [])
    path.append(
        {
            "round": rounds[-1]["name"],
            "branch": "extension (ad-hoc, not DAG re-entry)",
            "next": "__end__",
        }
    )

    updated = dict(existing)
    updated["rounds"] = rounds
    updated["path"] = path
    updated["results"] = new_round_results  # mirrors latest round (back-compat)
    updated["question_count"] = int(existing.get("question_count", 0)) + len(questions)
    update_panel_result(result_id, updated)

    # AC-7: persist the extended sessions, stamped with the extension's
    # decision (the freshest decision the transcript now serves).
    _persist_stamped_sessions(result_id, extended_sessions, decision_being_informed)

    response: dict[str, Any] = {
        "result_id": result_id,
        "appended_round": rounds[-1]["name"],
        "results": new_round_results,
        "synthesis": rounds[-1]["synthesis"],
        "path": path,
        "warnings": list(decision_warnings),
    }
    if synthesis_error is not None:
        # sp-0ozi: top-level synthesis_error so MCP clients can gate on
        # envelope shape without inspecting the appended round.
        response["synthesis_error"] = synthesis_error

    # GH#576: cost gate tripped mid-extension. The appended round is a
    # valid completed prefix (already persisted above); surface the same
    # typed partial markers run_panel emits plus the resume block.
    if cost_gate is not None and cost_gate.should_halt():
        _attach_cost_gate_halt(
            response,
            cost_gate=cost_gate,
            personas=personas,
            completed_names=[pr.persona_name for pr in panelist_results],
            result_id=result_id,
            tool="extend_panel",
            resume_hint=(
                "The partial extension round is already appended to the result "
                "(the pre-extend snapshot remains for rollback). extend_panel "
                "re-asks every saved panelist, so to cover remaining_personas "
                "either call extend_panel again with a raised (or omitted) "
                "max_cost — which appends a fresh full round — or accept the "
                "partial. The round's synthesis was skipped to avoid spending "
                "past the cap."
            ),
        )

    # v1.0.0 contract fields for the extension round.
    response = _finalize_contract_response(
        response,
        decision_being_informed=decision_being_informed,
        decision_warnings=(),  # already placed on the envelope above
        panelist_results=panelist_results,
        personas=personas,
        result_dicts=new_round_results,
        synthesis_dict=rounds[-1]["synthesis"],
        poll_summary=None,
        result_id=result_id,
    )
    return json.dumps(apply_response_gate(response), indent=2)


@mcp.tool()
async def list_panel_results() -> str:
    """List all saved panel results.

    Returns metadata for each result including ID, date, model, and counts.
    """
    results = _data_list_panel_results()
    return json.dumps(results, indent=2)


@mcp.tool()
async def get_panel_result(result_id: str, detail: str = "full") -> str:
    """Get a specific panel result by ID.

    This is the canonical way to retrieve the per-panelist transcript that
    ``run_panel`` / ``run_quick_poll`` omit under their default
    ``detail="summary"`` envelope. It therefore defaults to ``detail="full"``
    (unlike the run tools, whose default protects the agent's live context)
    so ``get_panel_result(result_id)`` returns the complete saved result —
    the behavior every existing caller already depends on.

    Args:
        result_id: The ID of the panel result to retrieve.
        detail: ``"full"`` (default) returns the complete saved result;
            ``"summary"`` drops the per-panelist transcript (top-level
            ``results`` mirror + any ``rounds[].results``) the same way the
            run tools' summary envelope does — useful for a cheap metadata
            /synthesis peek at a large saved panel.
    """
    if detail not in ("summary", "full"):
        return _invalid_tool_arg(
            f"'detail' must be 'summary' or 'full' (got {detail!r}).",
            field_path="detail",
        )
    result = _data_get_panel_result(result_id)
    return json.dumps(_apply_detail(result, detail), indent=2)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("persona-pack://{pack_id}")
async def resource_persona_pack(pack_id: str) -> str:
    """A specific persona pack."""
    pack = _data_get_persona_pack(pack_id)
    return json.dumps(pack, indent=2)


@mcp.resource("persona-pack://")
async def resource_persona_packs_list() -> str:
    """List all persona packs."""
    return json.dumps(_data_list_persona_packs(), indent=2)


@mcp.resource("panel-result://{result_id}")
async def resource_panel_result(result_id: str) -> str:
    """A specific panel result."""
    result = _data_get_panel_result(result_id)
    return json.dumps(result, indent=2)


@mcp.resource("panel-result://")
async def resource_panel_results_list() -> str:
    """List all panel results."""
    return json.dumps(_data_list_panel_results(), indent=2)


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------


@mcp.prompt()
def focus_group(
    topic: str,
    num_personas: int = 5,
    follow_up: bool = True,
) -> str:
    """Generate a focus group discussion prompt.

    Creates a structured prompt for running a focus group on a given topic.

    Args:
        topic: The topic or product to discuss.
        num_personas: Number of diverse personas to include.
        follow_up: Whether to include follow-up questions.
    """
    follow_up_section = ""
    if follow_up:
        follow_up_section = "\n\nAfter each response, ask one follow-up question to dig deeper into their perspective."

    return (
        f"Run a synthetic focus group with {num_personas} diverse personas "
        f"discussing: {topic}\n\n"
        f"For each persona, ask:\n"
        f"1. What is your initial reaction to this topic?\n"
        f"2. How does this relate to your daily experience?\n"
        f"3. What concerns or opportunities do you see?"
        f"{follow_up_section}\n\n"
        f"Use the run_panel tool with appropriate personas and questions."
    )


@mcp.prompt()
def name_test(
    names: str,
    context: str = "",
) -> str:
    """Test product or feature names with personas.

    Creates a prompt for evaluating name options with diverse perspectives.

    Args:
        names: Comma-separated list of name options to test.
        context: Optional context about what the name is for.
    """
    context_line = f" for {context}" if context else ""
    return (
        f"Test these name options{context_line}: {names}\n\n"
        f"For each persona, ask:\n"
        f"1. What does each name make you think of?\n"
        f"2. Which name do you prefer and why?\n"
        f"3. Does any name confuse you or feel wrong?\n\n"
        f"Use the run_panel tool with diverse personas to get varied feedback."
    )


@mcp.prompt()
def concept_test(
    concept: str,
    target_audience: str = "",
) -> str:
    """Test a concept or idea with personas.

    Creates a prompt for evaluating a concept with targeted personas.

    Args:
        concept: Description of the concept to test.
        target_audience: Optional description of the target audience.
    """
    audience_line = ""
    if target_audience:
        audience_line = f"\n\nTarget the personas toward: {target_audience}"

    return (
        f"Test this concept with synthetic personas:\n\n{concept}"
        f"{audience_line}\n\n"
        f"For each persona, ask:\n"
        f"1. Does this concept solve a problem you have?\n"
        f"2. What would make you try this?\n"
        f"3. What concerns would prevent you from using this?\n"
        f"4. How much would you expect to pay for this?\n\n"
        f"Use the run_panel tool with personas matching the target audience."
    )


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------


def serve() -> None:
    """Run the MCP server on stdio transport.

    Uses the SDK's public stdio runner. Pre-2.0 althing reimplemented the
    stdio loop here to inject a ``sampling`` advertisement into the
    initialize response — nested under ``experimental`` *and*, off-spec,
    at the top level of ``ServerCapabilities`` (sampling is defined by the
    spec as a *client* capability; the injection relied on
    ``extra="allow"``). SDK 2.0's ``MCPServer.run`` exposes no public
    initialization-option hook, and the 2026-07-28 revision's
    ``server/discover`` path never consults ``initialize`` options at
    all, so the advertisement is dropped entirely: whether sampling is
    used is decided at call time by probing the *client's* declared
    ``sampling`` capability (``decide_mode`` in althing.mcp.sampling),
    which works identically on both protocol eras. See docs/mcp.md.
    """
    logger.info("MCP server starting (stdio transport)")
    mcp.run(transport="stdio")
