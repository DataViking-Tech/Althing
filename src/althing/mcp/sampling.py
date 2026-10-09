"""MCP sampling bridge.

MCP's *sampling* feature lets an MCP server ask the invoking client
(e.g. Claude Desktop) to run an LLM completion on the server's behalf,
using the client's own subscription or credentials.

Sampling is deprecated as of MCP 2026-07-28 (SEP-2577) and Claude Code
never implemented the client side. The subscription CLI providers
(``claude-code:*`` / ``codex:*`` in
:mod:`althing.llm.providers.subscription_cli`) are the forward-looking
"run on the user's subscription" path; an explicit CLI model makes
:func:`decide_mode` pick BYOK. This lets althing give first-time users a
zero-configuration experience — no ``ANTHROPIC_API_KEY`` setup needed
to fire their first prompt or quick poll.

Design
======

Two routing decisions live here:

1. **Can we sample?** — the client must advertise the ``sampling``
   capability (on the ``initialize`` handshake for legacy protocol
   revisions, or in the per-request ``_meta`` envelope on 2026-07-28+)
   and a :class:`Context` must actually be threaded through from the
   tool call. Exposed via :func:`client_supports_sampling`.

2. **Should we sample?** — we honour an explicit ``use_sampling`` flag
   on the calling tool; otherwise we fall back to sampling only when no
   BYOK credentials are present in the environment. Exposed via
   :func:`decide_mode`.

Transport (spec 2026-07-28 / SDK 2.0)
=====================================

*How* a sampling request reaches the client depends on the negotiated
protocol revision:

* **Handshake era (≤ 2025-11-25):** the server sends a standalone
  ``sampling/createMessage`` server→client JSON-RPC request mid-call via
  ``ctx.session.create_message``. This is the classic sampling flow.
* **Modern era (2026-07-28+):** the protocol defines *no* server→client
  requests at all. Instead the tool call returns an
  ``InputRequiredResult`` carrying the batched ``CreateMessageRequest``s
  plus opaque ``request_state``; the client fulfils them through its
  sampling callback and retries the call (SEP-2322 multi-round-trip).

:class:`SamplingBridge` abstracts over both: tool executors declare the
samples they need via :meth:`SamplingBridge.gather` and either receive
the results (mid-call on legacy; restored from ``input_responses`` /
``request_state`` on a modern retry round) or a :class:`SamplingSuspend`
propagates so the tool can surface the ``InputRequiredResult``.

Tool handlers call :func:`sample_text` / :meth:`SamplingBridge.gather`
once the decision is made.
"""

from __future__ import annotations

import json
import logging
import warnings
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Sampling mode guardrails — keep invocations light so we don't blast
# the host agent's context window. Heavy research panels still require
# BYOK credentials.
SAMPLING_MAX_PERSONAS = 3
SAMPLING_MAX_QUESTIONS = 5
SAMPLING_MAX_TOKENS_DEFAULT = 2048

# sp-k2ed4a: canonical MCP stop_reason value indicating the host's token
# ceiling cut the response off mid-stream. Hosts (Claude Desktop, Cursor,
# Windsurf...) commonly cap output more aggressively than the request,
# silently truncating the JSON a structured-output engine then fails to
# parse. We surface this as a warning so callers can distinguish "host
# clipped me" from generic schema-fail.
SAMPLING_STOP_REASON_TRUNCATED = "maxTokens"

__all__ = [
    "SAMPLING_CRED_ENV_VARS",
    "SAMPLING_FIRST_RUN_HINT",
    "SAMPLING_MAX_PERSONAS",
    "SAMPLING_MAX_QUESTIONS",
    "SAMPLING_MAX_TOKENS_DEFAULT",
    "SAMPLING_STOP_REASON_TRUNCATED",
    "SampleSpec",
    "SamplingBridge",
    "SamplingDecision",
    "SamplingSuspend",
    "build_truncation_warning",
    "client_supports_sampling",
    "decide_mode",
    "has_byok_credentials",
    "sample_text",
    "uses_input_required",
]

# Environment variables we treat as "BYOK present". Matches the provider
# set in althing.llm.providers — must stay in sync with the CLI's
# _DEFAULT_MODEL_PREFERENCE and sdk._DEFAULT_MODEL_PREFERENCE, otherwise
# a user who set (e.g.) OPENROUTER_API_KEY gets routed to sampling or a
# "missing credentials" error despite the CLI recognising the same key.
SAMPLING_CRED_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
)

# Importing here (module scope) would create a cycle if credentials ever
# grew an MCP dependency; the function-local import in
# :func:`has_byok_credentials` keeps the boundary one-directional.

# First-run hint surfaced on successful sampling runs so users know they
# can graduate to BYOK for larger panels / ensembles.
SAMPLING_FIRST_RUN_HINT = (
    "Running in host-agent sampling mode — althing is borrowing your "
    "MCP client's LLM access instead of calling a provider directly. For "
    "cross-provider ensembles and larger panels, set ANTHROPIC_API_KEY "
    "(or another provider key) in your environment and re-run. "
    "See https://althing.dev/mcp#credentials."
)


@dataclass
class SamplingDecision:
    """Outcome of :func:`decide_mode`.

    Attributes:
        mode: One of ``"sampling"``, ``"byok"``, or ``"error"``.
        hint: When ``mode == "sampling"``, a one-line user hint to
            surface in the response. ``None`` otherwise.
        error: When ``mode == "error"``, a friendly error message
            explaining how to unblock. ``None`` otherwise.
    """

    mode: str
    hint: str | None = None
    error: str | None = None


def has_byok_credentials(env: dict[str, str] | None = None) -> bool:
    """Return True when any provider credential is available to BYOK.

    Checks both the process environment and the on-disk credential store
    written by ``althing login`` (sp-1ez / v0.9.4), so an MCP-launched
    subprocess — which often runs without the invoking shell's env —
    still recognises keys the CLI can see. When ``env`` is provided
    (test harness), only that mapping is consulted; the disk store is
    intentionally skipped so tests stay hermetic.
    """
    if env is not None:
        return any(env.get(var, "").strip() for var in SAMPLING_CRED_ENV_VARS)
    from althing.credentials import has_credential

    return any(has_credential(var) for var in SAMPLING_CRED_ENV_VARS)


def client_supports_sampling(ctx: Any) -> bool:
    """Return True when the invoking MCP client advertised sampling.

    ``ctx`` is an MCPServer :class:`mcp.server.mcpserver.Context`. We go
    through the underlying session because ``check_client_capability``
    is the supported way to interrogate the client's declared
    capabilities — on legacy revisions these come from the ``initialize``
    handshake, on 2026-07-28+ from the per-request ``_meta`` envelope;
    the SDK normalises both onto the same accessor. Any failure (no
    context, no session, capability object not importable) is treated as
    "no sampling" — we never want capability detection to raise into the
    tool handler.
    """
    if ctx is None:
        return False

    # ``ctx.session`` is a property that raises when the Context is
    # constructed outside a live request (in-process test
    # harness, synthetic invocations). We swallow any such failure —
    # capability detection must never raise into a tool handler.
    try:
        session = ctx.session
    except Exception:
        return False
    if session is None:
        return False

    try:
        from mcp.types import ClientCapabilities, SamplingCapability

        check = session.check_client_capability(ClientCapabilities(sampling=SamplingCapability()))
    except Exception:
        return False
    return bool(check)


def decide_mode(
    ctx: Any,
    *,
    use_sampling: bool | None = None,
    env: dict[str, str] | None = None,
    model: str | None = None,
) -> SamplingDecision:
    """Choose between sampling and BYOK for this tool call.

    Args:
        ctx: The MCPServer :class:`Context` passed into the tool.
        use_sampling: Explicit override from the tool caller. ``True``
            forces sampling (error if unsupported), ``False`` forces
            BYOK, ``None`` picks automatically.
        env: Optional env dict for testing. When ``None`` both the
            process environment and the on-disk credential store are
            consulted via :func:`has_byok_credentials`.
        model: The model the call will run on. A subscription CLI model
            (``claude-code:*`` / ``codex:*``) needs no API key, so it
            counts as available credentials in auto mode.

    Rules (in order):
        * ``use_sampling=True`` + client supports sampling → sampling.
        * ``use_sampling=True`` + client does NOT support sampling →
          error (explains the client must advertise ``sampling``).
        * ``use_sampling=False`` → BYOK unconditionally.
        * Auto + no creds + client supports sampling → sampling.
        * Auto + no creds + no sampling → error (set an API key OR use
          a sampling-capable client).
        * Auto + creds present (or a subscription CLI model) → BYOK
          (preserves existing behaviour and keeps ensemble /
          multi-provider features available).
    """
    from althing.llm.providers.subscription_cli import is_subscription_cli_model

    supports = client_supports_sampling(ctx)
    has_creds = is_subscription_cli_model(model) or has_byok_credentials(env)

    if use_sampling is True:
        if supports:
            return SamplingDecision(mode="sampling", hint=SAMPLING_FIRST_RUN_HINT)
        return SamplingDecision(
            mode="error",
            error=(
                "use_sampling=True was requested, but the invoking MCP "
                "client did not advertise the 'sampling' capability "
                "(Claude Code does not support MCP sampling). Set a "
                "provider API key (e.g. ANTHROPIC_API_KEY), or pass a "
                "subscription CLI model such as model='claude-code:haiku' "
                "to run on a signed-in Claude Code / Codex CLI."
            ),
        )

    if use_sampling is False:
        return SamplingDecision(mode="byok")

    # Auto mode.
    if has_creds:
        return SamplingDecision(mode="byok")
    if supports:
        return SamplingDecision(mode="sampling", hint=SAMPLING_FIRST_RUN_HINT)
    return SamplingDecision(
        mode="error",
        error=(
            "No provider credentials found (ANTHROPIC_API_KEY / "
            "OPENAI_API_KEY / XAI_API_KEY / GOOGLE_API_KEY / "
            "GEMINI_API_KEY / OPENROUTER_API_KEY) and the invoking "
            "MCP client did not advertise 'sampling' capability. Set a "
            "provider key in your environment, pass a subscription CLI "
            "model such as model='claude-code:haiku' to run on a "
            "signed-in Claude Code / Codex CLI, or run althing from a "
            "sampling-capable client (Claude Code is not one). "
            "See https://althing.dev/mcp#credentials."
        ),
    )


def build_truncation_warning(*, max_tokens: int, model: str | None) -> str:
    """Build a user-facing message describing host-side token-cap truncation.

    Surfaced in panel/quick-poll ``warnings`` lists so MCP/CLI consumers
    can distinguish a host max_tokens cap from a generic schema-fail when
    a structured-output post-parse fallback fires. ``model`` is whatever
    the host reported running (e.g. ``"claude-opus-4-6"``); ``None`` when
    the host did not name a model.
    """
    model_part = f" (host model: {model})" if model else ""
    return (
        f"MCP host truncated sampling output at the {max_tokens}-token "
        f"ceiling{model_part} — the response may be incomplete and any "
        "structured-output parse failure on this turn is likely caused by "
        "truncation rather than the model ignoring the schema. Hosts may "
        "cap output more aggressively than requested; for longer schemas, "
        "set a provider key (e.g. ANTHROPIC_API_KEY) to use BYOK with a "
        "higher token budget."
    )


async def sample_text(
    ctx: Any,
    *,
    prompt: str,
    system_prompt: str | None = None,
    max_tokens: int = SAMPLING_MAX_TOKENS_DEFAULT,
    temperature: float | None = None,
    accept_multimodal: bool = False,
) -> dict[str, Any]:
    """Run one sampling round via ``ctx.session.create_message``.

    Returns a dict with keys ``text``, ``model``, ``stop_reason``,
    ``role``, ``truncated``, ``requested_max_tokens``, and ``warning``.
    The model string is whatever the host agent chose to run (e.g.
    ``"claude-opus-4-6"`` when invoked from Claude Desktop). We
    normalise content blocks to a single joined string so downstream
    consumers don't have to special-case multi-block responses.

    Truncation detection (sp-k2ed4a): when the host reports
    ``stopReason == "maxTokens"`` the response was cut short by the
    host's output cap. This commonly happens because hosts (Claude
    Desktop, Cursor, Windsurf...) impose their own ceiling that ignores
    or undershoots ``max_tokens``. We log a warning, set ``truncated``,
    and surface a ready-to-display ``warning`` string so callers can
    propagate the signal into their ``warnings`` payload instead of
    chalking the partial JSON up to a generic schema-fail.

    Multimodal sampling (T6 / hq-l0lw): when ``accept_multimodal`` is
    ``True`` the result dict carries a ``content_blocks`` key holding
    the parsed list of althing
    :class:`~althing.llm.models.ContentBlock`\\ s for image / document /
    text content the host returned. Default-off preserves the silent-drop
    behaviour callers depended on before this flag landed. The cost cliff
    is real (multimodal tokens can be ~10x a text-only turn), so the flag
    is opt-in per call.
    """
    from mcp.shared.exceptions import MCPDeprecationWarning
    from mcp.types import SamplingMessage, TextContent

    messages = [
        SamplingMessage(
            role="user",
            content=TextContent(type="text", text=prompt),
        )
    ]
    with warnings.catch_warnings():
        # SDK 2.0 deprecates classic sampling (SEP-2577) but keeps it
        # functional for handshake-era clients. Suppress the advisory —
        # althing routes 2026-07-28+ clients through the InputRequired
        # flow (SamplingBridge) instead of this call.
        warnings.simplefilter("ignore", MCPDeprecationWarning)
        result = await ctx.session.create_message(
            messages=messages,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            temperature=temperature,
        )

    return _normalize_sample_result(result, max_tokens=max_tokens, accept_multimodal=accept_multimodal)


def _normalize_sample_result(
    result: Any,
    *,
    max_tokens: int,
    accept_multimodal: bool = False,
) -> dict[str, Any]:
    """Flatten a ``CreateMessageResult`` into althing's sampling result dict.

    Shared by the mid-call legacy path (:func:`sample_text`) and the
    2026-07-28 InputRequired path (:class:`SamplingBridge`), so both
    protocol eras surface identical result shapes to the executors —
    including truncation detection (sp-k2ed4a) and opt-in multimodal
    block extraction (T6 / hq-l0lw).
    """
    text = _extract_text(result.content)
    content_blocks = _extract_content_blocks(result.content) if accept_multimodal else None
    stop_reason = getattr(result, "stop_reason", None) or getattr(result, "stopReason", None)
    truncated = stop_reason == SAMPLING_STOP_REASON_TRUNCATED
    warning: str | None = None
    if truncated:
        warning = build_truncation_warning(max_tokens=max_tokens, model=result.model)
        logger.warning(
            "MCP sampling truncated: stopReason=%s requested_max_tokens=%d model=%s output_chars=%d",
            stop_reason,
            max_tokens,
            result.model,
            len(text),
        )
    return {
        "text": text,
        "content_blocks": content_blocks,
        "model": result.model,
        "stop_reason": stop_reason,
        "role": result.role,
        "truncated": truncated,
        "requested_max_tokens": max_tokens,
        "warning": warning,
    }


# ---------------------------------------------------------------------------
# 2026-07-28 multi-round-trip sampling (SEP-2322)
# ---------------------------------------------------------------------------

# request_state schema version for the sampling bridge. The SDK's
# RequestStateBoundary seals/verifies the token, so the payload only needs
# to be self-describing, not tamper-proof.
_BRIDGE_STATE_VERSION = 1

# Protocol revision at which server→client requests disappear and
# `tools/call` grows the InputRequiredResult retry loop.
_INPUT_REQUIRED_VERSION = "2026-07-28"


def uses_input_required(ctx: Any) -> bool:
    """True when this request negotiated a 2026-07-28+ protocol revision.

    Modern revisions define **no** server→client requests, so classic
    ``sampling/createMessage`` cannot be sent mid-call; sampling must ride
    the ``InputRequiredResult`` retry loop instead. Unknown or missing
    protocol versions conservatively return ``False`` (the legacy mid-call
    path), matching the SDK's own version-comparison semantics.
    """
    try:
        version = ctx.protocol_version
    except Exception:
        return False
    if not isinstance(version, str):
        return False
    try:
        from mcp_types.version import is_version_at_least

        return is_version_at_least(version, _INPUT_REQUIRED_VERSION)
    except Exception:
        return version >= _INPUT_REQUIRED_VERSION


@dataclass
class SampleSpec:
    """One sampling request an executor wants fulfilled.

    Rendered to a ``CreateMessageRequest`` on the modern path or passed to
    ``ctx.session.create_message`` on the legacy path.
    """

    prompt: str
    system_prompt: str | None = None
    max_tokens: int = SAMPLING_MAX_TOKENS_DEFAULT
    temperature: float | None = None


class SamplingSuspend(Exception):
    """The tool must return an ``InputRequiredResult`` and wait for a retry.

    Raised by :meth:`SamplingBridge.gather` on 2026-07-28+ connections when
    one or more requested samples have no recorded answer yet. Carries the
    batched ``input_requests`` map plus the serialized bridge state
    (already-answered samples) to echo through ``request_state``.
    """

    def __init__(self, input_requests: dict[str, Any], request_state: str) -> None:
        super().__init__("sampling requires client input (InputRequiredResult round-trip)")
        self.input_requests = input_requests
        self.request_state = request_state

    def to_input_required(self) -> Any:
        """Build the ``InputRequiredResult`` for the tool to return."""
        from mcp.types import InputRequiredResult

        return InputRequiredResult(
            input_requests=self.input_requests,
            request_state=self.request_state,
        )


@dataclass
class SamplingBridge:
    """Protocol-era-agnostic sampling executor support.

    Constructed once per tool invocation from the request :class:`Context`.
    Executors call :meth:`gather` with a mapping of stable keys to
    :class:`SampleSpec`\\ s:

    * **Legacy connections** (≤ 2025-11-25): each spec is fulfilled
      immediately via ``ctx.session.create_message`` (serial — host agents
      rate-limit sampling), exactly as before SDK 2.0.
    * **Modern connections** (2026-07-28+): answers recorded on earlier
      retry rounds are restored from ``request_state`` /
      ``input_responses``; any still-missing specs raise
      :class:`SamplingSuspend`, which the tool converts into an
      ``InputRequiredResult``. The client's sampling callback answers the
      batched requests and retries the call, re-running the executor with
      the answers available.

    Keys must be deterministic across retry rounds (the client echoes them
    back verbatim); executors use semantic keys like ``persona_0`` /
    ``synthesis``.
    """

    ctx: Any
    modern: bool = field(init=False)
    _answers: dict[str, dict[str, Any]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.modern = uses_input_required(self.ctx)
        if self.modern:
            self._restore()

    def _restore(self) -> None:
        """Load answers from the echoed request_state and this round's responses."""
        raw_state = getattr(self.ctx, "request_state", None)
        if raw_state:
            try:
                payload = json.loads(raw_state)
                if isinstance(payload, dict) and payload.get("v") == _BRIDGE_STATE_VERSION:
                    answers = payload.get("answers")
                    if isinstance(answers, dict):
                        self._answers.update(answers)
            except (ValueError, TypeError):
                # The SDK verified the token's integrity; a parse failure
                # here means a bridge-version skew. Re-ask from scratch.
                logger.warning("sampling bridge: unreadable request_state; restarting sampling flow")
        responses = getattr(self.ctx, "input_responses", None)
        if responses:
            for key, response in responses.items():
                dump = getattr(response, "model_dump", None)
                if dump is None:
                    continue
                self._answers[key] = dump(mode="json", by_alias=True, exclude_none=True)

    def _render_request(self, spec: SampleSpec) -> Any:
        from mcp.types import CreateMessageRequest, CreateMessageRequestParams, SamplingMessage, TextContent

        return CreateMessageRequest(
            params=CreateMessageRequestParams(
                messages=[SamplingMessage(role="user", content=TextContent(type="text", text=spec.prompt))],
                max_tokens=spec.max_tokens,
                system_prompt=spec.system_prompt,
                temperature=spec.temperature,
            )
        )

    def _decode_answer(self, key: str, spec: SampleSpec, *, accept_multimodal: bool) -> dict[str, Any]:
        from mcp.types import CreateMessageResult

        result = CreateMessageResult.model_validate(self._answers[key])
        return _normalize_sample_result(result, max_tokens=spec.max_tokens, accept_multimodal=accept_multimodal)

    async def gather(
        self,
        requests: dict[str, SampleSpec],
        *,
        accept_multimodal: bool = False,
        on_progress: Any = None,
    ) -> dict[str, dict[str, Any]]:
        """Fulfil every requested sample, or suspend for a retry round.

        Args:
            requests: Mapping of stable keys to sample specs. Keys must
                render identically on every retry round of the same call.
            accept_multimodal: Preserve image/document blocks from the host
                (T6 / hq-l0lw) in each result's ``content_blocks``.
            on_progress: Optional ``async (done, total)`` callback, invoked
                per fulfilled sample on the legacy path and once after
                restore on the modern path.

        Returns:
            Mapping of the same keys to normalized sampling result dicts
            (see :func:`_normalize_sample_result`).

        Raises:
            SamplingSuspend: Modern connection and at least one sample is
                unanswered; the tool must return
                ``SamplingSuspend.to_input_required()``.
        """
        if not self.modern:
            out: dict[str, dict[str, Any]] = {}
            for i, (key, spec) in enumerate(requests.items()):
                out[key] = await sample_text(
                    self.ctx,
                    prompt=spec.prompt,
                    system_prompt=spec.system_prompt,
                    max_tokens=spec.max_tokens,
                    temperature=spec.temperature,
                    accept_multimodal=accept_multimodal,
                )
                if on_progress is not None:
                    await on_progress(i + 1, len(requests))
            return out

        missing = {key: spec for key, spec in requests.items() if key not in self._answers}
        if missing:
            state = json.dumps({"v": _BRIDGE_STATE_VERSION, "answers": self._answers})
            raise SamplingSuspend(
                {key: self._render_request(spec) for key, spec in missing.items()},
                state,
            )
        results = {
            key: self._decode_answer(key, spec, accept_multimodal=accept_multimodal) for key, spec in requests.items()
        }
        if on_progress is not None:
            await on_progress(len(requests), len(requests))
        return results


def _extract_text(content: Any) -> str:
    """Flatten sampling result content into a single text string."""
    # ``content`` may be a single TextContent/ImageContent or a list of
    # them. We only surface text — image/audio content from the host
    # agent isn't useful to the panel simulation, so it's silently
    # dropped with a newline join between blocks.
    if content is None:
        return ""
    blocks = content if isinstance(content, list) else [content]
    parts: list[str] = []
    for block in blocks:
        btype = getattr(block, "type", None)
        if btype == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(p for p in parts if p)


def _extract_content_blocks(content: Any) -> list[Any]:
    """Convert MCP sampling result content into althing ContentBlocks.

    Used when callers opt into multimodal sampling (T6 / hq-l0lw). MCP
    ``TextContent`` lowers to :class:`~althing.llm.models.TextBlock`;
    ``ImageContent`` lowers to a base64
    :class:`~althing.llm.models.ImageBlock`. Unrecognized block types
    are dropped (the host shouldn't return them, but we don't want a
    schema fail to crash the sampling path).
    """
    from althing.llm.models import ImageBlock, InlineSource, TextBlock

    if content is None:
        return []
    blocks = content if isinstance(content, list) else [content]
    out: list[Any] = []
    for block in blocks:
        btype = getattr(block, "type", None)
        if btype == "text":
            text = getattr(block, "text", "")
            if text:
                out.append(TextBlock(text=text))
        elif btype == "image":
            data = getattr(block, "data", None)
            media_type = getattr(block, "mimeType", None) or getattr(block, "media_type", None)
            if (
                isinstance(data, str)
                and isinstance(media_type, str)
                and media_type in {"image/png", "image/jpeg", "image/gif", "image/webp"}
            ):
                out.append(
                    ImageBlock(
                        source=InlineSource(data=data),
                        media_type=media_type,  # type: ignore[arg-type]
                    )
                )
    return out
