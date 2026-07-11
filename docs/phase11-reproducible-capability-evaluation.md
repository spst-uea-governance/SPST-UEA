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
  -> deterministic marker scorer when the adapter supports it
  -> calibration result, SQLite provenance, cockpit report
```

The runner uses the same provider and same public case contracts for both arms.
Each case is represented in persisted evidence by an identifier, domain, and
prompt digest; neither raw prompts nor model text are stored.

## Metrics and Claims

Every report separates four observations:

- **Task quality**: paired marker-score delta, only when an adapter explicitly
  advertises `supports_structured_evaluation`.
- **Scaffold contract**: coverage of the capability-maximization and verifier
  context. This is useful in API-key-free mode but is not a language-quality
  score.
- **Latency**: baseline, maximized, and observed mean overhead in milliseconds.
- **Calibration**: `improved`, `neutral`, `regressed`, `scaffold_only`, or
  `blocked`.

`improved` is reported only for a positive observed task-quality delta. A
regression never claims uplift and never enables automatic adoption. All
reports retain `automatic_adoption: false` and require human interpretation.

## API-Key-Free Boundary

The default `codex-mediated-local` adapter does not advertise structured task
scoring. Its result is therefore `scaffold_only`: it can demonstrate that the
scaffold was constructed and observed, but it cannot claim GPT task-quality
improvement. A structured local adapter or an explicitly configured provider
may opt into task scoring through the vendor-neutral `ModelAdapter` capability;
this does not change the no-key default or make an official benchmark claim.

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
architecture changes falsifiable before they are trusted or expanded.
