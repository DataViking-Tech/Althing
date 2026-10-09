"""Structured output via tool-use forcing (SPEC.md §5).

The primary pattern:
1. Define a "respond" tool whose input schema matches the desired response format.
2. Set tool_choice to "specific" with the respond tool's name.
3. The LLM is forced to produce a tool invocation with valid JSON.
4. Extract the JSON input from the tool invocation as the structured response.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from althing.llm.client import LLMClient
from althing.llm.errors import LLMError
from althing.llm.models import (
    CompletionRequest,
    CompletionResponse,
    InputMessage,
    TextBlock,
    TokenUsage,
    ToolChoice,
    ToolDefinition,
    ToolInvocationBlock,
    ToolResultBlock,
)
from althing.llm.providers.subscription_cli import CLAUDE_CODE_PREFIX, CODEX_PREFIX

logger = logging.getLogger(__name__)

_DEFAULT_TOOL_NAME = "respond"
_DEFAULT_RETRY_LIMIT = 2

# Patterns that identify cheap/flash-tier models warranting escalation on
# the final strike (sp-d1x0 retry policy).
_CHEAP_MODEL_PATTERNS = ("flash", "haiku", "lite", "mini", "nano", "small")
_ESCALATION_MODEL = "sonnet"

# OpenRouter routing prefix. A model id of the form ``openrouter/<vendor>/...``
# is routed through OpenRouter (OPENROUTER_API_KEY), not the vendor's direct
# provider — see althing.llm.client._resolve_provider. The final-strike
# escalation must preserve this prefix so an ``openrouter/anthropic/*`` model
# escalates to an OpenRouter-served Sonnet (still on OPENROUTER_API_KEY) rather
# than the bare ``sonnet`` alias, which resolves to the direct Anthropic
# provider and demands ANTHROPIC_API_KEY the OpenRouter-only caller never set
# (sy-549).
_OPENROUTER_PREFIX = "openrouter/"
# OpenRouter slug for the escalation target. The bare ``sonnet`` alias maps to
# the direct-Anthropic flagship; this is its OpenRouter-served equivalent so
# escalation stays on the same provider/credentials.
_OPENROUTER_ESCALATION_MODEL = "openrouter/anthropic/claude-sonnet-4.5"

# gh#571: in-family escalation targets for the remaining native providers.
# Provider detection is prefix-based on the canonical model id (claude-* →
# Anthropic, gemini-* → Google, grok-* → xAI; see
# althing.llm.client._PROVIDER_REGISTRY), so each escalation target must
# share its family's prefix to resolve to the same provider — and the same
# credentials — as the original model.
_GEMINI_ESCALATION_MODEL = "gemini-2.5-pro"
_XAI_ESCALATION_MODEL = "grok-4"
# OpenAI-compatible models have no registry prefix (they hit the fallback
# provider on whatever base URL is configured), so the only safe "stronger
# sibling" is the same model id minus a cheap-tier suffix — e.g.
# ``gpt-4o-mini`` → ``gpt-4o``, ``gpt-5-nano`` → ``gpt-5`` — which stays on
# the same base URL and API key by construction.
_OPENAI_COMPAT_CHEAP_SUFFIXES = ("-mini", "-nano")
# Local-model routing prefixes (see althing.llm.aliases._LOCAL_PREFIXES):
# there is no known stronger sibling on a local endpoint.
_LOCAL_MODEL_PREFIXES = ("ollama:", "local:")

# sp-d1x0: terminal-failure warning, mirrors the sp-g59o synthesis warning
# surface so operators see consistent signal across extraction failures.
_TERMINAL_FAILURE_WARNING = (
    "structured output extraction failed after all retries — model may have "
    "ignored schema. sp-g59o: consider using a higher-quality model or "
    "simplifying the schema."
)


def _is_cheap_model(model: str) -> bool:
    """Return True when *model* resolves to a flash/cheap tier."""
    from althing.llm.aliases import resolve_alias

    canonical = resolve_alias(model).lower()
    return any(p in canonical for p in _CHEAP_MODEL_PATTERNS)


def _escalation_model_for(model: str) -> str | None:
    """Return the final-strike escalation target for *model*, or ``None``.

    Escalation must stay on the same *provider family* as the original model
    so it uses the same credentials (sy-549, gh#571). Historically everything
    that wasn't ``openrouter/*`` escalated to the bare ``sonnet`` alias, which
    resolves to the *direct* Anthropic provider — so a native ``gemini-*`` run
    with only GEMINI_API_KEY set died with "Missing API key for Anthropic" on
    the final strike (gh#571). Per family:

    * ``openrouter/*``  → OpenRouter-served Sonnet (same key; sy-549, unchanged)
    * ``claude-code:*`` → ``claude-code:sonnet`` (stays on the subscription CLI)
    * ``codex:*``       → none
    * ``claude-*``      → ``sonnet`` alias (historical behaviour, unchanged)
    * ``gemini-*``      → ``gemini-2.5-pro``
    * ``grok-*``        → ``grok-4``
    * OpenAI-compat     → same id minus a cheap-tier suffix (``gpt-4o-mini`` →
      ``gpt-4o``), which stays on the same base URL

    Returns ``None`` when no stronger same-family model is known (local
    models, an unrecognised OpenAI-compat id, or a model already at its
    family's escalation target). Callers must then skip escalation — the
    final strike reuses the original model and terminal failure flows to the
    existing fallback path — rather than demand another provider's key.
    """
    from althing.llm.aliases import resolve_alias

    if model.startswith(_OPENROUTER_PREFIX):
        return _OPENROUTER_ESCALATION_MODEL

    # Subscription CLI models must stay on the same CLI: ``claude-code:haiku``
    # starts with ``claude-`` and would otherwise escalate to the API-key
    # ``sonnet`` alias.
    if model.startswith(CLAUDE_CODE_PREFIX):
        target = f"{CLAUDE_CODE_PREFIX}sonnet"
        return None if model == target else target
    if model.startswith(CODEX_PREFIX):
        return None  # no stable cheap/strong model ladder on the Codex CLI

    canonical = resolve_alias(model)
    if canonical.startswith("claude-"):
        return _ESCALATION_MODEL
    if canonical.startswith("gemini-"):
        if canonical.startswith(_GEMINI_ESCALATION_MODEL):
            return None  # already at the family's escalation target
        return _GEMINI_ESCALATION_MODEL
    if canonical.startswith("grok-"):
        if canonical.startswith(_XAI_ESCALATION_MODEL):
            return None  # already at the family's escalation target
        return _XAI_ESCALATION_MODEL
    if model.startswith(_LOCAL_MODEL_PREFIXES) or canonical.startswith(_LOCAL_MODEL_PREFIXES):
        return None  # local endpoint — no known stronger sibling

    # OpenAI-compat fallback: strip a cheap-tier suffix to reach the bigger
    # sibling on the same base URL; otherwise no escalation is known.
    for suffix in _OPENAI_COMPAT_CHEAP_SUFFIXES:
        if canonical.endswith(suffix):
            return canonical[: -len(suffix)]
    return None


@dataclass
class StructuredOutputConfig:
    """Configuration for structured output extraction."""

    schema: dict[str, Any]
    tool_name: str = _DEFAULT_TOOL_NAME
    tool_description: str | None = None
    retry_limit: int = _DEFAULT_RETRY_LIMIT
    enabled: bool = True


@dataclass
class StructuredResult:
    """Result of a structured output extraction."""

    data: dict[str, Any]
    response: CompletionResponse
    retries_used: int = 0
    is_fallback: bool = False
    error: str | None = None
    # sp-d1x0: cumulative usage across all retry attempts; callers should
    # prefer this over response.usage for accurate cost accounting.
    total_usage: TokenUsage = field(default_factory=TokenUsage)


class StructuredOutputEngine:
    """Wraps an LLMClient to extract structured responses via tool-use forcing.

    Usage::

        engine = StructuredOutputEngine(client)
        config = StructuredOutputConfig(schema={
            "type": "object",
            "properties": {
                "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral"]},
                "summary": {"type": "string"},
            },
            "required": ["sentiment", "summary"],
        })
        result = engine.extract(
            model="sonnet",
            max_tokens=1024,
            messages=[InputMessage(role="user", content=[TextBlock(text="Analyze this.")])],
            config=config,
        )
        print(result.data)  # {"sentiment": "positive", "summary": "..."}
    """

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    def extract(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list[InputMessage],
        config: StructuredOutputConfig,
        system: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        seed: int | None = None,
        provider_routing: dict[str, Any] | None = None,
    ) -> StructuredResult:
        """Run a completion with tool-use forcing and extract structured data.

        Implements a 3-strike retry policy (sp-d1x0):
        - Strike 1: normal prompt
        - Strike 2: corrective prompt appended (same model)
        - Strike 3: corrective prompt + escalated model when original is cheap/flash
        """
        if not config.enabled:
            response = self._client.send(
                CompletionRequest(
                    model=model,
                    max_tokens=max_tokens,
                    messages=messages,
                    system=system,
                    temperature=temperature,
                    top_p=top_p,
                    seed=seed,
                    provider_routing=provider_routing,
                )
            )
            return StructuredResult(data={}, response=response, total_usage=response.usage)

        tool_def = ToolDefinition(
            name=config.tool_name,
            description=config.tool_description or "Respond with structured data.",
            input_schema=config.schema,
        )

        last_error: str | None = None
        last_response: CompletionResponse | None = None
        cumulative_usage = TokenUsage()

        for attempt in range(1 + config.retry_limit):
            # Strike 3: escalate to a higher-quality model on the final attempt
            # when the original model is in the cheap/flash tier.
            effective_model = model
            if attempt == config.retry_limit and _is_cheap_model(model):
                escalation = _escalation_model_for(model)
                if escalation is not None:
                    effective_model = escalation
                    logger.debug(
                        "structured output: escalating from %s to %s on final attempt",
                        model,
                        effective_model,
                    )
                else:
                    # gh#571: no stronger same-provider model is known — keep
                    # the original model rather than crossing provider
                    # families (which would demand credentials the caller
                    # never configured). Terminal failure flows to the
                    # fallback path below.
                    logger.debug(
                        "structured output: no same-provider escalation known for %s; final attempt keeps the original model",
                        model,
                    )

            # Build messages: on retries, append the failed response + correction.
            effective_messages = (
                _build_retry_messages(messages, last_response, config.tool_name, last_error)
                if attempt > 0 and last_response is not None
                else list(messages)
            )

            request = CompletionRequest(
                model=effective_model,
                max_tokens=max_tokens,
                messages=effective_messages,
                system=system,
                tools=[tool_def],
                tool_choice=ToolChoice.specific(config.tool_name),
                temperature=temperature,
                top_p=top_p,
                seed=seed,
                provider_routing=provider_routing,
            )

            try:
                response = self._client.send(request)
            except LLMError:
                raise

            cumulative_usage = cumulative_usage + response.usage
            last_response = response

            extracted = _extract_tool_data(response, config.tool_name)
            if extracted is None:
                last_error = f"Attempt {attempt + 1}: LLM did not produce a valid '{config.tool_name}' tool call"
                continue

            # Validate required schema fields (sp-d1x0: schema non-conformance retry)
            missing = _missing_required(extracted, config.schema)
            if missing:
                last_error = f"Attempt {attempt + 1}: tool call missing required fields {missing}"
                continue

            return StructuredResult(
                data=extracted,
                response=response,
                retries_used=attempt,
                total_usage=cumulative_usage,
            )

        # All strikes exhausted — emit warning and return partial/fallback.
        logger.warning(
            "structured output extraction exhausted all %d retries (model=%s): %s",
            config.retry_limit,
            model,
            _TERMINAL_FAILURE_WARNING,
        )
        return StructuredResult(
            data={"_error": last_error, "_fallback": True},
            response=last_response,  # type: ignore[arg-type]
            retries_used=config.retry_limit,
            is_fallback=True,
            error=last_error,
            total_usage=cumulative_usage,
        )


def _extract_tool_data(
    response: CompletionResponse,
    tool_name: str,
) -> dict[str, Any] | None:
    """Extract structured data from the first matching tool invocation."""
    for block in response.content:
        if isinstance(block, ToolInvocationBlock) and block.name == tool_name and isinstance(block.input, dict):
            return block.input
    return None


def _missing_required(data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Return names of required schema fields absent from *data*."""
    return [k for k in schema.get("required", []) if k not in data]


def _build_retry_messages(
    original_messages: list[InputMessage],
    failed_response: CompletionResponse,
    tool_name: str,
    error: str | None,
) -> list[InputMessage]:
    """Append the failed response + corrective turn to *original_messages*."""
    messages = list(original_messages)

    # Append the failed assistant turn so the model sees its mistake.
    messages.append(InputMessage(role="assistant", content=list(failed_response.content)))

    # Build a corrective user turn.
    correction: list[Any] = []
    tool_calls = [b for b in failed_response.content if isinstance(b, ToolInvocationBlock)]
    if tool_calls:
        # Provide tool_result blocks per API spec before appending text.
        for tc in tool_calls:
            err_text = f"Schema validation failed: {error}. Please call '{tool_name}' again with all required fields."
            correction.append(
                ToolResultBlock(
                    tool_use_id=tc.id,
                    content=[TextBlock(text=err_text)],
                    is_error=True,
                )
            )
    else:
        correction.append(
            TextBlock(
                text=(
                    f"You did not call the '{tool_name}' tool. "
                    f"You MUST use the '{tool_name}' tool with all required fields. "
                    "Please try again."
                )
            )
        )

    messages.append(InputMessage(role="user", content=correction))
    return messages
