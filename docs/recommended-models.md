# Recommended Models (SynthBench-driven)

Althing can consult the [SynthBench](https://synthbench.org) public
leaderboard to pick the best-ranked model for the kind of research you're
running. This closes the credibility loop: scores measured on the bench
drive defaults in the harness.

There are two ways to do this:

- **`--model auto`** (also `model="auto"` on MCP and the SDK) reads
  SynthBench's `routing.json`. It infers who the panel represents and
  what the questions are about, picks the best Althing-conditioned model
  or blend for that pair, falls back when the evidence is weak, and
  prints a one-sentence reason. This is the recommended option. See
  [model-routing.md](model-routing.md).
- **`--best-model-for TOPIC[:DATASET]`** reads `leaderboard.json` and
  takes the top row for one of the leaderboard's own topic labels. It is
  described on the rest of this page.

## TL;DR for `--best-model-for`

```bash
# Use the top-ranked model for a specific topic
althing panel run \
  --personas examples/personas.yaml \
  --instrument pricing-discovery \
  --var problem="choosing a project management tool" \
  --best-model-for "Economy & Work"

# Top-ranked model across a whole dataset (by SPS)
althing panel run ... --best-model-for ":globalopinionqa"

# Topic within an explicit dataset
althing panel run ... --best-model-for "Politics & Governance:globalopinionqa"
```

Before the run, Althing prints a recommendation line to stderr so you
can cancel and override:

```
synthbench: best model for globalopinionqa/Economy & Work → openrouter/anthropic/claude-sonnet-5.5 · SPS 0.967 · JSD 0.357 · n=100 · cached 0h ago · source=live
```

The trailing `source=` field is the **provenance discriminator** (sy-klp).
It tells you — and any agent parsing this line — whether the
recommendation reflects the live leaderboard, your local cache, or a
package-bundled fallback:

| `source=` value     | What it means                                                                                       | How current is the recommendation? |
|---------------------|-----------------------------------------------------------------------------------------------------|------------------------------------|
| `live`              | Fetched from the leaderboard URL this run — or upstream returned 304 confirming the cache is current | As of right now                    |
| `cache`             | User's on-disk cache was fresh (< 24h), no network call made                                         | Within the last 24h                |
| `stale-cache`       | Cache exceeded the 24h TTL and the network fetch failed; stale cache used                            | Whenever the cache was last refreshed (see `cached <N>h ago` field) |
| `bundled-snapshot`  | No user cache and the live fetch failed (or offline mode requested without a cache) — package fallback used | The snapshot date (currently 2026-10-14), *not* current |

Agents should treat `live` and `cache` as authoritative, `stale-cache`
as user-dated (caller's last successful sync), and `bundled-snapshot`
as package-dated — same age for everyone on the same release, so the
recommendation will drift from leaderboard reality after the snapshot.

## How it works

1. On first use, Althing fetches
   `https://synthbench.org/data/leaderboard.json` and caches it at
   `~/.althing/synthbench-cache.json` for 24 hours.
2. Entries are filtered to the requested `dataset` (default
   `globalopinionqa`), baseline rows are skipped, and the rest are
   ranked by the named topic's score when a topic is given, otherwise by
   overall SPS.
3. The top entry is turned into a runnable model. Leaderboard rows carry
   an OpenRouter-style `model_id` such as `qwen/qwen3.8-flash`; Althing
   runs it as `openrouter/qwen/qwen3.8-flash`, or through a direct
   provider alias when one exists and you have that provider's key (for
   example `anthropic/claude-haiku-4.5` becomes
   `claude-haiku-4-5-20251001` with `ANTHROPIC_API_KEY`). Plain ids like
   `haiku` go through the alias table. The result is stamped onto
   `--model`.
4. When the top entry is a blend (a row with `ensemble_members`), the
   recommendation carries every member with its weight
   (`Recommendation.members`, `Recommendation.model_spec()`), and
   `--best-model-for` uses the first runnable member. Use `--model auto`
   to run a blend as a weighted split.

> **Bundled snapshot.** Althing ships a trimmed copy of the leaderboard
> (taken 2026-10-14) at `althing/data/synthbench-snapshot.json` and falls
> back to it when the live URL is unreachable and no user cache exists.
> The fallback tags the entry as `source=bundled-snapshot`. To use your
> own mirror, set `ALTHING_SYNTHBENCH_URL=...`.

### Environment knobs

- `ALTHING_SYNTHBENCH_URL` — override the fetch URL (useful for
  internal mirrors, forks, or air-gapped environments — and the
  documented workaround for the GH #494 outage).
- `ALTHING_SYNTHBENCH_OFFLINE=1` — never hit the network; use the
  cache if present, otherwise the bundled snapshot, otherwise skip the
  recommendation.
- `ALTHING_SYNTHBENCH_REFRESH=1` — bypass the 24h TTL and force a
  fresh fetch (ignores the cached ETag).
- `SYNTH_PANEL_DATA_DIR` — override the data dir where the cache lives.

### Graceful offline behaviour

Every degraded path tags the recommendation with a non-`live` `source=`
discriminator (see the table above) and prints a one-line explanation
on stderr so agents and humans can both see how degraded the answer is.

- **Fresh cache hit (< 24h)** → no network. Recommendation tagged
  `source=cache`. No stderr noise.
- **Stale cache + 304** → conditional GET confirms cache is current.
  Recommendation tagged `source=live` and the cache timestamp is
  refreshed. No stderr noise.
- **Stale cache + network error** → stderr `althing: synthbench
  fetch failed (…); using stale cache from <ISO> (source=stale-cache)`.
  Recommendation tagged `source=stale-cache`.
- **No cache + network error** → bundled snapshot fallback (sy-nkh):
  stderr `althing: synthbench unavailable (…); using bundled
  snapshot from YYYY-MM-DD (source=bundled-snapshot) — override the URL
  via $ALTHING_SYNTHBENCH_URL if you have a mirror …`. Recommendation
  tagged `source=bundled-snapshot` and the CLI also emits a follow-up
  note explaining the implication and the override path.
- **Offline mode + fresh cache** → recommendation tagged
  `source=cache`. No stderr noise.
- **Offline mode + stale cache** → recommendation tagged
  `source=stale-cache`. No stderr noise (offline mode is opt-in; the
  user already knows network is disabled).
- **Offline mode + no cache** → bundled snapshot fallback with
  `source=bundled-snapshot`. Stderr explains the fallback once.
- **No cache + no bundled snapshot** → stderr "synthbench unavailable",
  fall through to whatever `--model` or default was already in effect.
- **Empty entries after filter** → same fall-through.

No recommendation is ever fatal. `--best-model-for` is advisory: a bad
network day won't take the panel down, and a 404'ing upstream URL still
yields a sensible default via the bundled snapshot — at the cost of
freshness, which the `source=bundled-snapshot` discriminator makes
explicit instead of pretending the data is current.

### Reading the source field from code

The wire field is part of the public `althing.synthbench`
surface — `Recommendation.source` and `LoadedLeaderboard.source` both
expose the same closed enum. Allowed values are exported as
`althing.synthbench.RECOMMENDATION_SOURCES`:

```python
from althing import synthbench

rec = synthbench.recommend("Economy & Work")
if rec is None:
    ...  # leaderboard unavailable; honour the existing --model
elif rec.source in ("live", "cache"):
    use_with_confidence(rec.model)
elif rec.source == "stale-cache":
    log_warning(f"using model from cache aged {rec.cache_age_hours:.0f}h")
    use_with_confidence(rec.model)
elif rec.source == "bundled-snapshot":
    log_warning(
        f"recommendation from package snapshot dated {rec.fetched_at.date()}; "
        "current leaderboard may have moved."
    )
    use_with_caveat(rec.model)
```

The same `source=` field appears at the tail of every stderr
recommendation line (`format_line()` output), so log-scraping agents
can extract it with one regex.

## What the bundled snapshot says

Generated from the leaderboard snapshot bundled with this release
(2026-10-14). Rankings change as new runs land; run the CLI flag or check
[synthbench.org](https://synthbench.org) for current picks. Topic scores
on GlobalOpinionQA rest on few questions for some topics (one question
for "Health & Science"), so treat the per-topic rows as rough.

| `--best-model-for` value | Top row | Model used |
|---|---|---|
| `:globalopinionqa` | Blend: Qwen 3.8 Flash (Althing), Sonnet 4 (Althing), DeepSeek V4.1 Flash, Gemini 2.5 Flash-Lite | first member, `openrouter/qwen/qwen3.8-flash` |
| `:opinionsqa`, `:gss`, `:subpop` | the same four-model blend | first member, `openrouter/qwen/qwen3.8-flash` |
| `"Economy & Work"` | Althing (Sonnet 5.5) | `openrouter/anthropic/claude-sonnet-5.5` |
| `"Politics & Governance"` | Blend: Haiku 5.5 (Althing), Llama 3.3 70B, Gemini 2.5 Flash-Lite | first member, `openrouter/anthropic/claude-haiku-5.5` |
| `"International Relations & Security"` | the four-model blend above | first member, `openrouter/qwen/qwen3.8-flash` |
| `"General Attitudes"` | Claude Sonnet 4.6 (raw prompt) | `openrouter/anthropic/claude-sonnet-4.6` |

Note that `--best-model-for` ranks every row, including rows that used a
raw prompt instead of Althing's persona conditioning. `--model auto` only
picks Althing-conditioned systems.

## Caveats

- **Display labels & runnable ids (gh-519).** Some leaderboard rows carry a
  human-readable display label (e.g. `Althing (Gemini Flash Lite)`) in
  their `model` field rather than a runnable provider model id. Althing
  never stamps such a label onto `--model`. Instead it substitutes a runnable
  id in this order:
  1. For blend rows, the first runnable member of `ensemble_members`.
  2. The row's runnable `model_id` (e.g. `google/gemini-2.5-flash-lite`)
     published by SynthBench, run as
     `openrouter/google/gemini-2.5-flash-lite` (or a direct alias, as
     above). A bare `model_id` is joined with `provider_id` first, unless
     `provider_id` is a framework tag such as `althing`, `raw` or
     `ensemble`.
  3. For product/ensemble rows (`framework=product`, `is_ensemble=true`)
     without a `model_id`, a base model inferred from the entry's `config_id`,
     adopted only when it resolves to a recognized provider id or alias.
  4. If none of these yields a runnable id, the recommendation is **refused** with
     an actionable stderr message and Althing keeps your existing
     `--model`/default. A stderr note records any substitution.
- **Sparse topics.** When the top entry's `run_count < 3`, a
  low-confidence warning is emitted. Treat those recommendations as
  suggestive rather than authoritative.
- **Provider/model strings vary.** The leaderboard publishes the raw
  `model` string the run used — sometimes a canonical id, sometimes a
  short alias. Althing passes the string through the alias resolver
  so either shape works, but the raw value is preserved in the
  recommendation line as `raw_model`.

## Scoping

`--best-model-for` picks a single model for the whole panel. It is
mutually exclusive with `--models` (which splits the panel across
multiple models) and with `--model auto`; mixing them is rejected before
the run starts.

For a *model mix* picker — i.e. when the answer to "which models?" is more
than one and you want it calibrated to the decision's stake rather than a
single SynthBench topic — see [docs/model-packs.md](model-packs.md), or
let `--model auto` pick a blend from benchmark data
([model-routing.md](model-routing.md)).
