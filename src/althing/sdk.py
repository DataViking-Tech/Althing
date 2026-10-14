"""Public Python SDK for althing.

Import the functions below directly from ``althing``::

    from althing import quick_poll, run_panel, run_prompt

This is the convenience layer for Python callers — Jupyter notebooks,
scripts, notebooks, CI pipelines, LangChain wrappers, etc. It has **no
runtime dependency on the optional ``mcp`` extra**: ``pip install
althing`` (without ``[mcp]``) is enough.

The eight public entry points mirror the MCP server's tools but return
typed :mod:`dataclasses` instead of JSON strings:

* :func:`run_prompt` — one-shot LLM call, no personas
* :func:`quick_poll` — single question across a panel
* :func:`run_panel` — full panel run (v1/v2/v3 instruments)
* :func:`extend_panel` — append an ad-hoc follow-up round
* :func:`list_personas` — installed persona packs (bundled + user)
* :func:`list_instruments` — installed instrument packs (bundled + user)
* :func:`list_panel_results` — saved panel-result summaries
* :func:`get_panel_result` — load a full saved result

The return types (:class:`PromptResult`, :class:`PollResult`,
:class:`PanelResult`) all expose a ``.to_dict()`` method for JSON
serialisation and dict-like ``__getitem__`` for back-compat with code
that used to receive the raw MCP payload.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic import BaseModel

from althing import model_routing as _model_routing
from althing._runners import (
    MAX_PERSONAS,
    MAX_QUESTIONS,
    resolve_extract_schema,
    run_multi_round_sync,
    run_panel_sync,
)
from althing.cost import (
    ZERO_USAGE,
    CostEstimate,
    aggregate_per_model,
    build_cost_fallback_warnings,
    estimate_cost,
    lookup_pricing,
    resolve_cost,
)
from althing.cost import (
    TokenUsage as CostTokenUsage,
)
from althing.instrument import Instrument, parse_instrument
from althing.llm import fast_default as _fast_default
from althing.llm.client import LLMClient
from althing.llm.models import CompletionRequest, InputMessage, TextBlock
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
    update_panel_result,
)
from althing.metadata import PanelTimer, build_metadata
from althing.orchestrator import (
    MultiRoundResult,
    run_panel_parallel,
)
from althing.prompts import build_question_prompt, persona_system_prompt
from althing.synthesis import synthesize_panel

__all__ = [
    "PanelResult",
    "PollResult",
    "PromptResult",
    "extend_panel",
    "get_panel_result",
    "list_instruments",
    "list_panel_results",
    "list_personas",
    "quick_poll",
    "run_panel",
    "run_prompt",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default model resolution
# ---------------------------------------------------------------------------


_DEFAULT_MODEL_PREFERENCE: list[tuple[str, str]] = [
    ("ANTHROPIC_API_KEY", "sonnet"),
    ("OPENAI_API_KEY", "gpt-4o-mini"),
    ("GEMINI_API_KEY", "gemini-2.5-flash"),
    ("GOOGLE_API_KEY", "gemini-2.5-flash"),
    ("XAI_API_KEY", "grok-3"),
    ("OPENROUTER_API_KEY", "openrouter/auto"),
]


def _default_model() -> str:
    """Pick a default alias from the first provider with credentials.

    Mirrors the CLI's resolution order so ``from althing import quick_poll``
    works with whichever API key the user has set (env var or
    ``althing login``), falling back to ``"sonnet"`` when nothing is
    set (so the LLM client's missing-credentials error is the one the
    user sees).
    """
    from althing.credentials import has_credential

    for env_var, alias in _DEFAULT_MODEL_PREFERENCE:
        if has_credential(env_var):
            return alias
    return "sonnet"


def _default_model_for_panel(persona_count: int) -> str:
    """Resolve the default model, preferring fast equivalents for big panels.

    synthbench#261 / sy-2ag: an auto-resolved ``openrouter/auto`` default
    can route a ≥10-persona panel to a slow reasoning model (>15 min for
    20 personas). Swap it for a known-fast equivalent via the shared
    :mod:`althing.llm.fast_default` policy and emit a one-line
    warning so the caller knows how to override. Explicit ``model=``
    arguments never reach this function — call sites guard on
    ``model is None`` — so a deliberate ``openrouter/auto`` is honored.
    """
    base = _default_model()
    model, swapped_from = _fast_default.fast_default_for_panel(base, persona_count)
    if swapped_from is not None:
        logger.warning(
            _fast_default.format_fast_default_note(model, swapped_from, persona_count, override_hint="model=")
        )
    return model


# Module-level shared client — reused across calls to avoid rebuilding the
# provider cache on every invocation. Thread-safe (see LLMClient).
_shared_client: LLMClient | None = None


def _get_client() -> LLMClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = LLMClient()
    return _shared_client


# ---------------------------------------------------------------------------
# Public result dataclasses
# ---------------------------------------------------------------------------


class _DictLikeMixin:
    """Support ``result["key"]`` and ``"key" in result`` for dict-compat."""

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain-dict representation of the result."""
        return asdict(self)  # type: ignore[call-overload]


@dataclass
class PromptResult(_DictLikeMixin):
    """One LLM response to a single prompt, with cost + usage metadata.

    Returned by :func:`run_prompt`.

    Attributes:
        response: The text content of the model's reply.
        model: The canonical model id the provider reported (e.g.
            ``"claude-sonnet-4-6"``).
        usage: Per-turn token usage — input, output, cache read/write.
        cost: Estimated USD cost formatted as ``"$0.0012"``.
    """

    response: str
    model: str
    usage: dict[str, Any]
    cost: str


@dataclass
class PollResult(_DictLikeMixin):
    """Panel responses to a single question with optional synthesis.

    Returned by :func:`quick_poll`.

    Attributes:
        result_id: Opaque id persisted under
            ``$SYNTH_PANEL_DATA_DIR/results/`` — pass to
            :func:`get_panel_result` or :func:`extend_panel`.
        question: The question that was asked.
        responses: One dict per panelist — ``persona``, ``responses``,
            ``usage``, ``cost``, ``error``.
        synthesis: Synthesis payload (themes, agreements, recommendation,
            etc.) or ``None`` when ``synthesis=False`` was passed.
        model: The panelist model alias that was used.
        total_usage: Aggregate token usage across panel + synthesis.
        total_cost: Formatted USD total.
        metadata: Full run metadata (per-tier cost breakdown, timings).
    """

    result_id: str
    question: str
    responses: list[dict[str, Any]]
    synthesis: dict[str, Any] | None
    model: str
    total_usage: dict[str, Any]
    total_cost: str
    metadata: dict[str, Any] | None = None
    # sp-avmm: synthesis failure surfaced at the top-level (synthesis_error)
    # so consumers see it without walking into the nested synthesis dict.
    # run_invalid stays False for synthesis-stage failures — panelist
    # responses are complete; it flags panelist-level invalidity only.
    run_invalid: bool = False
    synthesis_error: dict[str, Any] | None = None
    # sy-4yd: deterministic structured-response rollup (vote counts,
    # weighted scores, segment splits, top objections, recommended next
    # test). ``None`` when synthesis disabled OR when no panelist produced
    # a parseable structured response. See :mod:`althing.poll_summary`.
    poll_summary: dict[str, Any] | None = None


@dataclass
class PanelResult(_DictLikeMixin):
    """Result of a full panel run (v1/v2/v3 instruments).

    Returned by :func:`run_panel`, :func:`extend_panel`, and
    :func:`get_panel_result`.

    The shape mirrors the MCP server's ``run_panel`` payload so code
    that previously consumed the JSON can swap in the SDK without a
    rewrite.

    Attributes:
        result_id: Persistent id under
            ``$SYNTH_PANEL_DATA_DIR/results/``.
        model: Panelist model alias.
        persona_count: Number of panelists that participated.
        question_count: Total questions asked across all rounds.
        rounds: Per-round dicts with ``name``, ``results``,
            ``synthesis``, ``usage``.
        path: Routing decisions actually taken —
            ``[{round, branch, next}, ...]``.
        synthesis: Final synthesis payload (or the single round's
            synthesis for v1/v2 and ``questions`` input).
        total_cost: Formatted USD total across panel + synthesis.
        total_usage: Aggregate token usage.
        warnings: Parser + runtime warnings. Includes one
            ``"Cost for model '<X>' computed using DEFAULT_PRICING
            fallback..."`` entry per contributing model that had no
            explicit pricing tier, so callers can spot estimated spend
            without inspecting ``cost_is_estimated``.
        cost_is_estimated: ``True`` when any contributing model (panelist
            or synthesis) was priced via ``DEFAULT_PRICING`` fallback —
            ``total_cost`` should be treated as an estimate, not a
            billed amount. ``False`` when every model has an explicit
            pricing entry.
        terminal_round: The last round whose synthesis fed final
            synthesis. ``None`` for v1 runs and when synthesis is off.
        results: Flat panelist results for the terminal round
            (back-compat mirror — equivalent to ``rounds[-1]["results"]``).
        metadata: Full run metadata bundle (timings, per-tier cost
            breakdown). Present for fresh runs; may be ``None`` on
            results loaded from disk that predate metadata capture.
    """

    result_id: str
    model: str
    persona_count: int
    question_count: int
    rounds: list[dict[str, Any]]
    path: list[dict[str, Any]]
    synthesis: dict[str, Any] | None
    # sp-avmm: declared before trailing defaulted fields so they remain
    # accessible on PanelResult without breaking existing kwargs callers.
    total_cost: str
    total_usage: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    cost_is_estimated: bool = False
    terminal_round: str | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] | None = None
    # sp-avmm: synthesis failure markers, mirroring PollResult. run_invalid
    # is not set by synthesis-stage failures (see PollResult).
    run_invalid: bool = False
    synthesis_error: dict[str, Any] | None = None
    # sy-4yd: deterministic structured-response rollup. See PollResult
    # for field semantics — same payload shape across both result types.
    poll_summary: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_personas(personas: list[dict[str, Any]]) -> None:
    for i, p in enumerate(personas):
        if not isinstance(p, dict):
            raise TypeError(f"Persona at index {i} must be a dict, got {type(p).__name__}.")
        if "name" not in p or not str(p["name"]).strip():
            raise ValueError(f"Persona at index {i} is missing required field 'name'.")
    if len(personas) > MAX_PERSONAS:
        raise ValueError(f"Too many personas ({len(personas)}). Maximum is {MAX_PERSONAS}.")


def _validate_questions(questions: list[dict[str, Any]]) -> None:
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            raise TypeError(f"Question at index {i} must be a dict, got {type(q).__name__}.")
        if "text" not in q or not str(q["text"]).strip():
            raise ValueError(f"Question at index {i} is missing required field 'text'.")
    if len(questions) > MAX_QUESTIONS:
        raise ValueError(f"Too many questions ({len(questions)}). Maximum is {MAX_QUESTIONS}.")


def _resolve_personas(
    personas: list[dict[str, Any]] | None,
    pack_id: str | None,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = list(personas) if personas else []
    if pack_id is not None:
        pack = _data_get_persona_pack(pack_id)
        merged.extend(pack.get("personas", []))
    if not merged:
        raise ValueError("No personas provided. Pass personas=... and/or pack_id=...")
    _validate_personas(merged)
    return merged


def _coerce_questions(questions: list[str] | list[dict[str, Any]] | str) -> list[dict[str, Any]]:
    """Accept a string, list of strings, or list of dicts; normalise to dicts."""
    if isinstance(questions, str):
        return [{"text": questions}]
    out: list[dict[str, Any]] = []
    for q in questions:
        if isinstance(q, str):
            out.append({"text": q})
        else:
            out.append(dict(q))
    return out


def _extract_attachment_refs(
    instrument_obj: Instrument | None,
    flat_results: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Persist response-borne attachment payloads to CAS and rewrite refs.

    Walks ``instrument_obj.attachments`` (the bank) first to reserve the
    stable ``att-bank-<id>`` ref id for any base64-source bank entry, then
    walks each panelist's ``responses[i]["attachments"]`` list, dedupes
    payloads by sha256, writes bytes through
    :func:`althing.attachments.store.write_blob`, and replaces the
    inline dicts with ref-id strings (mutating *flat_results* in place).
    Returns the ``{ref_id: AttachmentRef}`` map for
    :func:`save_panel_result(..., attachments=...)`.

    Only ``image`` and ``document`` attachments with a ``base64`` source —
    the ones blowing up ``result.json`` (per hq-hjk8) — are pulled out.
    URL-source / file-source images and ``url`` / ``html`` types have no
    inline blob to dedupe; we leave those entries untouched in the
    response stream so the readback path stays a no-op for them.
    """
    import base64
    import hashlib

    from althing.attachments import write_blob

    refs: dict[str, dict[str, Any]] = {}
    sha_to_id: dict[str, str] = {}

    _IMAGE_EXT = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }

    def _to_blob(att: dict[str, Any]) -> tuple[bytes | None, str, str, str]:
        atype = att.get("type")
        source = att.get("source") if isinstance(att.get("source"), dict) else None
        if atype == "image" and source and source.get("type") == "base64":
            data = source.get("data")
            if not isinstance(data, str) or not data:
                return None, "", "", ""
            try:
                blob = base64.b64decode(data, validate=False)
            except (ValueError, TypeError):
                return None, "", "", ""
            media_type = att.get("media_type", "image/png")
            return blob, _IMAGE_EXT.get(media_type, ".bin"), media_type, "image"
        if atype == "document" and source and source.get("type") == "base64":
            data = source.get("data")
            if not isinstance(data, str) or not data:
                return None, "", "", ""
            try:
                blob = base64.b64decode(data, validate=False)
            except (ValueError, TypeError):
                return None, "", "", ""
            media_type = att.get("media_type", "application/pdf")
            return blob, ".pdf", media_type, "pdf"
        return None, "", "", ""

    def _register(att: dict[str, Any], preferred_id: str) -> str | None:
        blob, ext, content_type, kind = _to_blob(att)
        if blob is None:
            return None
        sha = hashlib.sha256(blob).hexdigest()
        if sha in sha_to_id:
            return sha_to_id[sha]
        ref_id = preferred_id
        if ref_id in refs:
            n = 2
            while f"{ref_id}-{n}" in refs:
                n += 1
            ref_id = f"{ref_id}-{n}"
        write_blob(blob, ext=ext)
        refs[ref_id] = {
            "id": ref_id,
            "kind": kind,
            "sha256": sha,
            "content_type": content_type,
            "byte_size": len(blob),
        }
        sha_to_id[sha] = ref_id
        return ref_id

    if instrument_obj is not None:
        for bank_id, att in (instrument_obj.attachments or {}).items():
            if isinstance(att, dict):
                _register(att, f"att-bank-{bank_id}")

    for rd in flat_results:
        responses = rd.get("responses") if isinstance(rd, dict) else None
        if not isinstance(responses, list):
            continue
        for q_idx, resp in enumerate(responses):
            if not isinstance(resp, dict):
                continue
            atts = resp.get("attachments")
            if not isinstance(atts, list):
                continue
            new_atts: list[Any] = []
            for a_idx, att in enumerate(atts):
                if isinstance(att, str):
                    new_atts.append(att)
                    continue
                if not isinstance(att, dict):
                    new_atts.append(att)
                    continue
                rid = _register(att, f"att-q{q_idx}-{a_idx}")
                new_atts.append(rid if rid is not None else att)
            resp["attachments"] = new_atts
    return refs


def _compute_poll_summary_payload(
    *,
    result_dicts: list[dict[str, Any]],
    questions: list[dict[str, Any]] | None,
    synthesis: dict[str, Any] | None,
    personas: list[dict[str, Any]] | None,
    rounds: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Run :func:`build_poll_summary` against a freshly produced result.

    Returns the JSON-safe ``PollSummary.to_dict()`` payload or ``None``
    when the run is empty / synthesis-free and no structured response
    landed. Keeping this helper close to the result builders means every
    SDK return path attaches an identical summary shape without each
    caller re-implementing the assembly.

    sy-4yd.
    """
    from althing.poll_summary import build_poll_summary

    envelope: dict[str, Any] = {
        "results": result_dicts,
        "synthesis": synthesis,
    }
    if questions is not None:
        envelope["questions"] = questions
    if rounds is not None:
        envelope["rounds"] = rounds

    summary = build_poll_summary(envelope, personas=personas)
    if not summary.questions and not summary.top_objections and not summary.recommended_next_test:
        # Degenerate run (no panelists / no responses). Returning None
        # is more honest than emitting an empty summary that consumers
        # might mistake for "panel answered nothing of substance".
        return None
    return summary.to_dict()


def _build_panel_result_from_single_round(
    result_id: str,
    model: str,
    personas: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    result_dicts: list[dict[str, Any]],
    panelist_usage: CostTokenUsage,
    panelist_cost: Any,
    synthesis_dict: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    total_usage: CostTokenUsage,
    total_cost: Any,
    contributing_models: list[str | None] | None = None,
) -> PanelResult:
    # sp-nn8k: when any contributing model hits DEFAULT_PRICING fallback,
    # emit a warning per offender and flip the top-level estimated flag.
    # Callers can pass ``contributing_models`` to cover mixed-model runs;
    # otherwise the single ``model`` is the only candidate.
    cost_warnings = build_cost_fallback_warnings(contributing_models if contributing_models is not None else [model])
    # sp-avmm: carry synthesis failure markers onto the top-level result
    # so MCP/CI consumers can spot the failure without walking into the
    # nested synthesis envelope. A synthesis-stage failure does NOT set
    # run_invalid — the panelist responses are complete and usable, so
    # degrade to a warning + synthesis_error instead of invalidating.
    synthesis_error = synthesis_dict.get("synthesis_error") if isinstance(synthesis_dict, dict) else None
    # sp-g59o: surface synthesis-level heuristic warnings (e.g. degenerate
    # structured output) at the top level so MCP consumers don't have to
    # walk into the nested synthesis dict to find them.
    synthesis_warnings = list(synthesis_dict.get("warnings") or []) if isinstance(synthesis_dict, dict) else []
    if isinstance(synthesis_error, dict):
        synthesis_warnings.append(
            f"synthesis_failed: {synthesis_error.get('message', 'synthesis failed')} — panelist responses are complete"
        )
    poll_summary_payload = _compute_poll_summary_payload(
        result_dicts=result_dicts,
        questions=questions,
        synthesis=synthesis_dict,
        personas=personas,
    )
    return PanelResult(
        result_id=result_id,
        model=model,
        persona_count=len(personas),
        question_count=len(questions),
        rounds=[
            {
                "name": "default",
                "results": result_dicts,
                "synthesis": synthesis_dict,
            }
        ],
        path=[],
        synthesis=synthesis_dict,
        total_cost=total_cost.format_usd(),
        total_usage=total_usage.to_dict(),
        warnings=[*cost_warnings, *synthesis_warnings],
        cost_is_estimated=bool(cost_warnings),
        terminal_round=None,
        results=result_dicts,
        metadata=metadata,
        run_invalid=False,
        synthesis_error=synthesis_error if isinstance(synthesis_error, dict) else None,
        poll_summary=poll_summary_payload,
    )


def _build_panel_result_from_multi_round(
    result_id: str,
    model: str,
    personas: list[dict[str, Any]],
    instrument: Instrument,
    mr: MultiRoundResult,
    metadata: dict[str, Any] | None,
) -> PanelResult:
    from althing._runners import format_panelist_result

    rounds_payload: list[dict[str, Any]] = []
    flat_results: list[dict[str, Any]] = []
    total_question_count = 0
    for rr in mr.rounds:
        round_dict_results = [format_panelist_result(pr, model) for pr in rr.panelist_results]
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
        flat_results = round_dict_results

    pricing, _ = lookup_pricing(model)
    total_cost = estimate_cost(mr.usage, pricing)
    final_synth_dict = (
        mr.final_synthesis.to_dict()
        if mr.final_synthesis is not None and hasattr(mr.final_synthesis, "to_dict")
        else None
    )

    # sp-nn8k: append per-model cost-fallback warnings alongside the
    # parser/runtime warnings carried on ``mr.warnings``. Every panelist
    # model and the synthesis model (when present) is checked.
    contributing = {getattr(pr, "model", None) or model for rr in mr.rounds for pr in rr.panelist_results}
    synth_model = getattr(mr.final_synthesis, "model", None) if mr.final_synthesis else None
    cost_warnings = build_cost_fallback_warnings([*sorted(contributing), synth_model])

    # sp-g59o: gather synthesis-level heuristic warnings from per-round
    # syntheses + the final reduce so the degenerate-output detector
    # surfaces uniformly across single and multi-round runs.
    synthesis_warnings: list[str] = []
    for rr in mr.rounds:
        round_warnings = getattr(rr.synthesis, "warnings", None)
        if round_warnings:
            synthesis_warnings.extend(round_warnings)
    final_warnings = getattr(mr.final_synthesis, "warnings", None) if mr.final_synthesis else None
    if final_warnings:
        synthesis_warnings.extend(final_warnings)

    # sy-4yd: walk every round (not just the terminal one) so v3 branching
    # runs surface vote counts from any round that asked a structured
    # question. The summary's per-question entries preserve original
    # question ordering across rounds.
    poll_summary_payload = _compute_poll_summary_payload(
        result_dicts=flat_results,
        questions=None,
        synthesis=final_synth_dict,
        personas=personas,
        rounds=rounds_payload,
    )

    return PanelResult(
        result_id=result_id,
        model=model,
        persona_count=len(personas),
        question_count=total_question_count,
        rounds=rounds_payload,
        path=mr.path,
        synthesis=final_synth_dict,
        total_cost=total_cost.format_usd(),
        total_usage=mr.usage.to_dict(),
        warnings=list(mr.warnings) + list(cost_warnings) + synthesis_warnings,
        cost_is_estimated=bool(cost_warnings),
        terminal_round=mr.terminal_round,
        results=flat_results,
        metadata=metadata,
        poll_summary=poll_summary_payload,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_prompt(
    prompt: str,
    *,
    model: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    seed: int | None = None,
) -> PromptResult:
    """Send a single prompt to an LLM and return its response.

    The simplest entry point — ask a quick one-shot question without
    constructing personas or running a panel.

    Args:
        prompt: The text to send.
        model: Model alias or canonical id. Defaults to the first
            provider whose API key is in the environment (Anthropic →
            ``sonnet``, OpenAI → ``gpt-4o-mini``, etc.).
        temperature: Sampling temperature (0.0-1.0). ``None`` leaves the
            provider default.
        top_p: Nucleus sampling threshold (0.0-1.0). Alternative to
            ``temperature``.

    Returns:
        :class:`PromptResult` with the response text, canonical model
        id, token usage, and USD cost.

    Example:
        >>> from althing import run_prompt
        >>> result = run_prompt("Summarise the MECE principle in one line.")
        >>> print(result.response)
    """
    if not prompt or not prompt.strip():
        raise ValueError("prompt must be a non-empty string.")
    model = model or _default_model()

    client = _get_client()
    request = CompletionRequest(
        model=model,
        max_tokens=4096,
        messages=[InputMessage(role="user", content=[TextBlock(text=prompt)])],
        temperature=temperature,
        top_p=top_p,
        seed=seed,
    )
    response = client.send(request)
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
    return PromptResult(
        response=response.text,
        model=response.model,
        usage=usage.to_dict(),
        cost=cost.format_usd(),
    )


def quick_poll(
    question: str,
    personas: list[dict[str, Any]] | None = None,
    *,
    pack_id: str | None = None,
    model: str | None = None,
    synthesis: bool = True,
    synthesis_model: str | None = None,
    synthesis_prompt: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    seed: int | None = None,
) -> PollResult:
    """Ask one question of a panel and get synthesized findings back.

    A streamlined version of :func:`run_panel` for the common single-
    question case. Each persona answers independently in parallel; by
    default a synthesis step then aggregates themes, agreements, and
    disagreements.

    Args:
        question: The question to put to every persona.
        personas: List of persona dicts (each needs at least ``name``).
            Provide ``personas`` and/or ``pack_id``; at least one is
            required.
        pack_id: Name of an installed persona pack — see
            :func:`list_personas`. Merged after ``personas``.
        model: Model alias for panelist responses. Defaults per
            environment.
        synthesis: Run the synthesis step after collecting responses.
            Default ``True``.
        synthesis_model: Model for the synthesis step. Defaults to the
            panelist model.
        synthesis_prompt: Custom synthesis prompt. Replaces the default
            template.
        temperature: Panelist sampling temperature.
        top_p: Panelist nucleus sampling threshold.

    Returns:
        :class:`PollResult` — per-panelist responses, synthesis, and
        cost. Result is persisted; use ``result.result_id`` with
        :func:`extend_panel` to probe deeper.

    Example:
        >>> from althing import quick_poll
        >>> out = quick_poll(
        ...     "What's confusing about our pricing page?",
        ...     pack_id="general-consumer",
        ... )
        >>> print(out.synthesis["recommendation"])
    """
    if not question or not question.strip():
        raise ValueError("question must be a non-empty string.")
    merged = _resolve_personas(personas, pack_id)
    if model is None:
        model = _default_model_for_panel(len(merged))
    questions = [{"text": question}]

    client = _get_client()
    timer = PanelTimer()
    (
        panelist_results_full,
        result_dicts,
        panelist_usage,
        panelist_cost,
        synthesis_dict,
        _variant_data,
    ) = run_panel_sync(
        client=client,
        personas=merged,
        questions=questions,
        model=model,
        synthesis=synthesis,
        synthesis_model=synthesis_model,
        synthesis_prompt=synthesis_prompt,
        temperature=temperature,
        top_p=top_p,
        seed=seed,
    )

    # sp-atvc: re-price per actual model when panelists ran across
    # providers so the PollResult surfaces real spend.
    per_model_usage, per_model_cost = aggregate_per_model(panelist_results_full, model)
    multi_model_run = len(per_model_usage) > 1
    if multi_model_run:
        panelist_cost = CostEstimate()
        for _c in per_model_cost.values():
            panelist_cost = panelist_cost + _c

    synthesis_usage_obj: CostTokenUsage | None = None
    synthesis_cost_obj = None
    # sp-avmm: synthesis_dict is either a full synthesis payload (with
    # "usage") or an error envelope (with "synthesis_error"). Guard the
    # cost math so the SDK can still emit a PollResult on failure.
    synthesis_error = synthesis_dict.get("synthesis_error") if isinstance(synthesis_dict, dict) else None
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
        persona_count=len(merged),
        question_count=1,
        timer=timer,
        panelist_per_model=panelist_per_model_meta,
    )

    result_id = save_panel_result(
        results=result_dicts,
        model=model,
        total_usage=total_usage.to_dict(),
        total_cost=total_cost.format_usd(),
        persona_count=len(merged),
        question_count=1,
        # sy-oyl: thread the question (and its response_schema if any)
        # into the saved JSON so a reloaded result preserves type info
        # for poll_summary's _detect_kind.
        questions=questions,
        synthesis=synthesis_dict,
        metadata=metadata,
    )

    # sy-4yd: compute the deterministic poll summary inline so callers
    # never see a result without one. Built from the same envelope the
    # SDK persists, so a saved-result reload via
    # :func:`get_panel_result` reconstructs an equivalent summary.
    poll_summary_payload = _compute_poll_summary_payload(
        result_dicts=result_dicts,
        questions=questions,
        synthesis=synthesis_dict,
        personas=merged,
    )

    return PollResult(
        result_id=result_id,
        question=question,
        responses=result_dicts,
        synthesis=synthesis_dict,
        model=model,
        total_usage=total_usage.to_dict(),
        total_cost=total_cost.format_usd(),
        metadata=metadata,
        # A synthesis-stage failure does not invalidate the poll — the
        # panelist responses are complete; synthesis_error carries the
        # failure detail for consumers that want to re-synthesize.
        run_invalid=False,
        synthesis_error=synthesis_error if isinstance(synthesis_error, dict) else None,
        poll_summary=poll_summary_payload,
    )


def run_panel(
    personas: list[dict[str, Any]] | None = None,
    instrument: dict[str, Any] | None = None,
    *,
    questions: list[dict[str, Any]] | list[str] | None = None,
    instrument_pack: str | None = None,
    pack_id: str | None = None,
    model: str | None = None,
    response_schema: dict[str, Any] | None = None,
    synthesis: bool = True,
    synthesis_model: str | None = None,
    synthesis_prompt: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    seed: int | None = None,
    persona_models: dict[str, str] | None = None,
    extract_schema: type[BaseModel] | str | dict[str, Any] | None = None,
    synthesis_temperature: float | None = None,
    variants: int = 0,
    use_case: str | None = None,
    topic: str | None = None,
) -> PanelResult:
    """Run a full synthetic focus group panel.

    Accepts personas inline or via a saved pack, plus **one** of three
    question sources:

    1. ``questions=[...]`` — flat list (or list of strings), v1 shape.
    2. ``instrument={...}`` — a v1/v2/v3 instrument body. v3 instruments
       with ``route_when`` clauses run as a branching multi-round panel.
    3. ``instrument_pack="pricing-discovery"`` — load an installed
       instrument pack. See :func:`list_instruments`.

    Args:
        personas: List of persona dicts. Each needs ``name``. Provide
            this, ``pack_id``, or both.
        instrument: Inline instrument body (the value under the top-
            level ``instrument:`` key in YAML). Takes precedence over
            ``questions``.
        questions: Flat question list for the simplest case. Strings are
            auto-wrapped as ``{"text": ...}``.
        instrument_pack: Installed instrument pack name. Takes
            precedence over both ``instrument`` and ``questions``.
        pack_id: Persona pack name. Merged after inline ``personas``.
        model: Model alias for panelist responses. Defaults per
            environment. Pass ``"auto"`` to pick the model, or a weighted
            blend, from SynthBench routing data for this panel's
            respondent group and question topic. A blend runs as a
            per-persona split; the decision is recorded in
            ``metadata["model_routing"]``. See docs/model-routing.md.
        response_schema: JSON Schema for structured per-persona output.
        synthesis: Run the synthesis step. Default ``True``.
        synthesis_model: Override the synthesis-step model.
        synthesis_prompt: Custom synthesis prompt.
        temperature: Panelist sampling temperature.
        top_p: Panelist nucleus sampling threshold.
        persona_models: Per-persona model overrides, e.g.
            ``{"Sarah Chen": "sonnet", "Marcus": "haiku"}``.
        extract_schema: Post-hoc extraction schema. Accepts:

            * a built-in name — ``"sentiment"``, ``"themes"``, ``"rating"``,
              or a bundled response model (``"likert"``, ``"pick_one"``,
              ``"yes_no"``, ``"ranking"``, ``"annotated_choice"``);
            * an inline JSON Schema dict;
            * a :class:`pydantic.BaseModel` subclass — the wire schema is
              generated via ``model_json_schema()`` and the extracted
              payload is validated through ``model_validate``. Validation
              failures land on each response as
              ``response["extraction_validation_error"]`` so the run still
              produces a usable result (LLM output that wire-validates but
              violates a typed constraint, e.g. ``rating: 7`` for a 1..5
              Likert, surfaces the field-path error rather than crashing
              the panel).
        synthesis_temperature: Temperature for the synthesis step,
            independent of the panelist temperature.
        variants: Number of persona variants per base persona. ``0``
            disables the robustness path. ``1..20`` generates variants
            and computes robustness scores in the result.
        use_case: Only with ``model="auto"``. Respondent group to route
            for (``us-general``, ``us-subgroups``, ``cross-national``).
            Default: inferred from ``pack_id``.
        topic: Only with ``model="auto"``. Question topic to route for
            (``politics``, ``economy``, ``technology``, ``science-health``,
            ``identity``, ``society``, ``global`` or ``all``). Default:
            inferred from the question text.

    Returns:
        :class:`PanelResult` — rounds, routing path, synthesis, cost.

    Example:
        >>> from althing import run_panel
        >>> panel = run_panel(
        ...     pack_id="general-consumer",
        ...     instrument_pack="pricing-discovery",
        ... )
        >>> print(panel.path)

    Typed extraction with a Pydantic class:
        >>> from pydantic import BaseModel, Field
        >>> from althing import run_panel
        >>>
        >>> class FeatureChoice(BaseModel):
        ...     feature: str = Field(..., min_length=1)
        ...     confidence: int = Field(..., ge=1, le=5)
        ...
        >>> panel = run_panel(
        ...     pack_id="developers",
        ...     questions=[{"text": "Which feature should we ship first?"}],
        ...     extract_schema=FeatureChoice,
        ... )
        >>> for r in panel.results:
        ...     extracted = r["responses"][0].get("extraction")
        ...     if extracted is not None:
        ...         choice = FeatureChoice.model_validate(extracted)
        ...         print(choice.feature, choice.confidence)
    """
    if variants < 0 or variants > 20:
        raise ValueError("variants must be between 0 and 20.")
    auto_routing = _model_routing.is_auto_model(model)
    if auto_routing and persona_models:
        raise ValueError("model='auto' picks the per-persona split itself; drop persona_models or pass a model.")
    if not auto_routing and (use_case is not None or topic is not None):
        raise ValueError("use_case and topic only apply with model='auto'.")
    merged = _resolve_personas(personas, pack_id)
    if auto_routing:
        # Fallback default for routing; routing reports when it is used.
        model, _swapped = _fast_default.fast_default_for_panel(_default_model(), len(merged))
    elif model is None:
        model = _default_model_for_panel(len(merged))

    resolved_extract_schema = resolve_extract_schema(extract_schema)

    # Resolve instrument source (pack > inline instrument > questions).
    instrument_obj: Instrument | None = None
    if instrument_pack is not None:
        pack_body = _data_load_instrument_pack(instrument_pack)
        raw = pack_body.get("instrument", pack_body)
        instrument_obj = parse_instrument(raw)
    elif instrument is not None:
        raw = instrument.get("instrument", instrument)
        instrument_obj = parse_instrument(raw)

    route_decision: _model_routing.RouteDecision | None = None
    if auto_routing:
        if instrument_obj is not None:
            routing_questions: list[Any] = [q for rnd in instrument_obj.rounds for q in rnd.questions]
        else:
            routing_questions = _coerce_questions(questions) if questions else []
        route_decision = _model_routing.route_models(
            questions=routing_questions,
            persona_pack=pack_id,
            use_case=use_case,
            topic=topic,
            default_model=model,
            warn=lambda msg: logger.warning("%s", msg),
        )
        model, persona_models, split_warnings = _model_routing.apply_decision_to_personas(route_decision, merged)
        for w in split_warnings:
            logger.warning("model routing: %s", w)
        logger.info("model routing: %s", route_decision.reason)

    client = _get_client()

    if instrument_obj is not None:
        timer = PanelTimer()
        mr = run_multi_round_sync(
            client=client,
            personas=merged,
            instrument=instrument_obj,
            model=model,
            response_schema=response_schema,
            synthesis=synthesis,
            synthesis_model=synthesis_model,
            synthesis_prompt=synthesis_prompt,
            temperature=temperature,
            top_p=top_p,
            seed=seed,
            persona_models=persona_models,
            extract_schema=resolved_extract_schema,
            synthesis_temperature=synthesis_temperature,
        )
        pricing, _ = lookup_pricing(model)
        panelist_usage = ZERO_USAGE
        for rr in mr.rounds:
            for pr in rr.panelist_results:
                panelist_usage = panelist_usage + pr.usage

        # sp-atvc: accurate per-model cost across rounds when panelists
        # ran on different providers (persona_models routing).
        all_pr = [pr for rr in mr.rounds for pr in rr.panelist_results]
        per_model_usage, per_model_cost = aggregate_per_model(all_pr, model)
        multi_model_run = len(per_model_usage) > 1
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
        timer.stop()

        total_cost = panelist_cost_est + (synthesis_cost_for_meta or CostEstimate())

        total_question_count = sum(
            len(next((r.questions for r in instrument_obj.rounds if r.name == rr.name), [])) for rr in mr.rounds
        )
        panelist_per_model_meta = (
            {_m: (per_model_usage[_m], per_model_cost[_m]) for _m in per_model_usage} if multi_model_run else None
        )
        metadata = build_metadata(
            panelist_model=model,
            synthesis_model=getattr(mr.final_synthesis, "model", None) if mr.final_synthesis else None,
            panelist_usage=panelist_usage,
            panelist_cost=panelist_cost_est,
            synthesis_usage=synthesis_usage_for_meta,
            synthesis_cost=synthesis_cost_for_meta,
            total_usage=mr.usage,
            total_cost=total_cost,
            persona_count=len(merged),
            question_count=total_question_count,
            timer=timer,
            panelist_per_model=panelist_per_model_meta,
        )
        if route_decision is not None:
            metadata["model_routing"] = route_decision.to_dict()

        # Flat results for persistence — take the terminal round's panelist list.
        from althing._runners import format_panelist_result

        flat_results = [format_panelist_result(pr, model) for pr in mr.rounds[-1].panelist_results] if mr.rounds else []

        final_synth_dict = (
            mr.final_synthesis.to_dict()
            if mr.final_synthesis is not None and hasattr(mr.final_synthesis, "to_dict")
            else None
        )
        # hq-hjk8: pull inline attachment payloads out of responses and
        # into CAS before persisting; without this, ``result.json`` carries
        # base64 blobs (79 MB at 15p x 10 images in the 2026-05-09 dogfood).
        attachment_refs = _extract_attachment_refs(instrument_obj, flat_results)

        # sy-oyl: flatten every round's questions in declaration order so
        # the saved JSON carries per-question response_schema metadata —
        # poll_summary needs it to avoid classifying open-text questions
        # as scales just because the prose mentioned a number.
        flat_questions: list[dict[str, Any]] = []
        for r in instrument_obj.rounds:
            flat_questions.extend(r.questions)
        result_id = save_panel_result(
            results=flat_results,
            model=model,
            total_usage=mr.usage.to_dict(),
            total_cost=total_cost.format_usd(),
            persona_count=len(merged),
            question_count=total_question_count,
            questions=flat_questions,
            synthesis=final_synth_dict,
            attachments=attachment_refs or None,
            metadata=metadata,
        )
        return _build_panel_result_from_multi_round(
            result_id,
            model,
            merged,
            instrument_obj,
            mr,
            metadata,
        )

    # No instrument — fall back to the flat questions list.
    if not questions:
        raise ValueError("Provide one of: questions=..., instrument=..., or instrument_pack=...")
    normalised_questions = _coerce_questions(questions)
    _validate_questions(normalised_questions)

    timer = PanelTimer()
    (
        panelist_results_full,
        result_dicts,
        panelist_usage,
        panelist_cost,
        synthesis_dict,
        variant_data,
    ) = run_panel_sync(
        client=client,
        personas=merged,
        questions=normalised_questions,
        model=model,
        response_schema=response_schema,
        synthesis=synthesis,
        synthesis_model=synthesis_model,
        synthesis_prompt=synthesis_prompt,
        temperature=temperature,
        top_p=top_p,
        seed=seed,
        persona_models=persona_models,
        extract_schema=resolved_extract_schema,
        synthesis_temperature=synthesis_temperature,
        variants=variants,
    )

    # sp-atvc: per-model accounting for multi-provider panelist routing.
    per_model_usage, per_model_cost = aggregate_per_model(panelist_results_full, model)
    multi_model_run = len(per_model_usage) > 1
    if multi_model_run:
        panelist_cost = CostEstimate()
        for _c in per_model_cost.values():
            panelist_cost = panelist_cost + _c

    synthesis_usage_obj: CostTokenUsage | None = None
    synthesis_cost_obj = None
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
        persona_count=len(merged),
        question_count=len(normalised_questions),
        timer=timer,
        panelist_per_model=panelist_per_model_meta,
    )
    if route_decision is not None:
        metadata["model_routing"] = route_decision.to_dict()

    variant_count = variant_data["variant_count"] if variant_data else 0
    # hq-hjk8: same CAS extraction for the flat-questions path. No
    # instrument bank in scope here, so all refs land as ``att-q<i>-<j>``.
    attachment_refs = _extract_attachment_refs(None, result_dicts)
    result_id = save_panel_result(
        results=result_dicts,
        model=model,
        total_usage=total_usage.to_dict(),
        total_cost=total_cost.format_usd(),
        persona_count=len(merged),
        question_count=len(normalised_questions),
        variant_count=variant_count,
        # sy-oyl: preserve per-question response_schema so poll_summary
        # can respect declared question types after reload.
        questions=normalised_questions,
        synthesis=synthesis_dict,
        attachments=attachment_refs or None,
        metadata=metadata,
    )

    synth_model_for_warning = synthesis_dict.get("model") if synthesis_dict else None
    contributing_models = [*sorted(per_model_usage.keys()), synth_model_for_warning]
    panel = _build_panel_result_from_single_round(
        result_id=result_id,
        model=model,
        personas=merged,
        questions=normalised_questions,
        result_dicts=result_dicts,
        panelist_usage=panelist_usage,
        panelist_cost=panelist_cost,
        synthesis_dict=synthesis_dict,
        metadata=metadata,
        total_usage=total_usage,
        total_cost=total_cost,
        contributing_models=contributing_models,
    )
    if variant_data:
        panel.metadata = dict(panel.metadata or {})
        panel.metadata["robustness_scores"] = variant_data["robustness_scores"]
        panel.metadata["per_persona_robustness"] = variant_data["per_persona_robustness"]
        panel.metadata["variant_count"] = variant_data["variant_count"]
    return panel


def extend_panel(
    result_id: str,
    questions: list[dict[str, Any]] | list[str] | str,
    *,
    model: str | None = None,
    synthesis: bool = True,
    synthesis_model: str | None = None,
    synthesis_prompt: str | None = None,
) -> PanelResult:
    """Append one ad-hoc round to a saved panel result.

    Always appends exactly **one** improvised round on top of an
    existing result, reusing each panelist's saved session so the
    follow-up sees full conversational context. It is **not** a way to
    re-enter the authored v3 DAG: the original instrument's
    ``route_when`` clauses are not consulted, no routing decision is
    made, and the result's ``path`` grows by one entry flagged as an
    extension. For adaptive branching, run a fresh :func:`run_panel`
    against a v3 instrument instead.

    A pre-extend snapshot is preserved alongside the result file
    (``<result_id>.pre-extend.json``) so the operation is reversible.

    Args:
        result_id: ID of a previously saved panel result.
        questions: One or more follow-up questions. Strings are
            auto-wrapped.
        model: Model alias for the new round. Defaults per environment.
        synthesis: Synthesize the new round.
        synthesis_model: Override synthesis model.
        synthesis_prompt: Custom synthesis prompt for the new round.

    Returns:
        :class:`PanelResult` with the full result after extension,
        including the new round.

    Example:
        >>> from althing import extend_panel
        >>> result = extend_panel(
        ...     result_id="result-20260416-...",
        ...     questions="What would change your mind?",
        ... )
    """
    existing = _data_get_panel_result(result_id)
    model = model or _default_model()
    normalised_questions = _coerce_questions(questions)
    if not normalised_questions:
        raise ValueError("At least one follow-up question is required.")

    sessions = load_panel_sessions(result_id)
    personas: list[dict[str, Any]] = [{"name": name} for name in sessions]
    if not personas:
        raise ValueError(f"No saved panelist sessions for result {result_id!r}.")

    client = _get_client()
    panelist_results, _registry, _sessions = run_panel_parallel(
        client=client,
        personas=personas,
        questions=normalised_questions,
        model=model,
        system_prompt_fn=persona_system_prompt,
        question_prompt_fn=build_question_prompt,
        sessions=sessions,
    )

    synth = None
    if synthesis:
        try:
            synth = synthesize_panel(
                client,
                panelist_results,
                normalised_questions,
                model=synthesis_model,
                panelist_model=model,
                custom_prompt=synthesis_prompt,
            )
        except Exception:
            synth = None

    from althing._runners import format_panelist_result

    new_round_results = [format_panelist_result(pr, model) for pr in panelist_results]

    rounds = list(existing.get("rounds") or [])
    appended_name = f"extension-{len(rounds) + 1}"
    rounds.append(
        {
            "name": appended_name,
            "results": new_round_results,
            "synthesis": synth.to_dict() if synth is not None and hasattr(synth, "to_dict") else None,
            "extension": True,
        }
    )
    path = list(existing.get("path") or [])
    path.append(
        {
            "round": appended_name,
            "branch": "extension (ad-hoc, not DAG re-entry)",
            "next": "__end__",
        }
    )

    updated = dict(existing)
    updated["rounds"] = rounds
    updated["path"] = path
    updated["results"] = new_round_results
    updated["question_count"] = int(existing.get("question_count", 0)) + len(normalised_questions)
    update_panel_result(result_id, updated)

    return get_panel_result(result_id)


def list_personas() -> list[dict[str, Any]]:
    """Return metadata for every persona pack — bundled and user-saved.

    Each entry carries ``id``, ``name``, ``persona_count``, and a
    ``builtin`` flag. Use the ``id`` with :func:`quick_poll` or
    :func:`run_panel` via ``pack_id=...``.

    Example:
        >>> from althing import list_personas
        >>> for pack in list_personas():
        ...     print(pack["id"], pack["persona_count"])
    """
    return _data_list_persona_packs()


def list_instruments() -> list[dict[str, Any]]:
    """Return metadata for every installed instrument pack.

    Each entry carries the manifest fields (``id``, ``name``,
    ``version``, ``description``, ``author``) plus ``source``
    (``"bundled"`` or ``"user"``). Use the ``id`` with
    :func:`run_panel` via ``instrument_pack=...``.

    Example:
        >>> from althing import list_instruments
        >>> for pack in list_instruments():
        ...     print(pack["id"], "—", pack["description"])
    """
    return _data_list_instrument_packs()


def list_panel_results() -> list[dict[str, Any]]:
    """Return summaries for every saved panel result.

    Each entry carries ``id``, ``created_at``, ``model``,
    ``persona_count``, and ``question_count``. Use the ``id`` with
    :func:`get_panel_result` to load a full result.

    Example:
        >>> from althing import list_panel_results, get_panel_result
        >>> latest = list_panel_results()[0]
        >>> full = get_panel_result(latest["id"])
    """
    return _data_list_panel_results()


def get_panel_result(result_id: str) -> PanelResult:
    """Load a saved panel result by id.

    Args:
        result_id: The id returned by an earlier :func:`run_panel`,
            :func:`quick_poll`, or :func:`extend_panel` call.

    Returns:
        :class:`PanelResult`. Results saved before certain fields
        existed may return ``None`` / ``[]`` for those fields.

    Example:
        >>> from althing import get_panel_result
        >>> panel = get_panel_result("result-20260416-123456-abcdef")
        >>> print(panel.synthesis)
    """
    data = _data_get_panel_result(result_id)
    rounds = data.get("rounds") or []
    results = data.get("results") or (rounds[-1]["results"] if rounds else [])
    return PanelResult(
        result_id=data.get("id", result_id),
        model=data.get("model", ""),
        persona_count=int(data.get("persona_count", 0)),
        question_count=int(data.get("question_count", 0)),
        rounds=rounds,
        path=data.get("path") or [],
        synthesis=data.get("synthesis"),
        total_cost=data.get("total_cost", ""),
        total_usage=data.get("total_usage") or {},
        warnings=data.get("warnings") or [],
        cost_is_estimated=bool(data.get("cost_is_estimated", False)),
        terminal_round=data.get("terminal_round"),
        results=results,
        metadata=data.get("metadata"),
    )
