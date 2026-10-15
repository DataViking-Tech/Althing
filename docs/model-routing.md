# Auto model routing (`--model auto`)

`--model auto` picks the model for a panel from SynthBench results. It
looks at who your panel represents and what your questions are about,
then uses the model (or weighted mix of models) that scored best on
matching benchmark questions when run through Althing's persona
conditioning.

```bash
althing panel run \
  --personas general-consumer \
  --instrument tech-attitudes.yaml \
  --model auto
```

Before the run starts, the CLI prints the decision to stderr. Example
output for an instrument whose questions are mostly about technology and
privacy (the models and numbers depend on the current data):

```
model routing: openrouter/qwen/qwen3.8-flash (25%), openrouter/google/gemini-3.5-flash-lite (25%), openrouter/meta-llama/llama-3.3-70b-instruct (25%), openrouter/openai/gpt-6-luna (25%)
model routing: slice=us-general topic=technology confident=true source=live
model routing: Ensemble: Qwen 3.8 Flash, Gemini 3.5 Flash-Lite, Llama 3.3 70B, GPT-6 Luna (Althing) is the top Althing-conditioned blend for US general public on Technology and media (SynthBench index 51.5, 151 questions).
```

When the evidence for the topic is weak, the reason says so and the
slice's all-topics route is used instead. For example, a panel filtered
with `--population country=France,Japan` asking about the economy gets:

```
model routing: openrouter/qwen/qwen3.8-flash (25%), openrouter/google/gemini-3.5-flash-lite (25%), openrouter/meta-llama/llama-3.3-70b-instruct (25%), openrouter/openai/gpt-6-luna (25%)
model routing: slice=cross-national topic=economy confident=true source=live
model routing: The Economy and work route for Respondents outside the US is not confident (n=75), so this uses Ensemble: Qwen 3.8 Flash, Gemini 3.5 Flash-Lite, Llama 3.3 70B, GPT-6 Luna (Althing), the top Althing-conditioned blend for Respondents outside the US on all topics (SynthBench index 19.9, 525 questions).
```

The same decision is saved in the run's JSON output under
`metadata.model_routing`.

The same option exists on the other surfaces:

| Surface | How to ask for it | Where the decision goes |
|---|---|---|
| CLI | `althing panel run --model auto [--use-case SLICE] [--topic TOPIC]` | stderr, and `metadata.model_routing` in the output |
| MCP | `run_panel` / `run_quick_poll` with `model: "auto"` and optional `use_case`, `topic` | `model_routing` in the response (and in `metadata` when present) |
| SDK | `run_panel(model="auto", use_case=None, topic=None, ...)` | `panel.metadata["model_routing"]` |

All three call the same function, `althing.model_routing.route_models`.

## Where the data comes from

SynthBench publishes `https://synthbench.org/data/routing.json`. For each
pair of respondent group (a "slice") and question topic, it lists the
systems that were benchmarked, their SynthBench index on those questions,
and which one is best. Althing reads that file.

Althing fetches the file with the same rules as the leaderboard:

- It is cached for 24 hours at `~/.althing/synthbench-routing-cache.json`.
- `ALTHING_SYNTHBENCH_ROUTING_URL` points it at a different URL.
- `ALTHING_SYNTHBENCH_OFFLINE=1` skips the network and uses the cache.
- `ALTHING_SYNTHBENCH_REFRESH=1` forces a fresh download.
- When the file cannot be fetched and there is no cache, Althing uses a
  copy bundled with the package
  (`althing/data/synthbench-routing-snapshot.json`).

The `source` field in the decision says which of these was used:
`live`, `cache`, `stale-cache` or `bundled-snapshot`. If nothing could be
loaded it is `unavailable` and the default model is used.

## Slices: who the panel represents

| Slice | Meaning | Benchmark data |
|---|---|---|
| `us-general` | The US general public | OpinionsQA, GSS |
| `us-subgroups` | US demographic subgroups | SubPOP |
| `cross-national` | Respondents outside the US | GlobalOpinionQA |

Althing picks the slice in this order:

1. `--use-case` (CLI) or `use_case` (MCP, SDK), if given.
2. `cross-national` when `--population` names a country other than the
   US (for example `country=France`), or the persona pack is
   `global-respondents`.
3. `us-subgroups` when `--population` filters on US demographic
   attributes such as `age`, `gender`, `race`, `education`, `income`,
   `party`, `religion` or `region`, or the persona pack is a US subgroup
   pack.
4. `us-general` otherwise.

## Topics: what the questions are about

SynthBench uses seven topics: `politics`, `economy`, `technology`,
`science-health`, `identity`, `society` and `global`. Each topic has a
list of keywords in `routing.json`.

Althing picks the topic in this order:

1. `--topic` (CLI) or `topic` (MCP, SDK), if given. `all` is also
   accepted.
2. Otherwise each question is matched against the keywords. A keyword
   counts only as a whole word or phrase, so `nation` does not match
   inside `discrimination`. Each question goes to the topic with the most
   keyword matches.
3. If more than half of the questions go to one topic, that topic is
   used. Otherwise the topic is `all`.

## Choosing the model

For the chosen slice and topic, Althing takes the route's
`best_althing` entry: the best system whose members all ran with
Althing's persona conditioning. Rows that used a raw prompt are shown on
the SynthBench site but are not used here, because Althing always runs
with personas.

Each route has a `confident` flag. SynthBench sets it when the route has
at least 30 questions and the best system's lead over the runner-up is
clear in a bootstrap test. Althing falls back in this order:

1. The route for the slice and topic, if it is confident.
2. The slice's `all` route, if it is confident.
3. The top system on the slice's `all` route, even though its lead is not
   clear. Not confident means the best system and the runner-up are close,
   not that the best one is poor, so this is still a better guess than a
   default model that SynthBench ranks lower or never tested. The
   decision then has `confident: false`.
4. The default model you would have got without `--model auto`, only when
   there is no routing data for the slice or none of the members can run.

The reason line says which step was used and why.

## Blends

Some of the best systems are blends of two or more models. Althing runs
a blend as a weighted split of the panel: each persona answers on one
model, and the share of personas per model follows the blend's weights.
This is the same split as `--models a:0.5,b:0.5` (see
[ensemble.md](ensemble.md)). It works on single-round and multi-round
instruments and costs about the same as a single-model run.

Details:

- At most four members are used; the lowest-weight members are dropped
  past that.
- If you also pass `--blend` on the CLI and the instrument is
  single-round, every model answers the whole panel and the
  distributions are averaged by weight instead. `--blend` is refused for
  multi-round instruments, so with one of those the routed blend runs as
  a split and the CLI says so.
- MCP and SDK always use the split.

## Which models can run

SynthBench lists models by their OpenRouter id, such as
`qwen/qwen3.8-flash`. Althing runs them as `openrouter/<id>`, which
needs `OPENROUTER_API_KEY`. When Althing already has a direct alias for
the model and you have that provider's key, it calls the provider
directly instead. For example `anthropic/claude-haiku-4.5` runs as
`claude-haiku-4-5-20251001` when `ANTHROPIC_API_KEY` is set.

Members that cannot run are dropped and listed in the decision's
`dropped` field: ids that are not model ids, and models whose provider
key is missing. If a blend loses members, the remaining weights are
scaled back up to 100%. If one member is left, it runs as a single
model. If none are left, Althing moves to the next fallback step.

For blends, the CLI's normal pre-flight check still runs and probes each
model before any spend. Pass `--skip-preflight` to skip it.

## The decision record

`metadata.model_routing` (CLI, SDK) and `model_routing` (MCP) contain:

| Field | Meaning |
|---|---|
| `model` | The model, when the decision is a single model |
| `model_spec` | `[{model, weight}]`, when the decision is a blend |
| `slice` | The slice used |
| `topic` | The topic used for the lookup (`all` when there was no clear majority) |
| `route_topic` | The topic of the route actually used (`all` after a fallback) |
| `confident` | Whether the route used was confident (`false` for `slice-top` and `default`) |
| `fallback` | `none`, `slice-all`, `slice-top` or `default` |
| `source` | `live`, `cache`, `stale-cache`, `bundled-snapshot` or `unavailable` |
| `config_id`, `label`, `index`, `n_questions` | The SynthBench system chosen and its score |
| `dropped` | Members that could not run here, with the reason |
| `reason` | One sentence explaining the choice |
| `slice_reason`, `topic_reason` | How the slice and topic were inferred |

## Rules and limits

- `--model auto` cannot be combined with `--models` or
  `--best-model-for`. `--use-case` and `--topic` only work with
  `--model auto`.
- Over MCP, `model: "auto"` cannot be combined with `models` or
  `persona_models`.
- Per-persona `model:` fields in persona YAML still win over the routed
  split, as they do for `--models`.
- The bundled routing snapshot is dated. Check the `source` field; for
  current data, make sure the live file is reachable.

## Related

- [recommended-models.md](recommended-models.md): `--best-model-for`,
  which picks the top leaderboard row for a leaderboard topic label.
- [ensemble.md](ensemble.md): how weighted splits and `--blend` work.
- [model-packs.md](model-packs.md): fixed model mixes for when you want
  to choose yourself.
