# Agent Quickstart

The fastest path from zero to a structured, machine-readable panel result —
written for AI agents (Claude Code, Hermes, Cursor/Windsurf-style hosts, generic
MCP clients) and the humans who wire them up.

This is the **happy path**, end to end: install → verify → dry-run → run a small
panel → run a 30–40 persona poll → save → emit JSON. Two surfaces drive the same
engine — pick the one that matches your runtime:

- **[CLI path](#cli-path)** — shell agents, CI, terminals.
- **[MCP path](#mcp-path)** — editor and framework agents that call tools.

Every command and number below was captured on a recent `althing` release
(the exact version and Python patch your machine prints will differ). Costs are the
CLI's own pre-flight estimates (Haiku pricing) — your run will print the actual
spend.

> **Position results honestly.** A synthetic panel is a **preflight**, not
> market validation. See [Synthetic preflight, not
> validation](#synthetic-preflight-not-validation) before you act on a result —
> this matters most for agents, which will otherwise present the recommendation
> as fact.

---

## CLI path

### 1. Install

Pick the install that matches how you use Python tools:

```bash
pip install althing              # in your project venv (library + CLI)
pip install 'althing[mcp]'       # also want the MCP server
pipx install althing             # global, isolated, on your PATH
uvx --from althing althing --help   # zero-install, run once
```

The PyPI distribution and CLI are spelled **`althing`** (one word); the
importable module is **`althing`** (snake_case). Both resolve to the same
code.

### 2. Verify the install (no key needed)

Run the preflight *before* configuring a provider. `--install-only` exits `0`
when the package, deps, and bundled packs are healthy — even with no credentials
— so it's the canonical post-install smoke test:

```bash
althing --version              # entry point dispatches
althing doctor --install-only  # install health only — no key required
```

```text
althing 1.5.7
  ✓ python: 3.12.7 (>= 3.10)
  ✓ required deps: httpx, pyyaml
  ✓ optional: mcp installed
  ! credentials: none configured (install-only mode — run `althing login` before running a panel).
  ✓ checkpoint root: ~/.althing/checkpoints (writable, 0 existing runs)
  ✓ packs: 14 persona, 8 instrument (bundled)
1 warning, 0 errors.
```

For machine consumption, the JSON form separates `install_ok`,
`credential_configured`, and `checks_ok` so an agent or CI step can branch on
each surface independently:

```bash
althing --output-format json doctor --install-only
```

### 3. Configure a provider

Bring your own LLM key — Claude, OpenAI, Gemini, xAI, or any OpenAI-compatible
provider. Export it, or persist it once:

```bash
export ANTHROPIC_API_KEY="sk-..."
# or store it (written to ~/.config/althing/credentials.json, mode 0600):
althing login --provider anthropic --api-key sk-...

althing whoami          # which providers now resolve
althing doctor          # full preflight: install + credentials (exits 1 if none)
```

### 4. Dry-run first

`--dry-run` validates personas + instrument and prints the panel shape, call
count, and a cost estimate — **without making any LLM calls**. Always dry-run
before spending tokens, especially in autonomous loops:

```bash
althing panel run \
  --personas examples/personas.yaml \
  --instrument examples/survey.yaml \
  --model haiku \
  --dry-run
```

```text
DRY RUN — no LLM calls will be made
Model: haiku
Personas: 3
Questions: 2
Panel: 3 personas x 2 questions = 6 LLM calls
Estimated cost (haiku): ~$0.0096
Validation: OK
```

### 5. Run a small panel

Drop `--dry-run` to execute. The chosen model is printed to stderr before the
run so you can cancel and override:

```bash
althing panel run \
  --personas examples/personas.yaml \
  --instrument examples/survey.yaml \
  --model haiku
```

You get a per-panelist transcript plus a `synthesis` block (summary, themes,
agreements, disagreements, surprises, and a single `recommendation` line).

### 6. Scale to a 30–40 persona poll

Bigger panels surface segment splits a 3-person panel hides. The largest bundled
pack is 20 personas, so reach 30–40 by exporting two bundled packs and merging
them with `--personas-merge` (duplicate names are de-duped, later file wins, and
a warning names every drop):

```bash
althing pack list                                   # 15 bundled packs, with counts
althing pack export product-research -o pr.yaml     # 20 personas
althing pack export broad-professionals -o bp.yaml  # 20 personas

althing panel run \
  --personas pr.yaml \
  --personas-merge bp.yaml \
  --instrument examples/survey.yaml \
  --model haiku \
  --dry-run
```

```text
DRY RUN — no LLM calls will be made
Personas: 40
Panel: 40 personas x 2 questions = 80 LLM calls
Estimated cost (haiku): ~$0.1279
Validation: OK
```

Drop `--dry-run` to run it. Use `--max-cost 0.50` to hard-cap spend and
`--max-concurrent N` to tune throughput.

### 7. Save and emit JSON

`--save` writes the full result to `~/.althing/results`; the global
`--output-format json` flag emits the same machine-readable envelope you'd get
from the MCP `run_panel` tool:

```bash
althing --output-format json panel run \
  --personas pr.yaml \
  --personas-merge bp.yaml \
  --instrument examples/survey.yaml \
  --model haiku \
  --save > result.json

althing results list                 # what's saved on disk
althing report <result-id>            # render a shareable Markdown report
```

The JSON envelope (abridged):

```jsonc
{
  "result_id": "result-20260524-abc123",
  "model": "claude-haiku-4-5",
  "synthesis": {
    "summary": "...",
    "themes": [...],
    "agreements": [...],
    "disagreements": [...],
    "surprises": [...],
    "recommendation": "..."        // the headline call your agent acts on
  },
  "rounds": [{ "name": "default", "results": [...], "synthesis": null }],
  "total_cost": "$0.1312",
  "total_usage": { "input_tokens": 7860, "output_tokens": 9210, ... }
}
```

Read `synthesis.recommendation` for the call, `synthesis.disagreements` for
dissent, and `rounds[].results[]` for the per-panelist transcript. The full
envelope is documented in
[response-contract.md](response-contract.md).

---

## MCP path

Editor and framework agents call Althing as MCP tools instead of shelling
out. The server launches on demand over stdio — no long-running process.

### 1. Register the server

The `mcp install` helper writes the config entry for you (Claude Code user scope
by default; `--target` for other hosts):

```bash
althing mcp install                                   # ~/.claude.json (all projects)
althing mcp install --scope project                   # ./.mcp.json (checked in, shared)
althing mcp install --target ~/.cursor/mcp.json       # Cursor
althing mcp install --dry-run                         # print JSON, change nothing
```

Or write the config by hand. The generic stdio entry every MCP host
understands:

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

Host-specific snippets — Claude Code, Cursor, Windsurf, Zed (uses
`context_servers`), Hermes (YAML with explicit timeouts), and Claude Desktop —
are in the README's [Use with Claude Code / Cursor / Windsurf /
Zed](../README.md#use-with-claude-code--cursor--windsurf--zed) section. If
`althing` lives in a virtualenv, point `command` at its absolute path
(e.g. `/path/to/.venv/bin/althing`).

### 2. Call the research tools

Every panel-running tool requires a `decision_being_informed` field (12–280
chars, single line) — the panel **won't run without one**. This keeps results
anchored to a real decision instead of free-floating opinion.

```jsonc
// run_panel — full synthetic focus group against a bundled pack + instrument
{
  "tool": "run_panel",
  "arguments": {
    "pack_id": "product-research",
    "instrument_pack": "pricing-discovery",
    "decision_being_informed": "choosing launch tier price"
  }
}

// run_quick_poll — one question across a persona pack
{
  "tool": "run_quick_poll",
  "arguments": {
    "question": "Would you pay $79/month for this product?",
    "pack_id": "product-research",
    "decision_being_informed": "validating the $79 ceiling"
  }
}
```

`run_panel` returns the same envelope as the CLI's `--output-format json`. The
server exposes 12 tools in total (run/poll/extend, plus persona and instrument
pack management, and saved-result retrieval) — see
[mcp.md](mcp.md) for the full schemas, resources, and prompt templates.

> `extend_panel` appends **one** ad-hoc follow-up round to a saved result; it is
> **not** a re-entry into a v3 instrument's `route_when` DAG. For adaptive
> branching, run a fresh `run_panel` against a v3 instrument pack.

---

## Structured output (machine-readable, no prose parsing)

Free-text answers force every consumer into a regex it will regret. For agents,
**always pass a bounded `response_schema`** so each panelist returns parsed JSON
at generation time. The schema lives on the question in the instrument:

```yaml
# pricing-poll.yaml
instrument:
  version: 1
  questions:
    - text: "Would you pay $79/month for this product?"
      response_schema:
        type: enum                              # forced-choice: model emits exactly one
        options: ["Yes", "No", "Only with a discount"]
    - text: "How likely are you to recommend it? (1-5)"
      response_schema:
        type: scale                             # Likert / rating
        min: 1
        max: 5
```

```bash
althing --output-format json panel run \
  --personas pr.yaml \
  --instrument pricing-poll.yaml \
  --model haiku \
  --save > poll.json
```

When `response_schema` is set, each panelist's `response` is a parsed **dict**
(not prose) and `"structured": true` is attached so consumers can branch on it.
For a deterministic vote/score rollup — no LLM, no parsing — run `poll-summary`
on the saved result:

```bash
althing poll-summary <result-id> --format json
althing poll-summary <result-id> --segment-by occupation   # split the vote by attribute
```

`response_schema` types are `enum` (pick-one), `scale` (Likert/rating), `text`
(free), and `tagged_themes` (multi-select from a fixed list). For free-text
questions where you still want structure, add a post-hoc `--extract-schema`
pass. The full pattern catalogue — with a runnable 35-persona prioritization
example — is in [structured-polling.md](structured-polling.md). Pydantic-backed
typed extraction is covered in the README's [Typed extraction with
Pydantic](../README.md#typed-extraction-with-pydantic-104) section.

---

## Synthetic preflight, not validation

Synthetic panels are cheap and fast (pennies and seconds vs. $5k–$15k and
weeks), which makes them excellent for the *preflight* steps of research — and a
poor substitute for the real thing. Use them to:

- **pre-screen** an instrument before spending budget on real participants,
- **iterate** on names, copy, and positioning at the speed of thought,
- **generate hypotheses** across demographic segments,
- **concept-test** before a build commitment.

They do **not** replace real user research, and a result is **not** market
validation. Personas are LLM constructs, not customers; agreement among them is
evidence of *internal coherence*, not of what the market will do. When an agent
surfaces a recommendation, present it as *"a synthetic panel suggests X — worth
validating with N real users"*, never as *"users want X."* Treat
`synthesis.recommendation` as a prior to test, not a verdict to ship.

---

## Next steps

- [response-contract.md](response-contract.md) — the full result envelope.
- [structured-polling.md](structured-polling.md) — every structured-output pattern.
- [mcp.md](mcp.md) — all 12 MCP tools, resources, and prompts.
- [methodology.md](methodology.md) — how synthesis works and its limits.
- [recommended-models.md](recommended-models.md) — which model for which task.
