# Task-Oriented Recommendations — Picking a Persona Pack + Model Config

> **Audience:** AI agents (and the humans configuring them) deciding which
> persona pack and which model setup to use for a given research task.
> This page is the "what should I run?" lookup; the deep references are
> [docs/model-packs.md](model-packs.md) for the model configurations and
> [README §Builtin persona packs](../README.md#builtin-persona-packs-14-232-personas-total)
> for the full persona-pack catalogue.

## TL;DR — task → pack → model

| Task / decision shape | Persona pack | Model config | Why |
|---|---|---|---|
| **Product positioning / messaging** | `product-research` (20) | `fast-cheap-preflight` for iteration → `balanced-research-ensemble` before any claim leaves the building | PMs and researchers will steelman your wording; ensembles narrow which lines actually generalize across model families |
| **Broad mass-appeal copy / naming** | `broad-professionals` (20) | `consumer-mass-appeal` weighted split, or `balanced-research-ensemble` before publishing | "Person on the street" coverage; if results will be cited externally, use the model-diverse ensemble |
| **Enterprise GTM / procurement narrative** | `enterprise-ai-buyers` (18) **and/or** `skeptical-executives` (18) | `skeptical-red-team-ensemble` (`sonnet,gpt-4o,gemini-2.5-pro`) at `--temperature 0.9` | These personas are vendor-fatigued and ROI-fixated; the higher-rigor ensemble surfaces objections cheaper models miss |
| **Trust / credibility / methodology audit** | `market-research-critics` (16) | `skeptical-red-team-ensemble` | Hostile audience by design — pair with the model pack that doesn't agreement-bias |
| **Pricing discovery** | `general-consumer` (15) or audience-specific pack | `fast-cheap-preflight` for tier shape → a weighted split such as `--models 'haiku:0.34,sonnet:0.33,gemini-2.5-flash:0.33'` for the actual price point | Cheap models lock in the shape; spend on more models when the number matters. The bundled `pricing-discovery` instrument is multi-round, so `--blend` is refused; the weighted split works |
| **AI / eval tooling discovery** | `ai-eval-buyers` (20) | `--model auto` (SynthBench-driven model or blend); `balanced-research-ensemble` for high-stakes | This audience reads benchmark numbers, so match the model choice to benchmark evidence |
| **Dev-tool feedback / technical positioning** | `developer` (15) or `startup-founder` (15) | `technical-founder-research` (`sonnet:0.4,gpt-4o:0.4,gemini-2.5-pro:0.2`) | Technical buyers detect glibness; weight toward the higher-quality families |
| **Hiring / talent / job-search messaging** | `recruiters-talent` (15) for employer-side, `job-seekers` (15) for candidate-side | `fast-cheap-preflight` for shape → `balanced-research-ensemble` for finals | Two distinct audiences — don't pool them; run separate panels |
| **Healthcare messaging / patient comms** | `healthcare-patient` (15) | `balanced-research-ensemble` by default; **never single-model for clinical/compliance** copy | Stakes are high; ensemble + `althing report` + human SME review is the floor |
| **Education / student outreach** | `students` (15) | `fast-cheap-preflight` for early loops; `consumer-mass-appeal` weighted split for finals | Age-skewed audience; the weighted split keeps cost down |
| **Founder / early-stage hypothesis testing** | `startup-founder` (15) | `fast-cheap-preflight` is enough until you're charging money | Founders forgive directional answers; save the ensemble budget for paying-customer questions |
| **Pre-launch / board-deck / go-no-go** | Audience-appropriate pack (above) | `high-stakes-validation` 4-way ensemble + `--blend` (single-round instruments only; weighted split for multi-round) | The one place you *should* spend on the full ensemble + blend |

> **Heuristic for the model-config column:** if a human is going to act
> on the answer, use an **ensemble**. If an agent is just orienting
> itself ("does this question even make sense?"), `fast-cheap-preflight`
> is fine. `--model auto` is for the in-between case: a model or blend
> chosen from SynthBench results for your respondent group and question
> topic. See [docs/model-routing.md](model-routing.md).

## When single-model is acceptable

Use a single cheap model (`fast-cheap-preflight`) when:

- You're iterating on the **instrument** itself (wording, branching,
  schema), not the answer. The shape of the responses tells you whether
  the question is legible; the content barely matters yet.
- You're **dry-running** a panel before a real spend — verifying
  persona attachment, attachment refs, output format, etc.
- The decision downstream is **reversible and low-stakes** — internal
  team naming, a Slack-only experiment, draft copy you're going to
  rewrite anyway.
- The decision is **directional**, not quantitative — "are people
  excited or skeptical?" rather than "what % would buy at $79?".

Use **SynthBench routing** (`--model auto`) when:

- You want the model choice backed by benchmark evidence for your kind
  of respondents and questions. The CLI prints the chosen model or
  blend, the slice, the topic, whether the route is confident, and a
  one-sentence reason to stderr before the run, and records the same
  under `metadata.model_routing`. See
  [docs/model-routing.md](model-routing.md).

## When to escalate to an ensemble

Switch to a model-diverse ensemble (`balanced-research-ensemble` or
heavier) when **any** of the following is true:

- The answer will be **acted on** by a human or autonomous agent —
  shipped copy, a price tier, a positioning line, an investor message.
- The answer will be **cited externally** — pitch deck, blog post,
  podcast, conference talk.
- The audience is **professionally skeptical** (`market-research-critics`,
  `enterprise-ai-buyers`, `skeptical-executives`). Single-model panels
  systematically underweight objections for these audiences because
  cheap models agreement-bias.
- The cost of a **false positive** ("ship this") is much higher than
  the cost of a slower decision.
- You need **dispersion** signal — `synthesis.disagreements` and
  `synthesis.surprises` are much richer when models with different
  training data disagree.

The cost ratio is real: a 4-way ensemble runs the full panel 4×, so
budget accordingly. See [docs/ensemble.md](ensemble.md) for the
mechanics and the `--blend` aggregation algorithm.

## Copy/paste commands

Each row below is a complete, runnable invocation against a bundled
persona pack. Export the pack first (the CLI takes a file path, not a
pack name); the MCP / SDK surfaces accept the pack name directly.

### Product positioning — fast iteration

```bash
althing pack export product-research > /tmp/product-research.yaml
althing panel run \
  --personas /tmp/product-research.yaml \
  --instrument /tmp/positioning.yaml \
  --model haiku \
  --output-format json
```

```jsonc
// MCP equivalent
{
  "tool": "run_panel",
  "arguments": {
    "pack_id": "product-research",
    "instrument": { /* ... */ },
    "model": "haiku",
    "decision_being_informed": "iterating positioning before final commit"
  }
}
```

### Product positioning — pre-publication ensemble

```bash
althing panel run \
  --personas /tmp/product-research.yaml \
  --instrument /tmp/positioning.yaml \
  --models 'haiku,sonnet,gemini-2.5-flash' \
  --blend \
  --output-format json
```

```jsonc
{
  "tool": "run_panel",
  "arguments": {
    "pack_id": "product-research",
    "instrument": { /* ... */ },
    "models": ["haiku", "sonnet", "gemini-2.5-flash"],
    "decision_being_informed": "finalizing the launch positioning"
  }
}
```

### Enterprise GTM — skeptical ensemble

```bash
althing pack export skeptical-executives > /tmp/skeptical-executives.yaml
althing panel run \
  --personas /tmp/skeptical-executives.yaml \
  --instrument /tmp/gtm-pitch.yaml \
  --models 'sonnet,gpt-4o,gemini-2.5-pro' \
  --blend --temperature 0.9 \
  --output-format json
```

```jsonc
{
  "tool": "run_panel",
  "arguments": {
    "pack_id": "skeptical-executives",
    "instrument": { /* ... */ },
    "models": ["sonnet", "gpt-4o", "gemini-2.5-pro"],
    "temperature": 0.9,
    "decision_being_informed": "stress-test the GTM pitch before a CRO review"
  }
}
```

### Trust / credibility audit — hostile audience

```bash
althing pack export market-research-critics > /tmp/market-research-critics.yaml
althing panel run \
  --personas /tmp/market-research-critics.yaml \
  --instrument /tmp/methodology-claim.yaml \
  --models 'sonnet,gpt-4o,gemini-2.5-pro' \
  --blend --temperature 0.9 \
  --output-format json
```

### AI / eval discovery — SynthBench-routed model

```bash
althing pack export ai-eval-buyers > /tmp/ai-eval-buyers.yaml
althing panel run \
  --personas /tmp/ai-eval-buyers.yaml \
  --instrument /tmp/eval-tool-positioning.yaml \
  --model auto --topic technology \
  --output-format json
```

`--model auto` reads SynthBench routing data and prints its choice and
reason to stderr before the run (see
[docs/model-routing.md](model-routing.md)). Pass `--model <alias>`
instead if you want to choose yourself.

### Healthcare patient messaging — ensemble + report

```bash
althing pack export healthcare-patient > /tmp/healthcare-patient.yaml
althing panel run \
  --personas /tmp/healthcare-patient.yaml \
  --instrument /tmp/patient-comms.yaml \
  --models 'sonnet,gpt-4o,gemini-2.5-pro' \
  --blend \
  --save --output-format json \
| jq -r '.result_id' \
| xargs althing report --output patient-comms-panel.md
```

> **Never run healthcare comms on a single cheap model.** Use the
> ensemble + render to Markdown for human SME review before any patient-
> facing copy ships.

### Hiring messaging — two-sided

The employer-side and candidate-side audiences should run as **separate**
panels; pooling them dilutes the signal.

```bash
# Employer side
althing pack export recruiters-talent > /tmp/recruiters-talent.yaml
althing panel run --personas /tmp/recruiters-talent.yaml \
  --instrument /tmp/hiring-pitch.yaml --model haiku --save --output-format json

# Candidate side
althing pack export job-seekers > /tmp/job-seekers.yaml
althing panel run --personas /tmp/job-seekers.yaml \
  --instrument /tmp/hiring-pitch.yaml --model haiku --save --output-format json
```

## How the recommendations were chosen

These are not arbitrary defaults. They follow from three observations
that SynthBench surfaces in the live leaderboard data:

1. **No single model is best at everything.** Which model leads on
   [SynthBench](https://synthbench.org) changes by dataset and topic,
   and the rankings move as models are added. Single-model panels
   inherit that model's blind spots. → ensemble for high-stakes work.
2. **Blends of models from different families rank high.** On the
   leaderboard snapshot bundled with this release, a multi-family blend
   tops each core dataset. The trade-off is cost (N× panel for a full
   ensemble) and latency (parallel but bounded by the slowest
   provider). → reserve full ensembles for decisions that will be acted
   on; a weighted split gets family diversity at single-model cost.
3. **Skeptical audiences are not just persona-skeptical — they're
   model-skeptical.** Cheap models agreement-bias against critical
   personas, so combining a hostile pack with a cheap model gives you
   *less* signal, not more. → pair `market-research-critics` /
   `skeptical-executives` / `enterprise-ai-buyers` with the
   higher-rigor ensemble.

For deeper context: [docs/model-packs.md](model-packs.md) walks each
model pack with cost order-of-magnitude;
[docs/model-routing.md](model-routing.md) covers `--model auto`;
[docs/recommended-models.md](recommended-models.md) covers
`--best-model-for`; [docs/ensemble.md](ensemble.md) covers the
`--models` syntax and `--blend` algorithm;
[docs/calibration.md](calibration.md) covers the human-baseline
calibration step that closes the loop.

## Status — docs-only for v1.5.1

The convenience flags the upstream issue floats —

```bash
althing pack list --recommend
althing models recommend --task positioning
althing recommend --task enterprise-gtm
```

— **do not exist yet.** This page is the agent-facing surface for
v1.5.1. The flags above would compile to the same recommendations
documented here; if/when they ship, this table becomes their
implementation reference.
