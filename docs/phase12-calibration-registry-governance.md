# Phase 12: Calibration Registry & Regression Governance

Phase 12 makes Phase 11 evaluation evidence longitudinal, comparable, and
governed. It does not modify model weights, alter official benchmarks, or
automatically adopt an architecture change.

## Registry Contract

`CalibrationRegistry` stores compact, append-only local records through the
HMAC-protected `SQLiteRepository`. A record contains only:

- the evaluation and candidate identifiers,
- a suite/provider/model-version comparison contract,
- per-case numeric task scores and aggregate latency/scaffold observations,
- a comparison result and its governance policy.

Raw prompts, model text, and secrets are not copied into registry records.
The registry returns detached snapshots, so callers cannot mutate a retained
baseline in memory.

## Comparability Rules

Two runs are comparable only when their suite version and hash, provider name,
provider model version, and structured-scoring contract match. An explicit
baseline reference that fails one of these checks produces `not_comparable`,
not a performance claim.

When the contract matches, Phase 12 compares maximized task scores by matching
case identifier. The comparison publishes sample count, mean delta, population
variance, and a bounded confidence state. At least two paired cases are
required. Results are one of:

- `baseline_recorded`
- `improved`
- `neutral`
- `regressed`
- `insufficient_evidence`
- `not_comparable`
- `scaffold_only`

`scaffold_only` is retained for the default API-key-free
`codex-mediated-local` adapter. It confirms the scaffold contract but never
claims task-quality uplift.

## Regression Governance

Every registry record has `automatic_adoption: false`. A comparable
`regressed` result sets `requires_human_approval: true`. The normal 7-step
state transition then receives the deterministic reason
`calibration_regression_requires_human_approval` and remains pending at the
HITL gate until an explicit cockpit approval or rejection.

This policy governs promotion decisions, not the read-only evaluation itself.
Comparability failures and insufficient evidence remain visible observations;
they do not become unsupported claims or implicit approvals.

## Runtime and Cockpit

Dispatch an evaluation with optional local identifiers:

```json
{
  "prompt": "Evaluate a candidate configuration.",
  "evaluate_capability": true,
  "calibration_candidate": "candidate-v2",
  "calibration_baseline_id": "CAL-..."
}
```

- `GET /api/status` exposes the latest compact `calibration` record.
- `GET /api/calibrations` returns immutable registry history and the latest
  record.
- Normal evidence ledger entries retain only a compact calibration summary.

The implementation remains local and provider-neutral. It requires no OpenAI
API key in the default Codex-mediated mode and does not represent Codex
account-level memory.
