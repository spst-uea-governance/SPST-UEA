# Phase 12: Calibration Registry & Regression Governance

Phase 12 makes Phase 11 evaluation evidence longitudinal, comparable, and
governed. It does not modify model weights, alter official benchmarks, or
automatically adopt an architecture change.

## Registry Contract

`CalibrationRegistry` stores compact, append-only local records through the
HMAC-protected `SQLiteRepository`. A record contains only:

- the evaluation and candidate identifiers,
- a suite/provider/model-version comparison contract,
- per-case numeric contract-proxy scores and aggregate latency/scaffold
  observations,
- a comparison result and its governance policy.

Raw prompts, model text, and secrets are not copied into registry records.
The registry returns detached snapshots, so callers cannot mutate a retained
baseline in memory.

## Comparability Rules

Two runs are comparable only when their suite version and hash, provider name,
provider model version, structured-scoring contract, measurement scope, and
semantic-quality status match. An explicit baseline reference that fails one
of these checks produces `not_comparable`, not a performance claim.

When the contract matches, Phase 12 compares maximized scores for the named
measurement scope by matching case identifier. Current Phase 11/13 inputs use
`required_marker_and_json_shape_coverage`, so these are contract-proxy scores,
not semantic task-quality scores. The comparison publishes sample count, mean
delta, population variance, and a bounded confidence state. At least two paired
cases are required. Results are one of:

- `baseline_recorded`
- `improved`
- `neutral`
- `regressed`
- `insufficient_evidence`
- `not_comparable`
- `scaffold_only`

`scaffold_only` is retained for the default API-key-free
`codex-mediated-local` adapter. It confirms the scaffold contract but never
claims task-quality uplift. A proxy `improved` result also keeps
`semantic_task_quality_established: false` and cannot claim uplift.
Caller-provided `task_quality`, score, semantic-status, or uplift fields are
stored only as rejected attestations; the registry has no independent quality
verifier and therefore cannot promote them into its comparison path.

The proxy path is also fail-closed. The registry requires the canonical proxy
schema and scope, exact boolean flags, finite float scores in `[0, 1]`, a
positive integer pair count, and internally consistent delta arithmetic. It
then recomputes the aggregate from the per-case baseline and maximized contract
scores. A malformed proxy or an aggregate/case mismatch is retained as rejected
input and produces `scaffold_only`; caller-provided aggregate values are not a
calibration authority.

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
