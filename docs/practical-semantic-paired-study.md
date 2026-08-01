# Practical semantic paired study

The practical semantic study is the runtime-owned path for a fixed 30-to-50
pair, human-scored comparison over novel repository tasks. It does not treat a
contract proxy, receipt count, provider echo, or byte digest difference as
semantic task quality.

## Checked-in task pack

`runtime/spst_runtime/evaluation/novel_practical_tasks_v1.json` contains 32
heterogeneous prompts. The pack contains no reference answers, expected
patches, arm mapping, or expected scores. Each prompt asks for diagnosis,
minimal correction, verification, and residual risk. The pack is a prepared
cohort; its presence alone is not a completed study and does not authorize any
provider call.

Validate it without changing persistent state:

```powershell
cd runtime
python -m spst_runtime.practical_semantic_study_bridge validate-task-pack
```

## Pre-registration contract

Pre-registration fixes all of the following before generation:

- 30 to 50 unique task contracts;
- the exact repository identity;
- one model-adapter generator identity;
- the exact treatment-context file and model-facing instruction digests;
- a balanced, digest-bound per-task condition order;
- a fixed sample stop rule with no optional stopping or automatic retry;
- an arm-blinded human review policy;
- the exact correction policy;
- the claim boundary.

The standard correction policy permits at most one
`clerical_data_entry_error` correction. It requires the original reviewer,
the exact prior-review and blind-artifact digests, a renewed mapping-non-access
attestation, preservation of the superseded review, and correction before
unblinding. A post-finalization correction is rejected.

Pre-register only after the target commit is fixed and the worktree state is
the intended execution state:

```powershell
python -m spst_runtime.practical_semantic_study_bridge preregister `
  --evaluation-db .\study.db `
  --repository-root .. `
  --generator-id codex-cli-generator
```

Before execution, append a study-bound authority payload produced by
`authority-template`. The authority fixes the exact Study, Repository, task
pack, intervention, 64-call ceiling, ChatGPT-plan-only use, no-paid guard,
and no-retry/no-optional-stop rules. `preflight` is read-only and performs no
provider call. `execute` rejects the live default DB and requires explicit
isolated evaluation and memory DBs.

```powershell
python -m spst_runtime.practical_semantic_study_bridge authority-template `
  --evaluation-db .\study.db --repository-root .. `
  --study-id <study-id> --authority-label owner-authority

python -m spst_runtime.practical_semantic_study_bridge authorize-execution `
  --evaluation-db .\study.db --repository-root .. `
  --study-id <study-id> --payload .\authority.json

python -m spst_runtime.practical_semantic_study_bridge preflight `
  --evaluation-db .\study.db --memory-db .\study-memory.db `
  --repository-root .. --study-id <study-id>
```

`execute` makes exactly two fresh adapter invocations per task in the
pre-registered order. The Codex CLI adapter re-checks cached ChatGPT login and
the no-paid spend guard immediately before every invocation. A failed call is
not retried; partial execution remains ineligible and a fresh study is required.

## Per-task receipt binding

Every pair record requires a distinct v4 Routing Receipt generated from the
exact task prompt. Recording fails when the Receipt is missing, reused,
tampered, bound to another task, or does not match the pre-registered
repository identity. Baseline and treatment also require different provider
response identifiers. Receipt evidence binds orchestration; it does not by
itself prove provider identity, semantic correctness, or causal benefit.

The `record-pair` payload is an exact JSON object:

```json
{
  "task_id": "practical-se-01",
  "task_contract_sha256": "<sha256>",
  "routing_receipt_id": "<receipt-id>",
  "baseline": {
    "artifact_text": "<canonical structured answer>",
    "producer_response_id": "<provider-response-sha256>",
    "provider_observation": {"schema": "spst-provider-observation-v1"}
  },
  "treatment": {
    "artifact_text": "<canonical structured answer>",
    "producer_response_id": "<provider-response-sha256>",
    "provider_observation": {"schema": "spst-provider-observation-v1"}
  }
}
```

The ledger derives artifact, observation, and execution-guard digests itself
and revalidates them on every read. The blind artifact includes the task prompt
and answer text needed for real scoring, but never includes the baseline or
treatment label. Hash-only slots are not considered reviewable evidence.

## Reviewer separation and correction

The Reviewer must be human-labeled and have an identifier different from the
pre-registered generator. All blind slots must receive integer scores from 0
to 4 for the exact criteria:

- `technical_correctness` (0.40)
- `constraint_and_edge_coverage` (0.25)
- `minimality_and_safety` (0.15)
- `verification_quality` (0.20)

The runtime verifies role separation and self-attestations. It does not
cryptographically establish that the label belongs to a human or that the
Reviewer is organizationally independent.

Review is a three-step state transition: submit the complete blinded review,
optionally append the one permitted correction, then finalize and unblind.
The original review remains in the provenance-protected event history.

## Read-only Runtime status

`chat_bridge --status` reads the same SQLite store immutably and publishes:

- selected study ID and state;
- completed and required pair counts;
- verified per-task Receipt coverage;
- Reviewer role-separation and correction count;
- control and treatment means;
- paired delta and the fixed 95% Hoeffding bound;
- beneficial, harmful, or inconclusive direction;
- bounded claim eligibility.

Until a complete reviewed study exists, `task_quality_delta` remains `null`
with an explicit incomplete-state reason. Status reads never pre-register,
record, review, correct, finalize, or otherwise mutate the database.

## Authority boundary

Executing the checked-in 32-pair pack requires 64 fresh model calls. Commit,
provider execution, ChatGPT-plan use, paid use, retry authority, and the human
review are separate authorities. A local deterministic test of the complete
mechanism is not evidence that SPST improves general Codex quality.
