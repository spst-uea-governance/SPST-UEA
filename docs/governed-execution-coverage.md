# Governed Execution Coverage

## Purpose

Action Manifests show which known actions were bound to a Routing Receipt, but
they cannot reveal Codex tool calls that bypass the runtime. ARCH-05 adds a
smaller, falsifiable denominator: the actions declared for one task before any
Action Manifest exists for that Receipt.

```text
verified repository-bound Receipt
  -> immutable ordered execution plan
  -> declared action slots and exact action-contract digests
  -> later Action Manifests, one-to-one and in declared order
  -> fixed runtime evidence or external caller attestation
  -> read-only coverage projection
```

This is `declared_task_actions_only` coverage. It is not global Codex tool-call
coverage, proof that the declaration listed every real operation, or a task
quality metric.

## Register Before Actions

Create a JSON request before preparing or executing any Action Manifest for the
Receipt:

```json
{
  "schema": "spst-governed-execution-plan-request-v1",
  "actions": [
    {
      "slot_id": "status-before",
      "kind": "fixed_profile",
      "profile": "git_status"
    },
    {
      "slot_id": "workspace-edit",
      "kind": "external_tool",
      "operation": "workspace_edit",
      "tool_name": "codex.apply_patch",
      "arguments_sha256": "<canonical-arguments-sha256>"
    },
    {
      "slot_id": "tests",
      "kind": "fixed_profile",
      "profile": "pytest"
    }
  ]
}
```

Register it from `runtime/`:

```powershell
python -m spst_runtime.execution_coverage_bridge register `
  --session-db <isolated-session.db> `
  --receipt-id <receipt-id> `
  --plan-file <plan.json> `
  --repository-root ..
```

Registration fails when the Receipt is missing, invalid, repository-stale,
already has a plan, or already has any Action Manifest. The plan binds the
Receipt record and chain hashes, task-contract digest, Repository identity,
canonical workspace root, ordered slot contracts, derived risk, and expected
observation class. The plan is append-only; rewriting its key makes
verification fail.

Risk is derived, not supplied by the plan file. Unknown external operations are
R2. Duplicate slot identifiers, unsupported fixed profiles, malformed digests,
and undeclared request fields are rejected.

## Read-Only Status

After actions have run, inspect the plan without changing the SQLite database:

```powershell
python -m spst_runtime.execution_coverage_bridge status `
  --session-db <isolated-session.db> `
  --receipt-id <receipt-id>

python -m spst_runtime.execution_coverage_bridge verify `
  --session-db <isolated-session.db> `
  --plan-id <plan-id>
```

`chat_bridge --verify-receipt` also exposes the same projection under
`execution_coverage`.

The projection reports exact numerator, denominator, and ratio for:

- valid Action Manifests;
- actions authorized by governance and, where required, HITL;
- runtime-verified executions;
- successful runtime executions;
- completed result evidence;
- verified Repository transitions.

It also lists missing slots, actions prepared before the plan, out-of-plan or
duplicate actions, wrong-workspace actions, order drift, and invalid Manifest
bindings. A byte-identical clone does not satisfy the plan's workspace binding.

## Completion Semantics

`coverage_complete: true` requires every declared slot to have, in declared
order and with no extra valid actions:

1. an exact, Receipt-bound Action Manifest;
2. resolved authorization;
3. runtime-observed execution evidence;
4. a successful result; and
5. a verified Repository transition.

Only fixed profiles executed by `ActionManifestLedger` can meet all five.
An external result attestation may make `result_evidence_complete: true`, but
the status remains
`declared_governance_complete_external_execution_unverified` and
`coverage_complete` remains false. HITL approval does not change that boundary.

Every projection preserves:

```json
{
  "declaration_completeness_verified": false,
  "global_codex_tool_coverage": null,
  "global_coverage_reason": "codex_tool_call_denominator_unavailable"
}
```

The plan prevents post-hoc denominator selection for recorded Action Manifests.
It cannot detect an `apply_patch`, arbitrary shell, browser, connector, or other
Codex tool call that was neither declared nor represented by an Action
Manifest. OS-level interception or an authenticated Codex tool-event source is
still required for global execution coverage.
