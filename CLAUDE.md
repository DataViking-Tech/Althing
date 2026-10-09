# althing

A lightweight, LLM-agnostic research harness for running synthetic focus groups using AI personas.

## Architecture

Pure Python 3.10+ with minimal dependencies (`httpx` for HTTP, `pyyaml` for YAML parsing). Optional deps: `mcp` (MCP server).

```
src/althing/
├── llm/                  # Provider-agnostic LLM client
│   ├── client.py         # Unified send/stream interface
│   ├── aliases.py        # Model alias resolution (sonnet → claude-sonnet-4-6)
│   ├── errors.py         # Error types and retry logic
│   ├── models.py         # Data models (CompletionRequest, CompletionResponse, etc.)
│   └── providers/        # Provider implementations
│       ├── anthropic.py  # Claude
│       ├── openai_compat.py  # OpenAI, local models
│       ├── xai.py        # Grok
│       └── gemini.py     # Google Gemini
├── runtime.py            # Agent session loop (turns, tool calls, compaction)
├── orchestrator.py       # Parallel panelist execution (ThreadPoolExecutor)
├── structured/           # Schema-validated responses via tool-use forcing
├── cost.py               # Token tracking, model pricing, budget enforcement
├── persistence.py        # Session save/load/fork (JSON + JSONL)
├── plugins/              # Manifest-based extension system with hooks
├── instrument.py         # v1/v2/v3 instrument parser + DAG validator
├── routing.py            # v3 router predicates (contains/equals/matches)
├── mcp/                  # MCP server (12 tools, stdio transport)
│   ├── server.py         # MCP server entry point
│   └── data.py           # Persona + instrument pack and result persistence
├── packs/instruments/    # 5 bundled v3 branching instrument packs
├── cli/                  # CLI framework
│   ├── parser.py         # argparse setup
│   ├── commands.py       # Subcommand handlers (prompt, panel run)
│   ├── repl.py           # Interactive REPL
│   ├── slash.py          # Slash command registry
│   └── output.py         # Output formatting (text, json, ndjson)
└── main.py               # Entry point
```

## Key Commands

```bash
# Single prompt
althing prompt "Say hello"

# Full panel run (file path or installed pack name)
althing panel run --personas examples/personas.yaml --instrument examples/survey.yaml
althing panel run --personas examples/personas.yaml --instrument pricing-discovery

# v3 branching: list / show / install / graph instrument packs
althing instruments list
althing instruments graph pricing-discovery --format mermaid

# MCP server (stdio, for Claude Code / Cursor / Windsurf)
althing mcp-serve

# With specific model
althing prompt "Hello" --model haiku
althing prompt "Hello" --model gemini

# Output formats
althing prompt "Hello" --output-format json
althing prompt "Hello" --output-format ndjson
```

## Development

```bash
# Setup (uses uv)
uv venv .venv && source .venv/bin/activate
uv pip install -e ".[dev,mcp]"

# Or without install
PYTHONPATH=src python3 -m althing prompt "Hello"

# Run tests
pytest tests/

# Run acceptance tests (requires API key)
ANTHROPIC_API_KEY=sk-... pytest tests/test_acceptance.py -m acceptance
```

## Provider Configuration

Set the appropriate environment variable:

| Provider | Variable | Default Model |
|----------|----------|---------------|
| Anthropic | `ANTHROPIC_API_KEY` | claude-sonnet-4-6 |
| OpenAI | `OPENAI_API_KEY` | (must specify --model) |
| xAI | `XAI_API_KEY` | grok-3 |
| Google | `GOOGLE_API_KEY` or `GEMINI_API_KEY` | gemini-2.5-flash |
| Claude Code CLI | none (signed-in `claude`) | explicit only: `claude-code:<model>` |
| Codex CLI | none (signed-in `codex`) | explicit only: `codex:<model>` |

Subscription CLI providers live in `llm/providers/subscription_cli.py` and
register *before* Anthropic in `_PROVIDER_REGISTRY` (`claude-code:` would
otherwise match `claude-`). They never satisfy the credential fallback.

## YAML Formats

### Personas
```yaml
personas:
  - name: Sarah Chen
    age: 34
    occupation: Product Manager
    background: "Works at a mid-size SaaS company..."
    personality_traits: [analytical, pragmatic, detail-oriented]
```

### Instruments
```yaml
instrument:
  version: 1              # Schema version (default: 1 if omitted)
  questions:
    - text: "What frustrates you about your workflow?"
      response_schema:
        type: text
      follow_ups:
        - "Can you describe a specific example?"
```

### v3 Branching Instruments (0.5.0)
```yaml
instrument:
  version: 3
  rounds:
    - name: discovery
      questions:
        - text: "What's the most frustrating part?"
      route_when:
        - if: { field: themes, op: contains, value: price }
          goto: probe_pricing
        - else: __end__
    - name: probe_pricing
      questions:
        - text: "What would feel fair to pay?"
```

**R3 caveat (theme matching):** `route_when` predicates compare against
the *exact* strings the synthesizer emits. Always prefix v3 instruments
with a comment block listing canonical theme tags so the synthesizer
prefers them — otherwise routes silently fall through to `else`. See
the README "Theme Matching" section and any bundled pack
(`src/althing/packs/instruments/`) for the pattern.

Predicate ops: `contains` (substring), `equals` (exact), `matches`
(regex). Targets: any round name or the reserved `__end__` sentinel.
`else` clause is mandatory as the last entry.

`panel run` dispatches multi-round (v2/v3) instruments through
`run_multi_round_panel` — the same router-driven engine as the MCP/SDK
surfaces — and emits the rounds-shaped envelope with the real routing
`path`. Single-round-only features (checkpointing/`--resume`,
convergence/`--auto-stop`, `--max-cost`, `--question-failure-budget`,
`--variants`, ensemble `--models`/`--blend`, map-reduce synthesis,
`--no-synthesis` on branching instruments) are refused loudly up front;
see the README "CLI Flag Support for Multi-Round Runs" table.

## MCP Integration

The MCP server exposes 12 tools: `run_prompt`, `run_panel`, `run_quick_poll`, `extend_panel`, `list_persona_packs`, `get_persona_pack`, `save_persona_pack`, `list_instrument_packs`, `get_instrument_pack`, `save_instrument_pack`, `list_panel_results`, `get_panel_result`.

`run_panel` accepts `instrument` (inline dict) or `instrument_pack` (name) for v3 branching runs. `extend_panel` appends one ad-hoc round to a saved panel result — it is not a re-entry into the authored DAG.

**`extend_panel` vs branching (architect note 3):** `extend_panel` always
appends exactly **one** ad-hoc round on top of a saved panel result, reusing
each panelist's saved session. It is **not** a re-entry into the authored v3
DAG — the original instrument's `route_when` clauses are not consulted, no
routing decision is made, and the result's `path` grows by one extension
entry. If you want adaptive branching, run a fresh `run_panel` against a v3
instrument (with `route_when` clauses) instead. The boundary is intentional:
`extend_panel` is for human-in-the-loop follow-ups; v3 routing is for
agent-driven, instrument-authored adaptivity.

Claude Code plugin: install via `/plugin install althing`. Adds `/focus-group` skill.

Standalone MCP config for any editor:
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

## Clean-Room Implementation

This codebase was built via clean-room methodology from a functional specification (SPEC.md). No code was copied from any reference implementation. The spec describes behavioral contracts only — all naming, structure, and implementation are original.

## Conventions

- All modules use dataclasses for data models
- Errors use a category enum (retryable vs non-retryable)
- Provider detection is prefix-based (claude-* → Anthropic, gemini-* → Google, grok-* → xAI)
- Cost tracking is per-turn with 4 token buckets (input, output, cache_write, cache_read)
- MCP mode defaults to haiku (cheap/fast for iterative use); CLI defaults to sonnet
