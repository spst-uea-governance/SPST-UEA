# Phase 9: Evidence-Driven Runtime Governance

Phase 9 turns the Covenant's **Evidence Before Assertions** rule into a
runtime artifact. It remains a local, API-key-free, Codex-mediated governance
layer; it does not modify a model, claim external benchmark uplift, or execute
arbitrary commands.

## Runtime Flow

```text
Event
  -> TransitionEngine (seven-step trace)
  -> EvidenceLedger (trace, reflection, provenance, quality evidence)
  -> GovernanceEngine (policy and evidence decision)
  -> DecisionExplainer (human-readable rationale)
  -> SQLite evidence record and HMAC provenance chain
  -> Cockpit status / evidence API
```

`EvidenceLedger` generates an `EVID-...` identifier from compact, deterministic
fields. It deliberately does not retain raw prompts, model output, secret
values, or arbitrary command text.

## Quality Evidence Gate

Requests marked with `requires_quality_gate`, `breaking_change`, or
`architecture_change` carry a quality gate containing:

- `pytest`,
- `ruff`,
- `mypy`, and
- current SQLite provenance validation.

The caller supplies the local verification status through the event payload.
This is recorded as an attested runtime input, not misrepresented as a command
that the runtime independently executed. Missing or failed evidence lowers the
decision to a visible Low-trust HITL hold. A human reviewer may explicitly
approve or reject that exception through the cockpit; approval remains recorded
in the evidence and provenance trail.

## Evidence Record

Each ledger record contains:

- deterministic evidence identifier and schema version,
- event type, state version, and seven-step trace,
- pipeline, reflection, and provenance checks,
- compact quality-gate status with missing or failed check names,
- governance decision and Covenant policy snapshot,
- post-state-commit provenance summary, and
- a SQLite key in the form `runtime:evidence:EVID-...`.

The record is HMAC-chain protected by `SQLiteRepository`, while the current
repository state remains verifiable through `verify_provenance()`.

An in-memory `StateTransitionPipeline` that has no SQLite repository records
its provenance check as `not_applicable`; it never presents that path as a
cryptographically verified persistence chain.

## Cockpit Contract

The local cockpit exposes evidence without requiring an API key:

- `GET /api/status` includes `evidence`, `decision_explanation`, and Covenant
  policy context.
- `GET /api/evidence` returns the latest compact evidence record and its
  human-readable decision explanation.
- `POST /api/dispatch` returns the same fields for the dispatched transition.

The explanation presents stable reason codes and a recommended next action. It
is an audit aid, not an independent authority over the Covenant or human
approval.

## Boundary

Phase 9 does not make claims of autonomous truth. It preserves the distinction
between local runtime evidence, verified repository provenance, and external
validation that must still be independently performed and honestly reported.
