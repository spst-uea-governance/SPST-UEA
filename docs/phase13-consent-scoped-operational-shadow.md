# Phase 13: Consent-Scoped Operational Evaluation Corpus & Shadow Rollout

Phase 13 adds a bounded local corpus for evaluating real operational task
contracts and a non-mutating shadow runner. It does not deploy a candidate,
modify model weights, change a subject state, or claim a general model uplift.

## Consent and Retention

Every corpus task MUST provide all of the following before it can be stored:

- `consent.granted: true`
- `consent.scope: local_operational_evaluation`
- a valid `consent.retention_until` date
- a bounded task identifier, domain, split, and deterministic verification
  contract.

Tasks outside their retention date are excluded from future evaluation. They
remain as expired local records rather than being silently deleted; destructive
purging requires a separately governed, explicitly approved action.

The task prompt is retained only in the workspace-local private task record so
the runner can execute it. Public manifests, cockpit responses, calibration
records, and shadow reports contain only task identifiers, prompt digests, and
contract hashes. SQLite HMAC provenance detects tampering; it is **not**
encryption. Operators MUST NOT submit secrets, and the local security gate
blocks known literal secret patterns before storage.

## Corpus Contract

```json
{
  "task_id": "holdout-analysis",
  "prompt": "Local consented task content",
  "domain": "analysis",
  "split": "holdout",
  "required_markers": ["analyze", "verify"],
  "consent": {
    "granted": true,
    "scope": "local_operational_evaluation",
    "retention_until": "2099-12-31"
  }
}
```

`holdout` is the default shadow split. `calibration` may be registered for
separate local calibration work. A task may also declare `expected_json_keys`
for deterministic local JSON-contract verification.

## L0 Shadow Boundary

`OperationalShadowRunner` follows this read-only trace:

```text
observe -> retrieve -> infer -> reflect -> govern
```

It intentionally stops before `commit` and `act` for the subject runtime. The
runner records `subject_state_committed: false` and `actions_executed: false`.
Only compact evaluation evidence and its HMAC-protected calibration/audit
records are persisted.

The runner uses the same provider for baseline and capability-maximized arms.
Task-quality scoring is available only when the adapter explicitly advertises
structured evaluation support. The default API-key-free
`codex-mediated-local` adapter therefore reports `scaffold_only` and never
claims task-quality uplift.

## Calibration and Promotion

Each authorized shadow report is registered through the Phase 12
`CalibrationRegistry`. A comparable regression produces
`requires_human_approval: true` and `promotion.status:
held_for_human_review`. Even neutral or improved observations are never
automatically promoted; Phase 13 does not include a deployment or promotion
action.

## Cockpit API

- `POST /api/corpus/tasks` registers one consented local task.
- `GET /api/corpus` returns the redacted manifest and retention-aware counts.
- `POST /api/shadow-evaluations` runs a bounded L0 shadow evaluation.
- `GET /api/shadow-evaluations` returns compact shadow audit history.
- `GET /api/status` includes corpus statistics and the latest shadow result.

These interfaces remain local, provider-neutral, API-key-free by default, and
outside Codex account-level memory.
