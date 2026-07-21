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

Phase 16 adds the optional private `quality_rubric` contract for bounded,
task-specific exact-JSON scoring. When present, the producer records only the
rubric-addressed scalar projection and binds its digest into a v3 PBIND. See
`phase16-independent-paired-quality.md`; marker and key presence remain proxy
metrics and are never upgraded automatically.

## L0 Shadow Boundary

`OperationalShadowRunner` follows this read-only trace:

```text
observe -> retrieve -> infer -> reflect -> govern
```

It intentionally stops before `commit` and `act` for the subject runtime. The
runner records `subject_state_committed: false` and `actions_executed: false`.
Only compact evaluation evidence and its HMAC-protected calibration/audit
records are persisted.

Each persisted report also publishes one compact `producer_evidence` binding
for every task/arm pair. The baseline arm is assigned the explicit
`baseline_candidate_id` (default `runtime-baseline`) and the maximized arm is
assigned `candidate_id`. A binding contains only the producer run identifier,
task and arm identifiers, a configuration digest, an output-artifact digest,
contract-scoped semantic digests and resolution statuses, and a deterministic
binding identifier. It contains no prompt or model text.

For Phase 16 rubric tasks, a v3 binding additionally records runtime-generated
run-instance and per-inference execution identities plus the rubric-scoped
scoring-material digest. These identities distinguish actual repeated local
executions from a report whose caller label or run ID was merely rewritten.

The configuration digest excludes the candidate label itself. Renaming an
otherwise identical run therefore does not create a distinct execution
condition. The complete report and bindings are persisted through the existing
SQLite HMAC provenance chain.

Semantic artifact resolution is deliberately narrower than byte hashing. When
the task declares `expected_json_keys`, the producer parses an exact JSON
object, projects only those contract keys, rejects duplicate keys, and
canonicalizes JSON numbers and structure. JSON serialization order, token
whitespace, escape representation, filenames, timestamps, paths, environment
labels, and unrelated top-level fields cannot create semantic distinctness.
Free-text contract values, wrappers, suffixes, absent contracts, invalid JSON,
and other formats whose meaning cannot be safely established are recorded as
`unresolved`; they are not inferred to be independent evidence.

The runner uses the same provider for baseline and capability-maximized arms.
When an adapter advertises structured evaluation support, required markers and
JSON-key presence produce a `contract_compliance_proxy`. They do not make
`task_quality` available: marker stuffing, key-only JSON wrappers, and
provider-supplied claim fields are counterexamples that remain ineligible.
Semantic task quality requires the Phase 16 independent, arm-blinded, paired
outcome path with verified producer bindings, uncertainty evidence, and an
explicit digest-bound human review. The
default API-key-free `codex-mediated-local` adapter reports `scaffold_only` and
never claims task-quality uplift.

## Calibration and Promotion

Each authorized shadow report is registered through the Phase 12
`CalibrationRegistry`. A comparable regression produces
`requires_human_approval: true` and `promotion.status:
held_for_human_review`. Even neutral or improved observations are never
automatically promoted; these statuses are scoped to the recorded metric and do
not establish task quality. Phase 13 does not include a deployment or promotion
action.

## Cockpit API

- `POST /api/corpus/tasks` registers one consented local task.
- `GET /api/corpus` returns the redacted manifest and retention-aware counts.
- `POST /api/shadow-evaluations` runs a bounded L0 shadow evaluation.
- `GET /api/shadow-evaluations` returns compact shadow audit history.
- `GET /api/status` includes corpus statistics and the latest shadow result.

These interfaces remain local, provider-neutral, API-key-free by default, and
outside Codex account-level memory.
