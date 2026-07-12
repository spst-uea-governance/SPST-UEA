# Phase 15: Longitudinal Evidence Synthesis & Reversible Promotion Governance

Phase 15 turns independently verified local artifact outcomes into a bounded,
auditable recommendation for a **shadow-only** runtime policy. It does not
modify model weights, change an official benchmark score, execute a policy in
production, or automatically adopt a candidate.

## Evidence Contract

`LongitudinalPromotionGovernance` synthesizes every Phase 14 artifact outcome
whose `candidate_id` matches either the requested candidate or baseline. It
does not accept a hand-picked evidence subset. Rejected, blocked, stale, and
failed records remain visible in the aggregate outcome counts.

A proposal can be sufficient for a human-reviewed shadow activation only when:

1. SQLite HMAC provenance is valid.
2. Candidate and baseline identifiers are distinct and each uses active,
   consented corpus tasks.
3. Every paired source is a fixed-profile-passing Phase 14 terminal result
   (`verified_accepted` or `rejected_by_human`).
4. Each candidate/task and baseline/task pair is unambiguous; duplicate or
   unpaired task evidence is treated as insufficient rather than silently
   selected.
5. Every source has a verified Phase 13 producer binding. A binding cannot be
   reused, and paired candidate/baseline evidence must have resolved and
   different semantic configuration and semantic artifact digests. Raw byte
   digests, candidate/run/task identifiers, arm labels, and nonsemantic
   metadata alone never make evidence distinct.
6. At least three paired tasks exist, the verified-artifact acceptance delta is
   at least `0.05`, and its population variance does not exceed `0.25`.

The measured value is explicitly
`verified_artifact_acceptance_delta_not_task_quality`. It is a local evidence
coverage signal, not a claim that an LLM became intrinsically more capable.
`task_quality_uplift_claimed` and `official_benchmark_claimed` remain `false`.
Records created without a verified producer binding remain visible but are
ineligible for new promotion synthesis. Relabeled identical evidence is reported
as `candidate_evidence_not_distinct`. Unsupported or unsafe-to-interpret
artifact semantics are reported as `semantic_distinctness_unresolved` and are
excluded rather than guessed to be independent.

## Safe Policy Contract

The proposal contains only a bounded, versioned symbolic policy contract:

```json
{
  "policy_id": "verifier-contract",
  "version": "v1",
  "strategies": ["constraint_extraction", "verifier_loop"]
}
```

Allowed strategies are the existing capability scaffolding labels:
`constraint_extraction`, `context_compression`, `ia_task_alignment`,
`prompt_contract`, `spec_decomposition`, and `verifier_loop`. Free-form code,
commands, prompts, model outputs, and human review prose are not accepted.

## Promotion Lifecycle

```text
artifact evidence synthesis
  -> insufficient_evidence | held_for_human_review
  -> explicit human approval
  -> shadow_active
  -> explicit human-approved rollback
  -> rolled_back
```

- `held_for_human_review` is a Level 1 HITL checkpoint.
- `shadow_active` is an append-only registry projection only. It makes no
  subject-state commit, tool execution, external request, or live deployment.
- A human rejection is retained as `rejected_by_human`.
- A rollback is an append-only event and restores the proposal to an inactive
  state. Promotion remains local, reversible, and shadow-only at every stage.

## Provenance and Privacy

Proposal and decision records are stored through the existing HMAC-protected
`SQLiteRepository`. A failed provenance check blocks new synthesis, activation,
and rollback writes. HMAC detects tampering; it is not encryption.

The public history includes identifiers, aggregate counts, bounded policy
metadata, status, and governance decisions. It excludes corpus prompts, raw
model output, fixed-profile output, and free-text human calibration input.

## Cockpit API

- `POST /api/promotions` creates an evidence-only proposal.
- `GET /api/promotions` returns compact projections and coverage.
- `POST /api/promotions/approval` records `{ "promotion_id", "approved" }`.
- `POST /api/promotions/rollback` records a human-approved rollback.
- `GET /api/status` exposes the latest promotion projection and coverage.

These endpoints remain local and API-key-free in the default
`codex-chat-mediated` mode. They do not grant access to Codex account memory,
Codex internals, or external services.
