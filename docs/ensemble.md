# Multi-Model: `--models`, ensemble, and blend

The `--models` flag has **two distinct meanings** depending on its shape.
This page documents which shape does what, how weights map to persona
assignments, and the edge cases operators have tripped on.

> **Looking for recommended configurations?** See
> [docs/model-packs.md](model-packs.md) for seven named presets
> (`fast-cheap-preflight`, `balanced-research-ensemble`,
> `skeptical-red-team-ensemble`, …) that compile to the `--models` strings
> documented below, plus agent guidance on matching pack to decision stake.

## Two shapes of `--models`

### 1. Weighted per-persona assignment (has `:`)

```bash
althing panel run \
  --personas examples/personas.yaml \
  --instrument examples/survey.yaml \
  --models 'haiku:0.5,gemini-2.5-flash:0.5'
```

Splits your panel across models in the given ratio. With 6 personas and
`haiku:0.5,gemini:0.5`, you get 3 personas on haiku, 3 on gemini. Each
persona answers once, on the model they were assigned.

Useful for:

- Running a cross-provider panel cheaply (haiku + flash instead of all
  sonnet).
- A/B comparing provider behavior within a single panel run.

### 2. Ensemble (no `:`)

```bash
althing panel run \
  --personas examples/personas.yaml \
  --instrument examples/survey.yaml \
  --models 'haiku,sonnet,gemini-2.5-flash'
```

Runs the **full panel once per model**. With 6 personas and 3 models,
every persona answers on every model — 18 sessions total. Use with
`--blend` to weight-average the resulting distributions.

Useful for:

- Measuring cross-model agreement/disagreement on the same panel.
- Building weighted ensembles (`--blend`) for decision support.

The two shapes are mutually exclusive with `--model` but are chosen
implicitly by whether any entry in the spec contains `:`.

## Weighted assignment: the algorithm

Given a spec like `a:0.5,b:0.5` and N personas, the algorithm is:

1. **YAML overrides first.** Any persona whose YAML has a `model:` field
   keeps that model and is removed from the assignment pool. This always
   wins over `--models`.
2. **Normalize.** Sum the weights, divide each by the total. Absolute
   magnitudes don't matter — `a:2,b:3`, `a:0.4,b:0.6`, and `a:40,b:60`
   all produce the same split.
3. **Largest remainder.** Each model first gets the whole part of
   `weight_normalized * pool_size`. The personas left over go one each
   to the models with the largest fractional parts. When two models tie
   on the fractional part, the one listed earlier in the spec wins. The
   totals always add up to the pool size.
4. **At least one each.** When there are at least as many personas as
   models, every model gets at least one persona: a model that ended up
   with none takes one from the model that currently has the most. When
   there are fewer personas than models, some models get none and the
   CLI prints a warning naming them.
5. **Fill in order.** Personas are consumed from the pool in the order
   they appear in the YAML (minus YAML-override ones). Model order
   matches the `--models` spec.

The algorithm is **fully deterministic**: same personas + same spec →
same assignment, every run. No RNG, no seed.

## Edge cases

### Non-even division

With 7 personas and three equal models (`a:0.33,b:0.33,c:0.33`):

```
whole parts: 2.33 → 2, 2.33 → 2, 2.33 → 2   (6 assigned, 1 left)
fractional parts tie at 0.33, so the earlier model wins
→ a gets 3, b gets 2, c gets 2
```

With 7 personas at `a:0.5,b:0.5` the split is 4 / 3.

**Swapping model order changes who gets the extra on a tie.** If you
care about balance, put the model you want to over-sample first.

### Weights that don't sum to 1.0

Weights are normalized, so `a:2,b:3` works and means "40% / 60%". But a
sum far from 1.0 often means a typo (`0.3,0.3,0.3` intended as
`0.33,0.33,0.33`). The CLI prints a warning on sums outside
`1.0 ± 0.02`:

```
Warning: --models weights sum to 0.900, not 1.0. Weights will be
normalized, preserving the ratio.
```

The run still proceeds — this is a soft warning, not a hard error.

### Zero and negative weights

Rejected at parse time with an error. A model with weight 0 couldn't
receive any personas anyway, and negative weights don't have a sensible
meaning.

### YAML `model:` override

If a persona has a `model:` field in its YAML, that model is used for
that persona regardless of `--models`. Example:

```yaml
personas:
  - name: Alice
    model: claude-opus-4-7        # always Opus, ignores --models
  - name: Bob
    # no model field → assigned by --models
```

This is also the only way to set per-persona models via the MCP server
(`persona_models` argument on `run_panel`).

### Per-persona `llm_overrides:` (sp-4loufu)

For finer-grained control, a persona can carry an `llm_overrides:`
block that varies sampling parameters — `temperature`, `top_p`,
`max_tokens` — and optionally `model` away from the run-level
defaults:

```yaml
personas:
  - name: Alice
    llm_overrides:
      temperature: 0.3      # deliberate, stable answers
      top_p: 0.95
  - name: Bob
    llm_overrides:
      temperature: 0.9      # varied, exploratory
      max_tokens: 1024
  - name: Carol             # no overrides → uses --temperature default
```

Run-level `--temperature` / `--top_p` remain the fleet default; only
the personas with an explicit override diverge. Values are validated
before the run starts (`temperature` in `[0, 2]`, `top_p` in `[0, 1]`,
`max_tokens` a positive integer, no unknown keys) so a typo or
out-of-range value fails loudly instead of silently dropping.

`llm_overrides.model` works the same as the legacy top-level
`model:` — it routes the persona to a specific model when no
`--models` flag is given (or when `--models` uses a weighted spec).
When both are present, top-level `model:` wins so existing YAML keeps
its behaviour. Like the legacy field, neither override is applied in
ensemble mode (`--models a,b` with no weights), where every persona
runs against every model by design.

### Weights vs. ensemble — how the CLI tells them apart

A spec with **any** `:` is weighted. A spec with **no** `:` is
ensemble. Mixing shapes (`haiku,sonnet:0.5`) is a parse error — there's
no coherent meaning.

## Seeing the assignment

The CLI prints the resolved persona → model map to stderr before any
LLM calls:

```
Model assignment:
  Maya Chen        → haiku
  Derek Washington → gpt-mini
  ...
Totals: gemini-flash=2, gpt-mini=2, haiku=2
```

If the split looks wrong (rounding on a small panel, a YAML override
you forgot about), `^C` and re-run.

The same map is available in JSON output under `model_assignment`:

```json
{
  "model_assignment": {
    "Maya Chen": "haiku",
    "Derek Washington": "gpt-mini"
  }
}
```

## MCP equivalent

The MCP `run_panel` tool exposes this behavior through two separate
parameters:

- `persona_models: dict[str, str]` — pre-computed persona→model map.
  The MCP client is expected to do the weighted split itself if it
  wants one; MCP accepts the final map directly, which avoids rounding
  ambiguity across clients.
- `models: list[str]` — ensemble mode only. Every persona answers every
  model. The response has per-model results; there is no `blend`
  block and no blended distributions on the MCP surface.

There is no weighted-spec parser on the MCP surface. Strings like
`"haiku:0.5"` are rejected with an error.

`model="auto"` is the exception: when SynthBench routing picks a blend,
the server computes the same largest-remainder split shown above and
passes it as `persona_models`. See [model-routing.md](model-routing.md).

## Auto routing

`--model auto` (CLI), `model="auto"` (MCP) and `run_panel(model="auto")`
(SDK) pick a model or a weighted blend from SynthBench data. A routed
blend runs as the weighted split described above, so it works on
multi-round instruments too. `--blend` stays an explicit opt-in and
still requires a single-round instrument. See
[model-routing.md](model-routing.md).
