"""Subscription CLI providers: Claude Code and Codex in headless mode.

These providers route completions through a locally installed, signed-in
agent CLI (``claude -p`` / ``codex exec``) instead of an HTTP API, so the
calls draw on the user's Claude or ChatGPT subscription rather than a
BYOK API key. They are the replacement for MCP *sampling* as the
"borrow the host's model access" path: sampling is deprecated as of MCP
2026-07-28 (SEP-2577) and Claude Code never implemented it.

Selection is explicit and prefix-based — never a credential fallback:

* ``claude-code:<model>`` → ``claude -p --model <model>`` (``claude-code:``
  alone uses the CLI's default model). ``<model>`` accepts anything the
  CLI does: ``haiku`` / ``sonnet`` / ``opus`` or a full model id.
* ``codex:<model>`` → ``codex exec -m <model>`` (``codex:`` alone uses the
  CLI's default model).

Each call runs the CLI as an isolated, tool-less completion: the agent
system prompt is replaced by the request's ``system``, tools / MCP
servers / user settings are disabled, sessions are not persisted, and
the process runs in an empty scratch directory so no project context
(CLAUDE.md, AGENTS.md) leaks in. Structured output maps onto the CLIs'
native JSON-schema flags, so forced single-tool requests (the
:class:`~althing.structured.output.StructuredOutputEngine` path) come
back as ordinary :class:`ToolInvocationBlock` responses.

Limitations, by construction:

* No sampling controls — ``temperature`` / ``top_p`` / ``seed`` /
  ``max_tokens`` are not exposed by the CLIs and are ignored (one-shot
  warning for the sampling params).
* Multi-turn history is flattened into one prompt; text only (image /
  document attachments are rejected).
* Only forced single-tool calls are supported (no free tool use).
* Throughput is bounded by subscription usage limits; usage-limit
  errors surface as ``RATE_LIMIT`` so the client's backoff applies.
* No per-call bill exists, so no provider cost is reported and cost
  figures are local pricing-table estimates (API list-price
  equivalents). ``--max-cost`` therefore acts as a usage budget.

Environment knobs:

* ``ALTHING_CLAUDE_CODE_BIN`` / ``ALTHING_CODEX_BIN`` — executable path.
* ``ALTHING_CLI_MAX_CONCURRENT`` — max in-flight CLI processes per
  provider (default 4).
* ``ALTHING_CLI_TIMEOUT`` — per-call timeout in seconds (default 300).
* ``ALTHING_CLAUDE_CODE_EFFORT`` / ``ALTHING_CODEX_EFFORT`` — optional
  reasoning-effort level passed through to the CLI.
* ``ALTHING_CLAUDE_CODE_ALLOW_API_KEY=1`` — keep ``ANTHROPIC_API_KEY``
  in the child environment. By default it is stripped, because
  ``claude`` prefers an API key over the subscription login when both
  are present, which would silently bill the key.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from althing.llm.errors import LLMError, LLMErrorCategory
from althing.llm.models import (
    CompletionRequest,
    CompletionResponse,
    InputMessage,
    StopReason,
    StreamEvent,
    StreamEventType,
    TextBlock,
    ThinkingBlock,
    TokenUsage,
    ToolChoiceKind,
    ToolDefinition,
    ToolInvocationBlock,
    ToolResultBlock,
)
from althing.llm.providers.base import LLMProvider, ProviderConfig

logger = logging.getLogger(__name__)

CLAUDE_CODE_PREFIX = "claude-code:"
CODEX_PREFIX = "codex:"
SUBSCRIPTION_CLI_PREFIXES: tuple[str, ...] = (CLAUDE_CODE_PREFIX, CODEX_PREFIX)

_MAX_CONCURRENT_ENV = "ALTHING_CLI_MAX_CONCURRENT"
_TIMEOUT_ENV = "ALTHING_CLI_TIMEOUT"
_DEFAULT_MAX_CONCURRENT = 4
_DEFAULT_TIMEOUT = 300.0
_ALLOW_API_KEY_ENV = "ALTHING_CLAUDE_CODE_ALLOW_API_KEY"

# A system prompt is always sent: omitting it would leave the CLI's own
# coding-agent prompt in place and contaminate persona responses.
_NEUTRAL_SYSTEM_PROMPT = "You are a helpful assistant."

_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "usage limit",
    "hit your limit",
    "limit reached",
    "too many requests",
    "429",
)
_AUTH_MARKERS = (
    "not logged in",
    "/login",
    "codex login",
    "invalid api key",
    "authentication",
    "unauthorized",
    "401",
)
_OVERLOADED_MARKERS = ("overloaded", "529", "500", "502", "503", "internal server error")


def is_subscription_cli_model(model: str | None) -> bool:
    """Return True when *model* routes to a subscription CLI provider."""
    return bool(model) and str(model).startswith(SUBSCRIPTION_CLI_PREFIXES)


@dataclass(frozen=True)
class CLIProviderConfig(ProviderConfig):
    """Provider config for a CLI-backed provider.

    There is no API key: authentication is whatever the CLI is signed in
    with. ``has_credentials`` is always False so the client's
    "first provider with credentials" fallback never picks a CLI provider
    implicitly — they are reachable only through their model prefix.
    """

    executable: str = ""
    executable_env: str = ""

    def has_credentials(self) -> bool:
        return False

    def resolve_executable(self) -> str:
        override = os.environ.get(self.executable_env, "").strip() if self.executable_env else ""
        found = shutil.which(override or self.executable)
        if not found:
            raise LLMError(
                f"{self.name} provider requires the `{override or self.executable}` CLI on PATH "
                f"(signed in to your subscription). Install it, or point {self.executable_env} at the "
                "executable.",
                LLMErrorCategory.MISSING_CREDENTIALS,
            )
        return found


CLAUDE_CODE_CONFIG = CLIProviderConfig(
    api_key_env="",
    base_url_env="",
    default_base_url="",
    model_prefixes=(CLAUDE_CODE_PREFIX,),
    name="Claude Code",
    executable="claude",
    executable_env="ALTHING_CLAUDE_CODE_BIN",
)

CODEX_CONFIG = CLIProviderConfig(
    api_key_env="",
    base_url_env="",
    default_base_url="",
    model_prefixes=(CODEX_PREFIX,),
    name="Codex",
    executable="codex",
    executable_env="ALTHING_CODEX_BIN",
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_scratch_lock = threading.Lock()
_scratch_root: Path | None = None
_semaphores: dict[str, threading.BoundedSemaphore] = {}
_sampling_warned: set[str] = set()


def _scratch_dir() -> Path:
    """Return an empty process-wide working directory for CLI calls."""
    global _scratch_root
    with _scratch_lock:
        if _scratch_root is None:
            _scratch_root = Path(tempfile.mkdtemp(prefix="althing-cli-"))
            atexit.register(shutil.rmtree, _scratch_root, True)
        return _scratch_root


def _semaphore_for(name: str) -> threading.BoundedSemaphore:
    with _scratch_lock:
        sem = _semaphores.get(name)
        if sem is None:
            try:
                limit = int(os.environ.get(_MAX_CONCURRENT_ENV, "") or _DEFAULT_MAX_CONCURRENT)
            except ValueError:
                limit = _DEFAULT_MAX_CONCURRENT
            sem = threading.BoundedSemaphore(max(1, limit))
            _semaphores[name] = sem
        return sem


def _api_key_allowed() -> bool:
    return os.environ.get(_ALLOW_API_KEY_ENV, "").strip().lower() in ("1", "true", "yes")


def _timeout() -> float:
    try:
        return float(os.environ.get(_TIMEOUT_ENV, "") or _DEFAULT_TIMEOUT)
    except ValueError:
        return _DEFAULT_TIMEOUT


def _warn_ignored_sampling_params(provider_name: str, request: CompletionRequest) -> None:
    if request.temperature is None and request.top_p is None:
        return
    with _scratch_lock:
        if provider_name in _sampling_warned:
            return
        _sampling_warned.add(provider_name)
    logger.warning(
        "%s CLI provider does not expose temperature/top_p; requested values are ignored "
        "and the CLI's default sampling applies.",
        provider_name,
    )


def cli_model_name(model: str, prefix: str) -> str | None:
    """Strip the routing prefix; ``None`` means "use the CLI's default model"."""
    rest = model[len(prefix) :].strip() if model.startswith(prefix) else model.strip()
    return rest or None


def _block_to_text(block: Any) -> str:
    if isinstance(block, TextBlock):
        return block.text
    if isinstance(block, ThinkingBlock):
        return ""
    if isinstance(block, ToolInvocationBlock):
        return f"[structured response via {block.name}: {json.dumps(block.input, ensure_ascii=False)}]"
    if isinstance(block, ToolResultBlock):
        body = "\n".join(b.text for b in block.content)
        return f"[{'error' if block.is_error else 'result'}: {body}]"
    if getattr(block, "type", None) == "html":
        return block.text
    raise LLMError(
        f"Subscription CLI providers accept text content only; got a {getattr(block, 'type', type(block).__name__)!r} "
        "block. Use an API-key provider for attachments.",
        LLMErrorCategory.BAD_REQUEST,
    )


def _message_text(message: InputMessage) -> str:
    return "\n".join(t for t in (_block_to_text(b) for b in message.content) if t)


def render_prompt(messages: list[InputMessage]) -> str:
    """Flatten a conversation into a single prompt for a one-shot CLI call.

    A lone user message passes through verbatim. Longer conversations are
    rendered as a tagged transcript followed by the final user turn, so
    persona sessions keep their earlier answers in view.
    """
    if not messages:
        raise LLMError("Request has no messages", LLMErrorCategory.BAD_REQUEST)
    *history, last = messages
    final = _message_text(last)
    if not history:
        return final
    lines = ["<conversation_history>"]
    for message in history:
        tag = "you" if message.role == "assistant" else "user"
        lines.append(f"<{tag}>\n{_message_text(message)}\n</{tag}>")
    lines.append("</conversation_history>")
    lines.append("")
    if last.role == "assistant":
        lines.append(f"Continue your previous reply, which so far reads:\n{final}")
    else:
        lines.append(final)
    return "\n".join(lines)


def forced_tool(request: CompletionRequest) -> ToolDefinition | None:
    """Return the single tool the request forces, or ``None`` for plain text.

    Only forced single-tool calls map onto the CLIs' JSON-schema mode;
    anything else would need real tool calling, which a one-shot headless
    run can't provide.
    """
    if not request.tools:
        return None
    choice = request.tool_choice
    if choice is not None and choice.kind == ToolChoiceKind.SPECIFIC:
        for tool in request.tools:
            if tool.name == choice.name:
                return tool
        raise LLMError(f"tool_choice names unknown tool {choice.name!r}", LLMErrorCategory.BAD_REQUEST)
    if choice is not None and choice.kind == ToolChoiceKind.ANY and len(request.tools) == 1:
        return request.tools[0]
    raise LLMError(
        "Subscription CLI providers support only a single forced tool (structured output); "
        "free tool use is not available. Use an API-key provider for tool-calling agents.",
        LLMErrorCategory.BAD_REQUEST,
    )


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *schema* in OpenAI strict structured-output form.

    Codex's ``--output-schema`` enforces OpenAI strict mode: every object
    needs ``additionalProperties: false`` and must list all properties as
    required. Optional properties become required-but-nullable so their
    meaning survives.
    """

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        out = {k: walk(v) for k, v in node.items()}
        if out.get("type") == "object" or "properties" in out:
            props: dict[str, Any] = out.get("properties", {})
            required = set(out.get("required", []))
            for name, sub in props.items():
                if name not in required and isinstance(sub, dict):
                    sub_type = sub.get("type")
                    if isinstance(sub_type, str):
                        props[name] = {**sub, "type": [sub_type, "null"]}
                    elif isinstance(sub_type, list) and "null" not in sub_type:
                        props[name] = {**sub, "type": [*sub_type, "null"]}
            out["properties"] = props
            out["required"] = list(props)
            out["additionalProperties"] = False
        return out

    return walk(schema)


def classify_cli_error(message: str, *, status: int | None = None) -> LLMErrorCategory:
    """Map CLI error text (and an optional HTTP status) to an error category."""
    if status is not None:
        if status in (401, 403):
            return LLMErrorCategory.AUTHENTICATION
        if status == 429:
            return LLMErrorCategory.RATE_LIMIT
        if status >= 500:
            return LLMErrorCategory.SERVER_ERROR
        if 400 <= status < 500:
            return LLMErrorCategory.BAD_REQUEST
    lower = message.lower()
    if any(m in lower for m in _RATE_LIMIT_MARKERS):
        return LLMErrorCategory.RATE_LIMIT
    if any(m in lower for m in _AUTH_MARKERS):
        return LLMErrorCategory.AUTHENTICATION
    if any(m in lower for m in _OVERLOADED_MARKERS):
        return LLMErrorCategory.SERVER_ERROR
    return LLMErrorCategory.SERVER_ERROR


def _run_cli(
    provider_name: str,
    argv: list[str],
    *,
    stdin: str,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    sem = _semaphore_for(provider_name)
    with sem:
        try:
            return subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=cwd,
                env=env,
                timeout=_timeout(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(
                f"{provider_name} CLI timed out after {exc.timeout:.0f}s (raise {_TIMEOUT_ENV} for long calls)",
                LLMErrorCategory.TRANSPORT,
                cause=exc,
            ) from exc
        except OSError as exc:
            raise LLMError(
                f"Failed to launch {provider_name} CLI: {exc}",
                LLMErrorCategory.TRANSPORT,
                cause=exc,
            ) from exc


def _structured_response(
    *,
    model: str,
    tool: ToolDefinition | None,
    data: Any,
    text: str,
    usage: TokenUsage,
    stop_reason: StopReason | None,
) -> CompletionResponse:
    """Build a response; a forced tool + parsed object becomes a tool call."""
    if tool is not None and isinstance(data, dict):
        return CompletionResponse(
            id=f"cli_{uuid.uuid4().hex}",
            model=model,
            content=[ToolInvocationBlock(id=f"toolu_cli_{uuid.uuid4().hex[:24]}", name=tool.name, input=data)],
            stop_reason=StopReason.TOOL_USE,
            usage=usage,
        )
    # No parseable object: return the text so the structured-output engine's
    # retry ladder sees a missing tool call and re-prompts.
    return CompletionResponse(
        id=f"cli_{uuid.uuid4().hex}",
        model=model,
        content=[TextBlock(text=text)] if text else [],
        stop_reason=stop_reason or StopReason.END_TURN,
        usage=usage,
    )


def _single_shot_stream(response: CompletionResponse) -> Iterator[StreamEvent]:
    """Replay a completed response as a minimal event stream."""
    yield StreamEvent(type=StreamEventType.MESSAGE_START, data={"id": response.id, "model": response.model})
    if response.text:
        yield StreamEvent(type=StreamEventType.CONTENT_BLOCK_DELTA, index=0, data={"text": response.text})
    stop = response.stop_reason.value if response.stop_reason else None
    yield StreamEvent(type=StreamEventType.MESSAGE_DELTA, data={"stop_reason": stop})
    yield StreamEvent(type=StreamEventType.MESSAGE_STOP, data={})


def _try_parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
    return None


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------


class ClaudeCodeProvider(LLMProvider):
    """Completions via ``claude -p`` on the user's Claude subscription."""

    config = CLAUDE_CODE_CONFIG

    def __init__(self) -> None:
        self._exe = self.config.resolve_executable()

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if not _api_key_allowed():
            env.pop("ANTHROPIC_API_KEY", None)
        return env

    def send(self, request: CompletionRequest) -> CompletionResponse:
        _warn_ignored_sampling_params(self.config.name, request)
        tool = forced_tool(request)
        prompt = render_prompt(request.messages)
        with tempfile.TemporaryDirectory(dir=_scratch_dir()) as tmp:
            system_file = Path(tmp) / "system.txt"
            system_file.write_text(request.system or _NEUTRAL_SYSTEM_PROMPT, encoding="utf-8")
            argv = [
                self._exe,
                "-p",
                "--output-format",
                "json",
                "--system-prompt-file",
                str(system_file),
                "--tools",
                "",
                "--setting-sources",
                "",
                "--strict-mcp-config",
                "--no-session-persistence",
            ]
            model = cli_model_name(request.model, CLAUDE_CODE_PREFIX)
            if model:
                argv += ["--model", model]
            effort = os.environ.get("ALTHING_CLAUDE_CODE_EFFORT", "").strip()
            if effort:
                argv += ["--effort", effort]
            if tool is not None:
                argv += ["--json-schema", json.dumps(tool.input_schema)]
            proc = _run_cli(self.config.name, argv, stdin=prompt, cwd=Path(tmp), env=self._env())
        return self._parse(proc, request, tool)

    def _parse(
        self,
        proc: subprocess.CompletedProcess[str],
        request: CompletionRequest,
        tool: ToolDefinition | None,
    ) -> CompletionResponse:
        data = _try_parse_json(proc.stdout or "")
        if not isinstance(data, dict):
            detail = (proc.stderr or proc.stdout or "").strip()[-500:]
            raise LLMError(
                f"Claude Code CLI exited {proc.returncode} without a JSON result: {detail}",
                classify_cli_error(detail),
            )
        if data.get("is_error") or data.get("subtype") not in (None, "success"):
            detail = str(data.get("result") or data.get("subtype") or "unknown error")
            status = data.get("api_error_status")
            raise LLMError(
                f"Claude Code CLI error: {detail[:500]}",
                classify_cli_error(detail, status=status if isinstance(status, int) else None),
                status_code=status if isinstance(status, int) else None,
            )

        raw_usage = data.get("usage") or {}
        usage = TokenUsage(
            input_tokens=int(raw_usage.get("input_tokens") or 0),
            output_tokens=int(raw_usage.get("output_tokens") or 0),
            cache_write_tokens=int(raw_usage.get("cache_creation_input_tokens") or 0),
            cache_read_tokens=int(raw_usage.get("cache_read_input_tokens") or 0),
            reasoning_tokens=int((raw_usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0),
            # Subscription calls have no per-call bill, so cost falls back to
            # the local pricing-table estimate; with an API key allowed
            # through, the CLI's own cost figure is what was billed.
            provider_reported_cost=data.get("total_cost_usd") if _api_key_allowed() else None,
        )
        model_usage = data.get("modelUsage") or {}
        served = next(iter(model_usage), None)
        model = f"{CLAUDE_CODE_PREFIX}{served}" if served else request.model
        stop = StopReason.MAX_TOKENS if data.get("stop_reason") == "max_tokens" else StopReason.END_TURN
        result_text = str(data.get("result") or "")
        structured = data.get("structured_output")
        if tool is not None and structured is None:
            structured = _try_parse_json(result_text)
        return _structured_response(
            model=model, tool=tool, data=structured, text=result_text, usage=usage, stop_reason=stop
        )

    def stream(self, request: CompletionRequest) -> Iterator[StreamEvent]:
        yield from _single_shot_stream(self.send(request))


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

# Agent features switched off so a call behaves like a plain completion.
# Passed as ``-c features.<name>=false`` (unknown names are ignored, unlike
# ``--disable``, which errors on features a given codex version lacks).
_CODEX_DISABLED_FEATURES = (
    "apps",
    "plugins",
    "multi_agent",
    "shell_tool",
    "unified_exec",
    "goals",
    "hooks",
    "tool_suggest",
)


class CodexProvider(LLMProvider):
    """Completions via ``codex exec`` on the user's ChatGPT subscription."""

    config = CODEX_CONFIG

    def __init__(self) -> None:
        self._exe = self.config.resolve_executable()

    def send(self, request: CompletionRequest) -> CompletionResponse:
        _warn_ignored_sampling_params(self.config.name, request)
        tool = forced_tool(request)
        prompt = render_prompt(request.messages)
        with tempfile.TemporaryDirectory(dir=_scratch_dir()) as tmp:
            tmp_path = Path(tmp)
            instructions = tmp_path / "instructions.md"
            instructions.write_text(request.system or _NEUTRAL_SYSTEM_PROMPT, encoding="utf-8")
            last_message = tmp_path / "last_message.txt"
            argv = [
                self._exe,
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--sandbox",
                "read-only",
                "--cd",
                str(tmp_path),
                "--json",
                "--output-last-message",
                str(last_message),
                "-c",
                f"model_instructions_file={json.dumps(instructions.as_posix())}",
                "-c",
                "mcp_servers={}",
                "-c",
                "tools.web_search=false",
            ]
            for feature in _CODEX_DISABLED_FEATURES:
                argv += ["-c", f"features.{feature}=false"]
            effort = os.environ.get("ALTHING_CODEX_EFFORT", "").strip()
            if effort:
                argv += ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
            model = cli_model_name(request.model, CODEX_PREFIX)
            if model:
                argv += ["--model", model]
            if tool is not None:
                schema_file = tmp_path / "schema.json"
                schema_file.write_text(json.dumps(strict_schema(tool.input_schema)), encoding="utf-8")
                argv += ["--output-schema", str(schema_file)]
            argv.append("-")
            proc = _run_cli(self.config.name, argv, stdin=prompt, cwd=tmp_path)
            final_text = last_message.read_text(encoding="utf-8") if last_message.is_file() else ""
        return self._parse(proc, final_text, request, tool)

    def _parse(
        self,
        proc: subprocess.CompletedProcess[str],
        final_text: str,
        request: CompletionRequest,
        tool: ToolDefinition | None,
    ) -> CompletionResponse:
        usage = TokenUsage()
        messages: list[str] = []
        errors: list[str] = []
        for line in (proc.stdout or "").splitlines():
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            kind = event.get("type")
            if kind == "turn.completed":
                raw = event.get("usage") or {}
                usage = usage + TokenUsage(
                    input_tokens=int(raw.get("input_tokens") or 0),
                    output_tokens=int(raw.get("output_tokens") or 0),
                    cached_tokens=int(raw.get("cached_input_tokens") or 0),
                    reasoning_tokens=int(raw.get("reasoning_output_tokens") or 0),
                )
            elif kind == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    messages.append(str(item["text"]))
            elif kind in ("error", "turn.failed"):
                err = event.get("error") if isinstance(event.get("error"), dict) else {}
                errors.append(str(event.get("message") or err.get("message") or event))

        text = final_text.strip() or (messages[-1] if messages else "")
        if not text:
            detail = "; ".join(errors) or (proc.stderr or "").strip()[-500:] or f"exit code {proc.returncode}"
            raise LLMError(f"Codex CLI produced no response: {detail[:500]}", classify_cli_error(detail))

        structured = _try_parse_json(text) if tool is not None else None
        if tool is not None and isinstance(structured, dict):
            # Undo strict_schema's required-but-nullable rewrite for fields the
            # caller's schema left optional.
            required = set(tool.input_schema.get("required", []))
            structured = {k: v for k, v in structured.items() if v is not None or k in required}
        return _structured_response(
            model=request.model, tool=tool, data=structured, text=text, usage=usage, stop_reason=None
        )

    def stream(self, request: CompletionRequest) -> Iterator[StreamEvent]:
        yield from _single_shot_stream(self.send(request))
