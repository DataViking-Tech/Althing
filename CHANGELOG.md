# Changelog

All notable changes to althing (formerly synthpanel) are documented here.

For auto-generated release notes, see [GitHub Releases](https://github.com/DataViking-Tech/SynthPanel/releases).

## [Unreleased]

### Changed

- **MCP server upgraded to the Python SDK 2.0 / protocol revision
  2026-07-28.** The `mcp` extra now pins `mcp>=2.0,<3` and the server
  migrates from the removed `FastMCP` API to `MCPServer` (the
  decorator surface is unchanged; the package version is now passed via
  the public `version=` constructor argument instead of assigning the
  private `_mcp_server.version`, and the custom stdio loop is replaced
  by the SDK's public `mcp.run(transport="stdio")`). The server
  dual-speaks both protocol eras through the SDK: legacy clients
  negotiate via the `initialize` handshake exactly as before, while
  2026-07-28 clients negotiate via `server/discover`.

  The most consequential part of the migration is **sampling**, which
  remains a fully supported mode on both eras. The 2026-07-28 revision
  defines no server→client requests at all — a modern client can never
  receive a mid-call `sampling/createMessage` — so the sampling-mode
  tools (`run_prompt`, `run_panel`, `run_quick_poll`) now route through
  the protocol's multi-round-trip mechanism (SEP-2322) on modern
  connections: the tool call returns an `input_required` result
  batching every needed `CreateMessageRequest` (one round for all
  personas, a second for synthesis) plus sealed `request_state`
  carrying answers across rounds; the client fulfils the requests
  through its ordinary sampling callback and retries. SDK clients
  drive the loop automatically, so hosts observe the same behaviour as
  before — ask, sample, answer. On handshake-era connections the
  classic mid-call `create_message` flow is byte-identical to previous
  releases (the SDK deprecates it per SEP-2577 but keeps it functional;
  althing suppresses the advisory on that path since modern clients
  never reach it). BYOK routing, `decide_mode` semantics, and all
  sampling-mode restrictions are unchanged.

  One observable removal: the initialize response no longer advertises
  a `sampling` key in `ServerCapabilities` (previously injected under
  `experimental` and, off-spec, at the top level — sampling is defined
  by the spec as a *client* capability). SDK 2.0's public runner has no
  initialization-option hook and the modern `server/discover` flow
  never consults initialize options, so the advertisement was dropped
  rather than re-hacked; whether sampling is used was always decided at
  call time by probing the client's declared capability, which works
  identically on both eras. Inspectors that surfaced the off-spec key
  will no longer show it. See docs/mcp.md ("Protocol eras") for the
  full behaviour matrix.

- **Renamed: synthpanel → althing.** The project is now **Althing**
  (althing.dev) — named for the Norse general assembly. Everything ships
  under the new name: PyPI distribution `althing`, canonical import
  `import althing` (retiring the historical `synth_panel`-vs-`synthpanel`
  module split from #509), CLI `althing`, env vars `ALTHING_*`, container
  `ghcr.io/dataviking-tech/althing`, and site https://althing.dev.
  Compatibility for one deprecation cycle, removed next major release:
  - `import synthpanel` and `import synth_panel` still work (shim modules
    that re-export `althing`, with a `DeprecationWarning`)
  - the `synthpanel` console script still works (prints a rename notice)
  - `SYNTHPANEL_*` environment variables are honored when the `ALTHING_*`
    equivalent is unset (one-time `DeprecationWarning`)
  - `pip install synthpanel` remains published as a shim distribution that
    depends on `althing`
  Historical changelog entries below retain the old name.

- **Sampling docs corrected.** Claude Code is no longer listed as a
  sampling-capable host (it has never implemented the client side —
  anthropics/claude-code#1785), the docs note the MCP 2026-07-28
  sampling deprecation, and the no-credentials error now points at
  subscription CLI models.

### Added

- **Population packs and `althing.population`.** A population pack is a
  persona pack whose personas carry structured attributes (`country`).
  The bundled `global-respondents` pack has one minimal respondent persona
  per country (107 countries). `allocate_population` splits a sample budget
  across population members, weighting repeated members, deterministically
  per seed; `filter_personas` narrows a pack by attribute.
  `althing panel run --population "country=France,Japan"` filters a panel.
  `country`, `gender`, and `region` persona attributes are now stated in
  the persona system prompt (no bundled pack set them before, so existing
  prompts are unchanged). Used by SynthBench's `--persona-pack` option for
  GlobalOpinionQA.

- **Subscription CLI providers: `claude-code:<model>` and `codex:<model>`.**
  Completions can now run through a locally installed, signed-in Claude
  Code (`claude -p`) or Codex (`codex exec`) CLI, drawing on the user's
  Claude / ChatGPT subscription instead of an API key — the
  forward-looking replacement for MCP sampling, which MCP 2026-07-28
  deprecated (SEP-2577) and Claude Code never implemented. Each call is
  an isolated, tool-less completion (agent prompt replaced by the persona
  prompt; tools, MCP servers, user settings, and session persistence
  disabled; empty scratch working directory), and forced-tool structured
  output maps onto the CLIs' native JSON-schema flags, so panels,
  extraction, and SynthBench's `althing` provider work unchanged. The
  prefixes are explicit-only (never a credential fallback), structured
  output escalation stays on the same CLI, and an explicit CLI model
  makes MCP tools run without a key or sampling. Limits: no
  temperature/top_p/seed/max_tokens control, text-only, throughput bound
  by subscription usage limits (surfaced as rate limits). Knobs:
  `ALTHING_CLI_MAX_CONCURRENT`, `ALTHING_CLI_TIMEOUT`,
  `ALTHING_CLAUDE_CODE_EFFORT` / `ALTHING_CODEX_EFFORT`,
  `ALTHING_CLAUDE_CODE_BIN` / `ALTHING_CODEX_BIN`, and
  `ALTHING_CLAUDE_CODE_ALLOW_API_KEY` (by default `ANTHROPIC_API_KEY` is
  stripped from the child env so the CLI can't silently bill it).

- **`cost show <result-id>`.** Per-run cost breakdown for one saved
  panel result — total/panelist cost, per-model token + USD rollup, and
  synthesis cost — as a thin alias over the cost section of `panel
  inspect` (same result resolution: ID or JSON path). Previously the
  `cost` subcommand only had `summary` (cross-run aggregate) and per-run
  cost required reading the full `panel inspect` output. The
  "Result saved" follow-up hint now lists it.

- **Synthesis failure recovery ladder (sp-rcvr).** A thrice-reproduced
  production failure — a 20-persona panel whose questions embed ~14k-char
  page dumps passed the synthesis pre-flight yet deterministically failed
  with `OpenRouter (downstream: Azure) API error 400: Provider returned
  error` — exposed a gap: the map-reduce overflow fallback only fired on
  the PRE-FLIGHT token estimate, never on a downstream rejection, so a
  provider 400 was treated as fatal. The synthesis stage (in `panel run`,
  the MCP/SDK sync runner, and `panel synthesize`) is now wrapped in a
  bounded recovery ladder: (1) classify the failure as overflow /
  transient / fatal via the token-estimate machinery plus error-text
  heuristics; (2) retry transient errors (429/5xx/timeout) once with
  jitter; (3) route (suspected) context overflow into the existing
  map-reduce synthesis — capping the effective per-call limit below the
  documented window when the downstream rejected a prompt that
  pre-flight said fits, so sub-chunking actually splits; (4) when the
  OpenRouter error names the downstream provider, retry once with
  `provider: {"ignore": [<slug>]}` routing preferences (new
  `LLMError.downstream_provider` + `CompletionRequest.provider_routing`,
  honored by both OR transports); (5) fail loud as before
  (`synthesis_error`, `run_invalid`, exit 2), but the message now names
  the exact `panel synthesize <id> --synthesis-model <model>` recovery
  command with a concrete in-family large-context suggestion. Each rung
  logs one stderr line and runs at most once; `--synthesis-strategy=
  single` still disables the map-reduce rung (and the final error says
  so). Verified against live OpenRouter: the reproduced downstream 400
  now recovers end-to-end via capped map-reduce.
- **`panel synthesize` warns when the global `--model` flag is passed.**
  The flag was silently ignored (the synthesis model comes from
  `--synthesis-model`, falling back to the saved panelist model); it now
  warns and points at `--synthesis-model`.

### Fixed

- **A one-entry `--models` spec no longer routes through the ensemble
  path, silently skipping synthesis.** `panel run --models <one-model>`
  (weight-free spec, single entry) was classified as an "ensemble" and
  took the ensemble branch, which never runs synthesis, never engages the
  standard-path cost summary, and exited 0 with a bare "Ensemble
  complete" on stdout — a naive single-model run got no synthesis and no
  signal that anything was skipped. Single-entry specs are now demoted to
  the standard single-model path (identical to `--model X`, synthesis
  included) with a stderr note; ensemble comparison requires two or more
  models.
- **Runs with token usage no longer record $0.0000 when the provider
  reports a zero cost.** OpenRouter returns `usage.cost: 0` for BYOK keys
  (and some upstreams omit native cost); `resolve_cost` trusted the zero
  verbatim, so a run with tens of thousands of tokens recorded
  `total_cost $0.0000` / `cost_usd 0.0` — and, because the orchestrator's
  cost gate accrues via the same path, `--max-cost` could never trip.
  A provider-reported $0 for usage the local pricing table prices as
  nonzero now falls back to the local estimate (with a stderr-visible
  warning); genuinely free models still record $0. Dry-run estimates and
  synthesis costs (which always used the local table) were already
  correct — the mismatch between them and the $0 run cost was the tell.
- **Ensemble runs now state synthesis status and results location on
  stdout.** Text-mode output for a completed (>=2 model) ensemble run was
  the single line "Ensemble complete"; it now prints a terse summary —
  models x personas x questions, incident count, recorded cost,
  "Synthesis: skipped" with the exact `panel synthesize <result-id>`
  follow-up (or the `--save` prerequisite), and where the results live.
  JSON output carries `synthesis: null` + `synthesis_status: "skipped"`
  explicitly. Single-model text runs likewise end with a RUN SUMMARY
  block (personas, questions, errors, recorded cost, synthesis status,
  results path or the `--save` hint).
- **`--personas` / `--instrument` help text documents bundled names.**
  Both flags said "Path to a YAML file..." although bundled
  pack/instrument names (e.g. `general-consumer`, `general-survey`) are
  accepted and are the primary happy path; the help now points at
  `pack list` / `instruments list` and keeps the YAML-path alternative.
- **Synthesis judge final-strike escalation now stays in the run's
  provider family for every provider (GH#571, sy-549 class).** A native
  `--model gemini` run with only `GEMINI_API_KEY` set could complete all
  panelists and then fail the whole run with "Missing API key for
  Anthropic" when the structured-output judge exhausted its retries: the
  escalation target was hard-coded to the bare `sonnet` alias for every
  non-OpenRouter model. Escalation now maps per family — `gemini-*` →
  `gemini-2.5-pro`, `grok-*` → `grok-4`, OpenAI-compat cheap tiers to
  their bigger sibling on the same base URL (`gpt-4o-mini` → `gpt-4o`),
  Anthropic and OpenRouter unchanged — and when no stronger same-family
  model is known (local models, unrecognized ids) the final strike keeps
  the original model and degrades to the documented fallback-synthesis
  path instead of demanding another provider's credentials.
- **`synthpanel panel run` now accepts `--model` and `--output-format`
  after the subcommand (GH#571).** `synthpanel panel run --output-format
  json` previously exited 2 with an argparse usage dump because both
  flags were global-only. They are now also accepted in subcommand
  position, where they override a global-position value; the global
  position keeps working unchanged.

### Added

- **`synthpanel mcp install` now targets every documented editor
  (synthbench#262).** New `--host` flag with `claude-code`,
  `claude-desktop` (platform-specific path), `cursor`, `windsurf`, and
  `zed` (writes the `context_servers` schema with `"source": "custom"`),
  plus `--host auto` which detects hosts whose user-level config already
  exists and confirms each write (`--yes` accepts all). A first-class
  `synthpanel mcp uninstall` subcommand removes exactly the entry the
  installer manages. Successful writes print `Restart <host> to pick up
  the server.`; installs without `--env` print a pointer at
  `synthpanel login` instead of ever baking a key in by default.
- **Large-panel fast-default swap is now shared across CLI, SDK, and MCP
  (synthbench#261).** The ≥10-persona `openrouter/auto` →
  `openrouter/anthropic/claude-haiku-4.5` policy (previously MCP-only,
  GH#462) moved to `synth_panel.llm.fast_default` and now also applies
  to SDK `run_panel`/`quick_poll` and CLI `panel run` when the model was
  not explicitly chosen, with a one-line note on stderr. Explicit
  `--model openrouter/auto` is always honored.
- **MCP `run_panel` gains a first-class `vars` parameter (GH#562).**
  Template placeholders in a resolved instrument — whether loaded via
  `instrument_pack` or passed inline as `instrument` — are substituted from
  `vars: {key: value}`, reusing the same engine and fail-fast guard as the
  CLI's `--var` / `--vars-file` (sp-6yi). **Behavior change:** a
  placeholder-bearing instrument with missing (or omitted) `vars` now
  returns a typed `INVALID_TOOL_ARG` envelope naming the missing keys
  instead of "succeeding" while sending literal `{problem}` /
  `{candidates}` text to panelists and silently corrupting results. The
  `pricing-probe` and `name-test` skills now use `vars` directly instead of
  the `get_instrument_pack` → substitute → inline-`instrument` workaround.
- **The v1.0.0 agent contract is wired end to end on the MCP panel tools
  (P1-1).** `run_panel`, `run_quick_poll`, and `extend_panel` now route
  requests through the AC-4 grace shim (`apply_legacy_grace`): an omitted
  `decision_being_informed` synthesizes `"unspecified-legacy-call"` and
  returns a `W_DECISION_MISSING` nudge in the response `warnings[]`, while
  `SYNTHPANEL_SCHEMA_MIN>=1.1.0` hard-rejects with a typed
  `MISSING_DECISION`. The (real or synthesized) decision is persisted in the
  saved result JSON, stamped on the newly-persisted per-panelist session
  transcripts (AC-7, `results/<result_id>.sessions/`), and echoed at
  `panel_verdict.meta.decision_being_informed`. Successful persisted (BYOK)
  panel runs now emit the `panel_verdict` artifact (AC-6,
  `build_panel_verdict`) under the envelope's `panel_verdict` key alongside
  `synthesis`, with `schema_version: "1.0.0"` stamped at the envelope top
  level so the AC-9 response gate genuinely validates success responses on
  egress. Structured-output 3-strike exhaustion now routes through the AC-8
  contract pivot (`exhausted_retry_outcome`): typed `SCHEMA_DRIFT` error by
  default, degraded verdict with a `schema_drift` warn flag under
  `SYNTHPANEL_DRIFT_DEGRADE=1`. Sampling-mode and ensemble runs (nothing
  persisted) echo the decision but carry no verdict — documented in
  docs/response-contract.md.
- **MCP responses are compact by default with typed envelopes (#561).**
  `quick_poll` and `extend_panel` now return the same typed response
  envelope as `run_panel`, payloads are compact-by-default (verbose is
  opt-in), and a `pack_id` field identifies the instrument pack a run
  used.
- **Skills tool-name conformance guard (#559).** The bundled skills and
  their descriptors now reference the real MCP tool names, backed by a
  CI conformance check that fails the build if a skill names a tool the
  server does not expose.

### Changed

- **`retry_safe` semantics aligned with docs/response-contract.md.**
  Request-side validation errors (`MISSING_DECISION`, `DECISION_TOO_LONG`,
  `INVALID_TOOL_ARG`) now carry `retry_safe: false` — replaying an identical
  malformed request fails identically. `retry_safe: true` remains reserved
  for transient conditions (`MODEL_TIMEOUT`/`PANEL_TIMEOUT`, pre-exhaustion
  `SCHEMA_DRIFT`).
- **`PANEL_TIMEOUT` added to the v1.0.0 `error_codes_enum`.** The server has
  emitted this code on panel-run timeouts since v1.0.x; the schema now
  records it (enum-widening only). It was not renamed to `MODEL_TIMEOUT`
  because agents may already branch on the `PANEL_TIMEOUT` string.
- **Claude Opus pricing corrected (#560).** The local cost table
  overstated Claude Opus by ~3×; the entry is fixed and the model-alias
  table refreshed alongside it.
- **`--submit-to-synthbench` payloads pass the SynthBench validator
  (#557).** The submission payload is reshaped to satisfy the current
  SynthBench submission contract, so a `--submit-to-synthbench` run is
  accepted rather than rejected at validation.

### Fixed

- **CLI v3 branching instruments run through the multi-round engine
  (#556).** `panel run` now dispatches v3 branching instruments to the
  multi-round engine so `route_when` routing executes from the CLI
  instead of collapsing to a single round (the MCP path already did).

## [1.5.7] - 2026-06-04

### Changed

- **Empty attachments now fail loud (#550).** An attachment that resolves
  to empty content raises at the boundary instead of silently letting a
  panel run and synthesize on nothing.
- **Typed `response_schema` is enforced (#547).** Structured-output runs
  validate persona output against the declared `response_schema` type
  rather than accepting loosely-shaped JSON.

### Fixed

- **Synthesis routing follows the panel's provider (#549).** The
  synthesis/judge step routes through the provider the panel actually ran
  on instead of assuming a default, so cross-provider runs synthesize
  correctly.
- **Pre-flight model-reachability check (#546).** A panel run verifies the
  resolved model is reachable before dispatching panelist work, failing
  fast with a clear error instead of deep inside the run.

## [1.5.6] - 2026-06-03

### Added

- **`--save` JSON output includes `result_id` and `saved_path` (sy-659).**
  Callers get the stable handle and canonical path back directly instead
  of scraping stderr.
- **End-to-end agent quickstart** (CLI + MCP + structured output) added to
  the docs (sy-0wi).

### Fixed

- **`--dry-run` guards the vision / text-only attachment conflict (#536).**
  Attaching an image to a known text-only model is caught under
  `--dry-run` instead of burning a real call.
- **`mcp install` resolves an absolute `synthpanel` launcher path (#539).**
  The generated host config points at the real executable so hosts that
  don't inherit the shell `PATH` can still launch the server.
- **Saved-result hints point at the real `cost` command (#538)** and
  **`report` explains how to synthesize a flat-saved result (#537).**
- **`main()` no longer leaks `SIGPIPE=SIG_DFL` into the process
  (sy-6zq, sy-1n1).** Broken-pipe handling stays local to the CLI process.

## [1.5.5] - 2026-05-23

### Fixed

- **`--best-model-for` substitutes a runnable `model_id` from the
  leaderboard row (sy-i7a, #519).** When the top SynthBench row's `model`
  field is a display label (e.g. `SynthPanel (Gemini Flash Lite)`),
  SynthPanel prefers the runnable id the live leaderboard publishes in
  `model_id` (e.g. `google/gemini-2.5-flash-lite`, joined with
  `provider_id` when `model_id` is a bare slug) instead of refusing and
  falling through to the default (which, with only OpenRouter credentials
  present, had landed on `openrouter/auto`). A runnable `model_id` takes
  precedence over the `config_id` base-model heuristic; rows with no
  resolvable runnable id are still refused with an actionable message.
  The picked model is visible under `--dry-run`.

## [1.5.4] - 2026-05-23

### Added

- **`synthpanel results list` / `results show` (sy-g1g, #525).** Saved
  `--save` results live in the results store (`~/.synthpanel/results`),
  distinct from the checkpoint store that `runs list` shows — previously
  undiscoverable without a filesystem search. `results list` enumerates
  saved results (newest first) by stable ID; `results show <id>` resolves
  that handle to a provenance summary and the canonical `saved_path`. The
  `--save` confirmation prints the exact follow-up commands.

### Fixed

- **Saved-result provenance is populated (sy-g1g, #525).** Every `--save`
  artifact embeds the run `metadata` block (synthpanel/Python version,
  `config_hash`, and `cost.pricing_snapshot_date`), so `synthpanel report`
  for a freshly saved run no longer degrades those fields to `(unknown)`.
  Threaded through the CLI, MCP, and SDK save paths.
- **Latest-release metadata aligned across release surfaces (sy-3aj).**

## [1.5.3] - 2026-05-23

### Added

- **`python -m synthpanel` alias module (sy-het, #517).** A shim module so
  `python -m synthpanel` works alongside the `synthpanel` console script.

### Fixed

- **`pack import <registry-id>` resolves registry entries (sy-dwc, #520).**
  A bare slug that isn't a local file is treated as a registry id: it
  resolves the entry to its `gh:` source, imports it, and installs the pack
  under the registry id so a follow-up `--personas <id>` works end to end.
  Ids absent from the registry fall through to the existing local-file
  "not found" error; path-like inputs are never sent through a registry
  network probe.
- **`--best-model-for` refuses display-label model recommendations
  (sy-kh3, #521).** A leaderboard row whose `model` is a display label
  rather than a runnable id is refused with an actionable message instead
  of being stamped onto `--model` (the first half of the gh-519 arc
  completed by sy-i7a in 1.5.5).

## [1.5.2] - 2026-05-23

### Added

- **Provenance `source=` tag on `--best-model-for` recommendations
  (sy-klp, #515).** Every recommendation line now ends with
  `source=live|cache|stale-cache|bundled-snapshot` so callers can tell how
  current the pick is.
- **`--personas` resolves bundled persona-pack names (sy-n80, #513).**

### Changed

- **`mcp install` / `mcp-serve` guard a missing `mcp` extra (sy-xyn, #514;
  #512).** Both commands emit a clear "install `synthpanel[mcp]`" message
  instead of an import traceback when the optional extra is absent.

### Fixed

- **`poll-summary` respects declared question types (sy-oyl, #516).**
  Open-text answers containing numbers (e.g. version strings like
  *"SynthPanel v1.5.1"*) are no longer misclassified as scale questions;
  the whole response must be a numeric scale answer to count. Declared
  question types are persisted with saved results so a reload carries them
  through `poll-summary`.

## [1.5.1] - 2026-05-22

### Added

- **`synthpanel doctor --install-only` (sy-e28).** Validates package,
  dependency, bundled-pack, and checkpoint-root health without requiring a
  provider credential — for clean-room CI and post-`pip install` agent
  smoke tests. JSON output gains `install_ok` and `install_only` alongside
  `credential_configured` and `checks_ok`.
- **`poll-summary` detects structured choice fields from `--schema` runs
  (sy-bn7, #503).**

### Fixed

- **Bundled leaderboard snapshot fallback (sy-nkh, #502).**
  `--best-model-for` degrades to a package-bundled leaderboard snapshot
  (tagged `source=bundled-snapshot`) when there is no user cache and the
  live fetch fails, instead of skipping the recommendation.
- **`BrokenPipeError` handled cleanly when output is piped (sy-4bs).**

## [1.5.0] - 2026-05-22

### Added

- **`synthpanel mcp install` CLI (sy-skf).** Registers synthpanel as a
  stdio MCP server in a host's JSON config — defaults to Claude Code's
  user-scope `~/.claude.json`, with `--scope project` for `./.mcp.json`
  and `--target` for arbitrary hosts (Cursor, Windsurf, …). Supports
  `--env KEY=VALUE`, `--force`, `--dry-run`, and `--uninstall`. Mirrors
  GH#463; eliminates the hand-edit-and-restart step.
- **Four canonical persona packs for common research jobs (sy-a0i).**
- **Deterministic structured-response rollup for `poll-summary`
  (sy-4yd).** `metric_average` / `metric_distribution` computed
  deterministically from bounded questions.
- **MCP auto-picks a fast model for panels of ≥10 personas (sy-2ag).**
- **Hermes-compatible `synthpanel` director skill (sy-7pw).**

### Changed

- **Release + publish pipeline hardened.** PyPI publishing moved to
  Trusted Publishing (OIDC, #493/#492); the auto-tag workflow bumps
  `src/synth_panel/__version__.py` and re-renders the site before tagging
  (sy-r87, sy-hs4) so version artifacts can't drift.

### Docs

- Schema-enforced polling promoted as the agent default (sy-v2d);
  model-packs + ensemble guidance for agent-run panels (sy-8b4); builtin
  vs. registry packs clarified in the README and `docs/registry.md`
  (sy-6o3).

## [1.4.1] - 2026-05-12

### Fixed

- **Auto-tag workflow fails loud on a missing semver label and re-fetches
  fresh labels before tagging (sy-73j).**

## [1.4.0] - 2026-05-12

OpenRouter cost actuals are now surfaced explicitly alongside the local
pricing-table estimate on every public result schema. Consumers building
audit / budget-reconciliation pipelines (e.g. boardroom's `BudgetGuard`)
no longer need to inspect raw `usage.provider_reported_cost` themselves
or guess whether the existing `cost` field is "the bill" or "the guess".
This is purely additive — no existing field changes semantics, and
direct providers (Anthropic / OpenAI / Google) that do not return per-call
cost continue to populate the estimate side with the actual side `null`.

### Added

- **`cost_estimated_usd` + `cost_actual_usd` on `EnsembleResult`,
  `ModelRunResult`, and `SynthesisResult` (sy-ye1).** Both are computed
  properties — `cost_estimated_usd` always reflects the local pricing
  table for the run's token totals, and `cost_actual_usd` is the sum of
  `usage.provider_reported_cost` (or `None` when no upstream call
  returned a cost). Pairing them lets downstream consumers reconcile
  estimate-vs-bill without re-deriving either value.
- **`EnsembleResult.per_model_breakdown`** — list of
  `{model, tokens_prompt, tokens_completion, tokens_total,
  cost_estimated_usd, cost_actual_usd}` entries for audit-grade
  per-model accounting. Surfaced in the public JSON payload via
  `build_ensemble_output()` under the same key.
- **`build_ensemble_output()` payload** now includes top-level
  `cost_estimated_usd`, `cost_actual_usd`, and `per_model_breakdown`
  fields alongside the existing `cost_breakdown` block.
- **`SynthesisResult.to_dict()`** now emits `cost_estimated_usd` and
  `cost_actual_usd` alongside the existing `cost` field.
- **Map-reduce breakdowns** (`map_cost_breakdown`,
  `reduce_cost_breakdown`) now carry `cost_estimated_usd` and
  `cost_actual_usd` on every per-question and reduce-phase entry.
- **`synth_panel.cost.local_estimate_usd(usage, model)`** and
  **`actual_cost_usd(usage)`** — the small helpers that power the new
  fields; available for callers building their own cost surfaces.

### Notes

- The existing `cost: CostEstimate` field on both result types is
  unchanged. For `SynthesisResult` it remains the local estimate; for
  `EnsembleResult` / `ModelRunResult` it remains the provider-resolved
  value (actual if available, else estimate). The new explicit fields
  are the recommended surface for any new integration.
- Mixed-provider ensembles where some calls return cost and others
  don't will see a *partial* `cost_actual_usd` at the aggregate level;
  inspect `per_model_breakdown` to identify which models contributed
  the actual portion.

## [1.3.0] - 2026-05-12

`trafilatura` moves out of core dependencies into the new `[full]` extra so
that bare `pip install synthpanel` becomes installable on Cloudflare Python
Workers (pyodide) and any other runtime that can't satisfy `lxml`'s C
extension. v1.2.0's `pyodide_safe_mode` removed the runtime cliff;
v1.3.0 removes the import-time cliff. `synth_panel.ensemble` now imports
cleanly with only the curated CPython set.

This is an additive minor release for new installs. **Migration for v1.2.0
users that hit URL attachments / the fetch ladder:** install with
`pip install synthpanel[full]`. Without the extra, the trafilatura step in
the fetch ladder silently degrades — the rest of synthpanel is unaffected.

### Changed

- **`trafilatura` is now optional (sy-v8z).** Moved from
  `[project.dependencies]` to `[project.optional-dependencies]` under the
  new `full` extra. Bare `pip install synthpanel` no longer pulls
  `trafilatura` or its transitive `lxml` C extension. Consumers that need
  the URL-attachment fetch ladder install `pip install synthpanel[full]`.
  The fetch ladder already lazy-imports trafilatura and degrades gracefully
  to the screenshot path when it is missing, so existing callers that
  install the extra see no behavior change.

### Added

- **`full` optional-dependencies extra.** Currently just `trafilatura>=1.10`;
  reserved as the umbrella extra for any future C-extension-pulling
  dependency that has to stay opt-in for pyodide compatibility.

## [1.2.0] - 2026-05-12

`synth_panel.ensemble.synthesize_panel` gains a pyodide-safe / async-DI
surface so consumers running on Cloudflare Python Workers (pyodide) — or
any environment where threading.Lock / Semaphore / ThreadPoolExecutor
can't run — can drive the judge call through their own async LLM client
end-to-end. The original v1.1.0 sync surface is preserved: every existing
caller behaves identically when they don't pass the new kwargs.

This is an additive minor release. No breaking changes.

### Added

- **`synthesize_panel(..., judge_enabled=False)`** (sy-huo). Skips the
  judge LLM call entirely and returns a degenerate
  :class:`SynthesisResult` with empty LLM-derived fields. No thread
  spawn, no fetch deps, no LLM cost. Useful for consumers that only
  need the panelist responses and want to opt out of the extra judge
  cost — or that are running in environments where the threading-based
  client can't execute at all.

- **`synthesize_panel(..., llm_client=<AsyncLLMClient>)`** (sy-huo).
  When provided alongside `judge_enabled=True` (the default), the
  function returns a coroutine that drives the judge call through the
  injected async client instead of synthpanel's internal threading-based
  `LLMClient`. Caller must `await` the result. Required for pyodide /
  Cloudflare Workers where threading primitives are unavailable.

- **`synthesize_panel(..., pyodide_safe_mode=True)`** (sy-huo). Hard
  guard: raises `ValueError` if the call would fall through to the
  internal threading-based client. Requires either
  `judge_enabled=False` or `llm_client=<AsyncLLMClient>`.

- **`AsyncLLMClient` Protocol** (sy-huo). Minimal runtime-checkable
  Protocol — one method (`async complete(*, prompt, model, max_tokens)`)
  returning an `AsyncCompletion`. Any consumer's async LLM stack can
  satisfy it with a 10-line adapter. Exposed as
  `synth_panel.ensemble.AsyncLLMClient`.

- **`AsyncCompletion` dataclass** (sy-huo). The result type for
  `AsyncLLMClient.complete()` — `text: str`, `usage: TokenUsage | None`.
  Exposed as `synth_panel.ensemble.AsyncCompletion`.

### Changed

- The `client` positional argument of `synthesize_panel` now accepts
  `None` (typed as `LLMClient | None`). Callers using the new
  `judge_enabled=False` or `llm_client=` paths can pass `None` for
  `client`. Existing callers passing a real `LLMClient` are unchanged.

### Compatibility

- Default behavior of `synthesize_panel(client, panelist_results,
  questions)` is identical to v1.1.0 — sync return, internal client,
  judge enabled.

- `ensemble_run` and `run_panel_parallel` still spawn threads
  internally; they remain unsuitable for pyodide. Workers consumers
  should produce panelist data through their own async stack and then
  call `synthesize_panel` with the new kwargs for the judge step.

## [1.1.0] - 2026-05-12

`synth_panel.ensemble` is now the supported public API for the ensemble
core — the full "deliberation engine" surface that external agents
(e.g. boardroom) can import to run a panel across multiple models and
combine their outputs with real weighted synthesis instead of naive
string concatenation. The underlying primitives have shipped internally
for several releases; v1.1.0 freezes their import path, adds an
``__all__`` block enumerating the supported surface, and re-exports the
judge + map-reduce primitives from :mod:`synth_panel.synthesis` so
callers get one import path.

No behavior change for existing callers. Every v1.0.6 import keeps
working — this release is purely additive.

### Added

- **`synth_panel.ensemble` public API** (sy-0gy). The module now
  documents and freezes the public ensemble surface:

  * Ensemble runner — `ensemble_run`, `EnsembleResult`,
    `ModelRunResult`, `build_ensemble_output`,
    `build_mixed_model_rollup`, `collect_ensemble_incidents`,
    `build_ensemble_incident_warnings`
  * Blender (weighted distribution averaging across models) —
    `blend_distributions`, `BlendedResult`, `BlendedQuestion`. Accepts
    a `weights={model: weight}` mapping for model-weighted scoring.
  * Judge (single-LLM canonical synthesis) — `synthesize_panel`,
    `SynthesisResult` (re-exported from `synth_panel.synthesis`).
  * Map-reduce synthesis — `synthesize_panel_mapreduce`,
    `select_strategy`, `resolve_context_window`,
    `estimate_single_pass_tokens`, `MapPhaseFailure`,
    `MapChunkOverflowError`, strategy constants.
  * Seed pinning — `ensemble_run(..., seed=N)` threads through to
    `CompletionRequest.seed` for deterministic reproducibility on
    providers that support it (e.g. OpenRouter).

  The names enumerated in `synth_panel.ensemble.__all__` are the
  supported public surface; other symbols remain internal.

## [1.0.6] - 2026-05-10

Cross-town friction-sweep cycle. Three independent dogfoods (jotunheim,
midgard, yggdrasil) on v1.0.4 → v1.0.5 surfaced a tight cluster of
orchestrator-dispatch and ergonomics bugs that this release closes.
Plus the cadence itself is now codified in
`docs/release-dogfood-protocol.md` so future releases lock in the
property: three towns sweeping independently catch three different
classes of friction.

No API break — every v1.0.5 caller works unchanged.

### Fixed

- **Linear v3 multi-round panels now fall through positionally**
  (hq-fjdx). Previously, v3 instruments without explicit `route_when`
  clauses terminated after round 1 — `path` returned a single entry
  with `next: __end__`, `question_count: 1`, and rounds 2-N were
  silently skipped. The synthesis layer masked the bug by going broad
  in its single response, so callers saw a successful-looking run that
  had executed 1/N of their instrument. Now a v3 round with no
  explicit routing falls through to the next round in declaration
  order. CI integration test (hq-83ye) pins the contract.

- **Legacy single-round path resolves bank-ref attachments**
  (hq-ilke). v1 instruments with bank-ref attachment shape
  (`attachments: [synthbench_site]` referencing a `bank.attachments[]`
  entry) silently dropped the attachment payload —
  `_resolve_question_attachment_refs` was wired into
  `run_multi_round_panel` but not into the legacy single-round path
  that v1 instruments use. Bank-refs fell out of the
  `dict_attachments` filter, attachments arrived empty, and synthesis
  ran on a hallucinated empty page. Now both dispatch paths resolve
  bank-refs identically. Plus parse-time rejection of the failure
  mode so authors fail fast.

- **Ensemble panel runs with `--blend` emit synthesis**
  (hq-hjq8). Previously, ensemble mode (multiple `--models a,b` with
  no per-model weights) returned `synthesis: None`,
  `total_cost: None`, `model: None` while single-model and
  weighted-split (`a:0.5,b:0.5`) emitted the full synthesis block.
  Asymmetry hurt dashboard builders. Now `--blend` properly composes
  the per-model results into a top-level synthesis with weighted
  agreement scoring; cost rolls up; per-model `cost` field also now
  populated.

- **OpenRouter cost-table refreshed** (hq-xq36). v1.0.4 reported
  `openrouter/anthropic/claude-haiku-4.5` (the README headline model)
  at 99.4% drift from provider-reported cost — local table was
  ~180× too high. Same drift on `openrouter/google/gemini-2.5-flash`
  (~55% off). Refreshed against OR's `/v1/models` endpoint; added
  `scripts/refresh_or_cost_table.py` + a CI freshness gate so the
  table fails build if >30 days stale.

- **`pack export -o /path` auto-creates missing parent directories**
  (hq-pmi1). Previously errored deep in pathlib; now walks the path
  and `mkdir -p`s the parent before writing.

### Added

- **CI integration test for MCP-routed v3 multi-round panels**
  (hq-83ye). Asserts `path` length, `question_count`, per-round
  result shape, and cost rollup match instrument structure. Mock-LLM
  responses; runs across the full Python matrix. Catches the hq-fjdx
  class of regression at the boundary that surfaced it (MCP wrapper).

- **`docs/known-patterns/openrouter-byok-visual-review.md`** —
  midgard mayor's cross-town docs lift, merged earlier this cycle
  (PR #447, still REVIEW_REQUIRED at v1.0.6 cut). Documents the
  inline-HTML pattern for visual review of UI/HTML content via
  OpenRouter.

- **`docs/release-dogfood-protocol.md`** (PR #449, merged). Codifies
  the cross-town wave-style friction-sweep cadence that produced
  this release. Three independent dogfoods caught three different
  classes of friction; the protocol locks the property in. Authored
  by yggdrasil mayor.

- **`examples/instruments/with-attachments.yaml`** + cookbook page
  (hq-h5j2). Concrete starter for the `image / url / html`
  attachment shapes — first-contact friction was that there was no
  copy-pasteable example in the repo despite the README pitching
  attachments as a primary affordance.

### Changed

- **README response-shape docs** (hq-lux3). README now clearly
  separates the MCP `run_panel` verdict envelope (with `convergence`
  scalar) from the CLI `panel run --output-format json` shape (with
  `synthesis` block). Previously read like the same contract. Flags
  `panel_verdict` as a future feature for parity if/when it ships.

### Notes

- The cross-rig pattern note for this cycle: orchestrator dispatch
  paths silently degrade when a feature isn't wired into every
  entrypoint. v1 single-round path skipped the attachment resolver;
  MCP v3 multi-round path terminated after round 1. Same smell,
  different surface. The v1.0.6 fix scope deliberately covers BOTH
  paths in the new CI integration test (hq-83ye), and v1.0.7 will
  continue auditing dispatch-path symmetry.

- Bun 1.3.14 segfault that affected polecat productivity during this
  cycle is upstream (anthropics/claude-code#22632). Mitigation
  remains: pin Claude Code to 2.1.110 (Bun 1.3.13) +
  `DISABLE_AUTOUPDATER=1`. See jotunheim hq-dpm1 for the watch-item
  tracking when 1.3.15+ ships.

### Friction-sweep contributors (this release)

`yggdrasil`, `midgard`, `jotunheim`. Bead IDs: `hq-fjdx`, `hq-ilke`,
`hq-hjq8`, `hq-xq36`, `hq-83ye`, `hq-lux3`, `hq-h5j2`, `hq-pmi1`.
Sister-bug pattern callout: orchestrator dispatch silently degrades
when features aren't wired into every entrypoint.

## [1.0.5] - 2026-05-10

OpenRouter multimodal transport fix (the big one) + caller-API hardening
on the MCP boundary + persistence-CLI cleanup. Closes the v1.0.4
dogfood-surfaced bug cluster plus the long-standing OpenRouter-routes-
Anthropic image-drop that was masked across v1.0.0-1.0.4 by persona
roleplay in less-constrained prompts.

No API break — every v1.0.4 caller works unchanged.

### Fixed

- **`openrouter/anthropic/*` images silently dropped** (hq-olrk, fixes
  hq-m333). The OpenRouter provider used to send every request —
  including Anthropic-upstream models — through OR's
  `/v1/chat/completions` endpoint with OpenAI-shape multimodal blocks.
  For `openrouter/anthropic/*` traffic, OR's downstream conversion from
  OpenAI `image_url` to Anthropic image blocks is lossy in practice:
  the image content silently drops during normalization, the model
  receives text-only context, and the run looks successful (200 OK
  with "I don't see an image"). Reproduced empirically on midgard at
  **15/15**. The fix routes `openrouter/anthropic/*` through OR's
  Anthropic-native `/v1/messages` passthrough with an Anthropic-shape
  body (cache-control, native multimodal blocks, `anthropic-version`
  header all preserved). Non-Anthropic OR traffic
  (`openrouter/openai/*`, `openrouter/google/*`, etc.) continues to
  use chat-completions and is unaffected.

  Sources: OpenRouter Anthropic-passthrough docs,
  `claude-code-router#958` (same OR-Anthropic conversion bug).

- **`html` attachment type now reaches the model reliably** (hq-aaca).
  Previously, `HTMLBlock` was emitted as a separate text content
  block alongside the question text — two adjacent text blocks that
  the Anthropic API treats as semantically distinct, causing ~50%
  refusal rate via OpenRouter and inconsistent attention even on
  direct Anthropic. Fixed: `build_question_blocks` now inlines
  html-attachment text into the question's TextBlock with
  `--- HTML SOURCE ---` delimiters, eliminating the two-text-block
  shape. Wire-level HTMLBlock branches kept as defensive fallbacks.
  8 new tests in
  `tests/test_attachments_v1_0_1_wiring.py::TestHTMLAttachmentInlining`
  pin the contract.

- **AttachmentRef strict mode now reaches the MCP boundary**
  (hq-jviv). The v1.0.4 hq-nuz9 work promoted AttachmentRef to a
  strict BaseModel (`extra: forbid`) but the MCP `run_panel` handler
  read instrument attachments as raw dicts without going through
  `AttachmentRef.model_validate()`, so caller typos like `typo_field`
  silently propagated through the entire pipeline (echoed back in
  the response payload). Now: instrument-bank attachment shape
  enforces `extras='forbid'` at the parse boundary, surfacing
  ValidationError with the offending field name. Caller payload
  typos fail loud, as v1.0.4 promised.

- **`--save` flag now works in ensemble panel runs** (hq-0pnq).
  `synthpanel panel run --save` returned exit 0 but never wrote to
  `~/.synthpanel/results/`. The persistence call was wired only for
  the single-model path; ensemble runs (`--models a,b`) hit a code
  branch that bypassed the save step. Now both paths persist.

- **MCP `run_panel` accepts `models=[...]` (ensemble) without empty
  error** (hq-6j40). Calling `run_panel` via MCP with the `models`
  array parameter previously returned `"Error executing tool
  run_panel: "` (an exception with empty `str()` raised by an
  unwired code path). Now the ensemble arguments normalize at the
  MCP boundary: `models=[]` is treated as "no override", a
  single-element list collapses to the singular `model` parameter,
  and a multi-element list dispatches to the ensemble runner. Plus
  a clearer timeout policy on the MCP wrapper so callers can
  interrupt long ensemble runs.

### Changed

- **OpenRouter provider transport is now dual.** `openrouter/anthropic/*`
  models POST to `{base_url}/v1/messages` with Anthropic-shape body;
  everything else keeps `{base_url}/v1/chat/completions`. Callers that
  set a custom `OPENROUTER_BASE_URL` should ensure both paths are
  reachable. Anthropic serialization helpers (`build_anthropic_body`,
  `build_messages`, `build_content_blocks`, `parse_anthropic_response`,
  `parse_sse_stream`) moved into a shared
  `synth_panel.llm.providers._anthropic_format` module; the
  back-compat names (`_build_messages`, `_build_content_blocks`, ...)
  remain importable from `synth_panel.llm.providers.anthropic`.

### Site / Docs

- **Cache-Control headers aligned with dvi-25f cross-product policy**
  (hq-bxmp). Live audit of synthpanel.dev found drift on every
  bucket: HTML pages served `public, max-age=0` (policy:
  `private, no-store, must-revalidate`), well-known JSON endpoints
  cached too long (300/3600s vs policy's 60s), and unhashed static
  assets at 14400s vs policy's 300s. Fixed via `site/_headers` (CF
  Pages convention) with global `/*` set to HTML bucket and
  per-extension overrides for static assets. `site/_worker.js`
  markdown-rendition fallback flipped to HTML bucket. 3 new pinning
  tests in `tests/test_site_headers.py` so future edits can't
  silently drift again.

- **`docs/known-patterns/openrouter-byok-visual-review.md`** added
  with 3 example files (instrument-vars, vars, synthesize.py).
  Documents the inline-HTML pattern for visual review of UI/HTML
  content via OpenRouter (BYOK) — useful when callers can't reach
  Anthropic-direct creds, and as a reference example even after
  hq-olrk landed since the inline-HTML pattern is still the
  cleanest route for some UI-review workflows. Cross-town
  contribution from midgard mayor (Co-Authored-By: midgard mayor /
  openclaw@dataviking.tech).

### Notes

- The hq-olrk fix is independent of the hq-vw6o (v1.0.4) capability
  gate — both ship correctly. Capability gate still gives clear
  errors for known text-only models like Haiku 3.5; transport fix
  ensures vision-capable models actually receive images via OR.

- The per-rig Bun 1.3.14 segfault that affected the v1.0.5
  development cycle (see jotunheim hq-dpm1) is unrelated to
  synthpanel — upstream Bun runtime regression, mitigated by pinning
  Claude Code to 2.1.110 (Bun 1.3.13). Synthpanel itself is
  unaffected; this note is here so anyone reading the development
  history knows why this release cycle was bumpier than usual.

## [1.0.4] - 2026-05-10

Pydantic Phase 2 (caller-facing) + AttachmentRef migration + pre-flight
vision-capability gate. Brings the typed-Pydantic ergonomics from Phase 1
all the way to the SDK boundary so callers can pass their own
`response_schema=MyPydanticClass` and receive validated typed objects
back, promotes `AttachmentRef` from `TypedDict` to `BaseModel` for strict
runtime validation, and adds a pre-flight gate that fails fast with a
clear error when a caller attaches images to a known text-only model
(instead of silently burning the call and getting "I don't see an image"
from the persona).

No API break — every v1.0.3 caller works unchanged. The
`extract_schema` parameter now accepts `type[BaseModel] | dict | str |
None`, the `attachments` parameter still accepts plain dicts (auto-coerced
to `AttachmentRef` BaseModel internally).

### Added

- **Caller-facing `extract_schema=MyPydanticClass`** (hq-r39v).
  `synth_panel.sdk.run_panel(..., extract_schema=MyModel)` accepts a
  Pydantic `BaseModel` subclass directly. Each persona response is
  validated via `MyModel.model_validate_json(text)` and the typed
  instance is threaded through extraction, synthesis, and the final
  result. `ValidationError` surfaces on schema violations with the
  field path. Existing callers (registered string names, raw JSON
  Schema dicts, `None`) work unchanged via the dispatch in
  `synth_panel._runners.resolve_extract_schema`. README has a new
  "Typed extraction with Pydantic (1.0.4)" subsection with the
  caller-facing example.

- **Pre-flight vision-capability gate** (hq-vw6o) at
  `synth_panel.llm.capabilities`. `LLMClient.send` and
  `LLMClient.stream` now scan `CompletionRequest.content` for
  `ImageBlock` / `DocumentBlock` after `_prepare` (alias resolution)
  and raise `LLMError(BAD_REQUEST)` when the resolved model matches a
  known text-only pattern (e.g., `claude-3.5-haiku`,
  `claude-3-haiku`). Wired after alias resolution so OpenRouter
  routes like `openrouter/anthropic/claude-3.5-haiku` are gated
  correctly. Pattern list guards against false-positive on Haiku 4.5
  / Sonnet / Opus (all vision-capable). Caller now gets a clear
  error path instead of "NO IMAGE RECEIVED" from the persona.

  **Caveat:** the gate is a UX improvement for known text-only
  models, *not* a transport-layer fix. Vision-capable models on
  OpenRouter's Anthropic routes still drop image content blocks
  ~100% of the time per midgard mayor's deterministic repro
  (cross-town, 2026-05-10) — tracked separately as `hq-m333` for a
  future v1.0.x or v1.1.0.

### Changed

- **`AttachmentRef` promoted `TypedDict` → `BaseModel`** (hq-nuz9).
  `synth_panel.attachments.AttachmentRef` is now a Pydantic model
  with strict field validation (`model_config = {"strict": True,
  "extra": "forbid"}`). All call sites that previously passed plain
  dicts continue to work via Pydantic's `BaseModel.model_validate`
  on input boundaries. Internal codepaths now use attribute access
  (`ref.media_type`) instead of dict-key access (`ref["media_type"]`).
  Catches typos in caller payloads at the boundary instead of
  KeyErrors deep in serialization.

### Site / Docs

- **Cross-property brand unification** (hq-qxpe). DataViking-family
  unified footer + header back-link added to all 6 site/ surfaces
  (index, blog post, MCP card, recommended-models, calibration,
  panel-run docs). Shipped alongside the dataviking.tech IA
  restructure (MTG + Living Stone tucked under "Other Projects")
  and the canonical token spec extracted from dataviking-site
  (`docs/brand.md`). Synthpanel keeps its terminal-green accent
  (defensible per-product); structural unification carries the
  cohesion. Pure site/ + docs change — does not affect the wheel.

## [1.0.3] - 2026-05-10

Pydantic Phase 1 (additive). Adopts `pydantic>=2.7,<3` as a base
dependency and adds typed response models alongside the existing JSON
Schema dicts, with a parallel registry and an `extract_schema`
dispatch that accepts `type[BaseModel] | dict | str`. Internal
synthesis layer reads typed objects post-extraction; map-reduce
partials validate at the map boundary so single-persona schema flake
fails loud instead of producing empty themes. No API break — every
v1.0.2 caller works unchanged.

Pydantic Phase 1 patterns adopted directly from midgard mayor's
boardroom production code (cross-town consult 2026-05-09): v2.7+
pin, `model_validate_json` C-fast-path on parse boundaries, `Literal`
for state enums (caught real polecat hallucinations in their work),
`Field(ge=, le=, gt=, min_length=)` constraints. Hand-written wire
schemas remain the v1.0.0-frozen MCP contract; `model_json_schema()`
output is verified against the static schemas via a CI gate (P3) so
Pydantic minor-version drift can't silently change what we emit on
the wire.

Phase 2 (caller-facing `response_schema=MyPydanticClass` API +
AttachmentRef migration) is deferred to v1.1.0+ per the agreed phased
plan — keeps the v1.0 MCP wire frozen on JSON Schema.

### Added

- **Pydantic models for the 5 existing structured-output schemas**
  (hq-e25n) at `synth_panel.structured.models`: `PickOne`, `Likert`,
  `YesNo`, `Ranking`, `AnnotatedChoice`. Each uses `Literal` for
  closed enums, `Field(ge=, le=, gt=, min_length=)` for numeric and
  string constraints, and `model_validate_json` (C-fast-path) on the
  parse boundary. `MODEL_REGISTRY: dict[str, type[BaseModel]]`
  provides static lookup keyed by the same names as the existing
  JSON Schema registry.
- **`extract_schema` dispatch accepts `type[BaseModel] | dict | str`**
  (hq-e25n). `synth_panel._runners.resolve_extract_schema` now
  recognises all three input shapes and threads a `(schema, model)`
  pair downstream so structured-output extraction can call
  `Model.model_validate_json(text)` when a typed model is provided
  (clear ValidationError with field path on schema violations) or
  fall back to dict-only validation when only a JSON Schema dict is
  available. Existing callers (registered names, raw dicts, no
  schema) work unchanged.
- **Synthesis layer typed-attribute access** (hq-swzx). `synthesis.py`
  and `_runners.py` map-reduce paths now use `_typed_or_dict(extr,
  attr)` helper so persona extractions read as `r.choice` (when
  Pydantic) or `r["choice"]` (legacy dict) without dict-key
  fragility. Map-reduce partials validate at the map boundary via
  `PartialSummary.model_validate(m)`; a single-persona map flake now
  raises a clear `ValidationError` instead of silently producing
  empty themes downstream.

### Tests

- **Pydantic minor-version drift CI gate** (hq-fmo0) at
  `tests/test_pydantic_roundtrip.py`. Asserts that
  `model_json_schema(M)` matches the static `*_SCHEMA` dict
  structurally for each of the 5 schema-model pairs (property names,
  required list, type per property). Catches any silent drift in
  Pydantic's JSON-Schema output across minor versions (e.g.,
  `additionalProperties` defaults shifted 2.5→2.6 per midgard's
  empirical signal). Plus `test_pydantic_version_pinned` enforces
  `pydantic>=2.7` at runtime.

### Dependencies

- `pydantic>=2.7,<3` added to base `dependencies` in `pyproject.toml`
  (not optional — synthpanel needs it on every install). +5 MB wheel
  footprint, fine for a Python tool that runs locally.

### Out of scope (deferred)

- Pydantic for `AttachmentRef` (currently TypedDict; v1.0.x bake
  before promoting)
- Caller-facing `response_schema=MyPydanticClass` (Phase 3, v2.0.0)
- Replacing dataclass `ContentBlock` union with Pydantic
- Wire-format change (v1.0.0 frozen MCP contract stays JSON Schema)

## [1.0.2] - 2026-05-09

Hotfix release: closes the four remaining v1.0.x multimodal-attachments
wiring gaps surfaced during the 2026-05-09 dogfood panel and tracked in
hq-2yc6 (G2/G5/G6/G7). Combined with v1.0.1 (G1+G3), the multimodal
attachments feature is now fully production-ready end-to-end without
quality/efficiency caveats: bank-referenced URL attachments fetch and
reach the model with content; large multimodal panels persist their
synthesis to disk; attachment payloads land in content-addressable
sidecar storage instead of inflating result.json with inline base64;
panel-shared attachments lift to the cached prefix instead of being
re-emitted per question.

All four are bugfix-shaped — additive on top of v1.0.1, no API breaks,
no data-model changes. Tracking epic: hq-w7om.

### Fixed

- **G2 — `panel_shared_attachments` lift in run_multi_round_panel**
  (hq-ovxl). The SDK's `Instrument.attachments` bank was parse-validated
  and resolved per-question via the v1.0.1 G3 fix, but the explicit
  shared-prefix optimization never engaged — every question paid the
  per-question attachment cost even when bank entries were reused.
  `_compute_panel_shared(round_questions, bank)` now lifts any bank
  entry referenced by ≥2 questions to `panel_shared_attachments`,
  threaded through `run_multi_round_panel` → `run_panel_parallel` so
  the canonical block-emission order (shared docs → shared images →
  per-question → text → cache marker) fires correctly. Cache hit rates
  visible in stratum_fp logs now reflect actual sharing. Single-use
  bank entries stay per-question; explicit `shared: true` flag on bank
  entries reserved for v1.1.0 if a use case surfaces.
- **G5 — URLBlock lowering at frame stage** (hq-8iz3). URL attachments
  produced `URLBlock` plan nodes that never resolved through the
  hq-gmju fetcher in the panel-running path; URLBlocks reached
  serialization unhandled and silently dropped. A new frame-stage
  `lower_url_blocks(blocks, fetch_cache)` step runs before wire
  emission: each URLBlock dispatches through the hq-gmju content
  ladder per `attachment_intent` (text → trafilatura, visual →
  Playwright screenshot, both → emit both), reusing fetches across
  personas via a per-run in-memory L1 cache backed by the existing
  on-disk content-addressable cache at `~/.synthpanel/cache/url/`.
  SSRF perimeter (RFC1918, IMDS, DNS rebinding mitigation) preserved;
  per-attachment `on_failure` policy honoured. Wire serializers
  (`anthropic.py`, `_openai_format.py`) no longer encounter URLBlock —
  lowering runs first.
- **G6 — synthesis persisted on attachment-bearing panels** (hq-2p32).
  Larger multimodal panels lost the synthesis from saved `result.json`
  even though synthesis ran and the cost was reflected in `total_cost`.
  Root cause: the multi-round path in `sdk.run_panel` invoked
  `save_panel_result(...)` without forwarding `mr.final_synthesis`.
  Smaller text-only panels happened to traverse a different save path
  that already passed it. Fix threads `synthesis=mr.final_synthesis`
  uniformly through both code paths. Existing text-only panel
  synthesis save unaffected.
- **G7 — CAS attachment persistence wired from SDK** (hq-hjk8). Per
  the hq-cqt5 design, attachment payloads should land in a
  content-addressable sidecar (`~/.synthpanel/attachments/<sha256[0:2]>/
  <sha256>.<ext>`) with per-run `<result-id>.attachments/refs.json`
  carrying typed `AttachmentRef` records — bytes never inline. v1.0.1
  saved a 79 MB `result.json` with all base64 inlined and no sidecar.
  New `_extract_attachment_refs(instrument)` helper walks the bank,
  writes blobs via `synth_panel.attachments.store.write_blob`, builds
  AttachmentRefs, and `sdk.run_panel` threads the dict to
  `save_panel_result(attachments=...)`. `result_format_version` now
  bumps to `"1.1"` when any attachment is present. Cross-run dedup
  works (rerunning a panel reuses CAS blobs). Existing readers
  (cost_summary, analyze, inspect) unaffected; new opt-in hydration
  via `get_panel_result(load_attachments=True)`.

### Tests

- `tests/test_attachments_v1_0_2_panel_shared.py` — G2 coverage
- `tests/test_fetch_lower.py` — G5 URLBlock lowering coverage
- `tests/test_sdk_attachment_extraction.py` — G7 CAS extraction coverage
- G6 covered by extending existing persistence tests

### Empirical validation

15-persona × 3-question × 10-image dogfood panel with the canonical
bank-ref pattern on `openrouter/anthropic/claude-sonnet-4.5`:
content-aware feedback from every persona; result.json now compact
(refs only) with CAS sidecar populated; synthesis present in saved
result; URL attachment fetches once per panel run with content
reaching the model.

### Known v1.0.x limitations still open (planned for v1.1.0)

- Pydantic adoption for response-structure validation — async-wisp
  consult with midgard mayor (boardroom project) complete; phased
  plan recorded.
- Explicit `shared: true` flag on bank entries — currently inferred
  from ≥2 references; explicit opt-in deferred until a use case
  surfaces.

## [1.0.1] - 2026-05-09

Hotfix release: closes two wiring gaps in the v1.0.0 multimodal-attachments
system that were surfaced during a 2026-05-09 dogfood panel against
dataviking.tech preview tiles. Without these fixes, image and document
attachments silently disappeared on the OpenRouter / OpenAI-compat path,
and the bank-ref pattern (the canonical reference shape per the v1.0.0
data-model design) dropped at the orchestrator filter.

### Fixed

- **OpenAI-compat path now serialises `ImageBlock` / `DocumentBlock` /
  `HTMLBlock`** (G1). `_content_to_openai` previously emitted only
  `TextBlock` and `ToolInvocationBlock` branches; multimodal blocks fell
  through unhandled — no error, no warning, just dropped. Persona responses
  on the OpenRouter / xAI / generic OpenAI-compat path read "I don't see an
  attached image" even when the orchestrator emitted the multimodal blocks
  correctly. The fix emits OpenAI-style `image_url` data-URI for inline
  base64 images, `image_url` with the literal URL for URL-source images,
  the `file` content type for inline-base64 PDFs (per OpenAI's vision/file
  contract), and lowers `HTMLBlock` to text so the markup reaches the
  model verbatim. The native Anthropic provider was unaffected — the
  bug was scoped to `_openai_format.py`. Sample post-fix verification:
  Devon Kim accurately described a thumbnail's "yellow center circle,
  scattered gray dots, network/orbit pattern" via Sonnet 4.5 on
  OpenRouter.
- **Bank-ref strings in `question.attachments` now resolve at the
  orchestrator level** (G3). The canonical hq-xzsm data-model design
  supports two reference shapes: bank-ref strings into the top-level
  `Instrument.attachments` map (`["hero_creative_v3"]`) and inline dict
  blocks (`[{"type": "image", ...}]`). The frame-stage filter at
  `orchestrator.py:879-883` retained only dict-form refs, so bank-ref
  strings silently dropped before reaching the multimodal block emitter
  — every persona received only the question text. The fix adds
  `_resolve_question_attachment_refs(questions, bank)`, called inside
  `run_multi_round_panel` before each round dispatches, that expands
  string refs into copies of the bank entry. Inline dicts pass through
  unchanged; questions without attachments are no-op; absent bank
  preserves the legacy v0.12.0 pass-through behaviour.

### Tests

- 13 new tests at `tests/test_attachments_v1_0_1_wiring.py` lock in both
  fixes against silent regression: ImageBlock-with-base64,
  ImageBlock-with-URL, DocumentBlock-emits-file-payload, HTMLBlock-lowers-
  to-text, text-only-fast-path-preserved, regression-guard for image
  silent-drop, bank-ref string→dict expansion, dict refs pass through,
  unresolved bank ref raises ValueError, no-bank legacy fallback,
  attachment-less questions unchanged, resolved dict is a defensive
  copy (no alias bleed), non-string non-dict ref raises ValueError.

### Known v1.0.x gaps still open (planned for v1.1.0)

The dogfood test surfaced four additional wiring gaps that are *not*
addressed by this hotfix and remain known-issues in v1.0.1:

- **G5 — URL attachments don't lower to fetched content**. Inline
  `{"type": "url", "url": "..."}` attachments produce `URLBlock` plan
  nodes, but the lowering step (URLBlock → fetched markdown TextBlock or
  screenshot ImageBlock via the hq-gmju fetcher) doesn't fire in the
  panel-running path. Workaround: paste page contents as `html` or
  `text` attachments until v1.1.0.
- **G6 — Synthesis not persisted on attachment-bearing panels**. Smaller
  text-only panels save synthesis to `result.json` correctly; large
  multimodal panels (e.g. 15 personas × 3 questions × 10 image
  attachments) lose the synthesis from the saved record. Synthesis still
  runs and is returned on the `PanelResult` object — only the on-disk
  save path is affected.
- **G7 — CAS persistence not invoked from SDK**. The hq-cqt5 design
  specified content-addressable sidecar storage (bytes never inline);
  empirically a 15-persona × 10-image panel writes a 79 MB `result.json`
  with all base64 inlined and no `~/.synthpanel/attachments/` sidecar.
  `result_format_version` stays "1.0" instead of bumping to "1.1" when
  attachments are present.
- **G2 — `panel_shared_attachments` parameter not threaded through SDK**.
  `run_panel_parallel` accepts `panel_shared_attachments=`, but the SDK's
  `run_panel()` doesn't compute it from the instrument's bank. Bank-ref
  resolution (G3, fixed here) makes the bank usable; the explicit
  shared-prefix optimisation can come later.

Tracking bead: hq-2yc6 (the v1.0.0 wiring-gap bug filed during the
dogfood test). v1.1.0 will close the four gaps above.

## [1.0.0] - 2026-05-09

The frozen MCP contract release. The schema at
[`synthpanel/schemas/v1.0.0.json`](src/synth_panel/schemas/v1.0.0.json) is
embedded in the package, echoed in every response and every error
(`schema_version: "1.0.0"`), and append-only — breaking changes will ship as a
parallel `v2.0.0.json`, never as in-place edits.

This release also lands the **multimodal attachments system** (hq-pojo epic) —
panels can now react to images, fetched URLs, PDFs, and inline HTML across all
four attachment types, with per-persona stratified delivery, prompt-caching
across panel-shared content, and a unified security perimeter for URL fetches.
See the *Added (multimodal attachments, hq-pojo)* sub-section below.

### Added (multimodal attachments, hq-pojo epic)

- **Question/panel attachments data model** (hq-l0lw) — questions can carry
  references into a top-level `Instrument.attachments` bank (string IDs) plus
  `inline_attachments` for one-off blocks. Four attachment types supported:
  `image` (PNG/JPEG/GIF/WebP), `document` (PDF), `url` (fetched at frame
  stage), and `html` (inline text). `ContentBlock` union extended with
  frozen `ImageBlock` / `DocumentBlock` / `URLBlock` / `HTMLBlock` dataclasses
  using `Literal`-discriminated `type` fields and a tagged-union `source`
  variant (base64 / url / file_id) mirroring Anthropic API shape. New
  `accept_multimodal_sampling: bool = False` parameter on the four MCP tools
  preserves the existing silent-drop semantics for callers who haven't
  opted in (T6 migration: feature flag).
- **URL attachment fetcher with security perimeter** (hq-gmju) — content-type
  ladder (try `Accept: text/markdown` → trafilatura on HTML → optional
  Playwright screenshot for visual artifacts) selected per question's
  `attachment_intent` field. SSRF perimeter: deny RFC1918, loopback,
  link-local, IMDS (`169.254.169.254`), CGNAT, IPv6 ULA, IPv4-mapped IPv6;
  DNS-rebinding mitigation via pin-to-resolved-IP; magic-byte sniff via
  `puremagic`; size caps 8/25/10 MiB (HTML/PDF/image); httpx
  `Timeout(3,10,3,2)`; redirect cap 3 with per-hop SSRF + content-type
  recheck. Content-addressable on-disk cache (`~/.synthpanel/cache/url/`)
  with 15-min default TTL, 2 GiB LRU, per-question `pin: true` opt-out.
  Per-attachment failure policy: `abort` / `skip_question` (default) /
  `placeholder`. New optional dep `synthpanel[visual]` for Playwright.
- **Per-persona stratification** (hq-iczd) — each attachment can carry an
  `attachment_filter: list[predicate]` clause (predicates `{field, op,
  value}` with implicit AND across, ops `equals`/`contains`/`matches`/
  `gte`/`lte`/`in`). Filter evaluation reuses the v3 routing-predicate
  engine (refactored to accept arbitrary `valid_fields` allowlists).
  Non-matching personas receive the question text without the attachment
  — no skip, no fallback, no placeholder; their response is still a valid
  datapoint ("what would you say without seeing the ad?"). Frame-stage
  evaluation site at `orchestrator.py` mirrors the existing budget-gate
  sibling. New `count_strata(personas)` helper exposes the partition
  cardinality K to the caching layer.
- **Multimodal block emission + prompt caching** (hq-0pbp) — block order
  per Anthropic best practices: shared documents → shared images →
  per-question attachments → text. `cache_control: ephemeral` placed on
  the last shared block so the entire shared prefix is cached. Persona
  system prompts always cached. K≤5 strata cap enforced at frame stage
  (raises `PanelPlanningError` above K=5) — architecturally prevents the
  per-persona cache-defeat cliff (would cost 1.25× of uncached). 5-min
  cache tier default with explicit `panel.cache_tier` override; bypass
  for P=1 panels and prefixes below Anthropic's 1024-token cacheability
  minimum. Stratum-fingerprint logging surfaces cache-hit telemetry
  without sending custom keys (Anthropic derives keys from prefix bytes).
- **Persistence layer (CAS)** (hq-qd7r) — attachment payloads stored in a
  content-addressable two-tier layout: global CAS at
  `~/.synthpanel/attachments/<sha256[0:2]>/<sha256>.<ext>` for cross-run
  dedup, plus per-run `<result-id>.attachments/refs.json` carrying typed
  `AttachmentRef` records `{id, kind, sha256, content_type, byte_size,
  source_uri?, fetched_at?, dims?, thumb_sha256?, ...}`. Result JSON
  stores ref IDs only — bytes never inline. New
  `ANNOTATED_CHOICE_SCHEMA` extends the extractor registry with optional
  `attachment_id` linking responses back to the attachment they
  reacted to. New top-level `result_format_version` field on saved
  results (`"1.1"` when attachments present, else `"1.0"`); existing
  consumers untouched. New env var `SYNTH_PANEL_ATTACHMENT_DIR` for
  out-of-tree CAS storage. `pip install synthpanel[pdf]` covers PDF
  payload handling.
- **PDF attachment ingest decision tree** (hq-glz6) — moved from prior
  [Unreleased]. Native PDF submission for text-bearing files within
  Anthropic's limits, text extraction via `pypdfium2` for oversize
  text-bearing PDFs, page-as-image rendering at 150 DPI for scanned PDFs.
  Encrypted PDFs reject with `PdfEncryptedError`; oversize-and-scanned
  combinations reject with `PdfOversizeScannedError`. Submission mode
  recommendation (inline base64 below 4 MiB, Files API above) and an
  estimated-token cost preview surface alongside every plan. Install
  with `pip install synthpanel[pdf]` (`pypdfium2` + `Pillow`, both
  permissively licensed wheels — no system binary).

### Added

- `decision_being_informed` — required string field (12–280 chars, single
  line, UTF-8) on `run_panel`, `run_quick_poll`, and `extend_panel`. Echoed
  verbatim into `panel_verdict.meta.decision_being_informed` and stamped on
  every transcript row for audit join. Not used on `run_prompt`
  (sub-decisional scratch work). Validated pre-model at the request boundary.
- `panel_verdict.json` envelope — `additionalProperties: false`, with
  `headline` (≤ 140 chars), `convergence` (0–1), `dissent_count` (≥ 0),
  `top_3_verbatims` (0–3 `{persona_id, quote}` items), `flags[]` (closed
  enum), `extension[]` (open observability), `full_transcript_uri`, `meta`,
  and `schema_version`. Returned alongside every successful panel run.
- `flags[]` closed enum — seven codes (`low_convergence`, `demographic_skew`,
  `small_n`, `persona_collision`, `out_of_distribution`,
  `refusal_or_degenerate`, `schema_drift`) each carrying
  `severity: "info" | "warn" | "block"`. Multiple flags can stack; highest
  severity wins for gating. Agents must NOT branch on `extension[]` — that's
  the open escape hatch for non-enum signals.
- Typed error envelope — `{error_code, message, field_path?, schema_version,
  retry_safe}`. v1 codes: `MISSING_DECISION`, `DECISION_TOO_LONG`,
  `INVALID_TOOL_ARG`, `INVALID_FLAG`, `SCHEMA_DRIFT`, `MODEL_TIMEOUT`,
  `INTERNAL_ERROR`. `retry_safe = true` only for `MODEL_TIMEOUT` and
  `SCHEMA_DRIFT` pre-exhaustion.
- Embedded schema asset — `synthpanel/schemas/v1.0.0.json` ships inside the
  package. No remote URL, offline-safe, deterministic, no DNS dependency.
- `SYNTHPANEL_DRIFT_DEGRADE` env flag — opt-in beta of the v1.1 default.
  When set to `1`, 3-strike retry exhaustion returns a degraded
  `panel_verdict.json` with `flags: [{ "code": "schema_drift",
  "severity": "warn" }]` instead of the typed `SCHEMA_DRIFT` error.
  **Off by default in v1.0.0; on by default in v1.1.0.** See
  [docs/mcp.md#host-integration-flags](docs/mcp.md#host-integration-flags).
- New canonical docs:
  [`docs/response-contract.md`](docs/response-contract.md) (field-by-field
  reference), [`docs/migration-v1.md`](docs/migration-v1.md) (v0.12 → v1.0
  walkthrough with grace-window state diagram), and
  [`docs/methodology.md`](docs/methodology.md) (the inspectability landing).
  `SPEC.md` carries a verbatim frozen-contract appendix.

### Changed

- MCP request schema is **breaking** for panel-running tools — the new
  `decision_being_informed` field is required. Gated on `schema_version`;
  callers that miss it during the v1.0.x grace window log warning
  `W_DECISION_MISSING` and run with the synthesized placeholder
  `"unspecified-legacy-call"`. v1.1.0 will hard-reject with `MISSING_DECISION`.
- Validation now runs at **both sides** of structured output — request
  validated before model invocation (cheap reject, no token spend), response
  validated before the artifact leaves the server (closed-enum `flags[]`,
  `additionalProperties: false`).
- README reframed agent-first: lede is now an MCP tool-call example, not a
  CLI invocation. CLI moves to a "Human Operator" section below the fold.
  `convergence` is defined inline as "0–1 agreement score your agent can
  threshold on"; "BYO-key" is disambiguated as "bring your own LLM key —
  Claude, OpenAI, Gemini, or local"; the word "primitive" is dropped (per
  validation panel: read as lower-level infrastructure).

### Deprecated

- Implicit-decision panel calls (no `decision_being_informed` field).
  Synthesized in v1.0.x with warning `W_DECISION_MISSING`; **removal in
  v1.1.0** — at which point the call returns `MISSING_DECISION` and no panel
  runs. Migrate during the v1.0.x window.

### Removed

- Nothing. v1.0.0 is additive on top of the v0.12 surface; the only break is
  the new request-side requirement, which is gated by schema version.

### Added (cycle work folded in from prior `[Unreleased]`)
- (GH-289, sp-b8y47x) New bundled `students` persona pack — 15 personas spanning undergraduate (5), graduate (5), and non-traditional learners (5). Covers in-state public, HBCU, liberal-arts, large-public commuter, and international F-1 cohorts at the undergrad layer; PhD, professional master's (MBA, MPH), and MD-PhD at the graduate layer; and returning adult, online part-time, bootcamp career-changer, GI-bill veteran, and working-clinical-professional online-master's profiles for non-traditional. Demographics, funding sources, and pain points differ per persona to avoid the "rounding-error" failure mode flagged in the n=100 self-audit. Total shipped persona count rises 145 → 160 across 10 bundled packs.
- (GH-297, sp-tzavk0) `synthpanel analyze <result> --output responses-csv` — emit one row per (panelist, question) response as a flat CSV for spreadsheet workflows (Google Sheets / Excel pivots, qualitative coding). Default columns: `persona_id, persona_name, question_id, question_text, response, response_type, cost`; opt-in extras via `--columns` (`model`, `variant_of`, `input_tokens`, `output_tokens`, `error`). Distinct from the existing `--output csv` analytical summary. Cells are CSV-injection-safe (formula triggers `=`, `+`, `-`, `@` and control chars get a `'` prefix per OWASP guidance), embedded newlines and commas round-trip cleanly through `csv.DictReader`, and rows use RFC 4180 CRLF terminators. Structured (dict/list) responses serialize as JSON.
- (GH-308, sp-4y5.1) `synthpanel pack diff <pack-a> <pack-b>` — compare two persona packs side-by-side. Reports added/removed/unchanged/changed personas (matched by name), per-persona field-level diffs (age, occupation, background, traits, gender), and composition deltas (age range, age mean, role distribution, gender split when present). Accepts built-in pack names, user-saved pack IDs, or YAML file paths for either side; supports `--format json` for CI integration.
- (sy-ws76) `synthpanel panel run --resume <run-id>` is now a standalone entry point: pass just the run id and the original `--personas` / `--instrument` paths are recovered from the checkpoint's saved CLI args. Existing flags can still be passed to override. New `--allow-drift` flag downgrades checkpoint config drift from a hard error to a warning ("statistically inconsistent" run), for cases where intentionally mixing configs is acceptable. Pre-`sy-ws76` checkpoints (no `cli_args` field) still load — back-compat preserved.
- (sp-4loufu) Per-persona LLM overrides via a YAML `llm_overrides:` block on each persona, accepting `temperature`, `top_p`, `max_tokens`, and `model`. Lets researchers vary stochasticity within a single panel — e.g. a "deliberate" persona at `temperature: 0.3` and an "exploratory" one at `0.9` — without giving up the run-level `--temperature` default for everyone else. Overrides flow through the structured-output and extraction calls too, are validated up front (out-of-range temperature, unknown keys, etc. fail the run before any LLM call), and naturally show up in per-persona cost tracking because each persona's request carries its own `max_tokens`. The new `llm_overrides.model` is recognised alongside the legacy top-level `model` field; top-level wins on collision so existing YAML is unchanged.
- (sy-cxp) `--seed N` flag on `panel run` for reproducible sampling. Forwarded to providers that support it (OpenAI, Gemini, xAI, OpenRouter) and recorded in `metadata.parameters.seed` plus the resume fingerprint. Anthropic and other non-supporting providers log a single warning per run and proceed without determinism. See `docs/reproducibility.md` for the boundary between `--seed` (new runs) and `--resume` (replay).

### Documentation

- `docs/oauth-discovery.md` (sy-iaf) — records the AR-5 determination that OAuth/OIDC discovery metadata is not applicable to SynthPanel: the CLI is local, the MCP server is stdio, and synthpanel.dev is a static site, so no protected API exists for `/.well-known/openid-configuration` or `/.well-known/oauth-authorization-server` to describe.

### Fixed
- (GH-340, sp-xehk) Provider clients now share a single, formally-named retry/backoff policy (`synth_panel.llm.retry.RetryPolicy`) instead of relying on retry logic buried inside `LLMClient`. The class encapsulates the budgets, backoff curve, and `Retry-After` handling that previously lived in `_with_retry`, so all five providers (Anthropic, Gemini, xAI, OpenRouter, OpenAI-compatible) get identical behavior and the policy is now reusable / injectable via `LLMClient(retry_policy=...)`. Retry attempts log at `INFO` (was `WARNING`) with `provider`, `attempt`, and `reason` fields so operators can see where backoff is happening per-provider without enabling DEBUG. Provider display names are formalized on `ProviderConfig.name`. Behavior unchanged: 401 still does not retry, 429 retries with budget+jitter, server-supplied `Retry-After` still dominates exponential backoff. Closes #340.
- (GH #287, sp-stkj2w) The "Missing API key" error raised by every provider now names the missing env var (`ANTHROPIC_API_KEY`, etc.), recommends both the persistent option (`synthpanel login --provider <name>`) and a one-shot `export <ENV_VAR>=...`, and — for Anthropic specifically — calls out the Claude Code OAuth footgun (Claude Code's keychain tokens use a different auth scheme and are not reusable as Anthropic API keys). The Gemini path lists both `GEMINI_API_KEY` and `GOOGLE_API_KEY`. New `synth_panel.credentials.missing_api_key_message` helper centralises the wording so future providers stay consistent. Closes #287.
- (GH #298) Terminal output (model-assignment table, `panel inspect` per-persona summary, `analyze` frequency table) now aligns columns correctly when persona names or theme categories contain CJK characters, accented Latin (precomposed or decomposed), or emoji. Previously padding counted code points rather than rendered cells, so `"王芳"` (4 cells, 2 code points) would shift right of an ASCII row and break alignment. New stdlib-only `synth_panel.text_width` helper handles East Asian Wide / Fullwidth, combining marks (zero-width), and common emoji blocks; no new dependencies.
- (sp-4y5.9, GH #311) `synthpanel pack inspect <pack-id>` no longer silently truncates long persona fields. Description, occupation, background, and traits are word-wrapped to terminal width by default with a continuation indent. Pass `--full` to skip wrapping and preserve embedded newlines (paragraph breaks survive). Previously a long `description` or `background` field would appear cut off at terminal width with no indication that truncation had occurred — a copy-paste hazard for users reviewing personas. Closes #311.
- (GH-299, sp-60w2te) `synthpanel panel run --checkpoint-dir` no longer silently overwrites an existing checkpoint when a fresh run id collides with one already on disk. Before this fix, a `new_run_id()` collision (or two concurrent invocations sharing the same checkpoint root) could destroy the first run's progress without warning, and a later `--resume <id>` would resume the second run's state instead. The checkpoint writer now refuses on collision with a clear error pointing at `--resume <id>` to continue or `--force-overwrite` to replace; concurrent fresh starts on the same id are blocked by a per-directory `fcntl.flock`, so the race condition cannot happen even if the existence check would have passed. Closes #299.

### Fixed (continued)
- (GH-338, sp-d1x0) `structured/output.py` previously swallowed JSON parse errors and schema non-conformance (missing required fields) without retrying, returning a silent `is_fallback=True` result after only one attempt. The engine now implements a **3-strike retry policy**: (1) normal prompt, (2) corrective turn appended with the failed response + tool-result error block so the model sees its mistake, (3) final strike escalates to `sonnet` when the original model is in the cheap/flash tier (`haiku`, `flash`, `lite`, `mini`, `nano`, `small`). Terminal failure emits a `synth_panel.structured.output` `logger.warning` consistent with the sp-g59o surface. `StructuredResult.total_usage` accumulates token counts across all retry attempts for accurate cost telemetry; callers in `synthesis.py` and `orchestrator.py` now use `result.total_usage` instead of `result.response.usage`. Closes #338.

### Changed (loudness)
- (sp-g59o) Detection: warn loudly when synthesis output appears unstructured (likely model schema-adherence flake). Triggered when every list field — themes, agreements, disagreements, surprises — is empty while the recommendation slot carries >600 chars of prose. Surfaces as a `synth_panel.synthesis` `logger.warning`, on `SynthesisResult.warnings`, and propagated up to `PanelResult.warnings`. Schema-honoring runs are unchanged. Observed at ~25% on `gemini-flash-lite` synthesis; detection is provider-agnostic.
- (sp-k2ed4a) MCP sampling truncation surfacing: `synth_panel.mcp.sampling.sample_text` now detects host-side `stopReason="maxTokens"` truncation, logs a `logger.warning`, and returns `truncated`/`requested_max_tokens`/`warning` fields. The sampling paths in `run_prompt`, `run_panel`, and `run_quick_poll` propagate truncated turns into the response `warnings` list with persona/synthesis labels, so a failed structured-output parse can be attributed to the host clipping output rather than the model ignoring the schema. No protocol-level startup check is possible — MCP capability negotiation does not expose the host's max_tokens cap — so per-turn detection is the loud surface.

### Fixed
- (sp-4y5.7, GH #309) Cap `anthropic` and `openai` SDK loggers at WARNING by default, completing the third-party DEBUG-leak fix from PR #352. Issue #309 explicitly listed both libraries as noisy at DEBUG; they were missing from the `_NOISY_LOGGERS` set. `--debug-all` (and its help text) now surface them alongside `httpx`/`httpcore`/`urllib3`/MCP/websocket libs.
- (hq-kmyx) `tests/test_attachments_filter.py::TestInstrumentValidatesAttachmentFilter::test_valid_filter_parses` fixture aligned with the canonical bank-plus-string-ref shape per hq-xzsm data-model design (top-level `Instrument.attachments` keyed by string IDs, dict-form refs go in `inline_attachments`). Surfaced and resolved during the hq-pojo I-phase swarm.

## [0.12.0] - 2026-04-26

Minor bump shipping two new CLI features (`--best-model-for`,
`--submit-to-synthbench`), the new `synthpanel pack calibrate` subcommand,
six bundled persona packs deepened from 5 → 15 personas (~60 new personas
addressing the 'too generic' finding from the n=100 self-audit), and two
`synthpanel report` rendering improvements.

### Added
- (sp-zq3) `synthpanel panel run --best-model-for TOPIC[:DATASET]` — fetches SynthBench public leaderboard.json, picks the top-SPS model for the requested topic, surfaces the recommendation with SPS, JSD, n, and $/100q context. Uses 24h cache at `~/.synthpanel/synthbench-cache.json`. Falls back gracefully when SynthBench is unreachable. Plus a generated docs page at synthpanel.dev/recommended-models mapping use-cases to SynthBench-validated model picks.
- (sp-ezz) `synthpanel panel run --submit-to-synthbench` — opt-in submission of calibrated panel runs to the SynthBench public leaderboard via Tier-2 API. Hard-fails at parse time without `--calibrate-against` (only calibrated runs produce SynthBench-shaped scores). First-run consent prompt explaining privacy implications; `--yes` to bypass for CI use. Requires `SYNTHPANEL_SYNTHBENCH_API_KEY` env var.
- (sp-sghl) `synthpanel pack calibrate <pack-yaml> --against DATASET:QUESTION` — first-class pack calibration: runs a panel using the pack against a SynthBench baseline, computes JSD, writes the result back into the pack YAML as a top-level `calibration:` list. Supports `--n`, `--models`, `--samples-per-question`, `--output`, `--dry-run`, `--yes`. Round-trip preserves persona definitions exactly via ruamel.yaml. Plus new docs/calibration.md methodology guide explaining JSD interpretation and which packs to calibrate against which questions.
- (sp-edqg, sp-bzgm, sp-xjty, sp-cs0q, sp-z28k, sp-ebrl) Six bundled packs deepened from 5 → 15 personas: developer, enterprise-buyer, general-consumer, healthcare-patient, recruiters-talent, startup-founder. ~60 new personas across role specialty, demographic depth, and career stage. Addresses the 'persona packs too generic' finding from the n=100 self-audit.

### Changed
- (sp-f9jg) Per-model rollup in `synthpanel report` now buckets correctly by canonical model id. Prior reports showed alias rows (haiku, gemini-flash-lite) with tokens but no cost, AND canonical rows (openrouter/anthropic/claude-haiku-4.5) with cost but no tokens — duplicate rows that misled users. Now one row per model with both columns populated.
- (sp-xltd) `synthpanel report` synthesis section now renders the full themes / agreements / disagreements / recommendation, not just a 240-char summary peek. Closes the 'synthesis is a black box' gap that the n=100 self-audit surfaced as a real product weakness.

## [0.11.0] - 2026-04-24

Minor bump shipping the `sp-i2ub` scaled-orchestration epic (panelist-level
checkpointing, mid-run cost gate, valid-partial-JSON abort discipline), a
6-bug loudness sweep that converts silent failures into loud ones, and two
CI hygiene fixes.

### Added
- (sp-hsk3) Panelist-level checkpointing with `--resume <run-id>`: persists run state every K=25 panelists (override via `--checkpoint-dir PATH`, default `~/.synthpanel/checkpoints/<run-id>/`). Auto-checkpoint on SIGINT/SIGTERM. `--resume` picks up without reprocessing completed panelists.
- (sp-4hhk, replaces sp-utnk) `--max-cost <USD>` mid-run projected-total cost gate. Projected = `running_cost / current_n * total_n`; halt gracefully when projected exceeds threshold. Halt produces valid partial JSON with `run_invalid: true`, `cost_exceeded: true`, `halted_at_panelist`.
- (sp-56pb) Valid partial JSON on every abort path: rate-exhaustion, SIGINT, `--max-cost` gate, and individual panelist failure all produce parseable JSON for completed panelists `0..k` with `run_invalid: true` and a specific `abort_reason`. Exit code is non-zero (2) on abort.

### Changed (loudness)
- (sp-s1is) Alias config parse failures (YAML/JSON) now log warnings instead of silently returning an empty dict.
- (sp-qvqx) Synthesis with a partial structured payload now fails loudly instead of yielding empty fields.
- (sp-0ozi) MCP `extend_panel` surfaces synthesis exceptions in the tool response payload (was `synth: null`).
- (sp-t5ok) Condition evaluator warns loudly on unknown condition types and missing sentiment client (was silent default-True).
- (sp-319x) Orchestrator records follow-up exceptions in the response payload (was silently dropped).

### Fixed
- (sp-rmtj) `test_aliases` fixture isolated from the developer's `~/.synthpanel/aliases.yaml` (was flaky on machines with non-default aliases).

### CI
- (sp-42i) Auto-tag workflow now fails loudly on release PRs without a semver label, defaulting to `semver:patch` when the title starts with `chore(release):`.
- (sp-kdya) `pip-audit` ignores CVE-2026-3219 in pip 26.0.1 (no patched pip released yet; remove ignore once fix lands).

## [0.10.0] - 2026-04-23

Minor bump shipping three completed QRSPI epics — `sp-viz-layer` (post-hoc
Markdown reporting), `sp-inline-calibration` (inline calibration against
published human baselines), and `sp-pack-registry` (decentralized HACS-style
persona-pack registry) — plus supporting work merged since v0.9.9.

### Added
- (sp-viz-layer) `synthpanel report RESULT` — post-hoc Markdown renderer for saved panel results. Accepts a result ID or a path to a result JSON; writes to stdout by default or to a file via `-o PATH`. Every report opens with a mandatory synthetic-panel banner and closes with a matching footer so the output can't be mistaken for human-respondent data. Scope is Markdown v1 (provenance, per-model rollup, persona summary, synthesis, failure stats); `--format` accepts only `markdown` and is reserved as a forward-compat slot for HTML in v2. A `synthpanel[report]` optional-deps extra exists and installs cleanly but is currently empty — forward fence for v2 HTML deps. Ships via T1–T5: scaffold (sp-x8fl), loader (sp-kwhl), renderer (sp-u88v), CLI wiring (sp-awfz), docs (sp-z3uy). Full spec at `specs/sp-viz-layer/`.
- (sp-5r88 / sp-a6jc / sp-ttwy / sp-bldz) Inline SynthBench calibration via `panel run --calibrate-against DATASET:QUESTION`. Force-enables convergence tracking against a published human baseline (v1 allowlist: `gss`, `ntia`), auto-derives a `pick_one` extractor schema from the baseline when option count ≤ 5 (override with `--extract-schema`), and attaches a `calibration` sub-object to every tracked question in the output. The sub-object carries `jsd`, `baseline_spec`, `extractor`, `auto_derived`, and — on disjoint supports — `alignment_error`. Requires `pip install 'synthpanel[convergence]'`. Cadence is NOT implicit — pair with `--convergence-check-every` to control sampling.
- (sp-udsv) `gh:` URL resolver — parses `gh:owner/repo[@ref][/path]` into raw-content URLs with tight allowlist validation. Foundation piece for the pack registry (pack import from GitHub).
- (sp-7we4) Decentralized registry module at `synth_panel.registry` with HTTP fetch + on-disk cache layers. 24h TTL, offline fallback when the remote is unreachable, and deterministic cache keys so cold/warm runs produce identical lookups.
- (sp-w9a5) `synthpanel pack import gh:<user>/<repo>[@ref][/path]` — import persona packs directly from GitHub via the `gh:` resolver. `--unverified` affordance required for packs outside the curated registry; collision UX surfaces existing local packs and offers `--force` to overwrite.
- (sp-vzhl) `synthpanel pack search <term>` substring search over cached registry entries, and `synthpanel pack list --registry` to enumerate available packs from the registry (falls back to last good cache offline).
- (sp-lk3w) Optional `version:` field on persona packs. MCP surfaces a non-fatal shadow warning when a user-installed pack shadows a bundled pack with an older version string, so silently-stale packs can't sit on top of a newer bundled definition.

### Changed
- (sp-bldz) Convergence: inline calibration now attaches a `per_question[key].calibration` sub-object as the shipped wire format (`jsd`, `baseline_spec`, `extractor`, `auto_derived`, and — on disjoint supports — `alignment_error`). A flat `per_question[key].human_jsd` scalar was considered during D-gate and rejected; any downstream consumer that wrote speculative code against `.human_jsd` should migrate to `.calibration.jsd`.
- (sp-ttwy) `pick_one` extractor schema is auto-derived from the baseline when the baseline option count is ≤ 5; hard-fails otherwise so callers are forced to pass an explicit `--extract-schema`.

### Documentation
- (sp-z3uy) README + synthpanel.dev: document `synthpanel report` usage in the quick-start section with stdout / `-o FILE` examples, synthetic-panel banner call-out, and a note that the `[report]` extra is currently empty but installs cleanly.
- (sp-0g9r / sp-7npy) Convergence docs: document `--calibrate-against` and the shipped `per_question[key].calibration` sub-object wire format. Any downstream consumer that wrote speculative code against `.human_jsd` should migrate to `.calibration.jsd`.
- (sp-ezcq) New `docs/registry.md` reference covering `pack import gh:...`, `pack search`, `pack list --registry`, the 24h cache, and the contribution flow for community packs.
- (sp-o1y0) Landing page: "Who this isn't for" positioning block surfacing non-enterprise scope.
- (#251) Full doc audit and refresh for the v0.9.9 feature set across README, CHANGELOG, and site.

### Tests
- (sp-m1mz) Acceptance: live-registry smoke test covering cache miss → fetch → cache hit paths end-to-end.
- (sp-idqa) Acceptance: end-to-end calibration against live GSS HAPPY baseline validating the full `--calibrate-against` path.

## [0.9.9] - 2026-04-22

### Fixed
- (sp-exu6) Synthesis: `--synthesis-strategy=auto` now routes to `map-reduce` when the estimated prompt would overflow the synthesis model's single-pass context window, instead of hard-failing on the pre-flight check. Mayor introduced the regression during the sp-avmm × sp-9rzu rebase in 0.9.8 — pre-flight ran *before* strategy-select, so `auto` was effectively `single-only with a hard limit`. Dogfooded n=100 ensemble audit surfaced the bug on all three panels.
- (sp-9gcm) Cost: resolve aliases to their canonical OpenRouter-prefixed model IDs before keying into the pricing table. `--models haiku:0.25,deepseek-v3:0.25,gemini-flash-lite:0.25,qwen3-plus:0.25` previously missed sp-oshf's `deepseek-v3.2` and current `gemini-flash-lite` entries, so those models fell through to DEFAULT_PRICING and produced 40–93% divergence warnings in the n=100 audit. Top-level cost was already authoritative via sp-j3vk; this tightens the local-table sanity-check path.

### Added
- (sp-g270) `panel run --personas-merge` now warns (and optionally errors) when a merged pack contains persona names already present in bundled packs. Pre-run stderr line + new top-level `personas_merge_warnings` array in JSON output lists dropped names and post-dedup panel size. New `--personas-merge-on-collision={dedup,error}` flag controls behavior. Caught the n=100 silent 10% shrink that cost mayor 20 minutes of debugging.

### Changed
- (sp-ssrw) Version is now sourced from a single `src/synth_panel/__version__.py` and `pyproject.toml` reads it via `dynamic = ["version"]`. `site/index.html` renders from `site/index.html.j2` with `{{ version }}` substitution. Retires sp-lwy's drift-guard test as a render-correctness check; release-cut friction is now a one-line edit.

## [0.9.8] - 2026-04-22

### Fixed
- (sp-avmm) Synthesis: fail loud when `synthesize_panel()` raises or when the estimated synthesis prompt overflows the synthesis model's context window. Previously the CLI, SDK, and MCP/SDK sync runner all caught synthesis exceptions and proceeded as if synthesis had been skipped, so panel results shipped with `synthesis: null` and exit code 0 even though the API had returned 400 (observed at n=50 where the haiku call requested ~262k vs haiku's 200k context). Now each call site runs a pre-flight token-count check against a context-window table (haiku/sonnet/opus=200k, gemini-*=1M, qwen3=131k, deepseek-v3=128k, default=128k with warn), surfaces a structured `synthesis_error` payload (`error_type`, `message`, `suggested_fix`) at the top level, flags `run_invalid: true`, and exits with code 2 on the CLI path.
- (sp-kvpx) Cost: route per-model and per-panelist cost through `resolve_cost` so `cost_breakdown.total`, `per_model_results[*].cost`, and per-panelist `cost` honor sp-j3vk's provider-reported precedence. Prior to this, `ensemble_run`, `build_mixed_model_rollup`, the sync MCP/SDK runner, and `format_panelist_result` all called `estimate_cost(usage, lookup_pricing(model))` directly, so every non-top-level cost in the ensemble payload stayed on the local pricing table. Observed divergence in the mayor round-5 audit: `total_cost=$0.27` (authoritative) vs `cost_breakdown.total=$0.71` on the same panel.

### Added
- (sp-kkzz) Per-question map-reduce synthesis for the n=50-500 narrative band. `panel run` now accepts `--synthesis-strategy=<single|map-reduce|auto>` (default `auto`). In map-reduce mode, one synthesis call runs per question in parallel (summarizing just that question's responses, with optional cluster-aware persona metadata) followed by one reduce call that combines the per-question summaries into the final cross-question synthesis. `auto` compares a pre-flight token estimate against the synthesis model's context window and picks `single` when it fits, `map-reduce` otherwise.
- (sp-2hpi) Structured response_schema validation and deterministic distribution analysis for bounded question types (Likert, enum, yes/no). Aggregation pipeline computes per-question distributions, subgroup breakdowns, and correlations without an LLM call — foundation for scaling beyond n=500 where narrative synthesis is inappropriate.
- (sp-i2ub) Rate-limit-aware LLM client with `--max-concurrent N` and `--rate-limit-rps RPS` flags. 429s and provider-specific rate-limit errors back off with jitter and honor `retry-after` headers so large-n panels don't trip upstream rate limits.
- (sp-yaru) Live convergence telemetry for panel runs: `--convergence-check-every N` emits running JSD per bounded question, `--auto-stop` halts when rolling-average JSD stays below `--convergence-eps` for `--convergence-m` checks (min floor via `--convergence-min-n`), and the panel output gains a `convergence` report section with per-question curves and convergence-n. `--convergence-baseline DATASET:QUESTION` (optional `synthpanel[convergence]` extras) overlays a human baseline from SynthBench.
- (sp-6wbm) Four new bundled persona packs raising total shipped personas from 24 → 84: `job-seekers` (15), `recruiters-talent` (5), `product-research` (20), `ai-eval-buyers` (20).
- (sp-ftr) Ship the advertised `/synthpanel-poll` slash command.

## [0.9.7] - 2026-04-21

### Fixed
- (sp-j3vk) Cost: trust provider-reported cost over the local pricing table. When a provider returns `usage.cost` (OpenRouter) or equivalent in its response, that value is now recorded verbatim instead of being recomputed from token counts against our maintained rate table. This is the architectural root-cause fix that supersedes the sp-cxyb / sp-5ggf / sp-nn8k / sp-loil bandaids: local pricing drift can no longer inflate or deflate reported spend, and OpenAI-via-OpenRouter paths stop reporting 40× overages when our table is stale relative to the provider's billing.
- (sp-nn8k) Cost: surface `DEFAULT_PRICING` fallback loudly in panel output. When a model is not found in the pricing table and we fall back to the default rate, the panel result now includes a `pricing_fallback` warning listing the affected model(s), so silent mispricing can no longer hide in `$0` or inflated-cost runs. Bandaid ahead of sp-j3vk.
- (sp-27rz) Ensemble: guarantee every weighted model in `--models` gets at least 1 persona. Prior rounding could drop low-weight models entirely (weight < 1/n_personas produced 0 personas after floor), so the ensemble silently ran without models the user explicitly selected. Now ensures ≥1 persona per listed model, redistributing from higher-weight buckets.
- (sp-5ggf) Cost: add pricing table entries for common OpenRouter-proxied models (gpt-4o-mini, qwen, deepseek, mistral variants) so they stop falling through to `DEFAULT_PRICING` and reporting wrong costs. Bandaid ahead of sp-j3vk.
- (sp-cxyb) Cost: correct `SONNET_PRICING` to Claude Sonnet 4.5 rates ($3/M in, $15/M out, $0.30/M cached, $3.75/M cache-write) instead of the stale Opus-3 rates that were doubling reported Sonnet cost. Bandaid ahead of sp-j3vk.

## [0.9.6] - 2026-04-21

### Fixed
- (sp-atvc) Ensemble cost reporting: `metadata.cost.per_model` now buckets panelist token usage by the model that actually ran each panelist and prices each bucket at its own provider's rate. Previously ensemble, `--blend`, and `--models` weighted runs summed tokens across providers then priced the aggregate at the default model's rate, so multi-model runs held a single bucket for the default model only and `total_cost` undercounted by ~6x in the mayor round 4 audit.
- (sp-0h9x) Panel results: `per_model_results` and `cost_breakdown` are now populated on every non-ensemble `panel run` (CLI + MCP), not just `models=[...]` ensemble runs. Mixed-model panels via `persona_models` surface one rollup entry per distinct model; single-model panels surface a one-entry dict. sp-gl9 only wired these fields in the ensemble path, so mayor's audits and other consumers reading the flat panel shape still saw `None`.
- (sp-loil) Cost: price `openrouter/openai/gpt-5-mini` at the published OpenAI rate ($0.25/M in, $2.00/M out, $0.025/M cached input) instead of falling through to the Sonnet default pricing. Unknown-model fallback was inflating reported cost for gpt-5-mini by ~40x (13k/4.8k tokens reported $0.56 vs actual ~$0.013).

## [0.9.5] - 2026-04-21

### Added
- (sp-6yi) `panel run` fails fast on unsubstituted `{placeholder}` variables in instrument or persona packs, with actionable error output listing the missing `--var` keys. Previously the run would proceed and emit garbled prompts.
- (sp-anje) Landing-page-comprehension regression test locks in the sp-6yi fail-fast guard so future refactors can't silently re-allow unsubstituted placeholders into panel runs.
- (sp-on4) `panel run --personas-merge PATH` (repeatable): layer extra persona files onto the base `--personas` pack without hand-editing YAML. Files merge in order; persona entries sharing a `name` with an earlier one replace it in place.
- (sp-x8g) `panel run --dry-run` previews resolved personas, instrument rounds, model selection, and cost estimate without calling any LLM — useful for config validation in CI or pre-run sanity checks.
- (sp-bjt4) Run-level `run_invalid` flag: when ≥50% of panelists report missing required input at the synthesizer stage, the panel result is marked invalid so downstream tooling can surface the failure instead of silently publishing a bad run.
- (sp-8ap) Landing page: audience clarity section, concrete use cases, and example output to help first-time visitors evaluate the tool without digging into docs.
- (sp-6rm) 1280×640 GitHub social preview card asset.

### Fixed
- (sp-ui40) Metadata: resolved `--var` keys and hashed values now fold into `config_hash`, so runs with identical instruments but different variable substitutions produce distinct hashes and don't collide in result stores.
- (sp-mkpo) MCP: BYOK detection now routes through the credentials store rather than reading environment variables directly, so keys persisted via `synthpanel login` are visible to the MCP server.
- (sp-gl9) Ensemble: `per_model_results` and `cost_breakdown` shapes now match the documented contract — clients relying on these fields will no longer see missing keys or type drift.
- (sp-2xy) OpenRouter: request `usage.include` on chat completions and tolerate null `usage` payloads so we stop emitting $0 cost rows for completed turns.
- (sp-bzb) CLI: `--synthesis-model` help text corrected, and the resolved synthesis model now surfaces in the pre-run cost estimate.
- (sp-rn58) Site: drop `.html` from blog `og:url` and `<link rel="canonical">` to stop the 308 redirect that was breaking preview cards on some social platforms.
- (sp-oxw) Site: sync landing page version badge and Schema.org JSON-LD `softwareVersion` to v0.9.4.
- (sp-869) CI: use `tomli` as a fallback for Python 3.10 compatibility where `tomllib` isn't in stdlib.

### Documentation
- (sp-lb4b) README: bump Docker pin example from 0.9.1 to 0.9.4.
- (sp-da6) MCP: document the persona object schema with concrete examples in both `run_panel` and `run_quick_poll` tool descriptions.

## [0.9.4] - 2026-04-20

### Fixed
- (sp-1ez) P0 release packaging: `synthpanel login`/`logout`/`whoami` subcommands were merged to main via PR #178 (sp-lve) on 2026-04-20 but the PR carried no `semver:*` label, so auto-tag never fired and 0.9.3 shipped without the credential-store CLI. This release re-cuts the wheel so the advertised commands actually appear in `synthpanel --help`.

### Added
- (sp-lve) `synthpanel login` / `logout` / `whoami` — persist a per-provider API key to the on-disk credential store so the CLI works without exporting env vars. Key can also be piped (`echo sk-... | synthpanel login`) for CI/script use.

### Fixed
- (sp-t6r) MCP: recognise `OPENROUTER_API_KEY` as a BYOK credential and pick a sensible default model when OpenRouter is the only configured provider.
- (sp-d86) Site: prevent iOS Safari overscroll white flash on the landing page.
- (sp-v1w) Site: bump copy-button touch target to 44px to meet iOS Human Interface Guidelines.

### Documentation
- (sp-dub) Promote the MCP sampling-fallback story to the README opener and landing page; align framework count at 8 across surfaces.
- (sp-ovl) SEO: Schema.org JSON-LD, tightened meta descriptions, and `og:site_name`.
- (sp-fiv) Smithery registry section + refreshed registry-submissions runbook.
- (sp-f12) Add Anthropic Cookbook notebook as the canonical integration-example source.

## [0.9.2] - 2026-04-19

### Fixed
- (sp-6gd) P0 demo blocker: confirm `from synth_panel import quick_poll` works. The public SDK re-exports landed in `src/synth_panel/__init__.py` via sp-2cw.1 but were never published to PyPI — 0.9.0 shipped an empty `__init__.py`. This release cuts the first PyPI build that actually exposes the advertised surface (`quick_poll`, `run_prompt`, `run_panel`, `extend_panel`, `get_panel_result`, `list_instruments`, `list_panel_results`, `list_personas`, plus `PanelResult`, `PollResult`, `PromptResult`).

### Added
- (sp-2cw.1) Public Python SDK convenience layer: `from synth_panel import quick_poll, run_prompt, run_panel, …` now resolves against `synth_panel.sdk`. See `docs/stability.md` for the supported surface.
- (sp-2cw.2) `docs/examples/` — "Works with X" integration examples for 6 agent frameworks (Claude Agent SDK, OpenAI Agents, LangGraph, AutoGen, CrewAI, LlamaIndex).
- (sp-2cw.3) Composio toolkit registration manifest.
- (sp-2cw.4) Expanded Claude Code skills library under `skills/`.
- (sp-2cw.5) Production Docker image published to `ghcr.io/dataviking-tech/synthpanel` and `synthpanel/synthpanel` on tagged releases. Multi-arch (linux/amd64, linux/arm64), python:3.12-slim base, default CMD is `synthpanel mcp-serve`. Reads provider keys from env (`ANTHROPIC_API_KEY` etc.). New CI workflow `.github/workflows/docker.yml` builds and pushes on `v*` tag push or `workflow_dispatch`. README gains a "Run via Docker" section and a GHCR badge.
- (sp-6at) MCP sampling fallback for `run_prompt` and `run_quick_poll` so tools still function when the host supports MCP sampling but no provider key is configured.

### Documentation
- (sp-2cw.6) README "Works with" section lifted above the fold and expanded to seven frameworks.
- (sp-4rp) Landing-page sync for v0.9.x and demo polish.

## [0.9.0] - 2026-04-15

### Public Launch
- First release post-public-flip. Repo renamed from `synth-panel` to
  `SynthPanel` (PyPI distribution name `synthpanel` unchanged).
- Pre-launch audit verdict: READY-TO-FLIP (see `docs/release-audit-2026-04-15.md`).

### Documentation
- README badges: PyPI version, CI status, MIT license, Python versions.
- README links updated to canonical Pascal-case repo name.
- CHANGELOG backfilled with 0.5/0.6/0.7 entries.
- `docs/stability.md` documents `lookup_pricing_by_provider` as part of public surface.

### Internal
- Removed Gas Town agent-internal config from public repo.
- Reconciled conflicting CODEOWNERS file.

## [0.8.0] - 2026-04-14

### Added
- (sp-027) `lookup_pricing_by_provider(provider_string)` — parses synthbench-format provider strings (synthpanel/*, openrouter/*, raw-anthropic/*, etc.) into pricing tuples; returns `(None, False)` for ollama, baselines, ensembles, and unresolved providers.

### Fixed
- (sp-027) Multi-question CLI cost-drop: `_run_multi_cli` and `_run_multi_batch` now propagate `total_cost` / `panelist_cost` / `total_usage` / `panelist_usage` to per-response metadata, matching `_run_single`.

### Notes
- Version bumped from 0.4.1 → 0.8.0 to reconcile pyproject.toml with PyPI release line (last published was 0.7.4); minor bump reflects new public API.


## [0.4.1] - 2026-04-14

### Added
- `lookup_pricing_by_provider(provider_string)` helper in `synth_panel.cost`: parses synthbench `config.provider` strings (`synthpanel/`, `openrouter/`, `raw-anthropic/`, `raw-openai/`, `raw-gemini/`, `ollama/` plus `t=`/`profile=`/`tpl=` decorators) and resolves to `(ModelPricing, is_estimated)`. Refuses substring fallback to SONNET so callers (notably synthbench publish) decide whether to emit null. Returns `(None, False)` for `ollama/*`, the named baselines, `ensemble/*`, and unknown inner models. (sp-027)
- `pricing snapshot_date: 2026-04-14` comment above the pricing table to anchor downstream snapshot generation. (sp-027)
- `panelist_usage` field on the rounds-shaped CLI JSON output, restoring symmetry with `panelist_cost`/`total_cost`/`total_usage` so multi-question runs no longer drop a usage bucket downstream consumers rely on. (sp-027)
- v3 branching instruments with `route_when` predicates and DAG validation
- Router predicate engine: `contains`, `equals`, `matches` operators
- Multi-round branching orchestrator loop
- 5 bundled v3 instrument packs: `pricing-discovery`, `name-test`, `feature-prioritization`, `landing-page-comprehension`, `churn-diagnosis`
- `instruments` CLI subcommand: `list`, `show`, `install`, `graph`
- Instrument pack loader (single-file YAML with manifest fields)
- MCP `list_instrument_packs`, `get_instrument_pack`, `save_instrument_pack` tools
- Rounds-shaped panel output with `path`, `terminal_round`, and `warnings` fields
- `extend_panel` MCP tool for ad-hoc follow-up rounds
- Text-mode path line above panel run output
- `--var KEY=VALUE` and `--vars-file` for instrument templates (#39)
- `pack show <id>` as an API-parity alias (#41)
- CI guard to block live API calls in non-acceptance tests
- GitHub Release notes + changelog config in auto-tag workflow

### Fixed
- Multi-question CLI runs now emit the full cost shape (`total_cost`, `total_usage`, `panelist_cost`, `panelist_usage`) on the rounds-shaped output. Previously `panelist_usage` was absent, which silently zeroed the synthbench leaderboard's `$/100Q` column for new rows. (sp-027)
- Fail loud when all provider requests error (#37)
- Default `--model` now respects available credentials and announces pick (#38)
- Publish workflow trigger corrected + manual PyPI setup documented (#40)
- `contents: read` permission added to publish job (#42)

## [0.4.0] - 2026-04-10

First published release on [PyPI](https://pypi.org/project/synthpanel/).

### Added
- v2 multi-round linear instruments with session reuse across rounds
- Instrument v2 parser with multi-round support
- Template engine for dynamic question rendering
- Session persistence — save/load per panelist
- `response_sentiment` condition evaluator with LLM-based classification
- Panel synthesis module (`synthesize_panel`) wired into CLI and MCP
- Condition evaluation module for conditional follow-ups
- Persona pack registry with 5 bundled starter packs
- Structured output via tool-use forcing, wired through MCP and CLI
- Semver auto-tag + PyPI publish workflow (trusted publishing)

### Fixed
- MCP import guard + mock alias test to avoid live API calls
- Condition evaluation wired into orchestrator follow-up loop

## [0.3.0]

### Added
- Structured output via tool-use forcing
- Cost tracking with per-turn token accounting (4 buckets: input, output, cache_write, cache_read)
- MCP server with stdio transport (12 tools, 4 resources, 3 prompt templates)
- Persona-pack persistence (`save_persona_pack`, `get_persona_pack`, `list_persona_packs`)
- Panel result persistence and retrieval

## [0.7.4] - 2026-04-14

Patch release in the 0.7.x series. See the [README Versions table](README.md#versions) for the headline 0.7.x features and the GitHub Release notes for per-tag detail.

## [0.7.0] - 2026-04-14

### Added
- Multi-model ensemble blending (`--blend`)
- OpenRouter provider support
- Temperature / top_p controls
- Persona prompt template customization (see `templates/`)

## [0.6.0] - 2026-04-13

### Added
- `--models` weighted model spec (e.g., `haiku:0.33,gemini:0.33,gpt-4o-mini:0.34`)
- `--temperature` / `--top_p` flags
- Persona prompt templates
- Pack generation helpers
- Domain templates
- MCP server improvements

## [0.5.0] - 2026-04-12

### Added
- v3 branching instruments with `route_when` predicates and DAG validation
- Router predicate engine: `contains`, `equals`, `matches` operators
- Multi-round branching orchestrator loop
- 5 bundled v3 instrument packs: `pricing-discovery`, `name-test`, `feature-prioritization`, `landing-page-comprehension`, `churn-diagnosis`
- `instruments` CLI subcommand: `list`, `show`, `install`, `graph`
- Instrument pack loader (single-file YAML with manifest fields)
- MCP `list_instrument_packs`, `get_instrument_pack`, `save_instrument_pack` tools
- Rounds-shaped panel output with `path`, `terminal_round`, and `warnings` fields
- `extend_panel` MCP tool for ad-hoc follow-up rounds

[Unreleased]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.12.0...HEAD
[0.12.0]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.9.9...v0.10.0
[0.9.9]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.9.8...v0.9.9
[0.9.8]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.9.7...v0.9.8
[0.9.7]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.9.6...v0.9.7
[0.9.6]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.9.5...v0.9.6
[0.9.5]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.9.4...v0.9.5
[0.9.4]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.9.2...v0.9.4
[0.9.2]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.9.0...v0.9.2
[0.9.0]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.7.4...v0.8.0
[0.7.4]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.7.0...v0.7.4
[0.7.0]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.4.0...v0.5.0
[0.4.1]: https://github.com/DataViking-Tech/SynthPanel/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/DataViking-Tech/SynthPanel/releases/tag/v0.4.0
[0.3.0]: https://github.com/DataViking-Tech/SynthPanel/releases/tag/v0.3.0
