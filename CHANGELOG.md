# Changelog

## Unreleased

- Added receipt-bound Action Manifests with derived risk, append-only HITL
  decisions, fixed-profile execution evidence, provenance verification, and
  explicit exclusion of unobserved external Codex tools.
- Bound each fixed profile to a validated repository-relative execution root so
  callers cannot substitute an arbitrary working directory.
- Added repository-bound v3 Routing Receipts that capture HEAD and a canonical
  worktree digest, preserve v1/v2 verification, and report read-only live-match
  status separately from historical receipt validity.
- Bound Action Manifests to the parent Receipt repository identity and stored
  independently verifiable before/after worktree transitions in fixed-profile
  execution evidence, failing closed on pre-execution drift or unresolved
  after-state capture.

## 0.1.1 Runtime Completion

- Added lifecycle, scheduler, checkpoint, recovery, migration, SDK, and benchmark support.
- Hardened SQLite connection ownership and deterministic local search indexing.
- Added Phase 7 conformance coverage for lifecycle completion and public integration.

## 0.1.0 Initial foundation
