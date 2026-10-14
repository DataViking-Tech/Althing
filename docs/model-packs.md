# Model Packs — Topic-Appropriate Model Configurations for Agents

> **Audience:** AI agents (and the humans configuring them) deciding which
> model or model mix to run a panel through. Persona packs answer *"who
> answers the question?"* — model packs answer *"which LLMs do the
> answering?"*.
>
> **Looking for the task → pack + model lookup table?** See
> [docs/task-recommendations.md](task-recommendations.md) — that page maps
> common research tasks (positioning, GTM, pricing, trust audit, etc.)
> to the persona pack *and* model pack that match. This page is the
> deeper reference for the model packs themselves.

## TL;DR

| Pack name | Use when | Concrete `--models` value |
|---|---|---|
| [`fast-cheap-preflight`](#fast-cheap-preflight) | Smoke test, early ideation, dry-run shape check | single `haiku` (or `gpt-4o-mini`) |
| [`balanced-research-ensemble`](#balanced-research-ensemble) | Default for "real" research before you act on the answer | `haiku,sonnet,gemini-2.5-flash` (3-way ensemble) |
| [`skeptical-red-team-ensemble`](#skeptical-red-team-ensemble) | Trust / safety / credibility / buyer-objection probing | `sonnet,gpt-4o,gemini-2.5-pro` (ensemble, higher-rigor) |
| [`consumer-mass-appeal`](#consumer-mass-appeal) | Broad consumer panels — pricing, naming, ad copy | `haiku:0.34,gpt-4o-mini:0.33,gemini-2.5-flash:0.33` (weighted split) |
| [`enterprise-buyer-research`](#enterprise-buyer-research) | B2B procurement, security/IT decisions, RFP framing | `sonnet:0.5,gpt-4o:0.5` (weighted split, quality-leaning) |
| [`technical-founder-research`](#technical-founder-research) | Developer-tool feedback, technical positioning | `sonnet:0.4,gpt-4o:0.4,gemini-2.5-pro:0.2` |
| [`high-stakes-validation`](#high-stakes-validation) | Pre-launch, go/no-go, board-deck-ready evidence | `opus,sonnet,gpt-4o,gemini-2.5-pro` (4-way ensemble + `--blend`) |

> **Status (sy-8b4):** Model packs are documented presets — concrete
> `--models` strings you copy into the existing flag — not yet a first-class
> CLI flag. The runnable surface today is the `--models` syntax (`docs/ensemble.md`)
> and the MCP `models: []` parameter (`docs/mcp.md`). A future `--model-pack
> <name>` shortcut will compile to the same strings.
>
> **Want the data to pick?** `--model auto` (CLI) or `model="auto"` (MCP and
> SDK) chooses a model or weighted blend from SynthBench results for the
> panel's respondent group and question topic. See
> [model-routing.md](model-routing.md). The packs below are fixed presets
> for when you want to choose the mix yourself.
>
> **Multi-round instruments.** `--blend` and `--max-cost` only work with
> single-round instruments. The bundled packs `pricing-discovery`,
> `landing-page-comprehension`, `feature-prioritization`, `name-test` and
> `churn-diagnosis` are multi-round, so the `--blend` examples below use
> single-round instruments (`market-research`, `product-feedback`). A
> weighted `--models a:0.5,b:0.5` split works with either kind.

## Why model packs exist

Althing's [SynthBench](https://synthbench.org) leaderboard tracks model
fidelity against real human distributions (GlobalOpinionQA and friends). Two
findings from the live data drive these packs:

1. **No single model is best at everything.** Which model leads changes
   by dataset and by topic, and the rankings move as new models are
   added. Check the live [leaderboard](https://synthbench.org) or
   `https://synthbench.org/data/leaderboard.json` for current numbers
   instead of relying on a list in these docs.
2. **Blends of models from different families rank high.** On the
   leaderboard snapshot bundled with this release (October 2026), a blend
   of models from several families scores above every single model on
   each of the four core datasets. The trade-off is cost and latency: an
   ensemble runs the full panel once per model.

The corollary an agent should internalize:

> **A single cheap model is fine for "what direction does this lean?".
> It is not adequate evidence for "should we ship this?".**

Agents should pick a model pack with care equivalent to picking a persona
pack. Calling [`run_panel`](mcp.md) with only `gpt-4o-mini` and treating the
output as "what users think" is the model-pack analogue of running a panel
with only male software engineers.

---

## The seven packs

Each pack lists:

- **When to use it** — the decision shape the pack is calibrated for.
- **CLI** — drop-in `althing panel run` invocation.
- **MCP / SDK** — equivalent agent invocation (the `models` array passed to
  the [`run_panel`](mcp.md) tool, or `persona_models=` from the
  [Python SDK](../README.md#use-as-a-python-library)).
- **What you get** — cost order of magnitude and what the response metadata
  will report.

### fast-cheap-preflight

**When to use:** smoke-testing a new instrument, dry-running persona shape,
sanity-checking the question wording. Treat the answer as directional only —
this pack is for "is this question even legible?" not "what would users do?".

**CLI:**

```bash
althing panel run \
  --personas examples/personas.yaml \
  --instrument pricing-discovery \
  --var problem="choosing a project management tool" \
  --model haiku \
  --output-format json
```

**MCP / SDK:**

```jsonc
// MCP run_panel
{
  "instrument_pack": "pricing-discovery",
  "vars": {"problem": "choosing a project management tool"},
  "pack_id": "general-consumer",
  "model": "haiku",
  "decision_being_informed": "early ideation, not a launch decision"
}
```

```python
# Python SDK
from althing import run_panel
panel = run_panel(pack_id="general-consumer", instrument_pack="pricing-discovery", model="haiku")
```

**What you get:** ~$0.01–$0.03 for a 15-persona panel.
`metadata.cost.per_model` will have a single entry — this is the
single-model-panel signature. Treat the result as directional only; see
[Agent Guidance](#agent-guidance) below.

---

### balanced-research-ensemble

**When to use:** the default for any panel whose result will inform a
decision an agent or human is actually going to act on. Three families, full
panel per model.

**CLI:**

```bash
althing panel run \
  --personas examples/personas.yaml \
  --instrument market-research \
  --var problem="choosing a project management tool" \
  --models 'haiku,sonnet,gemini-2.5-flash' \
  --blend \
  --output-format json
```

**MCP / SDK:**

```jsonc
// MCP run_panel
{
  "instrument_pack": "market-research",
  "vars": {"problem": "choosing a project management tool"},
  "pack_id": "general-consumer",
  "models": ["haiku", "sonnet", "gemini-2.5-flash"],
  "decision_being_informed": "choosing launch tier price"
}
```

**What you get:** 3× the cost and latency of single-model (~$0.05–$0.15 for
15 personas). On the CLI, `--blend` adds a `blend` block with the weighted
response distribution per question and each model's own distribution
(`per_model`). Over MCP, `models` returns per-model results
(`per_model_results`) and no `blend` block; compare the models yourself.
`metadata.cost.per_model` will have three buckets (one per family).

---

### skeptical-red-team-ensemble

**When to use:** when the question is "would they trust us / believe us /
buy this", and the cost of false positives is high. Bumps quality, drops the
cheapest tier — these models are less prone to agreement-bias and surface
more objections.

**CLI:**

```bash
althing panel run \
  --personas examples/personas.yaml \
  --instrument product-feedback \
  --var product="Acme, a deploy tool: ship faster with automated deploys. Start free, no card required." \
  --models 'sonnet,gpt-4o,gemini-2.5-pro' \
  --blend \
  --temperature 0.9 \
  --output-format json
```

To run the multi-round `landing-page-comprehension` pack with this model
set, drop `--blend` (the CLI refuses it for multi-round instruments) or use
a weighted split such as `--models 'sonnet:0.34,gpt-4o:0.33,gemini-2.5-pro:0.33'`.

**MCP / SDK:**

```jsonc
// MCP run_panel
{
  "instrument_pack": "landing-page-comprehension",
  "pack_id": "enterprise-buyer",
  "models": ["sonnet", "gpt-4o", "gemini-2.5-pro"],
  "temperature": 0.9,
  "decision_being_informed": "credibility audit before paid acquisition"
}
```

**What you get:** ~10–20× the cost of `fast-cheap-preflight`. Higher
`temperature` and the quality-leaning model set produce noisier but more
critical responses — the `synthesis.disagreements` and `synthesis.surprises`
blocks are the payload to read first.

---

### consumer-mass-appeal

**When to use:** broad B2C decisions — naming, ad copy, packaging, pricing
for a non-technical product. Weighted split across three cheap models in
three families gets you cross-provider diversity at preflight prices.

**CLI:**

```bash
althing panel run \
  --personas examples/personas.yaml \
  --instrument 'name-test' \
  --var candidates="Core, Plus, Pro" \
  --models 'haiku:0.34,gpt-4o-mini:0.33,gemini-2.5-flash:0.33' \
  --output-format json
```

**MCP / SDK:** the MCP server's `models: []` parameter only supports
unweighted ensemble. To get weighted assignment, set the model per persona
in the persona pack YAML (`model:` field) or via `persona_models=` in the
Python SDK:

```python
panel = run_panel(
    pack_id="general-consumer",
    instrument_pack="name-test",
    persona_models={
        "Maya Chen": "haiku",
        "Derek Washington": "gpt-4o-mini",
        "Priya Sharma": "gemini-2.5-flash",
        # ... assign the remaining personas across the three models
    },
)
```

**What you get:** each persona answers once, but the panel as a whole spans
three families. ~3× cheaper than `balanced-research-ensemble` and ~3× more
diverse than `fast-cheap-preflight`. **No `--blend` block** — there's
nothing to blend, each persona only answered once.

---

### enterprise-buyer-research

**When to use:** B2B research where the buyer is procurement, IT, security,
or compliance — segments that are under-represented in cheap models'
training data. Skip the cheapest tier; lean on the two strongest
general-purpose models.

**CLI:**

```bash
althing panel run \
  --personas enterprise-buyer \
  --instrument 'feature-prioritization' \
  --var features="SSO, audit logs, and role-based access control" \
  --models 'sonnet:0.5,gpt-4o:0.5' \
  --output-format json
```

**MCP / SDK:**

```jsonc
// MCP run_panel
{
  "pack_id": "enterprise-buyer",
  "instrument_pack": "feature-prioritization",
  "models": ["sonnet", "gpt-4o"],
  "decision_being_informed": "B2B feature priority for Q3 roadmap"
}
```

**What you get:** weighted split gets each persona one answer on one of two
strong models — closer in cost to a single sonnet run than to a 3-way
ensemble. Use when the credibility signal matters more than provider
diversity.

---

### technical-founder-research

**When to use:** the panel is developers, founders, or technical PMs being
asked about engineering decisions, code-tool feedback, or technical
positioning. Adds `gemini-2.5-pro` to the enterprise mix because it tends
to over-index on technical detail in free-text.

**CLI:**

```bash
althing panel run \
  --personas developer \
  --instrument 'feature-prioritization' \
  --var features="SSO, audit logs, and role-based access control" \
  --models 'sonnet:0.4,gpt-4o:0.4,gemini-2.5-pro:0.2' \
  --output-format json
```

**MCP / SDK:** use `persona_models=` in the SDK to mirror the weighted split
(see [consumer-mass-appeal](#consumer-mass-appeal)), or pass
`["sonnet", "gpt-4o", "gemini-2.5-pro"]` in `models: []` over MCP for the
unweighted ensemble variant.

**What you get:** the weighted shape costs roughly 2× a single `sonnet`
run; the ensemble shape costs ~3×. Pick weighted for breadth, ensemble for
agreement-measurement.

---

### high-stakes-validation

**When to use:** the result will land in a board deck, a launch go/no-go,
or a public marketing claim. Four families, top tier per family, full panel
per model, with blending. **This is the most expensive pack.**

**CLI:**

```bash
althing panel run \
  --personas examples/personas.yaml \
  --instrument market-research \
  --var problem="choosing a project management tool" \
  --models 'opus,sonnet,gpt-4o,gemini-2.5-pro' \
  --blend \
  --max-cost 5.00 \
  --output-format json
```

`--max-cost` is a hard ceiling: the run stops starting new panelists once
the projected spend would pass it. Both `--blend` and `--max-cost` need a
single-round instrument; for `pricing-discovery`, use a weighted split
(`--models 'opus:0.25,sonnet:0.25,gpt-4o:0.25,gemini-2.5-pro:0.25'`) and
no `--max-cost`.

**MCP / SDK:**

```jsonc
// MCP run_panel
{
  "instrument_pack": "pricing-discovery",
  "vars": {"problem": "choosing a project management tool"},
  "pack_id": "general-consumer",
  "models": ["opus", "sonnet", "gpt-4o", "gemini-2.5-pro"],
  "decision_being_informed": "launch-tier pricing for Q3 GA"
}
```

**What you get:** four full passes of the panel (15 personas × 4 models ×
N questions). Cost typically $1–$5 for a moderate instrument. Read the
`blend` block's `per_model` distributions next to the blended
`distribution`: when four families give similar distributions, the result
does not hinge on one provider. There is no single agreement score in the
output; compare the per-model distributions directly.

---

## Inspecting the model mix in a response

Every panel result echoes the model set in its `metadata` block so agents
can branch on it without re-deriving from the request:

```jsonc
{
  "result_id": "result-20260522-abc123",
  "model": "haiku",                      // headline / synthesis model

  // Present in CLI --output-format json when weighted --models routing
  // produced a per-persona assignment, OR when the persona YAML carries a
  // model: field, OR when persona_models= is passed via the SDK / MCP.
  "model_assignment": {
    "Maya Chen": "haiku",
    "Derek Washington": "gpt-4o-mini",
    "Priya Sharma": "gemini-2.5-flash"
  },

  "metadata": {
    "models": {
      "panelist": "claude-haiku-4-5-20251001",
      "synthesis": "claude-haiku-4-5-20251001"
    },
    "cost": {
      "total_tokens": 5333,
      "total_cost_usd": 0.0142,
      // One bucket per model that produced billable tokens. Multi-bucket
      // → the panel was actually model-diverse. Single bucket → single-
      // model panel (treat as directional).
      "per_model": {
        "claude-haiku-4-5-20251001":  { "tokens": 1800, "cost_usd": 0.0040 },
        "gpt-4o-mini":                { "tokens": 1700, "cost_usd": 0.0038 },
        "gemini-2.5-flash":           { "tokens": 1833, "cost_usd": 0.0064 }
      }
    }
  },

  "synthesis": { "...": "..." },
  "warnings": [ /* cost-fallback, synthesis, convergence warnings */ ]
}
```

Two keys to check programmatically:

- **`metadata.cost.per_model`** — the canonical "which models actually ran
  this panel?" answer. Always present. **The number of keys is the model-
  diversity signal:** one key = single-model panel; ≥2 keys = real model
  mix. Tokens and cost are bucketed to the model that billed them.
- **`model_assignment`** — present only in the CLI JSON output and only
  when per-persona model routing actually happened (weighted `--models`
  spec, persona YAML `model:` field, or SDK `persona_models=`). Use it to
  map a specific panelist's response back to the model that produced it;
  use `metadata.cost.per_model` for the panel-level diversity check.

A single-line agent check:

```python
diversity = len(result["metadata"]["cost"]["per_model"])
if diversity < 2:
    # treat the result as directional, not validating — see Agent Guidance
    ...
```

## Agent guidance

A short checklist an agent should apply before reporting back to its
caller:

1. **Match pack to claim strength.**
   - "Let's explore" → `fast-cheap-preflight` is fine.
   - "We're considering shipping" → `balanced-research-ensemble`.
   - "We're about to act on this externally" → `skeptical-red-team-ensemble`
     or `high-stakes-validation`.

2. **Surface the model mix when reporting.** When summarising the result
   back to a human or another agent, include the
   `metadata.cost.per_model` keys verbatim. *"The panel ran on haiku
   only"* is qualitatively different from *"the panel ran on
   haiku + sonnet + gemini-2.5-flash"* and the caller needs the
   distinction.

3. **Check `metadata.cost.per_model`.** If it has only one bucket, the
   panel was single-model — treat the result as directional only, and
   escalate to a multi-model re-run before acting on the answer. See the
   [SynthBench leaderboard](https://synthbench.org) for how far single
   models and blends sit from human answers on each dataset.

4. **Honor `decision_being_informed`.** All MCP entry points require a
   12–280 character `decision_being_informed` string — it forces the caller
   to name the stake, which is the same lever that should drive model-pack
   choice. A throwaway string like *"exploring"* should map to the cheap
   pack; *"choosing launch tier price"* should not.

5. **Don't generalise from one provider.** A panel answered only by
   OpenAI models is one slice of one company's training data. Cross-family
   diversity is a cheap (in `balanced-research-ensemble`) way to keep that
   confound out of the conclusion.

## How this composes with `--model auto` and `--best-model-for`

`--model auto` (see [docs/model-routing.md](model-routing.md)) picks the
best Althing-conditioned model or blend from SynthBench routing data for
the panel's respondent group and question topic, and says why on stderr.
`--best-model-for "<topic>"` (see
[docs/recommended-models.md](recommended-models.md)) picks the single
top-ranked leaderboard row for a leaderboard topic label. Both are mutually
exclusive with `--models`. Choose by question:

- "What does the benchmark say to use for this panel?" → `--model auto`.
- "Which single model tops this leaderboard topic?" → `--best-model-for`.
- "Which model *mix* fits this decision's stake?" → a model pack from this
  page.

For high-stakes work both axes matter; `high-stakes-validation` already
includes the per-family top models on most leaderboard topics, so it
dominates `--best-model-for` for that use case.

## Related

- [`docs/ensemble.md`](ensemble.md) — the runnable `--models` syntax these
  packs compile down to (weighted shapes, ensemble shapes, `--blend`).
- [`docs/recommended-models.md`](recommended-models.md) — single-model
  picker driven directly by the SynthBench leaderboard.
- [`docs/methodology.md`](methodology.md) — what SynthBench actually measures
  (SPS, JSD) and why ensembles tend to win.
- [`docs/mcp.md`](mcp.md) — MCP tool schemas, including the `models: []`
  parameter referenced from every pack above.
