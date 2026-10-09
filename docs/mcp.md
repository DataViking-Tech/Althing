# MCP Server Reference

althing ships an [MCP](https://modelcontextprotocol.io/) server so AI agents can run synthetic focus groups as tool calls. The server uses stdio transport and defaults to the `haiku` model for cheap, fast iterative use.

## Starting the Server

```bash
althing mcp-serve
```

The server communicates over stdin/stdout using JSON-RPC (the MCP protocol). It is designed to be launched by an MCP-aware editor or agent framework.

## Editor Configuration

### Claude Code / Claude Desktop / Cursor / Windsurf / Zed

The fastest path is the bundled installer (sy-skf, synthbench#262):

```bash
althing mcp install                                 # writes ~/.claude.json (Claude Code)
althing mcp install --scope project                 # writes ./.mcp.json (checked in)
althing mcp install --host claude-desktop           # platform-specific Claude Desktop config
althing mcp install --host cursor                   # ~/.cursor/mcp.json (--scope project → ./.cursor/mcp.json)
althing mcp install --host windsurf                 # ~/.codeium/windsurf/mcp_config.json
althing mcp install --host zed                      # ~/.config/zed/settings.json (context_servers schema)
althing mcp install --host auto                     # detect installed hosts, confirm each (--yes accepts all)
althing mcp install --target /path/to/mcp.json      # any other host with an mcpServers map
althing mcp install --env ANTHROPIC_API_KEY=sk-...  # bake credentials into the entry (optional)
althing mcp uninstall --host zed                    # remove exactly the entry the installer manages
```

The command merges into the host's existing servers map (`mcpServers`,
or `context_servers` for Zed — the written JSON matches the README's
per-host snippets exactly), never touches other servers or unrelated
settings, refuses to overwrite a clashing entry without `--force`, and
writes user-scoped files with mode `0600`. `--host auto` only offers
hosts whose user-level config file already exists — it never creates a
config for an editor that isn't installed. After a real write it prints
`Restart <host> to pick up the server.`

No secret is written unless you pass `--env` explicitly: by default the
entry has no `env` block and the installer prints a note pointing at
`althing login` / provider env vars (sampling-capable hosts need no
key at all — see [Sampling Mode](#sampling-mode)).

The installer also refuses by default when the `mcp` optional extra
isn't installed in the current Python env — without it,
`althing mcp-serve` would crash at launch time and the host would
report a generic "server failed to start" with no actionable hint. The
refusal points at the one-line fix (`pip install 'althing[mcp]'`) and
mentions the `--allow-missing-extra` escape hatch for cross-machine
setups where the editor and the server live in different envs (sy-xyn).
Equivalent guard runs at `mcp-serve` start so a stale host config that
points at a bare-extras install still produces a usable error message
instead of a Python traceback.

`--dry-run` previews the change without touching disk. In text mode it
puts the human prose on stderr and the resulting JSON config on stdout,
so you can pipe the preview through `jq`, redirect it to a file, or feed
it back into the installer:

```bash
althing mcp install --target ~/.cursor/mcp.json --dry-run
# stderr: Would install MCP server 'althing' in /Users/you/.cursor/mcp.json.
# stdout: { "mcpServers": { "althing": { ... } } }

althing mcp install --target ~/.cursor/mcp.json --dry-run 2>/dev/null \
  | jq '.mcpServers.althing'
```

In `--output-format json` mode the entire payload (action, entry, and
the full `resulting_config`) lands on stdout as a single JSON object
with no stderr noise — one stream, fully parseable.

If you'd rather hand-edit, the entry it produces is:

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

Set the environment variable for whichever LLM provider you want to use. See the main [README](../README.md#llm-provider-support) for the full provider table.

Prefer a zero-config first run? Skip the `env` block entirely — see
[Sampling Mode](#sampling-mode) below.

### Claude Code Plugin

```
/plugin install althing
```

This adds the `/focus-group` skill plus the `/althing-poll` slash command to your Claude Code session. The plugin auto-discovers the bundled `commands/` and `skills/` directories.

Prefer to install without the plugin, or use a different host? See
[Agent Skills & Slash Commands](agent-skills.md) for manual copy
steps and guidance for hosts that don't speak Claude Code's
slash-command convention.

### Hermes

Hermes uses a YAML config with an `mcp_servers` map and explicit timeout
fields. Add this block to your Hermes config:

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

If you don't want to install the `althing` binary globally, use
[`uvx`](https://docs.astral.sh/uv/) to fetch and run it on demand:

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

- `timeout` (180s) covers a full panel run — panels with many personas
  and rounds can take 60–120s in BYOK mode.
- `connect_timeout` (60s) gives the subprocess room to import the MCP
  Python SDK and providers on first launch.
- Drop the `env` block entirely if your Hermes host already advertises
  `sampling` and you want zero-config first-run (see
  [Sampling Mode](#sampling-mode)).

Restart Hermes after editing the config so the new server entry is
picked up.

### Other MCP hosts

Any host that speaks the MCP stdio transport works the same way: launch
`althing mcp-serve` as a subprocess and pass provider keys through
the environment. The Hermes block above is the canonical shape — most
hosts map onto either that YAML form or the JSON form used by Claude
Code / Cursor / Windsurf. If your host needs an explicit transport
field, set it to `stdio`.

### Manual Install (Claude Code without plugin)

If you configured the MCP server manually (without `/plugin install`) you can still get all commands and skills by running:

```bash
althing install-skills
```

This copies the bundled slash commands and skills into `~/.claude/`:

| Type | Name | Installs to |
|------|------|-------------|
| Slash command | `/althing-poll` | `~/.claude/commands/althing-poll.md` |
| Skill | `concept-test` | `~/.claude/skills/concept-test/SKILL.md` |
| Skill | `focus-group` | `~/.claude/skills/focus-group/SKILL.md` |
| Skill | `name-test` | `~/.claude/skills/name-test/SKILL.md` |
| Skill | `pricing-probe` | `~/.claude/skills/pricing-probe/SKILL.md` |
| Skill | `survey-prescreen` | `~/.claude/skills/survey-prescreen/SKILL.md` |

For a project-scoped install (places files in `.claude/` relative to the current directory instead of `~/.claude/`):

```bash
althing install-skills --target .claude
```

The command is idempotent — running it again overwrites existing files with the current bundled versions.

## Frozen v1.0.0 Contract

Every panel-running tool call (`run_panel`, `run_quick_poll`, `extend_panel`)
**requires** a `decision_being_informed` string field. `run_prompt` does not.

- 12–280 chars, single line, UTF-8.
- Echoed verbatim into `panel_verdict.meta.decision_being_informed`.
- *Omitted* → v1.0.x grace: the server synthesizes
  `"unspecified-legacy-call"`, returns a `W_DECISION_MISSING` nudge in
  `warnings[]`, and proceeds; under `ALTHING_SCHEMA_MIN>=1.1.0` omission
  is a hard typed `MISSING_DECISION` reject.
- Provided but empty after trim → `MISSING_DECISION`. `<12` chars →
  `INVALID_TOOL_ARG`. `>280` → `DECISION_TOO_LONG`. No silent truncation.

Successful persisted panel runs return the `panel_verdict.json` artifact
(closed-shape, `schema_version: "1.0.0"`, closed `flags[]` enum) under the
envelope's `panel_verdict` key, alongside `synthesis`. Full reference:
[docs/response-contract.md](response-contract.md). Migration guide:
[docs/migration-v1.md](migration-v1.md).

Running panels unattended (CI, agent pipelines)? The operational contract —
typed errors and `retry_safe` semantics, partial-failure behavior, timeout
budgets, cost gates, checkpoint/resume, determinism, credential handling,
and log/stream discipline — is documented in
[docs/production-operations.md](production-operations.md).

## Tools (12)

> **Structured polling is the agent default.** Both `run_panel` and
> `run_quick_poll` accept a `response_schema` argument (JSON Schema
> dict) that forces structured output at generation time — no prose
> parsing required. See [docs/structured-polling.md](structured-polling.md)
> for the full pattern catalogue covering forced-choice, Likert,
> tagged-themes (objections/risks), confidence, and ranking, plus a
> runnable 35-persona prioritization example.

> **Picking which `pack_id` / `model` / `models[]` to pass?** See
> [docs/task-recommendations.md](task-recommendations.md) for the
> task → persona pack → model config lookup with copy/paste examples
> (positioning, GTM, pricing, trust audit, healthcare comms, hiring).

### Research Tools

| Tool | Description |
|------|-------------|
| `run_prompt` | Send a single prompt to an LLM. No personas required. The simplest tool — ask a quick research question. **Does not require `decision_being_informed`.** |
| `run_panel` | Run a full synthetic focus group panel. Each persona answers all questions independently in parallel, followed by synthesis. Accepts inline `questions`, an inline `instrument` dict (v1/v2/v3), or an `instrument_pack` name. Template placeholders in the instrument (e.g. the bundled packs' `{problem}` / `{candidates}`) are filled via the `vars` argument — the MCP equivalent of the CLI's `--var`. Optional `max_cost` (USD) arms the mid-run cost gate (see [`max_cost`](#max_cost-hard-spend-ceiling)). **Requires `decision_being_informed`.** |
| `run_quick_poll` | Quick single-question poll across personas. A simplified `run_panel` for one question with synthesis. Accepts inline `personas` and/or a saved `pack_id` (merged; falls back to a built-in diverse set when both are omitted). Optional `max_cost` (USD) spend ceiling. **Requires `decision_being_informed`.** |
| `extend_panel` | Append a single ad-hoc round to a saved panel result. Reuses each panelist's saved session for conversational context. **Not** a re-entry into the v3 DAG — use for human-in-the-loop follow-ups. Optional `max_cost` (USD) spend ceiling for the extension round. **Requires `decision_being_informed`.** |

### Tool-call examples

```jsonc
// run_panel
{
  "tool": "run_panel",
  "arguments": {
    "pack_id": "general-consumer",
    "instrument_pack": "pricing-discovery",
    "vars": { "problem": "tracking cloud spend across teams" },
    "decision_being_informed": "choosing launch tier price"
  }
}

// run_quick_poll
{
  "tool": "run_quick_poll",
  "arguments": {
    "question": "Which name feels most premium: Core, Plus, or Pro?",
    "pack_id": "general-consumer",
    "decision_being_informed": "naming the paid tier"
  }
}

// extend_panel
{
  "tool": "extend_panel",
  "arguments": {
    "result_id": "result-20260503-abc123",
    "questions": ["What would you pay for this if it shipped tomorrow?"],
    "decision_being_informed": "validating the indie pricing ceiling"
  }
}

// run_prompt — no decision_being_informed
{
  "tool": "run_prompt",
  "arguments": {
    "prompt": "Summarize the Q3 retention drop in two sentences."
  }
}
```

### Persona Pack Management

| Tool | Description |
|------|-------------|
| `list_persona_packs` | List all saved persona packs (bundled + user-saved). Returns ID, name, persona count. |
| `get_persona_pack` | Get a specific persona pack by ID. Returns the full persona definitions. |
| `save_persona_pack` | Save a persona pack for reuse. Validates persona data before saving. |

### Instrument Pack Management

| Tool | Description |
|------|-------------|
| `list_instrument_packs` | List installed instrument packs (bundled + user-saved). Returns manifest metadata. |
| `get_instrument_pack` | Load an installed instrument pack by name. Returns the full YAML body. |
| `save_instrument_pack` | Install an instrument pack. Validates the instrument via the parser before writing to disk. |

### Result Management

| Tool | Description |
|------|-------------|
| `list_panel_results` | List all saved panel results. Returns ID, date, model, and counts. |
| `get_panel_result` | Get a specific panel result by ID. Returns the full result with all rounds and synthesis (`detail="full"` by default) — the canonical way to fetch the per-panelist transcript the run tools omit under their default `detail="summary"`. Pass `detail="summary"` for a metadata/synthesis-only peek. |

## Resources (4 URI Patterns)

MCP resources allow agents to read data without invoking a tool.

| URI Pattern | Description |
|-------------|-------------|
| `persona-pack://{pack_id}` | A specific persona pack |
| `persona-pack://` | List all persona packs |
| `panel-result://{result_id}` | A specific panel result |
| `panel-result://` | List all panel results |

## Prompt Templates (3)

Prompt templates provide pre-built research workflows that agents can use as starting points.

| Prompt | Parameters | Description |
|--------|------------|-------------|
| `focus_group` | `topic` (required), `num_personas` (default: 5), `follow_up` (default: true) | Generate a structured focus group discussion prompt for a given topic. |
| `name_test` | `names` (required, comma-separated), `context` (optional) | Test product or feature name options with diverse perspectives. |
| `concept_test` | `concept` (required), `target_audience` (optional) | Test a concept or idea with targeted personas. |

## Response Shape

All panel runs (`run_panel`, `run_quick_poll`, `extend_panel`) return a uniform
response shape. The example below is `detail="full"`:

```json
{
  "result_id": "result-20260410-...",
  "model": "haiku",
  "persona_count": 5,
  "question_count": 3,
  "detail": "full",
  "rounds": [
    {
      "name": "discovery",
      "results": [
        {
          "persona": "Sarah Chen",
          "responses": ["..."],
          "usage": { "input_tokens": 450, "output_tokens": 120 },
          "cost": "$0.0012",
          "error": null
        }
      ],
      "synthesis": { "themes": [...], "summary": "..." }
    }
  ],
  "path": [
    { "round": "discovery", "branch": "themes contains price", "next": "probe_pricing" }
  ],
  "terminal_round": "probe_pricing",
  "warnings": [],
  "synthesis": { "themes": [...], "summary": "...", "recommendation": "..." },
  "poll_summary": { "questions": [...] },
  "per_model_results": { "haiku": { "usage": {...}, "cost": "$0.02", "result_count": 5, "personas": ["Sarah Chen", "..."] } },
  "cost_breakdown": { "by_model": { "haiku": "$0.02" }, "total": "$0.0234" },
  "total_cost": "$0.0234",
  "total_usage": { "input_tokens": 2250, "output_tokens": 600 },
  "results": [...]
}
```

- `rounds` — per-round results with panelist responses and per-round synthesis
- `path` — the routing decisions that fired (v3 branching instruments)
- `terminal_round` — the round whose synthesis fed final synthesis (present on
  every path, including flat `questions` runs where it is `"default"`)
- `warnings` — parser or runtime warnings
- `results` — back-compat flat array mirroring the terminal round's panelist results
- `per_model_results` — per-model `{usage, cost, result_count, personas}`. It does
  **not** duplicate the transcript: the panelist responses live only in
  `rounds[].results` (each row is `model`-tagged, so per-model slices are
  recoverable by filtering). The `models=[...]` ensemble path is the one
  exception — there each model ran independently, so it keeps a `results` block.

For v1/v2 instruments and raw `questions` input, `path` is empty or linear and
`warnings` is typically empty — the shape is uniform across versions.

### `detail`: compact-by-default responses

`run_panel` and `run_quick_poll` accept a **`detail`** argument — `"summary"`
(the default) or `"full"`.

- **`summary`** (default) returns `synthesis`, `panel_verdict`, `poll_summary`,
  `metadata`, `result_id`, costs, `per_model_results` (usage/cost only),
  `warnings`, `path` and `terminal_round`, but **drops the per-panelist
  transcripts** — the top-level `results` mirror and every `rounds[].results`
  list. With caps of `MAX_PERSONAS` (100) × `MAX_QUESTIONS` (50), a full panel's
  transcript can serialise to megabytes; keeping it out of the default response
  protects the agent's context window. The envelope marks the omission
  (`"detail": "summary"`, `"results_omitted": true`, per-round `result_count`)
  and points at the full copy via `transcript_uri` (also
  `panel_verdict.full_transcript_uri`).
- **`full`** returns every panelist row — the pre-existing shape.

The dropped transcript is always retrievable from the persisted result:

```jsonc
{ "tool": "get_panel_result", "arguments": { "result_id": "result-20260410-..." } }
```

or via the `panel-result://{result_id}` resource. `get_panel_result` defaults to
`detail="full"` (back-compat — every existing caller keeps getting the whole
result); pass `detail="summary"` for a cheap metadata/synthesis peek at a large
saved panel.

**Sampling** responses are never persisted (no `result_id`), so their transcript
is not retrievable later — sampling always returns full transcripts regardless of
`detail`. `extend_panel` returns its single appended round in full.

### `max_cost`: hard spend ceiling

`run_panel`, `run_quick_poll`, and `extend_panel` accept an optional
**`max_cost`** argument — a hard ceiling on the run's total spend, in USD. It
is the MCP analog of the CLI's `--max-cost` and wires into the same `CostGate`
machinery (`src/althing/cost.py`): after each panelist completes,
`running_cost / completed_n * total_n` is compared against the ceiling.

```jsonc
// run_panel with a $2 ceiling
{
  "tool": "run_panel",
  "arguments": {
    "pack_id": "general-consumer",
    "questions": [{ "text": "What would you pay for this?" }],
    "max_cost": 2.0,
    "decision_being_informed": "choosing launch tier price"
  }
}
```

When the projection exceeds the ceiling the run **soft-halts**: the current
panelist(s) finish, no new panelists start, and synthesis is skipped (spending
more to summarize a deliberately-truncated panel would produce an
untrustworthy result). The response is still a **valid partial envelope** —
the completed prefix is persisted under `result_id` as usual — carrying:

```jsonc
// response (truncated) after a cost-gate trip
{
  "run_invalid": true,
  "cost_exceeded": true,
  "abort_reason": "cost_exceeded",
  "halted_at_panelist": 4,
  "cost_gate": {
    "max_cost_usd": 2.0,
    "running_cost_usd": 1.62,          // spend so far
    "projected_total_usd": 4.05,       // what tripped the gate
    "completed": 4,
    "total_panelists": 10,
    "halted": true,
    "halted_projection_usd": 4.05
  },
  "resume": {
    "partial_result_id": "result-20260716-...",
    "completed_panelists": ["..."],
    "remaining_personas": ["..."],
    "how_to_resume": "..."             // fetch partial via get_panel_result;
                                       // re-run remaining_personas with a raised cap
  },
  "synthesis": null                    // skipped on the partial
}
```

The partial envelope composes with `detail: "summary"` and still carries the
`panel_verdict` contract fields; the full partial transcript stays retrievable
via `get_panel_result(result_id)`.

Support matrix (mirrors the CLI's `--max-cost`, which the multi-round engine
refuses): `max_cost` applies to **BYOK** runs with inline `questions` (and
`extend_panel`'s single extension round). Combining it with sampling mode (the
host agent pays; the server sees no per-panelist costs), `models` ensembles,
`variants`, or `instrument` / `instrument_pack` inputs returns a typed
`INVALID_TOOL_ARG` on `max_cost` — a loud refusal before any spend, never a
silently unenforced ceiling. `max_cost` must be > 0.

### Typed error envelopes

Boundary errors return a typed `INVALID_TOOL_ARG` envelope
(`{ "error_code", "message", "field_path", "schema_version", "retry_safe",
"error" }`) rather than a raw FastMCP "Error executing tool":

- Unknown `pack_id` (`run_panel`, `run_quick_poll`), `instrument_pack`
  (`run_panel`), or `result_id` (`extend_panel`) → `INVALID_TOOL_ARG` naming the
  offending field and enumerating the available ids.
- `detail` outside `{"summary", "full"}` → `INVALID_TOOL_ARG` on `detail`.
- Unsubstituted `{placeholder}` tokens left in a resolved instrument after
  `vars` is applied (`run_panel`) → `INVALID_TOOL_ARG` on `vars`, naming the
  missing keys and showing an example `vars` payload. This fires even when
  `vars` is omitted entirely, so a placeholder-bearing pack can never send
  literal `{problem}` text to panelists. Passing `vars` alongside plain
  `questions` (no instrument) is likewise an `INVALID_TOOL_ARG` on `vars`.

Runtime failures on `run_panel`, `run_quick_poll`, and `extend_panel` are also
typed:

- Every panelist failing (e.g. a bad model alias that 400s upstream) →
  `{ "run_invalid": true, "total_failure": {...} }`.
- A per-panelist timeout budget exceeded → `{ "error_code": "PANEL_TIMEOUT",
  "retry_safe": true, "timeout_seconds": N }`.

## Model Resolution Order

Two questions are answered at the start of every MCP tool call: **which
execution mode** (sampling vs BYOK) and, in BYOK, **which default
model**. Both are deterministic and observable from the response payload
(`mode` and `model` fields).

Source of truth: `decide_mode()` in `src/althing/mcp/sampling.py`
and `_resolve_mcp_default_model()` in `src/althing/mcp/server.py`.

### Stage 1 — execution mode

| Host advertises `sampling`? | Provider key available? | `use_sampling` arg | Mode |
|------|------|------|------|
| yes | no  | (auto)  | **sampling** |
| yes | yes | (auto)  | **BYOK** — local key wins |
| yes | (any) | `true`  | **sampling** — even when a key is set |
| yes | (any) | `false` | **BYOK** — never sample |
| no  | no  | (auto)  | **error** — set a key OR use a sampling-capable client |
| no  | yes | (auto)  | **BYOK** |
| no  | (any) | `true`  | **error** — host did not advertise `sampling` |
| no  | (any) | `false` | **BYOK** — falls through to a missing-creds error if no key is set |
| (any) | (any) | (auto), with `model="claude-code:*"` / `"codex:*"` | **BYOK** — the subscription CLI needs no key |

"Provider key available" means any of `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `XAI_API_KEY`, `GOOGLE_API_KEY`, `GEMINI_API_KEY`, or
`OPENROUTER_API_KEY` — checked first against the process environment,
then against the on-disk credential store written by `althing login`
(so MCP-launched subprocesses recognise keys the CLI can see). An
explicit subscription CLI model (`claude-code:<model>` /
`codex:<model>`) counts as available credentials: it runs on the
signed-in CLI and needs no key (see
[Subscription CLI models](#subscription-cli-models)).

The auto rule "local key wins over sampling" exists so users who *have*
configured BYOK keep BYOK's full feature set (cross-provider ensembles,
structured-output extraction, deterministic model versioning, per-call
cost telemetry). Pass `use_sampling=true` to force the host's model
even with a key configured.

### Stage 2 — default model (BYOK only)

When `model` is omitted, the server picks a cheap-and-fast default
based on which credential is present. The preference chain is walked in
order; the first match wins:

| Order | Credential | Default alias |
|-------|------------|---------------|
| 1 | `ANTHROPIC_API_KEY` | `haiku` |
| 2 | `OPENAI_API_KEY` | `gpt-4o-mini` |
| 3 | `GEMINI_API_KEY` | `gemini-2.5-flash` |
| 4 | `GOOGLE_API_KEY` | `gemini-2.5-flash` |
| 5 | `XAI_API_KEY` | `grok-3` |
| 6 | `OPENROUTER_API_KEY` | `openrouter/auto` |
| (none) | — | `haiku` (terminal fallback; the LLM client surfaces the missing-creds error) |

**Large-panel fast default (sy-2ag / GH#462 / synthbench#261):** when
the panel has **≥ 10 personas** and `model` was *not* explicitly
supplied, a known-slow auto-resolved default is swapped for a fast
equivalent — today that means `openrouter/auto` →
`openrouter/anthropic/claude-haiku-4.5` (OpenRouter's auto-router can
pick a slow reasoning model, turning a 20-persona panel into a
15-minute stall). The same policy applies on all three surfaces (MCP,
SDK, `panel run`) via the shared `althing.llm.fast_default` module,
and each surface emits a one-line note when the swap fires. An explicit
`model="openrouter/auto"` is always honored — the swap only touches the
implicit default.

Pass `model=` explicitly to override (e.g. `"opus"`, `"gpt-4o"`,
`"gemini-2.5-pro"`). The CLI's weighted-spec syntax
(`haiku:0.25,gpt-4o-mini:0.25`) is **not** supported on the MCP surface
— pass plain aliases. In sampling mode the `model` argument is ignored;
the host agent picks its own model, and the actual model used is
reported back in the response's `model` field.

### Tool coverage

`run_prompt`, `run_panel`, and `run_quick_poll` go through both stages
and accept `use_sampling`. `extend_panel` and the pack/result
management tools always use BYOK and skip Stage 1 — heavier workflows
benefit from direct provider access and structured outputs.

## Sampling Mode

MCP has a spec-level feature called
[**sampling**](https://modelcontextprotocol.io/specification/2026-07-28/client/sampling)
where the server can ask the invoking client to run an LLM completion
on its behalf. althing uses this to deliver a zero-configuration
first-run UX: if you haven't set a provider API key and your client
advertises `sampling`, the `run_prompt`, `run_panel`, and
`run_quick_poll` tools borrow the client's own LLM access instead of
failing.

See [Model Resolution Order](#model-resolution-order) for the full
configuration → mode matrix.

> **Deprecated, and not available in Claude Code.** MCP 2026-07-28
> deprecated sampling (SEP-2577) — it keeps working for at least twelve
> months, with removal no earlier than the first revision after
> 2027-07-28 — and **Claude Code has never implemented the client side**
> ([anthropics/claude-code#1785](https://github.com/anthropics/claude-code/issues/1785)),
> so sampling mode never activates under Claude Code. The
> forward-looking way to run on a subscription is a
> [subscription CLI model](#subscription-cli-models).

### Tradeoffs

Sampling mode is intentionally less capable than BYOK:

- **One provider.** The host agent picks the model (Claude Desktop →
  Claude; other clients may route through whichever provider they have
  configured). Cross-provider ensembles require BYOK.
- **No cost accounting.** Token usage is charged to the host agent's
  subscription; althing returns `"usage": null` and `"cost": null`.
- **Capped panel size.** `run_quick_poll` and `run_panel` are limited
  to 3 personas (and `run_panel` to 5 questions) in sampling mode to
  protect the host agent's context window. Larger runs require BYOK.
- **No structured output extraction.** Free-text only.

These limits keep sampling mode focused on what it's for: a frictionless
first invocation that produces real results, not a replacement for the
research-grade BYOK path.

### Response fields

Sampling responses include two extra fields:

- `"mode"` — `"sampling"` or `"byok"` so downstream tooling can
  condition on the execution mode.
- `"hint"` — a one-line hint on the first sampling run explaining how
  to upgrade to BYOK. Safe to surface to end users.

### Opting in explicitly

Pass `use_sampling=True` (or `use_sampling=False`) to either tool to
override the automatic decision — useful when you have keys configured
but want a quick sampling-mode preview, or when you want to force BYOK
inside a sampling-capable client for reproducibility.

### Tools that support sampling

- `run_prompt` — no persona/question caps, fully supported.
- `run_quick_poll` — up to `SAMPLING_MAX_PERSONAS` (3) personas.
- `run_panel` — up to `SAMPLING_MAX_PERSONAS` (3) personas and
  `SAMPLING_MAX_QUESTIONS` (5) questions; no ensembles, `max_cost`,
  persona variants, or v3 branching instruments, and results are not
  persisted (no `result_id`).

The remaining tools (`extend_panel`, pack/result management) always use
BYOK — heavier workflows benefit from direct provider access and
structured outputs.

### Protocol eras (spec 2026-07-28)

althing speaks both MCP protocol eras via the Python SDK 2.0, and the
sampling *experience* is identical on each — the wire mechanics differ:

- **Handshake era (protocol ≤ 2025-11-25):** the server sends a
  standalone `sampling/createMessage` server→client request mid-call —
  the classic sampling flow, unchanged.
- **Modern era (protocol 2026-07-28, negotiated via `server/discover`):**
  the revision defines *no* server→client requests, so the tool call
  instead returns an `input_required` result (SEP-2322) carrying the
  batched `CreateMessageRequest`s plus sealed `request_state`. The
  client fulfils them through its ordinary sampling callback and
  retries the call; SDK clients (`mcp.client.client.Client`) drive this
  loop automatically. A panel with synthesis takes two rounds: one
  batching all persona requests, one for the synthesis call.

Because the modern retry loop re-invokes the tool on the same server
process, sealed `request_state` is bound to the server's per-process
key: a server restart mid-round invalidates the round and the client
simply re-asks.

**Capability advertisement change (SDK 2.0):** earlier releases
injected a `sampling` key into the initialize response's
`ServerCapabilities` (under `experimental`, and — off-spec — at the top
level). The MCP spec defines sampling as a *client* capability, SDK
2.0's public stdio runner exposes no initialization-option hook, and
the 2026-07-28 `server/discover` flow never consults initialize options
at all, so the advertisement is gone. Whether sampling is used is
decided per call by probing the client's declared `sampling` capability
(from the `initialize` handshake on legacy connections, or the
per-request `_meta` envelope on modern ones).

## Subscription CLI models

`claude-code:<model>` and `codex:<model>` route each completion through a
locally installed, signed-in agent CLI in headless mode (`claude -p` /
`codex exec`), drawing on your Claude or ChatGPT subscription instead of
an API key. Unlike sampling it works under any MCP host (including
Claude Code), on the CLI and SDK, and has no persona/question caps or
structured-output restriction:

```jsonc
// run_quick_poll arguments
{ "question": "...", "pack_id": "...", "model": "claude-code:haiku" }
```

Each call is an isolated, tool-less completion — the CLI's agent prompt
is replaced by the persona prompt, tools / MCP servers / user settings
are disabled, nothing is persisted, and the process runs in an empty
scratch directory. The CLI must be installed and signed in on the
machine running `althing mcp-serve`.

Tradeoffs: no `temperature` / `top_p` / `seed` / `max_tokens` control,
text-only content, throughput bounded by subscription usage limits
(surfaced as rate limits), and cost figures that are API list-price
estimates rather than bills. See the
README's "Subscription CLI models" section for the environment knobs
(`ALTHING_CLI_MAX_CONCURRENT`, `ALTHING_CLI_TIMEOUT`,
`ALTHING_CLAUDE_CODE_ALLOW_API_KEY`, …).

## Host Integration Flags

### `ALTHING_DRIFT_DEGRADE`

Controls what the server returns when the structured-output engine's 3-strike
retry budget exhausts (see the `sp-d1x0` retry policy).

| Setting | v1.0.0 default | v1.1.0 default | Behavior on exhaustion |
|---|---|---|---|
| unset / `0` | ✓ | — | Typed `SCHEMA_DRIFT` error envelope, `retry_safe: true` |
| `1` | — | ✓ | Degraded `panel_verdict.json` with `flags: [{ "code": "schema_drift", "severity": "warn" }]` |

**Semantics.** The `1` setting is the v1.1 default rolled out as an opt-in
beta in v1.0. The panel ran; the partial signal is returned with a flag the
agent can branch on. The off-by-default setting in v1.0 returns a typed error
instead, leaving the recovery decision to the caller.

**Rollback.** This is a runtime env flag — change it and restart the
server. No persisted state. To roll back from `1` to off, unset the env var
on the next launch.

**v1.1.0 migration.** The default flips to on. If your agent code relies on
seeing `SCHEMA_DRIFT` errors today, switch to checking
`flags[].code == "schema_drift"` before the v1.1 cutover. The CHANGELOG entry
for v1.1.0 will mark the flip.

Set in your MCP `env` block alongside provider keys:

```json
{
  "mcpServers": {
    "althing": {
      "command": "althing",
      "args": ["mcp-serve"],
      "env": {
        "ANTHROPIC_API_KEY": "sk-...",
        "ALTHING_DRIFT_DEGRADE": "1"
      }
    }
  }
}
```

See [docs/response-contract.md](response-contract.md) for the full envelope
shape and [docs/migration-v1.md](migration-v1.md) for the grace-window state
diagram.

## Data Storage

Panel results, persona packs, and instrument packs are stored under `~/.althing/` (configurable via `SYNTH_PANEL_DATA_DIR`):

```
~/.althing/
├── persona_packs/          # Saved persona packs (YAML)
├── packs/instruments/      # Installed instrument packs (YAML)
└── results/                # Panel results (JSON) + session data
```

## Troubleshooting

### `command not found: althing`

The MCP host can't see the `althing` binary on its `PATH`. This is
the single most common failure mode, because MCP subprocesses inherit
the host's PATH — not your shell's.

**Fixes, in order of preference:**

1. Install globally so any host can find it:
   ```bash
   pip install "althing[mcp]"      # or pipx install "althing[mcp]"
   which althing                    # confirm the binary is on PATH
   ```
2. Or point `command` at the absolute binary path:
   ```jsonc
   "command": "/Users/you/.venv/bin/althing"
   ```
3. Or run it through `uvx` so the host fetches it on demand:
   ```jsonc
   "command": "uvx",
   "args": ["--from", "althing[mcp]", "althing", "mcp-serve"]
   ```

Claude Desktop on macOS is particularly strict about PATH — it runs
under launchd and does not inherit your shell environment. Use an
absolute path or `uvx` for that host.

### Server starts but no tools appear

The server launched but the host isn't seeing `run_panel`,
`run_quick_poll`, etc. in the tool picker.

- **MCP extra missing.** `pip install althing` alone is not enough —
  the MCP server requires the SDK from the `[mcp]` extra:
  ```bash
  pip install "althing[mcp]"
  ```
- **Stale host cache.** Some hosts cache the tool list per server entry.
  Restart the host (full quit, not just window close) after editing
  config or upgrading the package.
- **Server logs.** Run `althing mcp-serve` in a terminal to confirm
  it boots and produces no errors before the host launches it.

### Missing or invalid API key

Symptom: tool calls return `MISSING_CREDS` or a provider-specific
401/403 error.

- The MCP subprocess only sees env vars from the host's `env` block (or
  the host's inherited environment). Setting `ANTHROPIC_API_KEY` in
  your shell profile does **not** automatically propagate — put it in
  the `env` block of the MCP server entry.
- Run `althing login` to seed the on-disk credential store; the MCP
  server reads from there as a fallback when the env is empty (see
  [Model Resolution Order](#model-resolution-order)).
- To run without a key, pass a subscription CLI model
  (`model="claude-code:haiku"` or `"codex:"`) — it uses the signed-in
  CLI on the server machine; see
  [Subscription CLI models](#subscription-cli-models). If your host
  advertises MCP `sampling` (**not** Claude Code), you can also omit
  the key and althing will borrow the host's LLM access — see
  [Sampling Mode](#sampling-mode).

### Timeouts on long panel runs

Symptom: the host kills the subprocess mid-panel with a timeout error.

- A 5-persona × 3-question BYOK panel typically takes 30–90 seconds.
  Cross-provider ensembles and larger personas can take 2–5 minutes.
- Raise the host's per-tool timeout. For Hermes:
  ```yaml
  mcp_servers:
    althing:
      timeout: 300         # 5 min for heavy panels
      connect_timeout: 60
  ```
  Other hosts use their own field names; consult the host's MCP docs.
- For exploratory work, prefer `run_quick_poll` (single question) over
  `run_panel` (full instrument) — it returns in seconds.

### Tool calls fail with `MISSING_DECISION`

Every panel-class tool (`run_panel`, `run_quick_poll`, `extend_panel`)
**requires** a `decision_being_informed` string (12–280 chars). This
is a frozen v1.0.0 contract requirement. See
[Frozen v1.0.0 Contract](#frozen-v100-contract) for the rule and
[docs/response-contract.md](response-contract.md) for the error
envelope. `run_prompt` does not require it.

### `SCHEMA_DRIFT` errors on synthesis

The structured-output engine's 3-strike retry budget was exhausted.
Either re-run the tool (transient model output drift is recoverable),
or set `ALTHING_DRIFT_DEGRADE=1` in the MCP `env` block to get a
degraded result with a `schema_drift` flag instead of an error — see
[Host Integration Flags](#host-integration-flags).
