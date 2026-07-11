# Phase 14: Artifact-Verified Outcome Evidence & Human Calibration

Phase 14 binds a consented operational task to a fixed local verification
snapshot and a compact human outcome decision. It raises the quality of local
evidence; it does not modify model weights, publish benchmark claims, promote a
candidate, or execute arbitrary commands.

## Required Evidence Contract

An artifact outcome record requires all of the following:

1. An active Phase 13 consented task contract.
2. An expected source snapshot (`revision` and `git_status_digest`) from a
   fixed Phase 10 verification run.
3. A fresh `VerificationRunner` result with the same snapshot and every fixed
   profile passing.
4. A human calibration decision of `accepted` or `rejected`, with explicit
   local evaluation consent whose retention date does not outlive the task
   consent window.

The ledger stores task identifiers, prompt digests, source snapshot hashes,
compact profile results, and the decision label. It deliberately accepts no
free-text review rationale and stores no task prompt, model text, raw command,
or raw test output.

## Outcome States

| Status | Calibration eligibility |
|---|---|
| `verified_accepted` | Eligible as artifact outcome evidence only |
| `rejected_by_human` | Ineligible |
| `stale_source_snapshot` | Ineligible |
| `verification_failed` | Ineligible |
| `blocked` | Ineligible |

Eligible means that the artifact and human decision are auditable. It does
**not** mean a general model-quality uplift was established, an official score
changed, or a runtime profile may be automatically promoted.

## Governance and Provenance

`ArtifactOutcomeLedger` is a local evidence-only action. `GovernanceEngine`
rejects records without an active consented task, exact expected snapshot,
explicit human calibration consent, valid decision, or bounded retention.

Every accepted or rejected record is persisted through the HMAC-protected
SQLite repository. HMAC provenance detects tampering; it is not encryption.
The Phase 10 runner remains the only supported execution path and uses its
fixed shell-free profiles (`git_head`, `git_status`, `git_diff_check`,
`pytest`, `ruff`, and `mypy`).

## Cockpit API

- `POST /api/artifact-outcomes` records one evidence-only outcome.
- `GET /api/artifact-outcomes` returns compact history and domain coverage.
- `GET /api/status` includes artifact-outcome coverage and the latest record.

Phase 14 remains API-key-free by default and local to this workspace. It does
not access Codex account-level memory or invoke direct GPT API inference.
