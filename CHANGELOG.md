# Changelog

## Unreleased

- Changed long-term memory reuse to fail closed for missing or mismatched
  policy versions across search, consolidation, and RuleCrystal distillation;
  added read-only retrieval-health reason counts and counterexample coverage.
- Aligned Runtime package and benchmark metadata with Runtime 0.1.1, added
  strict pytest configuration and typed-function body checking, and added
  executable public-repository hygiene checks.
- Replaced publication placeholders with an Apache-2.0 license, security
  reporting policy, contribution contract, and explicit experimental-maturity
  boundaries.
- Removed the checkout-only workflow that could report a green CI result
  without executing any verification.
- Removed Runtime source-tree writes from dynamic verifier generation and
  disabled Repository-wide cache deletion during autonomous maintenance;
  generated source is now in-memory, digest-bound, and process-local.
- Measured 83% aggregate branch coverage and added a conservative 80% CI floor;
  the Live DB guard now runs even when the Full suite fails.
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
- Added post-hoc external result attestations that bind a caller-supplied
  result digest and attestation-time repository identity to provenance without
  claiming authenticated or execution-verified external tool coverage.

## 0.1.1 Runtime Completion

- Added lifecycle, scheduler, checkpoint, recovery, migration, SDK, and benchmark support.
- Hardened SQLite connection ownership and deterministic local search indexing.
- Added Phase 7 conformance coverage for lifecycle completion and public integration.

## 0.1.0 Initial foundation
