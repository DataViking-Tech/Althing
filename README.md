# althing

[![PyPI](https://img.shields.io/pypi/v/althing.svg)](https://pypi.org/project/althing/)
[![CI](https://github.com/DataViking-Tech/Althing/actions/workflows/ci.yml/badge.svg)](https://github.com/DataViking-Tech/Althing/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python versions](https://img.shields.io/pypi/pyversions/althing.svg)](https://pypi.org/project/althing/)
[![MCP](https://img.shields.io/badge/MCP-enabled-brightgreen.svg)](https://github.com/DataViking-Tech/Althing/blob/main/docs/mcp.md)
[![GHCR](https://img.shields.io/badge/ghcr.io-dataviking--tech%2Falthing-2496ED?logo=docker&logoColor=white)](https://github.com/DataViking-Tech/Althing/pkgs/container/althing)
[![Ko-fi](https://img.shields.io/badge/Support-Ko--fi-FF5E5B?logo=ko-fi)](https://ko-fi.com/dataviking)

Site: <https://althing.dev> · Benchmark: <https://synthbench.org>

**Althing is the synthetic-population MCP server for AI agents.**

When your agent needs to know what a representative slice of humans would say
about a decision — pricing, naming, friction points, copy — it makes one tool
call:

```jsonc
// MCP tool call
{
  "tool": "run_panel",
  "arguments": {
    "pack_id": "general-consumer",
    "questions": [{ "text": "Would you pay $49 or $79 for this launch tier?" }],
    "decision_being_informed": "choosing launch tier price"
  }
}

// Response (same envelope from both `run_panel` and `panel run --output-format json`)
{
  "result_id": "result-20260510-abc123",
  "model": "claude-haiku-4-5",
  "synthesis": {
    "summary": "Cohort splits on $79; price is the dominant objection.",
    "themes": [...],
    "agreements": [...],
    "disagreements": [...],
    "surprises": [...],
    "recommendation": "..."
  },
  "rounds": [{ "name": "default", "results": [...], "synthesis": null }],
  "path": [],
  "warnings": [],
  "total_cost": "$0.0142",
  "total_usage": { "input_tokens": 4231, "output_tokens": 1102, ... },
  ...
}
```

You get a structured `synthesis` block — themes, agreements, disagreements,
surprises, and a single recommendation line your agent can act on — plus the
full per-panelist transcript under `rounds[].results[]` and per-turn cost
telemetry. **Bring your own LLM key — Claude, OpenAI, Gemini, or local.**
Drops into Claude Code, Cursor, Windsurf, LangChain, CrewAI, OpenAI Agents
SDK.

> **Note (v1.0.6):** the v1.0.0 `panel_verdict` artifact (`headline`,
> `convergence`, `dissent_count`, `flags[]`, `schema_version`) defined in
> [`schemas/v1.0.0.json`](src/althing/schemas/v1.0.0.json) is now **emitted
> on the success path** of the MCP panel tools (`run_panel`, `run_quick_poll`,
> `extend_panel` in BYOK mode): it rides under the envelope's `panel_verdict`
> key alongside the `synthesis` block above, and the response gate validates it
> on egress. Sampling-mode and ensemble runs don't persist a transcript and
> carry no verdict; the CLI `panel run` envelope is unchanged — see
> [docs/response-contract.md](docs/response-contract.md). Error responses use
> the typed envelope (`error_code`, `schema_version`, `retry_safe`).

```bash
pip install althing
```

**Frozen contract:** the v1.0.0 schema lives in the package at
[`althing/schemas/v1.0.0.json`](src/althing/schemas/v1.0.0.json) and is
echoed on every persisted-panel success envelope and every typed error
(`schema_version: "1.0.0"`). Field-by-field reference:
[docs/response-contract.md](docs/response-contract.md). Migrating from v0.12?
[docs/migration-v1.md](docs/migration-v1.md). Methodology and inspectability:
[docs/methodology.md](docs/methodology.md).

**Zero-config inside any MCP host that speaks sampling** (Claude Desktop,
Claude Code, Cursor, Windsurf) — drop the config in and run `run_prompt` and
small `run_quick_poll` calls (up to 3 personas) with **no API key set**. The
host runs the model on your behalf, using its own subscription. Full panels,
ensembles, and reproducible model pins need your own provider key. Personas and instruments are plain YAML; every
response is schema-validated with per-turn cost telemetry.

## Why

Traditional focus groups cost $5,000-$15,000 and take weeks. Synthetic panels cost pennies and take seconds. They don't replace real user research, but they're excellent for:

- **Pre-screening** survey instruments before spending budget on real participants
- **Rapid iteration** on product names, copy, and positioning
- **Hypothesis generation** across demographic segments
- **Concept testing** at the speed of thought

## Agent Quick Start

> **New here?** [**docs/agent-quickstart.md**](docs/agent-quickstart.md) is the
> full end-to-end walkthrough — install → verify → dry-run → run → 30–40 persona
> poll → save → emit JSON, on both the CLI and MCP surfaces, with a
> structured-output example. The snippets below are the short version.

Wire the MCP server into your editor (see [Use with Claude Code / Cursor /
Windsurf / Zed](#use-with-claude-code--cursor--windsurf--zed) below) and call
the four research tools from agent code. Every panel-running call requires a
`decision_being_informed` field (12–280 chars, single line) — the panel won't
run without one.

```jsonc
// run_panel — full synthetic focus group
{
  "tool": "run_panel",
  "arguments": {
    "pack_id": "general-consumer",
    "instrument_pack": "pricing-discovery",
    "decision_being_informed": "choosing launch tier price"
  }
}

// run_quick_poll — one question across personas
{
  "tool": "run_quick_poll",
  "arguments": {
    "question": "Which name feels most premium: Core, Plus, or Pro?",
    "pack_id": "general-consumer",
    "decision_being_informed": "naming the paid tier"
  }
}

// extend_panel — append an ad-hoc follow-up round
{
  "tool": "extend_panel",
  "arguments": {
    "result_id": "result-20260503-abc123",
    "questions": ["What would you pay for this if it shipped tomorrow?"],
    "decision_being_informed": "validating the indie pricing ceiling"
  }
}
```

Read `synthesis.recommendation` for the headline call, `synthesis.disagreements`
for dissent, and `rounds[].results[]` for the per-panelist transcript. See
[docs/response-contract.md](docs/response-contract.md) for the full v1.0.0
envelope (as of v1.0.6, `panel_verdict` — `headline`, `convergence`,
`dissent_count`, `flags[]` — is emitted on the success path of the MCP panel
tools; see the note in the lede for the sampling-mode/ensemble exceptions).

**Structured polling is the agent default.** For pick-one, Likert,
confidence, and tagged-themes questions, pass a bounded
`response_schema` so panelists return parsed JSON, not prose. No regex,
no post-hoc parsing. See [docs/structured-polling.md](docs/structured-polling.md)
for the full pattern catalogue and a runnable 35-persona prioritization
example.

## Human Operator Quick Start

Prefer a terminal? Same engine, CLI surface. Pick the install path that
matches how you use Python tools:

| Path | When | Command |
|------|------|---------|
| **pip** (in your project venv) | You're integrating Althing as a library | `pip install althing` |
| **pip + MCP** (in your project venv) | You also want the MCP server for agent integration | `pip install 'althing[mcp]'` |
| **pipx** (global, isolated) | You want `althing` on your PATH without polluting any project | `pipx install althing` |
| **uvx** (zero-install) | You just want to run it once — no install at all | `uvx --from althing althing --help` |
| **source** (latest unreleased) | You want main-branch fixes ahead of the next PyPI cut | `pip install git+https://github.com/DataViking-Tech/Althing.git@main` |

After installing, verify the CLI is on your PATH and the runtime is sane
*before* configuring providers:

```bash
althing --version              # smoke: package metadata + entry point dispatch
althing doctor --install-only  # install health only — no credentials needed
althing doctor                 # full preflight (install + credentials)
althing whoami                 # which providers (if any) have credentials
```

### Package vs. module name

The PyPI distribution and CLI entry point are spelled **`althing`** (one
word). The importable Python module — the historical PEP 8 spelling — is
**`althing`** (two words, snake_case):

```python
import althing                       # canonical
from althing import run_panel, sdk   # library use
```

```bash
althing --version          # CLI
python -m althing --version  # canonical module form
python -m althing --version   # one-word alias also works (sy-het)
```

Both spellings resolve to the same code. The one-word `althing` module
is a thin shim (`__path__` redirect + ``__main__.py``) shipped so agents
that guess `python -m <pypi-name>` don't hit a wall. New code should still
prefer `import althing` — it's what `__all__`, the docs, and the
schemas refer to.

`doctor` exits non-zero with actionable guidance when something's
missing (no provider configured, wrong Python, MCP extra absent, etc.) —
it's the canonical "did the install land cleanly?" check, and
`clean-install-smoke` in CI runs the same sequence against the built
wheel on every push, so all three commands are part of the supported
contract.

Use `althing doctor --install-only` immediately after `pip install
althing` to validate the package, dependencies, and bundled packs
without provisioning a provider key — exit 0 in that mode means the
install is healthy, even when credentials are not yet configured. The
JSON output (`--output-format json doctor --install-only`) separates
`install_ok`, `credential_configured`, and `checks_ok` so agents and CI
can branch on each surface independently.

Then provide an API key (Claude, OpenAI, Gemini, xAI, or any
OpenAI-compatible provider) — either export it in your shell or persist
it once via `althing login`:

```bash
export ANTHROPIC_API_KEY="sk-..."
# or
althing login --provider anthropic --api-key sk-...    # stored at
# ~/.config/althing/credentials.json, mode 0600
althing whoami

# Run a single prompt
althing prompt "What do you think of the name Traitprint for a career app?"

# Run a full panel
althing panel run \
  --personas examples/personas.yaml \
  --instrument examples/survey.yaml
```

## Works with

Althing is **MCP-native** — it ships an MCP server, and every major
agent framework now supports MCP as a first-class tool source. That
means Althing works out of the box with any framework that speaks
MCP, with zero framework-specific wrapper packages to install. Runnable
examples for each framework live in
[`examples/integrations/`](examples/integrations/README.md).

| Framework | Example | Bridge | One-line install |
|-----------|---------|--------|------------------|
| [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/mcp/) | [openai_agents.py](examples/integrations/openai_agents.py) | Built-in `MCPServerStdio` | `pip install openai-agents althing[mcp]` |
| [LlamaIndex](https://docs.llamaindex.ai/) | [llamaindex_tool.py](examples/integrations/llamaindex_tool.py) | `llama-index-tools-mcp` | `pip install llama-index-tools-mcp llama-index-llms-anthropic althing[mcp]` |
| [CrewAI](https://docs.crewai.com/) | [crewai_tool.py](examples/integrations/crewai_tool.py) | `crewai-tools[mcp]` | `pip install "crewai-tools[mcp]" crewai althing[mcp]` |
| [LangChain](https://python.langchain.com/) | [langchain_tool.py](examples/integrations/langchain_tool.py) | `langchain-mcp-adapters` | `pip install langchain-mcp-adapters langchain-anthropic althing[mcp]` |
| [LangGraph](https://langchain-ai.github.io/langgraph/) | [langchain_tool.py](examples/integrations/langchain_tool.py) | `langchain-mcp-adapters` | `pip install langchain-mcp-adapters langgraph langchain-anthropic althing[mcp]` |
| [Microsoft Agent Framework 1.0](https://learn.microsoft.com/en-us/agent-framework/) | [microsoft_agent.py](examples/integrations/microsoft_agent.py) | Built-in `MCPStdioTool` | `pip install agent-framework althing[mcp]` |
| [n8n](https://n8n.io/) | [n8n_workflow.json](examples/integrations/n8n_workflow.json) | Built-in MCP Client tool | `pip install althing[mcp]` on the n8n runner |
| [LangChain via Composio](https://composio.dev/) | [composio_langchain.py](examples/integrations/composio_langchain.py) | `althing.integrations.composio` (in-process, non-MCP) | `pip install composio composio_langchain langchain langchain-anthropic althing` |
| [CrewAI via Composio](https://composio.dev/) | [composio_crewai.py](examples/integrations/composio_crewai.py) | `althing.integrations.composio` (in-process, non-MCP) | `pip install composio composio_crewai crewai althing` |

Also reaches [Zapier MCP](https://zapier.com/mcp) (30K+ actions), the
[VS Code AI Toolkit](https://code.visualstudio.com/api/extension-guides/ai/mcp),
Windsurf, Cursor, Zed, Claude Code, and Claude Desktop via the same MCP
server — all clients in that list install Althing with
`pip install althing[mcp]` and a one-line MCP config entry (see
[Use with Claude Code / Cursor / Windsurf / Zed](#use-with-claude-code--cursor--windsurf--zed)).

> **Don't see your framework?** MCP bridges are available for nearly
> every major agent framework. Start from
> [`examples/integrations/README.md`](examples/integrations/README.md)
> — the pattern is identical in each case (point the client at
> `althing mcp-serve` over stdio) — or
> [file an issue](https://github.com/DataViking-Tech/Althing/issues)
> so we can add a sibling example.

## Run via Docker

A pre-built image is published to both
[GitHub Container Registry](https://github.com/DataViking-Tech/Althing/pkgs/container/althing)
and Docker Hub on every tagged release. Use it for ephemeral or serverless
invocation (Lambda, Cloud Run, GitHub Actions, n8n) where you'd rather
spin up a container than pip-install.

```bash
# Pull (either registry works — same image, multi-arch: amd64 + arm64)
docker pull ghcr.io/dataviking-tech/althing:latest
docker pull althing/althing:latest

# One-off prompt
docker run --rm \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  althing/althing \
  prompt "What makes a name feel trustworthy?"

# MCP server on stdio (default CMD — wire this into an agent's MCP config)
docker run --rm -i \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  althing/althing

# Panel run with a mounted instrument file
docker run --rm \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -v "$PWD":/work -w /work \
  althing/althing \
  panel run --personas personas.yaml --instrument survey.yaml
```

The image's default `CMD` is `mcp-serve`, so omitting the command starts
the MCP stdio server. Any `althing` subcommand can be passed as
arguments to override. Provider keys are read from environment variables
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`/`GEMINI_API_KEY`,
`XAI_API_KEY`) — pass whichever your model requires.

Pin to a version tag in production rather than `:latest` — either the exact
release (`:1.5.7`) or the floating major.minor tag (`:1.5`), both published on
every stable release.

## Use as a Python Library

Everything the CLI and MCP server can do is also callable from Python.
No subprocess, no extra install — just import and go.

```python
from althing import quick_poll, run_panel, run_prompt

# One-shot LLM call
reply = run_prompt("What makes a name feel trustworthy?")
print(reply.response, reply.cost)

# Ask a bundled persona pack a single question
poll = quick_poll(
    "Which pricing tier name feels most premium: Core, Plus, or Pro?",
    pack_id="general-consumer",
)
print(poll.synthesis["recommendation"])

# Run a full branching instrument against a bundled pack
panel = run_panel(
    pack_id="general-consumer",
    instrument_pack="pricing-discovery",
)
print(panel.path)         # e.g. ["discovery", "probe_pricing", "validation"]
print(panel.total_cost)
```

The package root exposes eight functions plus three typed return
dataclasses — `PromptResult`, `PollResult`, `PanelResult`. Every
result is dict-compatible (`result["model"]`) so code that used to
consume the MCP JSON payload works unchanged.

| Function | What it does |
|----------|--------------|
| `run_prompt(prompt, *, model=...)` | Single LLM call — no personas |
| `quick_poll(question, pack_id=...)` | One question across a panel + synthesis |
| `run_panel(pack_id=..., instrument_pack=...)` | Full branching panel run |
| `extend_panel(result_id, questions)` | Append an ad-hoc follow-up round |
| `list_personas()` / `list_instruments()` | Discover installed packs |
| `list_panel_results()` / `get_panel_result(id)` | Reload saved results |

Use this path when subprocess overhead hurts (Jupyter, serverless, CI)
or when you want to wrap Althing in a LangChain / LlamaIndex tool
in three lines. See [`examples/sdk_usage.py`](examples/sdk_usage.py)
for a runnable end-to-end walkthrough.

### Typed extraction with Pydantic (1.0.4)

`run_panel` accepts a `pydantic.BaseModel` subclass directly as
`extract_schema=`. Althing generates the wire JSON Schema via
`model_json_schema()` and runs `model_validate` on each panelist's
extracted payload — validation failures surface per-response as
`extraction_validation_error` (the panel still produces a usable result
even when the LLM emits wire-valid JSON that violates a typed
constraint, e.g. `rating: 7` against a 1..5 `Likert`).

```python
from pydantic import BaseModel, Field
from althing import run_panel

class FeatureChoice(BaseModel):
    feature: str = Field(..., min_length=1)
    confidence: int = Field(..., ge=1, le=5)

result = run_panel(
    pack_id="developers",
    questions=[{"text": "Which feature should we ship first?"}],
    extract_schema=FeatureChoice,  # typed class accepted at the SDK boundary
)

for r in result.results:
    extracted = r["responses"][0].get("extraction")
    if extracted is not None:
        choice = FeatureChoice.model_validate(extracted)
        print(choice.feature, choice.confidence)
```

The same parameter still accepts a built-in name (`"sentiment"`,
`"likert"`, `"pick_one"`, …) or an inline JSON Schema dict — typed
classes are an additional dispatch, not a replacement.

## MCP Server (Agent Integration)

althing ships an MCP server so AI agents can run synthetic focus groups as tool calls.

```bash
pip install althing[mcp]
althing mcp-serve
```

Add to your editor's MCP config (Claude Code, Cursor, Windsurf):

```json
{
  "mcpServers": {
    "althing": {
      "command": "althing",
      "args": ["mcp-serve"],
      "env": { "ANTHROPIC_API_KEY": "sk-..." }
    }
  }
}
```

### Zero-config first run via sampling

No API key? When the invoking MCP client advertises the `sampling`
capability, althing falls back to asking the client to run the LLM
completion on its behalf — using the client's own subscription.

> **Sampling is deprecated and not universally supported.** MCP
> 2026-07-28 deprecated sampling (SEP-2577; still functional for at
> least 12 months), and **Claude Code has never implemented it**
> ([anthropics/claude-code#1785](https://github.com/anthropics/claude-code/issues/1785)).
> To use a Claude or ChatGPT subscription from Claude Code (or anywhere
> else), pass a subscription CLI model instead — e.g.
> `model="claude-code:haiku"` — see
> [Subscription CLI models](#subscription-cli-models-claude-code--codex).

With a sampling-capable client, That means `run_prompt`
and small `run_quick_poll` calls (up to 3 personas) work with zero env
setup:

```json
{
  "mcpServers": {
    "althing": {
      "command": "althing",
      "args": ["mcp-serve"]
    }
  }
}
```

Sampling mode is great for first-touch UX and quick exploratory polls.
For cross-provider ensembles, larger panels, and reproducible model
versioning, set a provider key in `env` to graduate to BYOK. See
[docs/mcp.md#model-resolution-order](docs/mcp.md#model-resolution-order)
for the full matrix of when sampling kicks in and which default model
each provider key picks.

### Tools (12)

| Tool | Description |
|------|-------------|
| `run_prompt` | Send a single prompt to an LLM — no personas required |
| `run_panel` | Run a full synthetic focus group panel with parallel panelists and synthesis |
| `run_quick_poll` | Quick single-question poll across personas with synthesis |
| `extend_panel` | Append an ad-hoc follow-up round to a saved panel result |
| `list_persona_packs` | List all saved persona packs (bundled + user-saved) |
| `get_persona_pack` | Get a specific persona pack by ID |
| `save_persona_pack` | Save a persona pack for reuse |
| `list_instrument_packs` | List installed instrument packs (bundled + user-saved) |
| `get_instrument_pack` | Load an installed instrument pack by name |
| `save_instrument_pack` | Install an instrument pack with validation |
| `list_panel_results` | List all saved panel results |
| `get_panel_result` | Get a specific panel result with all rounds and synthesis |

`run_panel` accepts an inline `instrument` dict or an `instrument_pack` name for v3 branching runs. `extend_panel` appends one ad-hoc round — it is **not** a re-entry into the v3 DAG. See [docs/mcp.md](docs/mcp.md) for full tool schemas, resources, and prompt templates.

## Use with Claude Code / Cursor / Windsurf / Zed

One-liner for any supported editor (writes exactly the JSON shown in the
sections below, merging non-destructively into your existing config):

```bash
althing mcp install --host auto   # detect installed editors and confirm each
```

Or pick a host explicitly: `--host claude-code | claude-desktop | cursor |
windsurf | zed`. `--dry-run` previews the change; `althing mcp uninstall`
removes exactly the entry it manages. No API key is written unless you pass
`--env` — set a provider key in your environment or run `althing login`
(sampling-capable hosts need no key at all).

Prefer to hand-edit? Copy the JSON snippet for your editor into the listed
config file, set your API key, and restart the editor. `althing mcp-serve`
is launched on demand over stdio — no long-running process to manage.

<details>
<summary><b>Claude Code</b></summary>

```bash
althing mcp install                  # ~/.claude.json (all projects)
althing mcp install --scope project  # ./.mcp.json (checked in)
```

Or add to `.mcp.json` at your project root (or `~/.claude.json` for all projects):

```json
{
  "mcpServers": {
    "althing": {
      "command": "althing",
      "args": ["mcp-serve"],
      "env": { "ANTHROPIC_API_KEY": "sk-..." }
    }
  }
}
```

Or install the bundled plugin (adds the `/althing-poll <question>`
slash command plus five skills — `focus-group`, `name-test`,
`concept-test`, `survey-prescreen`, `pricing-probe`):

```
/plugin install althing
```

Not using Claude Code, or prefer manual install? See
[`docs/agent-skills.md`](docs/agent-skills.md) for `cp`-based install
steps and per-host guidance.

</details>

<details>
<summary><b>Cursor</b></summary>

```bash
althing mcp install --host cursor                  # ~/.cursor/mcp.json (all projects)
althing mcp install --host cursor --scope project  # ./.cursor/mcp.json
```

Or add to `.cursor/mcp.json` at your project root (or `~/.cursor/mcp.json` for all projects):

```json
{
  "mcpServers": {
    "althing": {
      "command": "althing",
      "args": ["mcp-serve"],
      "env": { "ANTHROPIC_API_KEY": "sk-..." }
    }
  }
}
```

</details>

<details>
<summary><b>Windsurf</b></summary>

```bash
althing mcp install --host windsurf   # ~/.codeium/windsurf/mcp_config.json
```

Or add to `~/.codeium/windsurf/mcp_config.json` (or open
**Settings → Windsurf Settings → MCP Servers → View Raw Config**):

```json
{
  "mcpServers": {
    "althing": {
      "command": "althing",
      "args": ["mcp-serve"],
      "env": { "ANTHROPIC_API_KEY": "sk-..." }
    }
  }
}
```

</details>

<details>
<summary><b>Zed</b></summary>

```bash
althing mcp install --host zed   # ~/.config/zed/settings.json (context_servers schema)
```

Or hand-edit — Zed uses `context_servers` (not `mcpServers`). Add to `~/.config/zed/settings.json`:

```json
{
  "context_servers": {
    "althing": {
      "source": "custom",
      "command": "althing",
      "args": ["mcp-serve"],
      "env": { "ANTHROPIC_API_KEY": "sk-..." }
    }
  }
}
```

</details>

<details>
<summary><b>Hermes</b></summary>

Hermes uses a YAML config with `mcp_servers` and explicit timeout fields. Add this block to your Hermes config:

```yaml
mcp_servers:
  althing:
    command: "althing"
    args: ["mcp-serve"]
    timeout: 180
    connect_timeout: 60
    env:
      ANTHROPIC_API_KEY: "sk-..."
```

Or run on demand via `uvx` without a global install:

```yaml
mcp_servers:
  althing:
    command: "uvx"
    args: ["--from", "althing[mcp]", "althing", "mcp-serve"]
    timeout: 180
    connect_timeout: 60
    env:
      ANTHROPIC_API_KEY: "sk-..."
```

The 180s `timeout` covers a full panel run; the 60s `connect_timeout`
gives the subprocess room to import the MCP SDK on first launch.

</details>

<details>
<summary><b>Claude Desktop</b></summary>

```bash
althing mcp install --host claude-desktop   # platform-specific config path
```

Or open **Settings → Developer → Edit Config** (or edit the file directly):

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "althing": {
      "command": "althing",
      "args": ["mcp-serve"],
      "env": { "ANTHROPIC_API_KEY": "sk-..." }
    }
  }
}
```

Restart Claude Desktop after editing.

</details>

> **Using a non-Anthropic provider?** Swap `ANTHROPIC_API_KEY` for
> `OPENAI_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`, or
> `OPENROUTER_API_KEY` — see [LLM Provider Support](#llm-provider-support).
> The `althing` binary must be on the editor's `PATH`; if you installed
> into a virtualenv, point `command` at its absolute path
> (e.g. `/path/to/.venv/bin/althing`).

## What You Get

```
============================================================
Persona: Sarah Chen (Product Manager, 34)
============================================================
  Q: What is the most frustrating part of your workflow?
  A: Version control on documents that aren't in a proper system...

  Cost: $0.0779

============================================================
Persona: Marcus Johnson (Small Business Owner, 52)
============================================================
  Q: What is the most frustrating part of your workflow?
  A: I'll send my manager a menu update in an email, she makes
     her changes, sends it back...

  Cost: $0.0761

============================================================
Total: estimated_cost=$0.2360
```

Each persona responds in character with distinct voice, concerns, and perspective. Cost is tracked and printed per-panelist and in aggregate.

### Post-hoc Markdown report (`althing report`)

Already have a saved panel result and want a readable share-out? Render
the result to Markdown:

```bash
# Save a run first. --save prints the result ID plus the exact follow-up
# commands (report / results show / results list) so there's no need to
# scrape prose or search the filesystem for the artifact.
althing panel run \
  --personas examples/personas.yaml \
  --instrument examples/survey.yaml \
  --save

# Render to stdout, either by result ID or path to a result JSON
althing report <result-id>
althing report path/to/result.json

# Write to a file
althing report <result-id> -o report.md
```

**Machine-readable handle (agents):** pairing `--save` with
`--output-format json` adds two stable top-level keys to the stdout JSON —
`result_id` (the saved handle) and `saved_path` (its absolute path). Feed
`result_id` straight into `report`, `results show`, `analyze`, or the MCP
tools without scraping the stderr `Result saved:` line or guessing the file
location. The keys are present **only** when `--save` is active; a checkpointed
run additionally surfaces its checkpoint `run_id`. The human-facing
`Result saved:` hint still goes to stderr, keeping stdout pure JSON.

#### Discovering saved results (`althing results`)

`--save` writes to the results store (`~/.althing/results`), which is
**distinct** from the checkpoint store that `althing runs list` shows.
To rediscover a saved run by its stable ID:

```bash
althing results list             # all saved results, newest first
althing results show <result-id> # provenance + canonical saved_path
```

`results show` prints the recorded provenance (althing/Python version,
config hash, pricing snapshot date) and the canonical `saved_path`; in
`--output-format json` it emits the full result envelope plus `saved_path`.
Saved results now embed a `metadata` block, so `report` provenance for
freshly saved runs is populated rather than `(unknown)`.

Every rendered report opens with a mandatory synthetic-panel banner and
closes with a matching footer so the output can't be mistaken for
real-user research:

```markdown
# Panel Report: <result-id>

> **Synthetic panel.** All responses below were generated by AI personas,
> not human respondents. Do not cite as user-research data.

...

_Generated by althing report. Synthetic panel — AI-generated responses, not human data._
```

`report` is scoped to Markdown v1 — provenance, per-model rollup, persona
summary, synthesis, and failure stats. HTML and chart rendering are
deferred to v2. A `report` optional-deps extra exists (`pip install
althing[report]`) and installs cleanly, but is **currently empty** —
it is a forward-compat slot for v2 HTML dependencies and adds nothing
today.

Full spec: [`specs/sp-viz-layer/`](specs/sp-viz-layer/).

## Packs: Builtin vs Registry

althing has two kinds of packs, and the distinction matters when you're
searching for one:

- **Builtin packs** ship inside the `althing` wheel. After `pip install
  althing` they are immediately resolvable by name — no `pack import`, no
  network. Reference them anywhere a `--personas` or `--instrument` argument
  takes a *name* (the unified resolver also accepts a YAML path).
- **Registry packs** are community-authored. They live in third-party GitHub
  repos and are listed in
  [`DataViking-Tech/althing-registry`](https://github.com/DataViking-Tech/althing-registry).
  You pull them with `althing pack import gh:user/repo`; once imported they
  become resolvable by name like a builtin. See [docs/registry.md](docs/registry.md)
  for URI forms, verification, offline cache, and the submission flow.

### Builtin persona packs (14, 232 personas total)

| Pack | Personas |
|------|----------|
| `ai-eval-buyers` | 20 |
| `broad-professionals` | 20 |
| `developer` | 15 |
| `enterprise-ai-buyers` | 18 |
| `enterprise-buyer` | 15 |
| `general-consumer` | 15 |
| `healthcare-patient` | 15 |
| `job-seekers` | 15 |
| `market-research-critics` | 16 |
| `product-research` | 20 |
| `recruiters-talent` | 15 |
| `skeptical-executives` | 18 |
| `startup-founder` | 15 |
| `students` | 15 |

`althing pack list` (or MCP `list_persona_packs`) enumerates these plus any
user-saved packs. Picking the right pack for your task?
See [docs/task-recommendations.md](docs/task-recommendations.md) for the
task → pack → model-config matrix with copy/paste commands.

### Builtin instrument packs (8, all v3 branching)

`churn-diagnosis`, `feature-prioritization`, `general-survey`,
`landing-page-comprehension`, `market-research`, `name-test`,
`pricing-discovery`, `product-feedback`.

`althing instruments list` enumerates these plus any installed packs.

If you searched the registry for one of the names above and came up empty,
that's expected — they're SDK builtins, not registry entries. Use the name
directly with `panel run --personas <name>` or `--instrument <name>`.

## Defining Personas

```yaml
# personas.yaml
personas:
  - name: Sarah Chen
    age: 34
    occupation: Product Manager
    background: >
      Works at a mid-size SaaS company. 8 years in tech,
      previously a software engineer. Manages a team of 5.
    personality_traits:
      - analytical
      - pragmatic
      - detail-oriented

  - name: Marcus Johnson
    age: 52
    occupation: Small Business Owner
    background: >
      Runs a family-owned restaurant chain with 3 locations.
      Not tech-savvy but recognizes the need for digital tools.
    personality_traits:
      - practical
      - skeptical of technology
      - values personal relationships
```

### Merging Additional Personas (`--personas-merge`)

Layer extra personas onto a base file (or exported pack) without editing
the original. `--personas-merge` is repeatable and appends in order; a
later persona whose `name` matches an earlier one replaces it in place:

```bash
althing panel run \
  --personas developer.yaml \
  --personas-merge contrarian.yaml \
  --personas-merge intern.yaml \
  --instrument pricing-discovery \
  --var problem="choosing a project management tool"
```

### Community packs

You can import persona packs straight from GitHub:

```bash
# Listed in the althing registry — import by gh: URI
althing pack import gh:dataviking-tech/example-pack

# Not yet in the registry — opt in explicitly
althing pack import gh:alice/my-pack --unverified
```

The registry itself is an open, PR-based index at
[`DataViking-Tech/althing-registry`](https://github.com/DataViking-Tech/althing-registry).
See [docs/registry.md](docs/registry.md) for the full reference — supported URL
forms, cache + offline behavior, collision rules, and the flow for publishing
your own pack.

## Defining Instruments

```yaml
# survey.yaml
instrument:
  questions:
    - text: >
        What is the most frustrating part of your current
        workflow when collaborating with others?
      response_schema:
        type: text
      follow_ups:
        - "Can you describe a specific recent example?"

    - text: >
        If you could fix one thing about how you work with
        technology daily, what would it be?
      response_schema:
        type: text
```

### Attachments — show panelists a screenshot, a live URL, a PDF, or HTML

Instruments can carry an **attachment bank** so panelists react to something
visual or external instead of (or alongside) the question text. Declare a
top-level `attachments:` mapping on the instrument and reference entries by id
from a question's `attachments:` list. Four types are supported:

| Type | Source | Use when |
|------|--------|----------|
| `image` | `base64`, `url`, or `file_id` | A screenshot, mockup, or photo. |
| `document` | `base64`, `url`, or `file_id` | A PDF (spec, brief, contract). |
| `url` | A live URL fetched at run time | A real, currently-live page. |
| `html` | Inline string | A literal markup fragment (pricing block, email, copy variant). |

```yaml
instrument:
  version: 3
  attachments:
    landing_page:
      type: url
      url: https://example.com
      fetch_mode: screenshot   # auto | html_text | markdown | screenshot
  rounds:
    - name: reaction
      questions:
        - text: "What does this studio do, and is it for someone like you?"
          attachments: [landing_page]
```

> **Two gotchas worth knowing up front:**
> - **`image` / `screenshot` attachments need a vision-capable model.** Text-only
>   models (e.g. `claude-3.5-haiku`) are rejected fast with a clear error — use a
>   multimodal model such as `claude-haiku-4.5`, `gpt-4o-mini`, or `gemini-2.0-flash`.
> - **`fetch_mode: screenshot` requires the visual extra:** `pip install 'althing[visual]'`
>   (installs Playwright; then `python -m playwright install chromium`). Text modes
>   (`markdown` / `html_text`) don't need it.

Full guide and a runnable starter: [docs/cookbook/with-attachments.md](docs/cookbook/with-attachments.md).

## Adaptive Research (0.5.0): Branching Instruments

A v3 instrument is a small DAG of *rounds*. After each round, a routing
predicate decides which round runs next based on the synthesizer's themes
and recommendation. The panel chooses its own probe path — no human in the
loop, no hand-coded conditional flows.

```bash
# The Show HN demo: ~$0.20, one command, the panel decides
# whether to dig into pain, pricing, or alternatives.
althing panel run \
  --personas examples/personas.yaml \
  --instrument pricing-discovery \
  --var problem="choosing a project management tool"
```

`pricing-discovery` is one of eight bundled v3 instrument packs (see
[Builtin instrument packs](#builtin-instrument-packs-8-all-v3-branching)
above). List them with `althing instruments list`.

The output now carries a `path` array recording the routing decisions
that actually fired:

```
discovery -> probe[themes contains price] -> probe_pricing -> validation
```

Render the DAG of any instrument:

```bash
althing instruments graph pricing-discovery --format mermaid
```

### CLI Flag Support for Multi-Round Runs

`panel run` executes multi-round (v2 linear / v3 branching) instruments
through the same router-driven engine as the MCP and SDK surfaces
(`run_multi_round_panel`). Most flags work identically; a few features
are only wired into the single-round path today and are **refused
loudly** up front (exit 1, before any LLM spend) rather than silently
degrading:

| Supported with multi-round instruments | Refused (single-round only, for now) |
|---|---|
| `--model`, weighted `--models m:w,...`, per-persona YAML model overrides, `--best-model-for` | `--models a,b` ensemble comparison, `--blend` |
| `--temperature`, `--top-p`, `--seed` | `--variants` (robustness perturbation) |
| `--schema`, `--extract-schema` | `--resume`, `--checkpoint-dir`, `--checkpoint-every`, `--allow-drift`, `--force-overwrite` (checkpointing) |
| `--synthesis-model`, `--synthesis-prompt`, `--synthesis-temperature` | `--max-cost` (mid-run cost gate), `--question-failure-budget` |
| `--var` / `--vars-file`, `--prompt-template`, instrument-embedded `system_prompt_template` | `--convergence-*`, `--auto-stop`, `--calibrate-against`, `--submit-to-synthbench` |
| `--save`, `--strict`, `--failure-threshold` | `--synthesis-strategy=map-reduce`, `--synthesis-auto-escalate` |
| `--max-concurrent`, `--rate-limit-rps`, `--skip-preflight`, `--personas-merge` | `--allow-empty-attachments` |
| `--dry-run` (previews the DAG and names the engine) | `--no-synthesis` on **branching** instruments — per-round synthesis is what `route_when` evaluates. (Allowed on linear v2/v3 instruments with no `route_when`.) |

The compatibility audit also runs on `--dry-run`, so a dry-run "OK"
always means the real run is runnable. Note that for branching
instruments the dry-run question/call/cost figures cover every declared
round and are an upper bound — routing decides the executed path at run
time.

### Predicate Reference

`route_when` is a list of clauses evaluated in order. The first matching
clause wins; an `else` clause is **mandatory** as the last entry.

```yaml
route_when:
  - if: { field: themes, op: contains, value: price }
    goto: probe_pricing
  - if: { field: recommendation, op: matches, value: "(?i)wait|delay" }
    goto: probe_objections
  - else: __end__
```

| Field | Source |
|-------|--------|
| `themes` | `SynthesisResult.themes` (list, substring match) |
| `recommendation` | `SynthesisResult.recommendation` (string) |
| `disagreements`, `agreements`, `surprises` | `SynthesisResult` (lists) |
| `summary` | `SynthesisResult.summary` (string) |

| Op | Meaning |
|----|---------|
| `contains` | Substring match against any list entry or the string |
| `equals` | Exact string match |
| `matches` | Python regex match (use `(?i)` for case-insensitive) |

The reserved target `__end__` terminates the run; the path so far feeds
final synthesis.

### Theme Matching: The R3 Caveat

> **Predicates match against the synthesizer's *exact* theme strings.**

`themes contains price` only fires if the synthesizer actually emitted a
theme containing the substring `price`. LLM synthesizers paraphrase —
"cost concerns" or "sticker shock" will not match. The bundled packs
mitigate this with a comment block at the top of the instrument that
hints at the canonical theme tags the synthesizer should prefer:

```yaml
# Synthesizer guidance: when emitting `themes`, prefer the short
# canonical tags below so route_when predicates match reliably:
#   - "pain"   (workflow pain, frustration, broken status quo)
#   - "price"  (cost concerns, perceived value, sticker shock)
#   - "alternative" (existing tools, workarounds, competitors)
```

When you author your own v3 packs, **always** add a similar tag-hint
block. The synthesizer reads it and tends to use the canonical tags;
your `contains` predicates then route reliably. If you skip this step,
expect routes to silently fall through to `else` because the
synthesizer's prose theme labels won't match your predicate values.

Prefer short, lowercase, single-token tags (`price`, `pain`, `confusion`)
over long phrases. `contains` does substring matching, so `price` will
also match `pricing`, `priced`, etc.

### `instruments` Subcommand

```bash
althing instruments list                       # bundled + installed packs
althing instruments show pricing-discovery     # full YAML body
althing instruments install ./my-pack.yaml     # add a local pack
althing instruments graph pricing-discovery    # text DAG
althing instruments graph pricing-discovery \
  --format mermaid                                 # mermaid flowchart
```

The unified instrument resolver (used by `panel run --instrument`) accepts
either a YAML path *or* an installed pack name, so you can iterate on a
local file and then `install` it once it's stable.

## Examples

The [`examples/`](examples/) directory ships a persona pack plus one
instrument per format (v1 flat, v2 linear, v3 branching). Start from
[`examples/README.md`](examples/README.md) for the full index and
annotated walkthroughs — including two v3 branching patterns
(demographic segmentation and A/B concept testing) you can adapt to
your own studies.

## LLM Provider Support

althing works with any LLM provider. Set the appropriate environment variable:

| Provider | Environment Variable | Model Flag |
|----------|---------------------|------------|
| Anthropic (Claude) | `ANTHROPIC_API_KEY` | `--model sonnet` |
| Google (Gemini) | `GOOGLE_API_KEY` or `GEMINI_API_KEY` | `--model gemini` |
| OpenAI | `OPENAI_API_KEY` | `--model gpt-4o` |
| OpenRouter | `OPENROUTER_API_KEY` | `--model openrouter/anthropic/claude-haiku-4-5` |
| xAI (Grok) | `XAI_API_KEY` | `--model grok` |
| Any OpenAI-compatible | `OPENAI_API_KEY` + `OPENAI_BASE_URL` | `--model <model-id>` |
| Claude Code CLI (subscription) | none — signed-in `claude` on PATH | `--model claude-code:sonnet` |
| Codex CLI (subscription) | none — signed-in `codex` on PATH | `--model codex:` or `--model codex:<model-id>` |

```bash
# Use Claude (default)
althing panel run --personas p.yaml --instrument s.yaml

# Use GPT-4o
althing panel run --personas p.yaml --instrument s.yaml --model gpt-4o

# Use a local model via Ollama
OPENAI_BASE_URL=http://localhost:11434/v1 \
althing panel run --personas p.yaml --instrument s.yaml --model llama3
```

### Subscription CLI models (`claude-code:` / `codex:`)

The `claude-code:<model>` and `codex:<model>` prefixes run each completion
through a locally installed, signed-in agent CLI in headless mode
(`claude -p` / `codex exec`), so calls draw on your Claude or ChatGPT
subscription instead of an API key. Every call is an isolated, tool-less
completion: the CLI's agent system prompt is replaced by the persona
prompt, tools / MCP servers / user settings are disabled, nothing is
persisted, and the process runs in an empty scratch directory. Structured
output maps onto the CLIs' native JSON-schema flags.

```bash
althing panel run --personas p.yaml --instrument s.yaml --model claude-code:haiku
althing prompt "Hello" --model codex:          # Codex CLI's default model
```

Things to know before relying on it:

- **No sampling controls.** `temperature`, `top_p`, `seed`, and
  `max_tokens` are not exposed by the CLIs and are ignored (with a
  warning). Results are not directly comparable to API-key runs of the
  same model — label them separately (e.g. on SynthBench).
- **Subscription usage limits** bound throughput. Usage-limit errors
  surface as rate limits and get the normal backoff. Cap in-flight CLI
  processes with `ALTHING_CLI_MAX_CONCURRENT` (default 4) and per-call
  time with `ALTHING_CLI_TIMEOUT` (default 300 s).
- **Text only, forced single-tool only.** Attachments and free tool
  use are rejected; multi-turn persona history is flattened into one
  prompt per call.
- **Cost figures are estimates.** There is no per-call bill, so cost is
  the local pricing-table estimate (API list-price equivalent) and
  `--max-cost` acts as a usage budget.
- **API keys are stripped.** `claude` prefers `ANTHROPIC_API_KEY` over
  the subscription login, so althing removes it from the child process
  environment. Set `ALTHING_CLAUDE_CODE_ALLOW_API_KEY=1` to keep it.
- Optional: `ALTHING_CLAUDE_CODE_EFFORT` / `ALTHING_CODEX_EFFORT` pass a
  reasoning-effort level; `ALTHING_CLAUDE_CODE_BIN` / `ALTHING_CODEX_BIN`
  override the executable path.

These prefixes are only used when you ask for them — they are never
picked as an implicit default.

> **Large panels and the OpenRouter default (synthbench#261):** when
> `--model` is omitted, the default is resolved from whichever key is
> present — for an OpenRouter-only environment that's `openrouter/auto`,
> whose auto-router can pick a slow reasoning model and stall a
> 20-persona panel for 15+ minutes. So for runs with **≥ 10 personas**
> and no explicit model, althing auto-selects a fast equivalent
> (`openrouter/anthropic/claude-haiku-4.5`) and prints a one-line note.
> This applies identically on the CLI, SDK, and MCP surfaces. Pass
> `--model openrouter/auto` (or `model="openrouter/auto"`) explicitly to
> opt back into the auto-router — treat it as the "highest fidelity,
> accept the latency" option. Runs under 10 personas keep the normal
> default.

### Model Aliases

althing ships with short aliases (`sonnet`, `opus`, `haiku`, `grok`,
`gemini`, `gemini-pro`) that map to canonical model identifiers. You can
override or extend these without changing code:

**Resolution order (highest priority wins):**

1. **`ALTHING_MODEL_ALIASES` env var** — JSON string of alias→model pairs
2. **`~/.althing/aliases.yaml`** — YAML file
3. **Hardcoded defaults** — built into the package

```bash
# Override via env var (JSON)
export ALTHING_MODEL_ALIASES='{"sonnet": "claude-sonnet-4-6-20250414", "fast": "claude-haiku-4-5-20251001"}'
althing prompt "Hello" --model fast
```

```yaml
# ~/.althing/aliases.yaml
aliases:
  fast: claude-haiku-4-5-20251001
  smart: claude-opus-4-6
  sonnet: claude-sonnet-4-6-20250414
```

Env var entries override file entries, which override hardcoded defaults.
Aliases from all tiers are merged, so you only need to specify the ones you
want to add or change.

### SynthBench-driven model picker

`--best-model-for` consults the [SynthBench](https://synthbench.org)
leaderboard and uses the top-ranked model for a topic or dataset:

```bash
althing panel run --personas p.yaml --instrument s.yaml \
    --best-model-for "Economy & Work"
```

The leaderboard is cached for 24 hours at
`~/.althing/synthbench-cache.json`. See
[docs/recommended-models.md](docs/recommended-models.md) for the full
rules, offline behaviour, and a use-case → top-model table.

If the top-ranked entry exposes a display label (e.g. `Althing (Gemini
Flash Lite)`) rather than a runnable provider model id in its `model`
field, `--best-model-for` substitutes the runnable `model_id` the
leaderboard publishes alongside it (e.g. `google/gemini-2.5-flash-lite`),
so you get a real, runnable model instead of a refusal. Only when no
runnable id can be resolved does it refuse to stamp the label — printing
an actionable message and falling back to your existing `--model`/default.
Pair with `--dry-run` to see the picked model (and any such refusal)
before any LLM call is made.

### Model packs (agent guidance)

`--best-model-for` picks one model; **model packs** pick a model *mix*
calibrated for a decision's stake — e.g. `fast-cheap-preflight` (single
cheap model for smoke tests) vs. `balanced-research-ensemble`
(`haiku,sonnet,gemini-2.5-flash` for "real" decisions) vs.
`high-stakes-validation` (4-family ensemble with `--blend`). The packs are
documented presets that compile to the existing `--models` flag — see
[docs/model-packs.md](docs/model-packs.md) for the seven recommended
configurations, the SynthBench finding that motivates ensembles over single
cheap models, and a checklist for agents on matching pack to claim strength.

## Architecture

althing is a research harness, not an LLM wrapper. It orchestrates the research workflow:

```
personas.yaml ──┐
                 ├──> Orchestrator ──> Panelist 1 ──> LLM ──> Response
instrument.yaml ─┘                 ├──> Panelist 2 ──> LLM ──> Response
                                   └──> Panelist N ──> LLM ──> Response
                                                                  │
                                              Aggregated Report <──┘
```

### Components

| Module | Purpose |
|--------|---------|
| `llm/` | Provider-agnostic LLM client (Anthropic, Google, OpenAI, xAI) |
| `runtime.py` | Agent session loop (turns, tool calls, compaction) |
| `orchestrator.py` | Parallel panelist execution with worker state tracking |
| `structured/` | Schema-validated responses via tool-use forcing |
| `cost.py` | Token tracking, model-specific pricing, budget enforcement |
| `persistence.py` | Session save/load/fork (JSON + JSONL) |
| `plugins/` | Manifest-based extension system with lifecycle hooks |
| `mcp/` | MCP server for agent-native invocation (stdio transport) |
| `cli/` | CLI framework with slash commands, output formatting |

### Design Principles

- **Minimal dependencies** — Python 3.10+ with `httpx` for HTTP and `pyyaml` for YAML parsing. Optional: `mcp` for the MCP server
- **Agent-native** — invoke from your terminal or from an AI agent's MCP tool call
- **Provider agnostic** — swap LLMs without changing research definitions
- **Cost transparent** — every API call is tracked and priced
- **Reproducible** — same personas + same instrument = comparable output
- **Structured by default** — responses conform to declared schemas

## Output Formats

```bash
# Human-readable (default)
althing panel run --personas p.yaml --instrument s.yaml

# JSON (pipe to jq, store in database)
althing panel run --personas p.yaml --instrument s.yaml --output-format json

# NDJSON (streaming, one event per line)
althing panel run --personas p.yaml --instrument s.yaml --output-format ndjson
```

## Budget Control

```bash
# Set a dollar budget for the panel
althing panel run --personas p.yaml --instrument s.yaml --config budget.yaml
```

The cost tracker enforces soft budget limits — the current panelist completes, but no new panelists start if the budget is exceeded.

## Persona Prompt Template Variants

The `templates/` directory contains four prompt template variants for benchmarking how persona prompt construction affects response quality:

| Template | File | Fields | Purpose |
|----------|------|--------|---------|
| **Current** | `templates/current.txt` | name, age, occupation, background, personality_traits | Control — documents the default prompt style |
| **Demo** | `templates/demo.txt` | name, age, occupation, education_level, income_bracket, urban_rural, political_leaning, background | Demographic-enriched — adds SubPOP/OpinionsQA stratification axes |
| **Values** | `templates/values.txt` | name, age, occupation, background, core_values, decision_style | Values-enriched — adds belief and decision-making context |
| **Minimal** | `templates/minimal.txt` | name, age, occupation | Ablation control — tests how much narrative matters |

Usage:

```bash
althing panel run --personas personas.yaml --instrument survey.yaml --prompt-template templates/demo.txt
```

Templates use Python format-string syntax (`{field_name}`). Missing persona fields are left as literal `{field_name}` in the output.

## Reproducibility (`--seed`)

Pass `--seed N` to `panel run` for reproducible sampling on providers that
honor the seed parameter (OpenAI, Gemini, xAI, OpenRouter):

```bash
althing panel run --seed 42 --personas p.yaml --instrument s.yaml
```

What althing **can** promise:

- Forwards the seed to providers that support it.
- Records the seed in the run's `metadata.parameters.seed` and in the
  checkpoint fingerprint, so a `--resume` run with a different seed
  fails loudly instead of silently mixing samples.

What althing **cannot** promise:

- Anthropic's Messages API has no `seed` parameter. When `--seed` is set
  on a Claude model, althing logs a single warning per provider and
  proceeds without determinism. Use `--temperature 0` for closer-to-
  deterministic Claude output, but expect drift across model versions.
- Even on supporting providers, "seeded" sampling is best-effort: model
  serving infrastructure and silent server-side updates can still shift
  outputs between runs.

`--seed` is for *new* runs you want to be reproducible. To replay a
previously-cached run exactly, use `althing panel run --resume <run-id>`
— that path serves cached responses verbatim and is independent of
`--seed`. See [docs/reproducibility.md](docs/reproducibility.md) for
the full picture.

## Methodology Notes

Synthetic research is useful for exploration, hypothesis generation, and rapid iteration. It is **not** a replacement for talking to real humans.

Known limitations:
- Synthetic responses tend to cluster around means
- LLMs exhibit sycophancy (tendency to please)
- Cultural and demographic representation has blind spots
- Higher-order correlations between variables are poorly replicated

Use althing to pre-screen and iterate, then validate with real participants.

## Multi-Model Ensemble (0.7.0)

Run the same panel through multiple models and blend their response distributions for higher-fidelity results. [SynthBench experiments](https://github.com/DataViking-Tech/synthbench/blob/main/FINDINGS.md) show 3-model ensembles improve human-parity scores by +5-7 points over any single model.

```bash
# Run 3 models with equal weights and blend distributions
althing panel run \
  --models haiku:0.33,gemini:0.33,gpt-4o-mini:0.34 \
  --blend \
  --personas personas.yaml \
  --instrument survey.yaml

# Each persona is interviewed by all 3 models independently.
# The --blend flag averages response distributions across models,
# producing more representative synthetic survey data.
```

The blended output includes per-model distributions and the weighted ensemble distribution, letting you inspect both individual model perspectives and the consensus view.

## Running at Scale

For panels of 500 to 10,000+ panelists, althing can track response-distribution convergence live via Jensen-Shannon divergence and optionally auto-stop once every bounded (Likert / yes-no / pick-one / enum) question has stabilized. The post-run JSON gains a top-level `convergence` section showing the smallest `n` at which each question converged, so you can confidently run smaller next time.

```bash
althing panel run \
  --personas large-panel.yaml \
  --instrument pricing-discovery \
  --var problem="choosing a project management tool" \
  --convergence-check-every 20 \
  --auto-stop \
  --output-format json > result.json

jq '.convergence.overall_converged_at, .convergence.auto_stopped' result.json
# 473
# true
```

See [docs/convergence.md](docs/convergence.md) for methodology, tuning, and the
optional `--convergence-baseline` flag that compares your run against a real-human
baseline from [SynthBench](https://github.com/DataViking-Tech/synthbench) (install via `pip install 'althing[convergence]'`).

### Submitting calibrated runs to SynthBench

A calibrated panel run (one made with `--calibrate-against DATASET:QUESTION`)
produces a per-question JSD against a known human distribution — the same
score the [SynthBench leaderboard](https://synthbench.org) tracks. Add
`--submit-to-synthbench` to upload the result automatically when the run
completes:

```bash
export SYNTHBENCH_API_KEY=sk_synthbench_...    # mint at synthbench.org/account
althing panel run \
  --personas examples/personas.yaml \
  --instrument happiness-probe \
  --calibrate-against gss:HAPPY \
  --convergence-check-every 20 \
  --submit-to-synthbench
```

First use shows a one-screen privacy notice (recorded at
`~/.althing/synthbench-consent.json` so subsequent runs don't re-prompt;
pass `--yes` for CI). Submission failures are warned-but-non-fatal so a
slow SynthBench cannot fail your panel run. See
[docs/synthbench-integration.md](docs/synthbench-integration.md) for the
full privacy model, what does and does not get uploaded, and the failure
modes.

## Versions

| Version | Highlights |
|---------|-----------|
| 1.5.x *(current)* | `althing mcp install --host auto` one-liner host registration; four canonical persona packs; deterministic `poll-summary` rollups; MCP auto-picks a fast model for ≥10-persona panels; `results list`/`results show` for the saved-results store; `python -m althing` alias; fail-loud empty attachments and typed `response_schema` enforcement; synthesis routing follows the panel's provider; PyPI Trusted Publishing + auto-tag version-drift guard |
| 1.4.0 | Explicit `cost_estimated_usd` / `cost_actual_usd` (+ `per_model_breakdown`) on `EnsembleResult`, `ModelRunResult`, and `SynthesisResult` — estimate-vs-bill reconciliation without inspecting raw usage |
| 1.3.0 | `trafilatura` moved to the `[full]` extra so bare `pip install althing` works on pyodide/Cloudflare Python Workers (no `lxml` C extension required) |
| 1.2.0 | Pyodide-safe / async-DI surface for `althing.ensemble.synthesize_panel` — drive the judge call through your own async LLM client |
| 1.1.0 | `althing.ensemble` frozen as the supported public API for the ensemble/deliberation core |
| 1.0.6 | Cross-town dogfood friction sweep on the v1.0.x line; `panel_verdict` emitted on the MCP success path; release-dogfood protocol codified |
| 1.0.0 | Frozen MCP contract (`schemas/v1.0.0.json`, `schema_version` echoed on every response and typed error); multimodal attachments (images, URLs, PDFs, inline HTML) with stratified delivery and a URL-fetch security perimeter |
| 0.12.0 | `--best-model-for TOPIC[:DATASET]` SynthBench-driven model picker, `--submit-to-synthbench`, `althing pack calibrate`, six bundled packs deepened 5 → 15 personas |
| 0.11.0 | `sp-i2ub` scaled-orchestration epic: panelist-level checkpointing with `--resume <run-id>` and auto-checkpoint on SIGINT/SIGTERM (every K=25 panelists), `--max-cost <USD>` mid-run projected-total cost gate that halts gracefully with valid partial JSON, and valid-partial-JSON discipline on every abort path (rate-exhaustion, SIGINT, cost-gate, panelist failure) with `run_invalid: true` + specific `abort_reason` and exit code 2; 6-bug loudness sweep turning silent failures loud across alias parse, synthesis partial payload, MCP `extend_panel`, condition evaluator, orchestrator follow-up exceptions, and the `test_aliases` fixture; auto-tag now fails loudly on unlabeled release PRs; `pip-audit` ignores CVE-2026-3219 in pip 26.0.1 |
| 0.10.0 | `althing report` post-hoc Markdown renderer for saved panel results (behind `[report]` extra); inline SynthBench calibration via `panel run --calibrate-against DATASET:QUESTION` with auto-derived `pick_one` schema and `per_question[key].calibration` sub-object wire format; decentralized pack registry — `pack import gh:<user>/<repo>` with `--unverified`, `pack search`, `pack list --registry`, 24h cache + offline fallback; optional `version:` field on persona packs with opt-in shadow warning |
| 0.9.9 | `--synthesis-strategy=auto` now routes to map-reduce on context overflow instead of hard-failing; OpenRouter alias resolution tightened for sub-1¢ local-table sanity checks; `--personas-merge` warns (or errors via `--personas-merge-on-collision`) on name collisions with bundled packs; version single-sourced from `src/althing/__version__.py` with templated site render |
| 0.9.8 | Fail-loud synthesis (context-overflow pre-flight + structured `synthesis_error`), per-question map-reduce synthesis (`--synthesis-strategy=single\|map-reduce\|auto`), response-schema validation with deterministic distributions for bounded question types, rate-limit-aware client (`--max-concurrent`, `--rate-limit-rps`), live convergence telemetry + `--auto-stop`, 4 new bundled persona packs (`job-seekers`, `recruiters-talent`, `product-research`, `ai-eval-buyers`) raising shipped personas 24 → 84, `/althing-poll` slash command |
| 0.9.7 | Provider-reported cost is authoritative — when a provider returns `usage.cost` (e.g. OpenRouter), that value is recorded verbatim instead of being recomputed from a local pricing table; `pricing_fallback` warning surfaced when a model falls through to `DEFAULT_PRICING`; ensemble rounding no longer silently drops low-weight models |
| 0.9.5 | Fail-fast on unsubstituted `{placeholder}` variables in instruments/personas, `--personas-merge PATH` for layered persona packs, `--dry-run` pre-run preview, `run_invalid` flag on degenerate runs, MCP BYOK detection routes through the credentials store |
| 0.9.4 | `althing login` / `logout` / `whoami` credential-store CLI; MCP recognises `OPENROUTER_API_KEY` as BYOK and picks a sensible default; Docker images on GHCR + Docker Hub multi-arch; MCP sampling fallback for `run_prompt` and `run_quick_poll` |
| 0.9.0 | First release post-public-flip. Repo renamed to `Althing` (PyPI name `althing` unchanged) |
| 0.8.0 | `lookup_pricing_by_provider` public helper for synthbench-format provider strings; multi-question CLI cost shape symmetry (`total_cost` / `panelist_cost` / `total_usage` / `panelist_usage`) |
| 0.7.0 | Multi-model ensemble blending (`--blend`), OpenRouter provider support, temperature/top_p controls, prompt template customization |
| 0.6.0 | `--models` weighted model spec, `--temperature`/`--top_p` flags, persona prompt templates, pack generation, domain templates, MCP improvements |
| 0.5.0 | v3 branching instruments, router predicates, 5 bundled instrument packs, `instruments` subcommand (list/show/install/graph), MCP `*_instrument_pack` tools, rounds-shaped panel output, `extend_panel` ad-hoc round tool |
| 0.4.0 | `--var KEY=VALUE` and `--vars-file` for instrument templates, fail-loud on all-provider errors, default `--model` respects available credentials, `pack show <id>` alias, publish workflow fix |
| 0.3.0 | Structured output via tool-use forcing, cost tracking, MCP server (stdio), persona-pack persistence |

See [CHANGELOG.md](CHANGELOG.md) for detailed release notes.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and how to submit changes.

## Benchmarked on SynthBench

althing's ability to produce representative synthetic respondents is measured by [SynthBench](https://synthbench.org), an open benchmark for synthetic survey quality (a sibling DataViking project, operated by the Althing maintainers).

- **Want proof it works?** Browse the [leaderboard](https://synthbench.org) — the 3-model ensemble blend scores SPS 0.877 on opinionsqa, 0.858 on subpop, and 0.813 on globalopinionqa (leaderboard generated 2026-07-17; numbers move as the board recomputes).
- **Got a great configuration?** [Submit your scores](https://synthbench.org) and compare against baselines.
- **Contributing an adapter?** Heavy PRs with substantial behavior changes benefit from SynthBench results — reviewers can evaluate empirical quality, not just code. See [docs/adapter-guide.md](docs/adapter-guide.md) for the full adapter workflow.

## License

MIT
