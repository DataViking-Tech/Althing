# SynthBench integration (`--submit-to-synthbench`)

SynthBench is the public benchmark and leaderboard for measuring how
faithfully an LLM-driven panel reproduces a known human distribution.
Althing can submit a calibrated panel run directly to SynthBench at
the end of the run with one extra flag.

This integration is **opt-in**, **gated on `--calibrate-against`**, and
ships nothing without explicit user consent.

## When you can submit

Only **calibrated** runs are submittable. SynthBench scores per-question
JSD against a published human baseline, which Althing produces only
when you pass `--calibrate-against DATASET:QUESTION` (see
[`docs/convergence.md`](convergence.md) for the calibration mechanics).

A bare `althing panel run --personas ... --instrument ...` is
qualitative output and has no SynthBench score to submit. The CLI
hard-fails at parse time if you try:

```bash
$ althing panel run ... --submit-to-synthbench
Error: --submit-to-synthbench requires --calibrate-against. Only
calibrated runs produce a SynthBench-shaped score; bare panel runs
cannot be submitted to the leaderboard.
```

## Quickstart

```bash
# 1. Mint an API key at https://synthbench.org/account
export SYNTHBENCH_API_KEY=sk_synthbench_...

# 2. Run a calibrated panel and submit at completion.
althing panel run \
  --personas examples/personas.yaml \
  --instrument happiness-probe \
  --calibrate-against gss:HAPPY \
  --convergence-check-every 20 \
  --submit-to-synthbench
```

First-time use prints a one-screen consent block; accept with `y`. The
acceptance is recorded at `~/.althing/synthbench-consent.json` so
subsequent runs do not re-prompt. For CI pass `--yes` to bypass the
prompt:

```bash
althing panel run \
  --personas ./ci-personas.yaml \
  --instrument happiness-probe \
  --calibrate-against gss:HAPPY \
  --convergence-check-every 20 \
  --submit-to-synthbench --yes
```

On success the CLI prints:

```
Submitted to SynthBench: https://synthbench.org/submit/sub_abc123
```

## What gets uploaded

Per the consent notice:

* Per-question categorical response distributions (the
  `model_distribution` used to compute the calibration JSD), alongside
  the published human baseline distribution they are scored against.
* Parity metrics derived from those two distributions: per-question
  `jsd` and `kendall_tau`, plus aggregate `mean_jsd`,
  `mean_kendall_tau`, and `composite_parity`.
* The calibration spec (e.g. `gss:HAPPY`), extractor label, and panel
  sample size *n*.
* Run config: model identifier(s), persona pack name, instrument name.
* The Althing client version.

## Payload contract

The upload is shaped to pass SynthBench's submission validator
(`synthbench.validation`, Tier 1 schema + Tier 2 metric recomputation —
see SynthBench's `SUBMISSIONS.md`). Concretely:

```json
{
  "benchmark": "synthbench",
  "version": "0.1.0",
  "config": {
    "dataset": "gss",
    "provider": "althing/claude-haiku-4-5-20251001",
    "framework": "althing",
    "calibration_spec": "gss:HAPPY",
    "n": 60,
    "client": "althing",
    "client_version": "1.5.7",
    "panelist_model": "claude-haiku-4-5-20251001",
    "instrument": "happiness-probe",
    "persona_pack": "general-public"
  },
  "aggregate": {
    "n": 60,
    "n_questions": 1,
    "mean_jsd": 0.023996,
    "mean_kendall_tau": 0.333333,
    "composite_parity": 0.821335
  },
  "per_question": [
    {
      "key": "HAPPY",
      "human_distribution": { "Very happy": 0.31, "Pretty happy": 0.56, "Not too happy": 0.13 },
      "model_distribution": { "Very happy": 0.4667, "Pretty happy": 0.3833, "Not too happy": 0.15 },
      "jsd": 0.023996,
      "kendall_tau": 0.333333,
      "n": 60,
      "n_samples": 60,
      "extractor": "pick_one:auto-derived",
      "auto_derived": true
    }
  ]
}
```

Contract notes:

* `per_question` is a **list** of rows; each row carries the Tier-1
  required fields `key`, `human_distribution`, `model_distribution`,
  `jsd`, and `kendall_tau`.
* `version` mirrors the SynthBench harness version whose contract the
  payload targets (`synthbench.__version__` when the harness is
  importable).
* `config.dataset` is the `DATASET` half of the calibration spec;
  `config.provider` uses SynthBench's `althing/<model>` provider
  format so leaderboard rows classify under the *product* framework.
* `jsd` and `kendall_tau` are computed from the exact distributions in
  the payload using SynthBench's own metric functions (base-2 JSD,
  tau-b) when `synthbench` is importable — SynthBench's Tier-2
  validation recomputes both from the submitted distributions and
  rejects on mismatch, so this makes the recompute an identity check.
  `composite_parity` is the accepted 2-metric blend
  `0.5·(1−mean_jsd) + 0.5·(1+mean_tau)/2`.
* **A calibration baseline describes one survey question, so exactly one
  question is uploaded per run.** With a multi-question instrument, the
  baseline binds to the tracked question matching the baseline's
  question key (e.g. a question keyed `HAPPY` for `gss:HAPPY`); the
  other tracked questions are excluded from calibration and submission,
  and the CLI prints a warning naming them.

The contract is enforced by `tests/test_synthbench_contract.py`, which
builds a payload from a simulated calibrated run and asserts SynthBench's
actual `validate_submission()` reports zero errors in tiers 1 and 2. CI
runs it on every push (the `synthbench-contract` job installs the real
harness from GitHub — note that the `synthbench` package on PyPI is an
unrelated project).

## What does NOT get uploaded

* Free-text panelist responses or follow-ups.
* Persona definitions, system prompts, or any persona attributes.
* API keys, file paths, or local environment data.

**Do not use `--submit-to-synthbench` with confidential personas,
proprietary instruments, or topics you would not publish on a public
leaderboard.** The leaderboard is public; assume anything in the
uploaded payload is world-readable.

## Failure modes (and what they mean)

The submission step is **warned-but-non-fatal**: a slow or rejecting
SynthBench cannot turn a successful panel run into a non-zero CLI exit.
If something goes wrong you will see a `Warning: SynthBench submission
not accepted (...)` line on stderr but the panel data is still in the
JSON output and any `--save` location.

| `status`               | Meaning                                                     |
| ---------------------- | ----------------------------------------------------------- |
| `not_submittable`      | Run was invalid or carried no calibration JSD.              |
| `missing_api_key`      | `SYNTHBENCH_API_KEY` was unset (caught at parse time).      |
| `consent_declined`     | User answered `n` at the consent prompt.                    |
| `empty_payload`        | No question had both a model and human distribution.        |
| `http_<code>`          | Server rejected with a specific status; `error` carries it. |
| `error`                | Network-level failure (timeout, DNS, refused).              |
| `accepted` / `validating` / ... | Server-reported terminal state on success.        |

The `http_422` case is the most informative: SynthBench's Tier-2
recomputation found a schema mismatch and returned the field-level
reason. Surface that to the SynthBench team if it persists across runs.

## Privacy + consent record

Consent is stored as JSON at `~/.althing/synthbench-consent.json`:

```json
{
  "version": 1,
  "accepted": true,
  "client_version": "0.11.0"
}
```

Delete the file to be re-prompted on the next run. The file is
versioned: a future major change to what gets uploaded will bump
`version` and re-prompt even if consent is on disk.

## Configuration

| Variable                | Purpose                                                   |
| ----------------------- | --------------------------------------------------------- |
| `SYNTHBENCH_API_KEY`    | Required. Bearer token for the `/submit` endpoint.        |
| `SYNTHBENCH_API_URL`    | Optional. Override the default `https://api.synthbench.org`. |

Use `SYNTHBENCH_API_URL` to point at a staging instance during
SynthBench-side development. Both env vars match the names the
`synthbench` CLI itself uses.

## See also

* [`docs/convergence.md`](convergence.md) — the calibration / JSD
  mechanics that produce the score this integration uploads.
* `althing.synthbench_submit` — the implementation, including the
  payload transformer and HTTP transport.
