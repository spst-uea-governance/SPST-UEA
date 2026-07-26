# SPST-UEA

SPST-UEA is a specification-first runtime project. The runnable reference implementation is in `runtime/`, with architecture and RFC context in `docs/` and `rfc/`.

## North Star

SPST-UEA aims toward AI-human coexistence and mutual flourishing through a
next-generation autonomous multi-agent system. Its core ethos is:

> 改造ではなく、開花。シンプルで、美しく。

The project seeks to open latent capability through context, memory,
verification, collaboration, and governance rather than claiming hidden
modification of model weights or official benchmark truth.

The normative constitutional charter is `docs/spst-uea-covenant.md`.

## Project Maturity

This repository is an **experimental reference implementation**. It has no
tagged stable release and is not a production security certification. Runtime
receipts, tests, and provenance records demonstrate bounded implementation
properties; they do not demonstrate model-weight changes, general GPT quality
improvement, causal superiority, or complete coverage of external Codex tools.

## Quick Start

```bash
cd runtime
python -m pytest -q
```

For local development:

```bash
cd runtime
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
python -m mypy spst_runtime
```

## CI Live DB Guard

`Runtime CI / test` creates a protected SQLite sentinel outside the checkout,
records its SHA-256, checks import isolation, runs the Full suite, and verifies
that the hash did not change. The suite records branch coverage and rejects a
result below the measured 80% floor. It also rejects any `spst_cockpit.db`,
provenance key, or SQLite sidecar created in `runtime/`. Keep this existing job
name as a required pull-request status check so a guard failure blocks merge.

See `docs/live-db-hash-guard.md` for the profile contract and local reproduction
commands.

The workflow file is Repository evidence, not proof that a hosted branch rule
currently requires the check. Hosted enforcement must be verified separately
against the GitHub ruleset and a real failing pull request.

## Repository Map

- `runtime/` - Python reference runtime package and tests.
- `docs/` - architecture, governance, runtime model, and Codex handoff guidance.
- `rfc/` - normative RFC drafts and templates.
- `benchmark/` - reproducible RFC-0005 local benchmark runner.
- `sdk/` - typed, API-key-free Python cockpit SDK.

## Codex Notes

Before changing behavior, read `AGENTS.md`, `docs/spst-uea-covenant.md`, and `docs/codex-handoff.md`. Implementation work should preserve module boundaries, keep providers behind adapters, and avoid bypassing governance for convenience.

For a standalone Codex instruction set that connects software-engineering
autonomy to adaptive `light` / `standard` / `strict` routing, verified receipts,
and memory lifecycle boundaries, use
`docs/codex-autonomous-engineering-master-prompt-spst.md`.

The repository-scoped `$aether-refine-code` skill under
`.agents/skills/aether-refine-code/` applies AETHER's aesthetic,
reverse-entropy, and context-distillation ideas as a bounded refinement layer.
It preserves the current task scope and behavior, and sends durable lessons
through SPST RuleCrystal governance instead of creating a parallel
`AETHER_MEMORY.md` store.

## Receipt-Bound Actions

`spst_runtime.action_bridge` binds supported fixed local actions to a verified
routing receipt through an immutable Action Manifest, derived risk decision,
optional HITL record, compact execution evidence, and the SQLite HMAC
provenance chain. Receipt verification can then list its child actions.
Each fixed profile owns its repository-relative execution root; callers select
the repository boundary, not an arbitrary command working directory.

Repository tasks should route with `chat_bridge --repository-root <git-top-level>`.
The resulting v4 Routing Receipt includes a path-free identity for the exact
HEAD, index, tracked files, and non-ignored untracked files. Read-only receipt
verification separates an intact historical binding from a live
`current_match`; this is byte-level Git state and not semantic equivalence.
New fixed-profile Action Manifests carry that identity forward as a verified
before/after repository transition. Unexpected worktree mutation is retained
as evidence but rejected as a successful action; external Codex tools still
cannot claim an observed after-state.

Only runtime-executed fixed profiles can be marked `execution_verified`.
External Codex tools remain explicitly unobserved even when their planned
manifest and human approval are recorded. A post-hoc external result
attestation can bind a caller-supplied result digest and the repository state
captured at attestation time to the provenance chain, but it remains
source-unauthenticated and does not become verified execution. See
`docs/action-manifest-binding.md` for commands and evidence boundaries.

ARCH-05 can freeze an ordered, Receipt-bound action denominator before any
Manifest is created, then project its coverage through a read-only status path.
Missing, duplicate, out-of-plan, out-of-order, pre-plan, and wrong-workspace
actions prevent complete coverage. Fixed runtime actions and external
attestations remain separate; the latter never become verified execution.
This measures only declared task actions, so declaration completeness remains
unverified and global Codex tool coverage remains unavailable. See
`docs/governed-execution-coverage.md`.

ARCH-06 pre-registers a consented paired-outcome cohort, exact context and
provider contract, balanced condition schedule, fixed stop rule, adapter-call
cap, and external/paid execution authority before any provider request. It
recomputes the observation source from stored Producer records, so an in-process
fixture or post-hoc HTTPS relabel cannot become real outcome evidence. The
read-only projection separates mechanism validation, reviewed fixture outcomes,
and observed real-workload outcomes while keeping provider, reviewer, and
workload identity unverified. See `docs/real-paired-outcome-program.md`.

ARCH-07 adds a durable single-execution attempt, atomic compare-and-set
transitions, and read-only `recovery_required` status. It refuses automatic
retry after an interrupted provider attempt because the transport call count is
not established. See `docs/operational-hardening.md`.

ARCH-08 adds an opt-in provider transport contract. Supporting adapters must
acknowledge a Program-bound idempotency key and provide exact result
reconciliation. Only an explicit digest-bound recovery authority may resume an
interrupted run, and previously completed calls are replayed without new model
inference. Missing acknowledgement, altered receipts, unresolved results, or an
exhausted query cap remain blocked. Provider identity and exactly-once execution
are not claimed. See `docs/provider-transport-recovery.md`.

ARCH-09 exercises that recovery path across actual operating-system process
boundaries. A no-network provider worker commits to a dedicated durable SQLite
store, the submitting client is terminated before receiving the response, and
a fresh client reconciles and resumes without a duplicate logical inference.
The provider instance is Program-bound, but it remains locally simulated and
unauthenticated. See `docs/process-isolated-transport-recovery.md`.

ARCH-10 HMAC-authenticates the local provider-store rows with key material held
outside SQLite and fences recovery through a durable, heartbeat-renewed lease.
A killed supervisor may be adopted only after lease expiry and an exact
authority binding; stale tokens and unkeyed database rewrites are rejected. It
still does not authenticate an external provider or prove remote exactly-once
execution. See `docs/authenticated-provider-store-supervisor.md`.

## Current Runtime Status

The runtime can be executed locally from `runtime/`:

```bash
python -m spst_runtime --steps 3 --event codex_run
```

Implementation notes for the Codex enablement pass are recorded in `docs/implementation-notes.md`.
