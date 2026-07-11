# Phase 10: Verified Local Evidence Runner

Phase 10 upgrades an attested Phase 9 quality gate into an opt-in local
verification run. It remains a Codex-mediated, API-key-free runtime layer. It
does not modify model weights, publish external claims, or permit arbitrary
subprocess execution.

## Trigger and Flow

A dispatch payload with `verified_quality_gate: true` requests a bounded local
verification cycle:

```text
Verified-quality request
  -> GovernanceEngine preflight (L0, fixed profile only)
  -> ToolProvider fixed profile execution (no shell, workspace-bound, timeout)
  -> VerificationRunner compact result and source snapshot
  -> EvidenceLedger verified-quality gate
  -> Governance / HITL decision
  -> SQLite evidence and provenance persistence
```

The runner replaces user-supplied verification values for that transition with
its own compact local results and sets `requires_quality_gate: true`.

## Fixed Profiles

Only these built-in profiles are executable:

- `git_head` and `git_status` for a source snapshot,
- `git_diff_check`,
- `pytest`,
- `ruff`, and
- `mypy`.

`GovernanceEngine` authorizes each profile only when it originates from
`VerificationRunner`, is marked `analysis_only` and `read_only`, and matches
the fixed allowlist. `ToolProvider` independently rejects unknown profiles.
The general MCP `local.execute` boundary remains unchanged and does not accept
these new arbitrary forms.

## Evidence Semantics

The record contains profile status, exit code, duration, and a digest of the
captured output. It stores no raw test output, secret value, prompt, or
arbitrary command text. It also records a git revision when available and a
digest of the local git status output.

For a verified gate, `pytest`, `ruff`, `mypy`, and `git_diff_check` must pass,
alongside normal SQLite provenance validation. A failed, unavailable, or timed
out profile holds the state transition for HITL rather than allowing an
unverified autonomous commit.

## Cockpit Contract

- `GET /api/status` includes the latest `verification` record.
- `GET /api/verification` returns the compact latest verification record.
- `POST /api/dispatch` returns `verification` together with governance and
  evidence when `verified_quality_gate` is requested.

## Boundary and Deployment Note

The runner launches only local, fixed command profiles without a shell and
does not invoke a network tool. It does not provide an operating-system-level
network sandbox for arbitrary test code; a deployment that needs that stronger
guarantee must enforce it outside SPST-UEA with an OS, container, or firewall
policy. This distinction is intentionally preserved under Honesty First.
