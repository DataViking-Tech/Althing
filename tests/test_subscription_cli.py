"""Tests for the subscription CLI providers (claude-code:* / codex:*)."""

from __future__ import annotations

import json
import subprocess
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from althing.llm.client import LLMClient
from althing.llm.errors import LLMError, LLMErrorCategory
from althing.llm.models import (
    CompletionRequest,
    InputMessage,
    StopReason,
    TextBlock,
    ToolChoice,
    ToolDefinition,
    ToolInvocationBlock,
)
from althing.llm.providers import subscription_cli as cli
from althing.llm.providers.subscription_cli import (
    ClaudeCodeProvider,
    CodexProvider,
    render_prompt,
    strict_schema,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "choice": {"type": "string", "enum": ["A", "B"]},
        "note": {"type": "string"},
    },
    "required": ["choice"],
}
RESPOND = ToolDefinition(name="respond", input_schema=SCHEMA)


def _request(model: str, *, tool: bool = False, system: str | None = "You are Maria.") -> CompletionRequest:
    return CompletionRequest(
        model=model,
        max_tokens=256,
        system=system,
        messages=[InputMessage(role="user", content=[TextBlock(text="Pick one: (A) or (B)")])],
        tools=[RESPOND] if tool else None,
        tool_choice=ToolChoice.specific("respond") if tool else None,
    )


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _claude_result(**overrides) -> str:
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Hello there",
        "stop_reason": "end_turn",
        "total_cost_usd": 0.0123,
        "api_error_status": None,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 40,
            "output_tokens_details": {"thinking_tokens": 5},
        },
        "modelUsage": {"claude-haiku-5-5": {"inputTokens": 10}},
    }
    payload.update(overrides)
    return json.dumps(payload)


@pytest.fixture(autouse=True)
def _fake_executables():
    with patch.object(cli.shutil, "which", side_effect=lambda name: f"/usr/bin/{Path(name).name}"):
        yield


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class TestRouting:
    def test_claude_code_prefix_routes_before_anthropic(self):
        provider = LLMClient()._resolve_provider("claude-code:haiku")
        assert isinstance(provider, ClaudeCodeProvider)

    def test_codex_prefix_routes_to_codex(self):
        provider = LLMClient()._resolve_provider("codex:")
        assert isinstance(provider, CodexProvider)

    def test_plain_claude_model_still_routes_to_anthropic(self, monkeypatch):
        from althing.llm.providers.anthropic import AnthropicProvider

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        provider = LLMClient()._resolve_provider("claude-sonnet-5")
        assert isinstance(provider, AnthropicProvider)

    def test_cli_providers_never_used_as_credential_fallback(self):
        assert cli.CLAUDE_CODE_CONFIG.has_credentials() is False
        assert cli.CODEX_CONFIG.has_credentials() is False

    def test_missing_executable_is_missing_credentials(self):
        with patch.object(cli.shutil, "which", return_value=None), pytest.raises(LLMError) as exc:
            ClaudeCodeProvider()
        assert exc.value.category == LLMErrorCategory.MISSING_CREDENTIALS
        assert "claude" in str(exc.value)

    def test_is_subscription_cli_model(self):
        assert cli.is_subscription_cli_model("claude-code:sonnet")
        assert cli.is_subscription_cli_model("codex:")
        assert not cli.is_subscription_cli_model("claude-sonnet-5")
        assert not cli.is_subscription_cli_model(None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestRenderPrompt:
    def test_single_message_passes_through(self):
        msgs = [InputMessage(role="user", content=[TextBlock(text="Hi")])]
        assert render_prompt(msgs) == "Hi"

    def test_history_is_rendered_as_transcript(self):
        msgs = [
            InputMessage(role="user", content=[TextBlock(text="Q1")]),
            InputMessage(role="assistant", content=[TextBlock(text="A1")]),
            InputMessage(role="user", content=[TextBlock(text="Q2")]),
        ]
        out = render_prompt(msgs)
        assert "<user>\nQ1\n</user>" in out
        assert "<you>\nA1\n</you>" in out
        assert out.endswith("Q2")

    def test_tool_calls_in_history_render_as_text(self):
        msgs = [
            InputMessage(role="user", content=[TextBlock(text="Q1")]),
            InputMessage(
                role="assistant",
                content=[ToolInvocationBlock(id="t1", name="respond", input={"choice": "A"})],
            ),
            InputMessage(role="user", content=[TextBlock(text="Try again")]),
        ]
        assert '"choice": "A"' in render_prompt(msgs)

    def test_attachments_rejected(self):
        from althing.llm.models import ImageBlock, InlineSource

        msgs = [InputMessage(role="user", content=[ImageBlock(source=InlineSource(data="xx"))])]
        with pytest.raises(LLMError) as exc:
            render_prompt(msgs)
        assert exc.value.category == LLMErrorCategory.BAD_REQUEST


class TestForcedTool:
    def test_unforced_tools_rejected(self):
        req = _request("claude-code:haiku")
        req.tools = [RESPOND]
        req.tool_choice = ToolChoice.auto()
        with pytest.raises(LLMError) as exc:
            cli.forced_tool(req)
        assert exc.value.category == LLMErrorCategory.BAD_REQUEST

    def test_any_with_single_tool_is_forced(self):
        req = _request("claude-code:haiku")
        req.tools = [RESPOND]
        req.tool_choice = ToolChoice.any()
        assert cli.forced_tool(req) is RESPOND


class TestStrictSchema:
    def test_optional_fields_become_required_nullable(self):
        out = strict_schema(SCHEMA)
        assert out["additionalProperties"] is False
        assert out["required"] == ["choice", "note"]
        assert out["properties"]["note"]["type"] == ["string", "null"]
        assert out["properties"]["choice"]["type"] == "string"
        assert SCHEMA["required"] == ["choice"]  # input not mutated

    def test_nested_objects_are_strict(self):
        schema = {
            "type": "object",
            "properties": {"inner": {"type": "object", "properties": {"x": {"type": "integer"}}}},
        }
        out = strict_schema(schema)
        assert out["properties"]["inner"]["type"] == ["object", "null"]
        assert out["properties"]["inner"]["additionalProperties"] is False
        assert out["properties"]["inner"]["required"] == ["x"]


@pytest.mark.parametrize(
    ("message", "status", "expected"),
    [
        ("Claude AI usage limit reached|1760000000", None, LLMErrorCategory.RATE_LIMIT),
        ("You've hit your limit · resets 3pm", None, LLMErrorCategory.RATE_LIMIT),
        ("Not logged in · Please run /login", None, LLMErrorCategory.AUTHENTICATION),
        ("API Error: 529 Overloaded", None, LLMErrorCategory.SERVER_ERROR),
        ("anything", 429, LLMErrorCategory.RATE_LIMIT),
        ("anything", 400, LLMErrorCategory.BAD_REQUEST),
    ],
)
def test_classify_cli_error(message, status, expected):
    assert cli.classify_cli_error(message, status=status) == expected


# ---------------------------------------------------------------------------
# Claude Code provider
# ---------------------------------------------------------------------------


class TestClaudeCodeProvider:
    def test_plain_text_call(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")
        run = MagicMock(return_value=_completed(_claude_result()))
        with patch.object(cli.subprocess, "run", run):
            resp = ClaudeCodeProvider().send(_request("claude-code:haiku"))

        argv = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        assert argv[:2] == ["/usr/bin/claude", "-p"]
        assert argv[argv.index("--tools") + 1] == ""
        assert argv[argv.index("--setting-sources") + 1] == ""
        assert argv[argv.index("--model") + 1] == "haiku"
        assert "--strict-mcp-config" in argv
        assert "--no-session-persistence" in argv
        assert "--json-schema" not in argv
        assert kwargs["input"] == "Pick one: (A) or (B)"
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]

        assert resp.text == "Hello there"
        assert resp.model == "claude-code:claude-haiku-5-5"
        assert resp.stop_reason == StopReason.END_TURN
        assert resp.usage.input_tokens == 10
        assert resp.usage.output_tokens == 20
        assert resp.usage.cache_write_tokens == 30
        assert resp.usage.cache_read_tokens == 40
        assert resp.usage.reasoning_tokens == 5
        assert resp.usage.provider_reported_cost is None

    def test_system_prompt_replaces_agent_prompt(self):
        seen: dict[str, str] = {}

        def fake_run(argv, **kwargs):
            seen["system"] = Path(argv[argv.index("--system-prompt-file") + 1]).read_text(encoding="utf-8")
            return _completed(_claude_result())

        with patch.object(cli.subprocess, "run", side_effect=fake_run):
            ClaudeCodeProvider().send(_request("claude-code:", system=None))
        assert seen["system"] == cli._NEUTRAL_SYSTEM_PROMPT

    def test_default_model_omits_model_flag(self):
        run = MagicMock(return_value=_completed(_claude_result()))
        with patch.object(cli.subprocess, "run", run):
            ClaudeCodeProvider().send(_request("claude-code:"))
        assert "--model" not in run.call_args.args[0]

    def test_api_key_kept_when_allowed(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-keep")
        monkeypatch.setenv("ALTHING_CLAUDE_CODE_ALLOW_API_KEY", "1")
        run = MagicMock(return_value=_completed(_claude_result()))
        with patch.object(cli.subprocess, "run", run):
            resp = ClaudeCodeProvider().send(_request("claude-code:haiku"))
        assert run.call_args.kwargs["env"]["ANTHROPIC_API_KEY"] == "sk-keep"
        assert resp.usage.provider_reported_cost == Decimal("0.0123")

    def test_structured_output_becomes_tool_call(self):
        run = MagicMock(
            return_value=_completed(_claude_result(result='{"choice":"A"}', structured_output={"choice": "A"}))
        )
        with patch.object(cli.subprocess, "run", run):
            resp = ClaudeCodeProvider().send(_request("claude-code:haiku", tool=True))

        argv = run.call_args.args[0]
        assert json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMA
        assert resp.stop_reason == StopReason.TOOL_USE
        assert resp.tool_calls[0].name == "respond"
        assert resp.tool_calls[0].input == {"choice": "A"}

    def test_missing_structured_output_returns_text_for_retry(self):
        run = MagicMock(return_value=_completed(_claude_result(result="I refuse to pick")))
        with patch.object(cli.subprocess, "run", run):
            resp = ClaudeCodeProvider().send(_request("claude-code:haiku", tool=True))
        assert resp.tool_calls == []
        assert resp.text == "I refuse to pick"

    def test_usage_limit_error_is_rate_limit(self):
        out = _claude_result(is_error=True, result="Claude AI usage limit reached|1760000000")
        with (
            patch.object(cli.subprocess, "run", return_value=_completed(out, returncode=1)),
            pytest.raises(LLMError) as exc,
        ):
            ClaudeCodeProvider().send(_request("claude-code:haiku"))
        assert exc.value.category == LLMErrorCategory.RATE_LIMIT
        assert exc.value.retryable

    def test_non_json_output_is_classified(self):
        with (
            patch.object(cli.subprocess, "run", return_value=_completed("", "Not logged in", returncode=1)),
            pytest.raises(LLMError) as exc,
        ):
            ClaudeCodeProvider().send(_request("claude-code:haiku"))
        assert exc.value.category == LLMErrorCategory.AUTHENTICATION

    def test_timeout_is_transport_error(self):
        with (
            patch.object(cli.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=5)),
            pytest.raises(LLMError) as exc,
        ):
            ClaudeCodeProvider().send(_request("claude-code:haiku"))
        assert exc.value.category == LLMErrorCategory.TRANSPORT

    def test_stream_replays_single_response(self):
        from althing.llm.models import StreamEventType

        with patch.object(cli.subprocess, "run", return_value=_completed(_claude_result())):
            events = list(ClaudeCodeProvider().stream(_request("claude-code:haiku")))
        assert [e.type for e in events] == [
            StreamEventType.MESSAGE_START,
            StreamEventType.CONTENT_BLOCK_DELTA,
            StreamEventType.MESSAGE_DELTA,
            StreamEventType.MESSAGE_STOP,
        ]
        assert events[1].data == {"text": "Hello there"}


# ---------------------------------------------------------------------------
# Codex provider
# ---------------------------------------------------------------------------


def _codex_run(final_text: str, *, events: list[dict] | None = None, returncode: int = 0, seen: dict | None = None):
    """Build a fake subprocess.run that writes codex's last-message file."""
    default_events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": final_text}},
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 7, "reasoning_output_tokens": 2},
        },
    ]

    def fake_run(argv, **kwargs):
        if seen is not None:
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            if "--output-schema" in argv:
                schema_path = Path(argv[argv.index("--output-schema") + 1])
                seen["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
        if final_text:
            Path(argv[argv.index("--output-last-message") + 1]).write_text(final_text, encoding="utf-8")
        stdout = "\n".join(json.dumps(e) for e in (events if events is not None else default_events))
        return _completed(stdout, returncode=returncode)

    return fake_run


class TestCodexProvider:
    def test_plain_text_call(self):
        seen: dict = {}
        with patch.object(cli.subprocess, "run", side_effect=_codex_run("Hi, I'm Maria.", seen=seen)):
            resp = CodexProvider().send(_request("codex:gpt-5.5"))

        argv = seen["argv"]
        assert argv[:2] == ["/usr/bin/codex", "exec"]
        for flag in ("--ephemeral", "--ignore-user-config", "--skip-git-repo-check", "--json"):
            assert flag in argv
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert argv[argv.index("--model") + 1] == "gpt-5.5"
        assert "features.shell_tool=false" in argv
        assert any(a.startswith("model_instructions_file=") for a in argv)
        assert argv[-1] == "-"
        assert seen["kwargs"]["input"] == "Pick one: (A) or (B)"

        assert resp.text == "Hi, I'm Maria."
        assert resp.model == "codex:gpt-5.5"
        assert resp.usage.input_tokens == 100
        assert resp.usage.cached_tokens == 60
        assert resp.usage.output_tokens == 7
        assert resp.usage.reasoning_tokens == 2
        assert resp.usage.provider_reported_cost is None

    def test_structured_output_uses_strict_schema_and_strips_nulls(self):
        seen: dict = {}
        fake = _codex_run('{"choice":"B","note":null}', seen=seen)
        with patch.object(cli.subprocess, "run", side_effect=fake):
            resp = CodexProvider().send(_request("codex:", tool=True))

        assert seen["schema"]["additionalProperties"] is False
        assert "--model" not in seen["argv"]
        assert resp.stop_reason == StopReason.TOOL_USE
        assert resp.tool_calls[0].input == {"choice": "B"}

    def test_failed_turn_without_message_raises(self):
        events = [{"type": "turn.failed", "error": {"message": "You've hit your usage limit."}}]
        with (
            patch.object(cli.subprocess, "run", side_effect=_codex_run("", events=events, returncode=1)),
            pytest.raises(LLMError) as exc,
        ):
            CodexProvider().send(_request("codex:"))
        assert exc.value.category == LLMErrorCategory.RATE_LIMIT


# ---------------------------------------------------------------------------
# Integration with the structured-output engine and MCP mode selection
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_structured_engine_round_trip(self):
        from althing.structured.output import StructuredOutputConfig, StructuredOutputEngine

        out = _claude_result(result='{"choice":"A"}', structured_output={"choice": "A"})
        with patch.object(cli.subprocess, "run", return_value=_completed(out)):
            result = StructuredOutputEngine(LLMClient()).extract(
                model="claude-code:haiku",
                max_tokens=256,
                messages=[InputMessage(role="user", content=[TextBlock(text="Pick")])],
                config=StructuredOutputConfig(schema=SCHEMA),
            )
        assert result.data == {"choice": "A"}
        assert not result.is_fallback

    def test_escalation_stays_on_cli(self):
        from althing.structured.output import _escalation_model_for

        assert _escalation_model_for("claude-code:haiku") == "claude-code:sonnet"
        assert _escalation_model_for("claude-code:sonnet") is None
        assert _escalation_model_for("codex:gpt-5-mini") is None
        assert _escalation_model_for("claude-haiku-4-5-20251001") == "sonnet"

    def test_decide_mode_cli_model_needs_no_key_or_sampling(self):
        from althing.mcp.sampling import decide_mode

        ctx = MagicMock()
        ctx.session.check_client_capability.return_value = False
        assert decide_mode(ctx, env={}).mode == "error"
        assert decide_mode(ctx, env={}, model="claude-code:haiku").mode == "byok"
        assert decide_mode(ctx, env={}, model="codex:").mode == "byok"

    def test_decide_mode_explicit_sampling_still_wins(self):
        from althing.mcp.sampling import decide_mode

        ctx = MagicMock()
        ctx.session.check_client_capability.return_value = True
        assert decide_mode(ctx, env={}, model="claude-code:haiku", use_sampling=True).mode == "sampling"
