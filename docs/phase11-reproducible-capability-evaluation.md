# Phase 11: Reproducible Capability Evaluation & Calibration

Phase 11 turns the paired-evaluation requirement from Phase 8 into a local,
reproducible runtime capability. It measures system-conditioned evidence; it
does not alter model weights, mutate official benchmark scores, or claim a
general model uplift without observable task-quality evidence.

## Evaluation Flow

```text
evaluate_capability request
  -> GovernanceEngine L0 preflight
  -> same ModelAdapter + same versioned local case suite
  -> baseline context / capability-maximized context
  -> deterministic contract-compliance proxy when the adapter supports it
  -> calibration result, SQLite provenance, cockpit report
```

The runner uses the same provider and same public case contracts for both arms.
Each case is represented in persisted evidence by an identifier, domain, and
prompt digest; neither raw prompts nor model text are stored.

## Metrics and Claims

Every report separates five observations:

- **Contract-compliance proxy**: paired marker/shape-check delta, only when an
  adapter explicitly advertises `supports_structured_evaluation`. This can
  detect a contract regression but is not semantic answer quality.
- **Task quality**: unavailable in a Phase 11 report. Phase 16 can establish a
  separately reviewed, local-corpus measurement only after same-model/same-task
  producer bindings, arm-blinded task-specific scoring, an independent
  evaluator identity, at least eight paired samples, a fixed uncertainty bound,
  and a digest-bound human review.
- **Scaffold contract**: coverage of the capability-maximization and verifier
  context. This is useful in API-key-free mode but is not a language-quality
  score.
- **Latency**: baseline, maximized, and observed mean overhead in milliseconds.
- **Calibration**: `improved`, `neutral`, `regressed`, `scaffold_only`, or
  `blocked`.

`improved`, `neutral`, and `regressed` describe the explicitly named proxy
metric in Phase 11. They do not establish semantic task quality. Even a positive
proxy delta leaves `task_quality.available: false`,
`semantic_task_quality_established: false`, and
`task_quality_uplift_claimed: false`. A regression never enables automatic
adoption. All reports retain `automatic_adoption: false` and require human
interpretation.

## API-Key-Free Boundary

The default `codex-mediated-local` adapter does not advertise structured
contract scoring. Its result is therefore `scaffold_only`: it can demonstrate
that the scaffold was constructed and observed, but it cannot claim GPT
task-quality improvement. A structured local adapter or an explicitly
configured provider may opt into the contract proxy through the vendor-neutral
`ModelAdapter` capability. It cannot promote its own result fields, marker
stuffing, or a matching JSON key into task-quality evidence.

## Persistence and Cockpit

A dispatch payload with `evaluate_capability: true` stores its report as
`runtime:evaluation:EVAL-...` in the HMAC-protected SQLite repository. The
compact summary is also attached to the normal evidence ledger.

- `GET /api/status` includes the latest `evaluation`.
- `GET /api/evaluations` returns the latest compact report.
- `POST /api/dispatch` returns the report when evaluation is requested.

## Boundary

The Phase 11 suite is a transparent local calibration suite, not an external
leaderboard and not a hidden-answer benchmark. It is intended to make future
architecture changes falsifiable before they are trusted or expanded. Marker
presence and JSON shape are deliberately classified as a proxy; they remain
useful regression signals and are not upgraded by Phase 16. The independent
paired path is documented in `phase16-independent-paired-quality.md` and stays
separate from these reports.
