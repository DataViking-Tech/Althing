"""Shared synchronous panel runners.

Factored out of ``althing.mcp.server`` so that both the MCP server
handlers and the public Python SDK (``althing.sdk``) can drive the
same underlying logic without one depending on the other — the MCP
server requires the optional ``mcp`` extra, whereas the SDK has to
work in a plain ``pip install althing`` environment.

Nothing here imports the ``mcp`` library.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from pydantic import BaseModel

from althing.cost import ZERO_USAGE, CostGate, resolve_cost
from althing.cost import TokenUsage as CostTokenUsage
from althing.instrument import Instrument
from althing.llm.client import LLMClient
from althing.orchestrator import (
    MultiRoundResult,
    PanelistResult,
    run_multi_round_panel,
    run_panel_parallel,
)
from althing.perturbation import generate_panel_variants
from althing.prompts import build_question_prompt, persona_system_prompt
from althing.stats import robustness_score
from althing.structured.models import MODEL_REGISTRY
from althing.structured.schemas import _REGISTRY as _BUNDLED_SCHEMA_REGISTRY
from althing.synthesis import synthesize_panel

logger = logging.getLogger(__name__)

# sp-efip: max number of per-persona error samples to surface in the
# diagnostic output when a run fails wholesale. Keeps CLI banners and
# MCP error envelopes bounded while still naming real errors.
_MAX_SAMPLE_ERRORS = 3


class PanelTotalFailureError(RuntimeError):
    """Every panelist failed — no usable Q/A data was produced.

    Raised by ``run_panel_sync`` (and equivalents) when the orchestrator
    returns results where every panelist either errored wholesale or
    produced zero tokens with only errored responses. Callers should
    surface the message verbatim so operators see the failing model(s)
    and upstream provider error (e.g. "OpenRouter API error 400: …").
    """

    def __init__(self, message: str, *, diagnostic: dict[str, Any]) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


def _first_error_message(pr: Any) -> str | None:
    """Return a representative error string for *pr*, or None if clean."""
    err = getattr(pr, "error", None)
    if err:
        return str(err)
    for resp in getattr(pr, "responses", []) or []:
        if isinstance(resp, dict) and resp.get("error"):
            text = resp.get("response")
            if isinstance(text, str) and text:
                return text
            return "unspecified per-question error"
    return None


def detect_total_failure(panelist_results: list[Any]) -> dict[str, Any] | None:
    """Return a diagnostic dict when every panelist failed, else None.

    Total failure means that *no* panelist produced a usable Q/A pair.
    A panelist counts as failed when any of the following hold:

    * ``pr.error`` is set (wholesale panelist exception), OR
    * every response in ``pr.responses`` is flagged ``error: True``
      (per-question exceptions caught inside ``_run_panelist``), OR
    * the panelist produced zero tokens *and* recorded no primary
      non-errored response (the 400-on-every-call scenario from
      sp-efip, where tokens stayed at 0 because every request was
      rejected upstream).

    The returned dict carries the models exercised and a few sample
    per-persona error strings so downstream banners / MCP errors can
    name the upstream failure (e.g. the bad model name and HTTP 400
    the bead explicitly calls out).
    """
    if not panelist_results:
        return {
            "panelists": 0,
            "models": [],
            "sample_errors": [],
            "summary": "orchestrator returned no panelist results",
        }

    models: set[str] = set()
    sample_errors: list[tuple[str, str]] = []
    all_failed = True

    for pr in panelist_results:
        model = getattr(pr, "model", None)
        if model:
            models.add(str(model))

        pr_error = getattr(pr, "error", None)
        responses = getattr(pr, "responses", []) or []
        primary = [r for r in responses if isinstance(r, dict) and not r.get("follow_up")]
        has_clean_primary = any(isinstance(r, dict) and not r.get("error") and r.get("response") for r in primary)

        # A panelist is considered usable iff it produced at least one
        # non-errored primary response. The bead explicitly calls out
        # the "tokens=0 AND error set" short-circuit, which is already
        # subsumed by "no clean primary response" — zero-token panelists
        # whose only responses are error=True rows all fall here.
        panelist_failed = bool(pr_error) or not has_clean_primary
        if not panelist_failed:
            all_failed = False
            continue

        err_msg = _first_error_message(pr)
        if err_msg and len(sample_errors) < _MAX_SAMPLE_ERRORS:
            persona = getattr(pr, "persona_name", "unknown")
            sample_errors.append((str(persona), err_msg))

    if not all_failed:
        return None

    return {
        "panelists": len(panelist_results),
        "models": sorted(models),
        "sample_errors": sample_errors,
        "summary": (f"{len(panelist_results)} panelist(s) produced no usable Q/A pairs"),
    }


def format_total_failure_message(diagnostic: dict[str, Any]) -> str:
    """Render a single-line message naming failing models + sample error."""
    models = diagnostic.get("models") or []
    sample_errors = diagnostic.get("sample_errors") or []
    parts = [f"Panel run produced no usable results: {diagnostic.get('summary', 'every panelist failed')}"]
    if models:
        parts.append(f"models: {', '.join(models)}")
    if sample_errors:
        persona, err = sample_errors[0]
        parts.append(f"first error ({persona}): {err}")
    return ". ".join(parts)


# sp-avmm: context-window table for synthesis pre-flight check.
#
# Keyed by the canonical model ID (after alias resolution) OR a prefix
# match. Values are window sizes in tokens. When a model does not match,
# the caller falls back to ``_DEFAULT_CONTEXT_WINDOW`` and emits a WARN
# so operators know the limit they're running against is a guess.
_CONTEXT_WINDOWS: dict[str, int] = {
    # Anthropic
    "claude-haiku-4-5": 200_000,
    "claude-haiku-4": 200_000,
    "claude-haiku-3-5": 200_000,
    "claude-sonnet-5": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-opus-4-8": 200_000,
    "claude-opus-4-6": 200_000,
    "claude-opus-4": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
    # Google
    "gemini-2.5-pro": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    "gemini-2.5-flash-lite": 1_000_000,
    "gemini-2.0": 1_000_000,
    "gemini-1.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_000_000,
    # Open-weights on OpenRouter / local
    "qwen3": 131_072,
    "qwen-3": 131_072,
    "qwen2.5": 131_072,
    "deepseek-v3": 128_000,
    "deepseek-chat": 128_000,
    # OpenAI (approximate — most recent families)
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4.1": 1_000_000,
    "o1": 200_000,
    "o3": 200_000,
    # xAI
    "grok-3": 131_072,
    "grok-2": 131_072,
}

# Conservative default for unknown models — 128k is the floor for most
# modern production models. The pre-flight check emits a WARN when it
# falls back to this, so operators can plumb in a real value.
_DEFAULT_CONTEXT_WINDOW: int = 128_000

# Headroom reserved below the declared window so the pre-flight check
# errs on the side of caution. The bead suggests 8k; this leaves room
# for the synthesis template, the tool-use schema scaffolding, and the
# model's own output budget (``_MAX_TOKENS`` = 4k).
_SYNTHESIS_HEADROOM_TOKENS: int = 8_000

# Rough chars-per-token ratio for English text. Synthesis mirrors the
# ratio already used by ``_print_cost_estimate`` in ``synthesis.py`` so
# the two estimates stay in sync.
_CHARS_PER_TOKEN: int = 4


class SynthesisContextOverflowError(RuntimeError):
    """Estimated synthesis prompt exceeds the synthesis model's context window.

    Raised by :func:`detect_synthesis_context_overflow` (via caller check)
    when the pre-flight estimate is too large. Carries the diagnostic
    fields needed to build both the structured error payload and the
    human-readable banner without re-computing.
    """

    def __init__(
        self,
        message: str,
        *,
        diagnostic: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


def _resolve_context_window(model: str | None) -> tuple[int, bool]:
    """Return ``(window_tokens, is_default_fallback)`` for *model*.

    Tries a direct match first, then a prefix match against known model
    families. Returns ``(_DEFAULT_CONTEXT_WINDOW, True)`` when nothing
    matches so callers can warn.
    """
    if not model:
        return _DEFAULT_CONTEXT_WINDOW, True

    # Lazy import — aliases are a CLI-layer concern but the MCP runner
    # needs the same resolution so a pre-flight check against
    # "haiku" doesn't falsely pick the default window.
    try:
        from althing.llm.aliases import resolve_alias
        from althing.llm.providers.subscription_cli import SUBSCRIPTION_CLI_PREFIXES

        # Subscription CLI models ("claude-code:haiku") size like the model
        # they wrap.
        for cli_prefix in SUBSCRIPTION_CLI_PREFIXES:
            if model.startswith(cli_prefix):
                model = model[len(cli_prefix) :]
                break
        resolved = resolve_alias(model)
    except Exception:
        resolved = model

    candidates = (resolved, model)
    for candidate in candidates:
        if not candidate:
            continue
        lower = candidate.lower()
        if lower in _CONTEXT_WINDOWS:
            return _CONTEXT_WINDOWS[lower], False
        # Strip any provider prefix ("openrouter/anthropic/claude-…")
        trimmed = lower.split("/")[-1]
        if trimmed in _CONTEXT_WINDOWS:
            return _CONTEXT_WINDOWS[trimmed], False
        for prefix, size in _CONTEXT_WINDOWS.items():
            if trimmed.startswith(prefix) or lower.startswith(prefix):
                return size, False
    return _DEFAULT_CONTEXT_WINDOW, True


def _estimate_synthesis_prompt_tokens(
    panelist_results: list[Any],
    questions: list[dict[str, Any]],
    *,
    custom_prompt: str | None = None,
) -> int:
    """Estimate the synthesis call's prompt token count.

    Builds the same user content that ``synthesize_panel`` would assemble
    (template + question list + every panelist's Q/A bodies) and applies
    the ~4-chars-per-token heuristic already used elsewhere in the module.
    Kept intentionally pessimistic: over-estimating the prompt is safer
    than under-estimating because the API will reject the real call.
    """
    # Mirror synth.synthesis._format_panelist_data without importing it,
    # to avoid a circular dependency at module load time.
    chars = 0
    if custom_prompt:
        chars += len(custom_prompt)
    else:
        # Best-effort include of the default prompt template length.
        try:
            from althing.prompts import SYNTHESIS_PROMPT

            chars += len(SYNTHESIS_PROMPT)
        except Exception:
            chars += 2_000  # conservative guess for the template

    for q in questions or []:
        text = q.get("text") if isinstance(q, dict) else str(q)
        chars += len(text or "")
    for pr in panelist_results or []:
        chars += len(getattr(pr, "persona_name", "") or "")
        for resp in getattr(pr, "responses", []) or []:
            q_text = resp.get("question", "") if isinstance(resp, dict) else ""
            answer = resp.get("response", "") if isinstance(resp, dict) else ""
            if isinstance(answer, dict):
                # Panelist returned structured output — it gets serialized
                # with json.dumps(..., indent=2) before landing in the prompt.
                import json as _json

                try:
                    answer = _json.dumps(answer, indent=2)
                except Exception:
                    answer = str(answer)
            chars += len(str(q_text)) + len(str(answer))

    # Add fixed overhead for section headers ("## Questions Asked",
    # "### <persona>", etc.) and the tool-use scaffolding the
    # StructuredOutputEngine injects (~500 tokens).
    overhead_chars = 2_000
    return (chars + overhead_chars) // _CHARS_PER_TOKEN + 500


def detect_synthesis_context_overflow(
    panelist_results: list[Any],
    questions: list[dict[str, Any]],
    *,
    synthesis_model: str | None,
    custom_prompt: str | None = None,
    headroom_tokens: int = _SYNTHESIS_HEADROOM_TOKENS,
) -> dict[str, Any] | None:
    """Return a diagnostic dict when the synthesis prompt will overflow.

    Returns ``None`` when the estimated prompt fits inside the model's
    context window (minus *headroom_tokens*). Returns a structured
    diagnostic otherwise, with the fields needed to build both the
    structured error payload (``synthesis_error``) and the actionable
    human-readable message.

    The diagnostic dict always carries:

    * ``estimated_tokens`` — pessimistic pre-call token estimate.
    * ``context_window`` — the model's documented context window.
    * ``headroom_tokens`` — reserved below the window.
    * ``effective_limit`` — ``context_window - headroom_tokens``.
    * ``synthesis_model`` — the model we were about to call.
    * ``is_default_window`` — True when we fell back to the default.
    """
    window, is_default = _resolve_context_window(synthesis_model)
    if is_default:
        logger.warning(
            "Synthesis model %r not in the context-window table — falling back to %dk tokens. "
            "Add it to _CONTEXT_WINDOWS for a tighter pre-flight check.",
            synthesis_model,
            window // 1000,
        )

    estimated = _estimate_synthesis_prompt_tokens(
        panelist_results,
        questions,
        custom_prompt=custom_prompt,
    )
    effective_limit = max(0, window - headroom_tokens)
    if estimated <= effective_limit:
        return None
    return {
        "estimated_tokens": estimated,
        "context_window": window,
        "headroom_tokens": headroom_tokens,
        "effective_limit": effective_limit,
        "synthesis_model": synthesis_model,
        "is_default_window": is_default,
    }


def format_synthesis_overflow_message(diagnostic: dict[str, Any]) -> str:
    """Render the actionable pre-flight overflow message (sp-avmm).

    The message names the estimated size, the model limit, and suggests
    large-context alternatives and a rerun strategy. Sized to be usable
    as both a CLI stderr line and a structured ``message`` field.
    """
    model = diagnostic.get("synthesis_model") or "synthesis model"
    estimated = int(diagnostic.get("estimated_tokens", 0))
    window = int(diagnostic.get("context_window", 0))
    headroom = int(diagnostic.get("headroom_tokens", _SYNTHESIS_HEADROOM_TOKENS))

    def _k(n: int) -> str:
        if n >= 1_000_000 and n % 1_000_000 == 0:
            return f"{n // 1_000_000}M"
        return f"{n // 1000}k"

    return (
        f"synthesis input ~{_k(estimated)} tokens exceeds {model}'s {_k(window)}"
        f" context (with {_k(headroom)} headroom). Try --synthesis-model"
        f" gemini-flash-lite (1M ctx) or gemini-2.5-pro (1M ctx), or reduce"
        f" panel size. Rerun with the same --personas + --instrument to"
        f" produce identical panelist data; only the synthesis step will change."
    )


def _sanitize_api_error(exc: BaseException) -> str:
    """Shrink a raised API exception into a single-line error string."""
    text = str(exc).strip()
    if not text:
        text = exc.__class__.__name__
    # Collapse newlines and cap length so the sanitized message stays
    # banner-friendly in stderr and JSON envelopes.
    text = " ".join(text.splitlines())
    if len(text) > 1_000:
        text = text[:997] + "..."
    return text


def build_synthesis_error_payload(
    exc: BaseException | None,
    *,
    error_type: str,
    message: str,
    suggested_fix: str | None = None,
    diagnostic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the structured ``synthesis_error`` dict surfaced to callers.

    Mirrors the ``total_failure`` shape from sp-efip (structured, keyed
    by category) so downstream consumers (MCP/CI/dashboards) can branch
    on ``error_type`` without string-matching the human message.
    """
    payload: dict[str, Any] = {
        "error_type": error_type,
        "message": message,
    }
    if suggested_fix:
        payload["suggested_fix"] = suggested_fix
    if exc is not None:
        payload["exception"] = _sanitize_api_error(exc)
    if diagnostic:
        payload["diagnostic"] = diagnostic
    return payload


# Caps mirrored from the MCP server — the SDK inherits the same guardrails.
MAX_PERSONAS = 100
MAX_QUESTIONS = 50

# Per-panelist timeout budget used by async wrappers (seconds).
PANELIST_TIMEOUT = 30


# Built-in extraction schema registry — keyed by short name.
EXTRACT_SCHEMA_REGISTRY: dict[str, dict[str, Any]] = {
    "sentiment": {
        "type": "object",
        "properties": {
            "sentiment": {
                "type": "string",
                "enum": ["positive", "negative", "neutral", "mixed"],
                "description": "Overall sentiment of the response.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Confidence score (0-1) for the sentiment classification.",
            },
        },
        "required": ["sentiment", "confidence"],
    },
    "themes": {
        "type": "object",
        "properties": {
            "themes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key themes or topics mentioned in the response.",
            },
            "primary_theme": {
                "type": "string",
                "description": "The single most dominant theme.",
            },
        },
        "required": ["themes", "primary_theme"],
    },
    "rating": {
        "type": "object",
        "properties": {
            "rating": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "Numeric rating (1-10) implied by the response.",
            },
            "explanation": {
                "type": "string",
                "description": "Brief rationale for the assigned rating.",
            },
        },
        "required": ["rating", "explanation"],
    },
}


def resolve_extract_schema(
    value: str | dict[str, Any] | type[BaseModel] | None,
) -> dict[str, Any] | None:
    """Resolve ``extract_schema`` into a ``{schema, model}`` envelope.

    The return shape is::

        {"schema": <JSON Schema dict>, "model": <Pydantic class | None>}

    or ``None`` when the input is ``None``. Downstream code consumes the
    ``schema`` key for the wire-level structured-output forcing and uses
    the ``model`` key (when present) to apply a typed
    ``model_validate`` pass on the extracted payload.

    Inputs accepted:

    - ``None`` → ``None``.
    - ``str`` — looks up both registries: the post-hoc analytic registry
      (``EXTRACT_SCHEMA_REGISTRY`` — sentiment / themes / rating) and
      the bundled response registry (``schemas._REGISTRY`` — ranking /
      likert / yes_no / pick_one / annotated_choice). For names in the
      bundled registry the matching :class:`pydantic.BaseModel` from
      :data:`MODEL_REGISTRY` is attached.
    - ``dict`` — treated as a raw JSON Schema. ``model`` is ``None`` so
      the post-extraction pass falls back to plain ``json.loads``.
    - ``type[BaseModel]`` — generates the JSON Schema via
      ``model_json_schema()`` and attaches the class for typed
      validation. The wire format and the typed validator are guaranteed
      to be self-consistent in this branch.

    Example:
        >>> from pydantic import BaseModel, Field
        >>> class MyChoice(BaseModel):
        ...     option: str = Field(..., min_length=1)
        ...     score: int = Field(..., ge=1, le=5)
        ...
        >>> envelope = resolve_extract_schema(MyChoice)
        >>> envelope["model"] is MyChoice
        True
        >>> envelope["schema"]["type"]
        'object'
    """
    if value is None:
        return None
    if isinstance(value, type) and issubclass(value, BaseModel):
        return {"schema": value.model_json_schema(), "model": value}
    if isinstance(value, dict):
        return {"schema": value, "model": None}
    if isinstance(value, str):
        if value in EXTRACT_SCHEMA_REGISTRY:
            return {"schema": EXTRACT_SCHEMA_REGISTRY[value], "model": MODEL_REGISTRY.get(value)}
        if value in _BUNDLED_SCHEMA_REGISTRY:
            return {"schema": _BUNDLED_SCHEMA_REGISTRY[value], "model": MODEL_REGISTRY.get(value)}
        names = ", ".join(sorted({*EXTRACT_SCHEMA_REGISTRY, *_BUNDLED_SCHEMA_REGISTRY}))
        raise ValueError(
            f"Unknown extract_schema name {value!r}. Available: {names}. "
            "Or pass an inline JSON Schema dict, or a Pydantic BaseModel subclass."
        )
    raise TypeError(f"extract_schema must be a string, dict, or BaseModel subclass, got {type(value).__name__}")


def format_panelist_result(pr: PanelistResult, model: str) -> dict[str, Any]:
    """Render a :class:`PanelistResult` into the serialisable dict shape callers expect."""
    pr_model = pr.model or model
    # sp-kvpx: route through resolve_cost so per-panelist ``cost`` honors
    # sp-j3vk's precedence (provider-reported → local fallback). Prior to
    # this, ensemble/mixed-model runs quoted per-persona cost from the
    # local pricing table regardless of what the provider actually billed,
    # so ``per_model_results[*].results[i].cost`` drifted from the top-level
    # ``total_cost`` by the same ratio sp-j3vk fixed at the aggregate layer.
    persona_cost = resolve_cost(pr.usage, pr_model)
    rd: dict[str, Any] = {
        "persona": pr.persona_name,
        "responses": pr.responses,
        "usage": pr.usage.to_dict(),
        "cost": persona_cost.format_usd(),
        "error": pr.error,
    }
    if pr.model:
        rd["model"] = pr.model
    return rd


def compute_variant_data(
    result_dicts: list[dict[str, Any]],
    variant_names: set[str],
    variant_mapping: dict[str, str],
    variant_count: int,
    questions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute robustness scores from base + variant panel results."""
    # Separate base and variant results
    base_results = [r for r in result_dicts if r["persona"] not in variant_names]
    variant_results = [r for r in result_dicts if r["persona"] in variant_names]

    # Build per-question robustness scores
    robustness_scores: list[dict[str, Any]] = []
    per_persona_robustness: list[dict[str, Any]] = []
    n_questions = len(questions)

    for qi in range(n_questions):
        # Group variant responses by source persona
        variant_resps: dict[str, list[str]] = {}
        for vr in variant_results:
            source = variant_mapping.get(vr["persona"], "")
            if not source:
                continue
            resps = vr.get("responses", [])
            if qi < len(resps) and not resps[qi].get("error"):
                variant_resps.setdefault(source, []).append(resps[qi].get("response", ""))

        # Get base persona responses for this question
        for br in base_results:
            resps = br.get("responses", [])
            if qi >= len(resps) or resps[qi].get("error"):
                continue

            base_response = resps[qi].get("response", "")
            persona_name = br["persona"]
            v_resps = variant_resps.get(persona_name, [])

            if v_resps:
                try:
                    rs = robustness_score({persona_name: v_resps}, base_response)
                    per_persona_robustness.append(
                        {
                            "persona": persona_name,
                            "question_index": qi,
                            "robustness": round(rs.per_persona.get(persona_name, 0.0), 4),
                            "k_variants": len(v_resps),
                            "finding_value": base_response,
                            "interpretation": rs.interpretation,
                        }
                    )
                except (ValueError, ZeroDivisionError):
                    pass

        # Overall robustness for this question across all base personas
        if variant_resps:
            base_responses_q = [
                br["responses"][qi].get("response", "")
                for br in base_results
                if qi < len(br.get("responses", [])) and not br["responses"][qi].get("error")
            ]
            if base_responses_q:
                most_common = Counter(base_responses_q).most_common(1)[0][0]
                all_v: dict[str, list[str]] = {}
                for name, resps_list in variant_resps.items():
                    all_v[name] = resps_list
                try:
                    agg = robustness_score(all_v, most_common)
                    robustness_scores.append(
                        {
                            "question_index": qi,
                            "question_text": questions[qi].get("text", ""),
                            "finding_value": most_common,
                            "overall_robustness": round(agg.overall_robustness, 4),
                            "interpretation": agg.interpretation,
                        }
                    )
                except (ValueError, ZeroDivisionError):
                    pass

    return {
        "variant_count": variant_count,
        "robustness_scores": robustness_scores,
        "per_persona_robustness": per_persona_robustness,
    }


def run_panel_sync(
    client: LLMClient,
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
    seed: int | None = None,
    persona_models: dict[str, str] | None = None,
    extract_schema: dict[str, Any] | None = None,
    synthesis_temperature: float | None = None,
    variants: int = 0,
    attachment_bank: dict[str, dict[str, Any]] | None = None,
    allow_empty_attachments: bool = False,
    sessions_out: dict[str, Any] | None = None,
    cost_gate: CostGate | None = None,
) -> tuple[
    list[PanelistResult],
    list[dict[str, Any]],
    CostTokenUsage,
    Any,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    """Run a single-round panel synchronously.

    Returns ``(panelist_results, result_dicts, panelist_usage,
    panelist_cost, synthesis_dict, variant_data)``. ``variant_data`` is
    ``None`` when ``variants == 0``.

    ``sessions_out``: optional out-param. When a dict is passed, it is
    updated in place with the per-persona
    :class:`~althing.persistence.Session` objects the run produced,
    keyed by persona name. Kept as an out-param rather than a seventh
    tuple element so existing unpack sites stay valid. The MCP server
    uses this to persist AC-7 decision-stamped transcripts.

    ``cost_gate``: optional :class:`~althing.cost.CostGate` (sp-utnk /
    GH#576). Threaded to :func:`run_panel_parallel`, which records each
    completing panelist's priced cost and soft-halts dispatch when the
    projected run total exceeds the ceiling. When the gate has halted,
    synthesis is skipped (same rationale as the CLI: synthesizing a
    deliberately-truncated panel burns more budget without producing a
    trustworthy result). The caller keeps the gate reference and is
    expected to inspect ``cost_gate.should_halt()`` on return to shape
    its partial-result envelope.
    """
    all_personas = list(personas)
    variant_names: set[str] = set()
    variant_mapping: dict[str, str] = {}
    variant_count = 0

    if variants > 0:
        logger.info("Generating %d variants per persona", variants)
        variant_sets = generate_panel_variants(personas, client, k=variants, model=model)
        for vs in variant_sets:
            for v in vs.variants:
                all_personas.append(v.persona)
                variant_names.add(v.variant_name)
                variant_mapping[v.variant_name] = v.source_persona_name
                variant_count += 1
        logger.info("Running panel with %d base + %d variant personas", len(personas), variant_count)

    # GH#576: pass the gate only when armed so existing monkeypatched
    # test doubles / caller shims with explicit signatures keep working.
    _gate_kwargs: dict[str, Any] = {"cost_gate": cost_gate} if cost_gate is not None else {}
    panelist_results, _registry, _sessions = run_panel_parallel(
        client=client,
        personas=all_personas,
        questions=questions,
        model=model,
        system_prompt_fn=persona_system_prompt,
        question_prompt_fn=build_question_prompt,
        response_schema=response_schema,
        temperature=temperature,
        top_p=top_p,
        seed=seed,
        persona_models=persona_models,
        extract_schema=extract_schema,
        attachment_bank=attachment_bank,
        allow_empty_attachments=allow_empty_attachments,
        **_gate_kwargs,
    )
    if sessions_out is not None:
        sessions_out.update(_sessions)

    # sp-efip: fail loud when every panelist produced no usable data.
    # Without this, MCP callers see a normally-shaped "panel complete"
    # result even when every request 400'd upstream.
    total_failure = detect_total_failure(panelist_results)
    if total_failure is not None:
        raise PanelTotalFailureError(
            format_total_failure_message(total_failure),
            diagnostic=total_failure,
        )

    panelist_usage = ZERO_USAGE
    result_dicts: list[dict[str, Any]] = []
    for pr in panelist_results:
        result_dicts.append(format_panelist_result(pr, model))
        panelist_usage = panelist_usage + pr.usage

    # sp-kvpx: use resolve_cost so panelist_cost honors sp-j3vk precedence
    # (provider-reported → local fallback) for the MCP/SDK sync path too.
    panelist_cost = resolve_cost(panelist_usage, model)

    base_results = [pr for pr in panelist_results if pr.persona_name not in variant_names]
    synthesis_dict: dict[str, Any] | None = None
    # sp-avmm: synthesis failures surface as a ``synthesis_error`` key on
    # ``synthesis_dict``. Callers (MCP server / SDK) detect this and lift
    # it to a top-level ``synthesis_error`` + warning on the result
    # envelope. It does not invalidate the run — panelist responses are
    # complete; consumers can re-synthesize the saved result.
    # GH#576: mirror the CLI's cost-gate discipline — a halted run is a
    # deliberately-truncated partial, so spending more budget synthesizing
    # it would produce an untrustworthy result. Skip synthesis entirely.
    cost_gate_halted = cost_gate is not None and cost_gate.should_halt()
    if synthesis and cost_gate_halted:
        logger.warning("cost gate halted the panel; skipping synthesis on the partial result")
        synthesis = False
    if synthesis:
        synth_model_for_check = synthesis_model or model
        overflow = detect_synthesis_context_overflow(
            base_results,
            questions,
            synthesis_model=synth_model_for_check,
            custom_prompt=synthesis_prompt,
        )
        if overflow is not None:
            actionable = format_synthesis_overflow_message(overflow)
            logger.error("Synthesis pre-flight rejected: %s", actionable)
            synthesis_dict = {
                "synthesis_error": build_synthesis_error_payload(
                    None,
                    error_type="synthesis_context_overflow",
                    message=actionable,
                    suggested_fix=(
                        "Rerun with --synthesis-model gemini-2.5-flash-lite (1M context) "
                        "or gemini-2.5-pro (1M context), or reduce panel size."
                    ),
                    diagnostic=overflow,
                )
            }
        else:
            try:
                synthesis_result = synthesize_panel(
                    client,
                    base_results,
                    questions,
                    model=synthesis_model,
                    panelist_model=model,
                    custom_prompt=synthesis_prompt,
                    panelist_cost=panelist_cost,
                    temperature=synthesis_temperature,
                    seed=seed,
                )
                synthesis_dict = synthesis_result.to_dict()
            except Exception as exc:
                # sp-rcvr: synthesis recovery ladder — classify, retry
                # transients once, map-reduce on (suspected) downstream
                # context overflow, reroute away from a failing OpenRouter
                # downstream — before surfacing the structured error.
                from althing.synthesis_recovery import (
                    RecoveryContext,
                    SynthesisRecoveryError,
                    recover_synthesis_failure,
                )

                ctx = RecoveryContext(
                    client=client,
                    panelist_results=base_results,
                    questions=questions,
                    synthesis_model=synthesis_model,
                    panelist_model=model,
                    custom_prompt=synthesis_prompt,
                    temperature=synthesis_temperature,
                    top_p=top_p,
                    seed=seed,
                    personas=personas,
                    allow_map_reduce=synthesis_prompt is None,
                )
                try:
                    synthesis_dict = recover_synthesis_failure(exc, ctx).to_dict()
                except SynthesisRecoveryError as rexc:
                    logger.error("Synthesis failed: %s", rexc, exc_info=True)
                    synthesis_dict = {
                        "synthesis_error": build_synthesis_error_payload(
                            rexc,
                            error_type="synthesis_api_error",
                            message=f"Synthesis call failed: {_sanitize_api_error(rexc)}",
                            suggested_fix=(
                                f"Retry with a large-context in-family model: `{rexc.suggested_command}`. "
                                "Also check provider credentials and model availability."
                            ),
                            diagnostic={"recovery_rungs": rexc.rungs},
                        )
                    }

    variant_data: dict[str, Any] | None = None
    if variants > 0 and variant_mapping:
        variant_data = compute_variant_data(
            result_dicts,
            variant_names,
            variant_mapping,
            variant_count,
            questions,
        )

    return panelist_results, result_dicts, panelist_usage, panelist_cost, synthesis_dict, variant_data


def run_multi_round_sync(
    client: LLMClient,
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
    seed: int | None = None,
    persona_models: dict[str, str] | None = None,
    extract_schema: dict[str, Any] | None = None,
    synthesis_temperature: float | None = None,
    system_prompt_fn: Any = None,
    max_workers: int | None = None,
) -> MultiRoundResult:
    """Drive :func:`run_multi_round_panel` for v1/v2/v3 instruments.

    ``system_prompt_fn`` lets callers (the CLI's ``--prompt-template`` /
    instrument-embedded ``system_prompt_template``) override the default
    persona system prompt; ``None`` keeps :func:`persona_system_prompt`.
    ``max_workers`` bounds the per-round panelist thread pool (the CLI's
    ``--max-concurrent``); ``None`` keeps the orchestrator default.
    """

    def _round_synth(
        c: LLMClient,
        panelist_results: list[PanelistResult],
        questions: list[dict[str, Any]],
        *,
        model: str,
    ):
        return synthesize_panel(
            c,
            panelist_results,
            questions,
            model=synthesis_model,
            panelist_model=model,
            custom_prompt=synthesis_prompt,
            temperature=synthesis_temperature,
            seed=seed,
        )

    final_fn = _round_synth if synthesis else None
    return run_multi_round_panel(
        client=client,
        personas=personas,
        instrument=instrument,
        model=model,
        system_prompt_fn=system_prompt_fn or persona_system_prompt,
        question_prompt_fn=build_question_prompt,
        synthesize_round_fn=_round_synth if synthesis else (lambda *a, **kw: None),
        synthesize_final_fn=final_fn,
        response_schema=response_schema,
        max_workers=max_workers,
        temperature=temperature,
        top_p=top_p,
        seed=seed,
        persona_models=persona_models,
        extract_schema=extract_schema,
    )
