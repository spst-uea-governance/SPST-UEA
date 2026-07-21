# Phase 16: Independent Paired Quality Evidence

Phase 16 adds a bounded path from stored producer output to a task-specific
paired measurement. It does not infer quality from marker coverage, artifact
acceptance, candidate labels, or caller-supplied scores. It does not promote a
policy, modify model weights, or establish quality outside the registered local
corpus.

## Task-Specific Rubric

A corpus task may add an exact structured rubric:

```json
{
  "quality_rubric": {
    "schema": "task-specific-json-rubric-v1",
    "criteria": [
      {
        "id": "correct-answer",
        "json_pointer": "/answer",
        "expected": 42,
        "weight": 1.0
      }
    ]
  }
}
```

The rubric is private corpus data. It supports at most 16 weighted JSON-pointer
criteria, bounded scalar expected values, unique criterion identifiers, and
weights that sum to exactly one. Invalid rubrics are rejected as a whole.
Free-text judging and formats whose task semantics cannot be encoded safely in
this contract remain unsupported.

While producer output is still available, `OperationalShadowRunner` projects
only the rubric-addressed scalar values into a bounded `scoring_material`
record. Raw model text and unrelated JSON fields are not retained. The material
digest, rubric digest, and output digest are included in a v3 `PBIND`; reports
without this material continue to produce the legacy v2 binding and are not
eligible for Phase 16.

## Independent and Arm-Blinded Scoring

`PairedQualityEvidenceLedger` reloads both reports and recomputes each saved
PBIND. It rejects missing, altered, incomplete, wrong-task, wrong-arm, reused,
same-run, and rubric-mismatched references before scoring. Each pair must use:

- the same provider, model version, suite, task, and prompt digest;
- a baseline binding from one producer run and a maximized binding from another;
- resolved, different semantic configurations;
- distinct runtime-generated run-instance and execution identities bound into PBIND.

Changing only a caller label, report ID, filename, or artifact bytes cannot
create those identities. Conversely, two independently recorded executions that
produce the same semantic answer remain a legitimate zero-delta tie. Dropping
such ties would bias the paired estimate upward, so Phase 16 keeps them in the
sample while rejecting a copied/relabelled report that reuses a run instance.

The ledger replaces producer identities with random slot identifiers before it
calls `IndependentExactJsonScorer`. The scorer receives only the private rubric
and anonymous scoring material. It never receives task IDs, candidate IDs, arm
labels, run IDs, or binding IDs. Scores and a mapping commitment are hashed into
an immutable scoring artifact before the ledger reveals the paired direction.

The built-in scorer is an independent deterministic implementation, not an
independent organization. `organizational_evaluator_independence_verified`
therefore remains `false`.

## Minimum Sample and Uncertainty

At least eight valid paired tasks are required. The measured values are bounded
in `[0, 1]`; paired deltas are bounded in `[-1, 1]`. Phase 16 reports a fixed
two-sided 95% Hoeffding interval:

```text
half_width = sqrt(2 * ln(2 / 0.05) / n)
```

This bound is distribution-free and intentionally conservative. A positive
mean does not become claim-eligible unless the lower bound is greater than zero.
Fewer than eight pairs remain `insufficient_evidence` even when their observed
mean is positive.

## Human-Reviewed Scoring Artifact

A sufficient measurement stops at `pending_human_review` and keeps
`task_quality.available: false`. Review requires the exact scoring artifact
digest, an explicit human reviewer declaration, the fixed review scope, and an
accepted or rejected decision. The decision is append-only and HMAC-provenanced.

The local runtime records human identity as `self_attested` and
`human_identity_cryptographically_verified: false`; it must not be described as
cryptographic proof of who reviewed the artifact. Only an accepted review makes
task quality available for the recorded local corpus. It still does not set
`task_quality_uplift_claimed`, generalize beyond that corpus, or trigger an
automatic promotion.

## Cockpit API

- `POST /api/paired-quality-evaluations` accepts producer references only.
- `GET /api/paired-quality-evaluations` returns current projections and coverage.
- `POST /api/paired-quality-evaluations/review` appends a digest-bound review.
- `GET /api/status` exposes paired-quality coverage without changing state.

The conformance suite uses an isolated deterministic offline producer to prove
the complete path, including a genuine distinct pair and adversarial failures.
That execution proves the evaluator machinery, not a GPT or SPST quality uplift.
Real GPT evidence still requires separately captured same-model producer runs
and an actual human review of their scoring artifact.
