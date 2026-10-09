"""LLM Client with provider resolution and retry (SPEC.md §2)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator

from althing.llm.aliases import get_base_url_override, resolve_alias
from althing.llm.capabilities import assert_supports_attachments
from althing.llm.errors import LLMError, LLMErrorCategory
from althing.llm.models import CompletionRequest, CompletionResponse, StreamEvent
from althing.llm.providers.anthropic import ANTHROPIC_CONFIG, AnthropicProvider
from althing.llm.providers.base import LLMProvider, ProviderConfig
from althing.llm.providers.gemini import GEMINI_CONFIG, GeminiProvider
from althing.llm.providers.openai_compat import OPENAI_COMPAT_CONFIG, OpenAICompatibleProvider
from althing.llm.providers.openrouter import OPENROUTER_CONFIG, OpenRouterProvider
from althing.llm.providers.subscription_cli import (
    CLAUDE_CODE_CONFIG,
    CODEX_CONFIG,
    ClaudeCodeProvider,
    CodexProvider,
)
from althing.llm.providers.xai import XAI_CONFIG, XAIProvider
from althing.llm.retry import (
    DEFAULT_INITIAL_BACKOFF as _DEFAULT_INITIAL_BACKOFF,
)
from althing.llm.retry import (
    DEFAULT_MAX_BACKOFF as _DEFAULT_MAX_BACKOFF,
)
from althing.llm.retry import (
    DEFAULT_MAX_BACKOFF_RATE_LIMIT as _DEFAULT_MAX_BACKOFF_RATE_LIMIT,
)
from althing.llm.retry import (
    DEFAULT_MAX_RETRIES as _DEFAULT_MAX_RETRIES,
)
from althing.llm.retry import (
    DEFAULT_MAX_RETRIES_RATE_LIMIT as _DEFAULT_MAX_RETRIES_RATE_LIMIT,
)
from althing.llm.retry import (
    RetryPolicy,
)

logger = logging.getLogger(__name__)

# Provider detection order (SPEC.md §2 — Provider Resolution).
# Subscription CLI providers come first: ``claude-code:*`` would otherwise
# match Anthropic's ``claude-`` prefix. They never satisfy the credential
# fallback below, so they are only reachable by explicit prefix.
_PROVIDER_REGISTRY: list[tuple[ProviderConfig, type[LLMProvider]]] = [
    (CLAUDE_CODE_CONFIG, ClaudeCodeProvider),
    (CODEX_CONFIG, CodexProvider),
    (ANTHROPIC_CONFIG, AnthropicProvider),
    (GEMINI_CONFIG, GeminiProvider),
    (XAI_CONFIG, XAIProvider),
    (OPENROUTER_CONFIG, OpenRouterProvider),
    (OPENAI_COMPAT_CONFIG, OpenAICompatibleProvider),
]


class _TokenBucket:
    """Simple thread-safe token bucket for RPS throttling.

    Capacity defaults to the rate, giving a burst of up to one second of
    calls; refill is continuous.
    """

    def __init__(self, rate_per_second: float, capacity: float | None = None) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be > 0")
        self._rate = rate_per_second
        self._capacity = capacity if capacity is not None else rate_per_second
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        """Block until *tokens* are available, then subtract them."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                self._last_refill = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                needed = tokens - self._tokens
                wait = needed / self._rate
            time.sleep(wait)


class LLMClient:
    """Provider-agnostic LLM client with automatic provider resolution and retry.

    Usage::

        client = LLMClient()
        response = client.send(CompletionRequest(
            model="sonnet",
            max_tokens=1024,
            messages=[InputMessage(role="user", content=[TextBlock(text="Hi")])],
        ))

    For scaled panel runs, pass ``max_concurrent`` to cap in-flight calls
    and ``rate_limit_rps`` to smooth the request rate per second.
    """

    def __init__(
        self,
        *,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        initial_backoff: float = _DEFAULT_INITIAL_BACKOFF,
        max_backoff: float = _DEFAULT_MAX_BACKOFF,
        max_retries_rate_limit: int = _DEFAULT_MAX_RETRIES_RATE_LIMIT,
        max_backoff_rate_limit: float = _DEFAULT_MAX_BACKOFF_RATE_LIMIT,
        max_concurrent: int | None = None,
        rate_limit_rps: float | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._retry_policy = retry_policy or RetryPolicy(
            max_retries=max_retries,
            initial_backoff=initial_backoff,
            max_backoff=max_backoff,
            max_retries_rate_limit=max_retries_rate_limit,
            max_backoff_rate_limit=max_backoff_rate_limit,
        )
        self._provider_cache: dict[str, LLMProvider] = {}
        self._cache_lock = threading.Lock()
        self._concurrency: threading.Semaphore | None = (
            threading.Semaphore(max_concurrent) if max_concurrent and max_concurrent > 0 else None
        )
        self._bucket: _TokenBucket | None = (
            _TokenBucket(rate_limit_rps) if rate_limit_rps and rate_limit_rps > 0 else None
        )
        # Track which providers we've already warned for unsupported seed
        # (one-shot per LLMClient instance, per provider name).
        self._seed_warned: set[str] = set()
        self._seed_warned_lock = threading.Lock()

    def _resolve_provider(self, model: str) -> LLMProvider:
        """Resolve a provider for the given canonical model name."""
        with self._cache_lock:
            if model in self._provider_cache:
                return self._provider_cache[model]

            # 1. Match by prefix
            for config, cls in _PROVIDER_REGISTRY:
                for prefix in config.model_prefixes:
                    if model.startswith(prefix):
                        provider = cls()
                        self._provider_cache[model] = provider
                        return provider

            # 2. Fallback: first provider with credentials available
            for config, cls in _PROVIDER_REGISTRY:
                if config.has_credentials():
                    provider = cls()
                    self._provider_cache[model] = provider
                    return provider

        raise LLMError(
            "No LLM provider credentials found. Run `althing login` or set "
            "ANTHROPIC_API_KEY, GEMINI_API_KEY, XAI_API_KEY, OPENROUTER_API_KEY, "
            "or OPENAI_API_KEY — or use a subscription CLI model "
            "(claude-code:<model> / codex:<model>).",
            LLMErrorCategory.MISSING_CREDENTIALS,
        )

    def _prepare(self, request: CompletionRequest) -> CompletionRequest:
        """Resolve aliases and local model prefixes on the request model."""
        original = request.model
        base_url = get_base_url_override(original)
        canonical = resolve_alias(original)

        # For local models (ollama:*, local:*), cache a provider with the
        # correct base URL so _resolve_provider finds it.
        if base_url is not None:
            with self._cache_lock:
                if canonical not in self._provider_cache:
                    self._provider_cache[canonical] = OpenAICompatibleProvider(
                        base_url=base_url, api_key="no-key-required"
                    )

        if canonical != original:
            return CompletionRequest(
                model=canonical,
                max_tokens=request.max_tokens,
                messages=request.messages,
                system=request.system,
                tools=request.tools,
                tool_choice=request.tool_choice,
                stream=request.stream,
                temperature=request.temperature,
                top_p=request.top_p,
                seed=request.seed,
                cache_enabled=request.cache_enabled,
                provider_routing=request.provider_routing,
            )
        return request

    def _check_seed_support(self, request: CompletionRequest, provider: LLMProvider) -> None:
        """Emit a one-shot warning when ``seed`` is set on a provider that
        does not honor it (sy-cxp).

        Anthropic's Messages API has no equivalent of OpenAI's ``seed``
        parameter, and local / unknown OpenAI-compatible endpoints aren't
        guaranteed to support it either. Rather than silently dropping the
        flag, log once per (provider) so the user knows determinism won't
        hold for those calls — repeated per-turn warnings would be noise.
        """
        if request.seed is None:
            return
        config = getattr(provider, "config", None)
        supports = bool(getattr(config, "supports_seed", False))
        if supports:
            return
        name = _provider_name(provider)
        with self._seed_warned_lock:
            if name in self._seed_warned:
                return
            self._seed_warned.add(name)
        logger.warning(
            "--seed=%s is not supported by %s models; runs against this "
            "provider will not be deterministic. Use temperature=0 for "
            "closer-to-deterministic output.",
            request.seed,
            name,
        )

    def send(self, request: CompletionRequest) -> CompletionResponse:
        """Send a blocking completion request with automatic retry."""
        request = self._prepare(request)
        assert_supports_attachments(request)
        provider = self._resolve_provider(request.model)
        self._check_seed_support(request, provider)
        logger.debug("send model=%s max_tokens=%d", request.model, request.max_tokens)
        if self._bucket is not None:
            self._bucket.acquire()
        if self._concurrency is not None:
            self._concurrency.acquire()
        try:
            t0 = time.monotonic()
            response = self._retry_policy.run(
                lambda: provider.send(request),
                provider_name=_provider_name(provider),
            )
            elapsed = time.monotonic() - t0
        finally:
            if self._concurrency is not None:
                self._concurrency.release()
        logger.debug(
            "response model=%s latency=%.2fs tokens_in=%d tokens_out=%d",
            response.model,
            elapsed,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        return response

    def stream(self, request: CompletionRequest) -> Iterator[StreamEvent]:
        """Send a streaming request. Retry is NOT applied to streams."""
        request = self._prepare(request)
        assert_supports_attachments(request)
        provider = self._resolve_provider(request.model)
        self._check_seed_support(request, provider)
        return provider.stream(request)


def _provider_name(provider: LLMProvider) -> str:
    """Return the canonical display name for a provider.

    Reads ``provider.config.name`` (set on each provider's ``ProviderConfig``).
    Falls back to the class name so retry logs always have a value.
    """
    config = getattr(provider, "config", None)
    name = getattr(config, "name", "") if config is not None else ""
    return name or type(provider).__name__
