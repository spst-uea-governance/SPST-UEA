# Phase 7: Sovereign Cognitive Governance OS

This phase extends the local Codex-mediated runtime with four bounded
governance capabilities. It remains a local runtime layer; it is not a model
replacement, a general operating system, or direct GPT API execution.

## Multi-Workspace Federation

`WorkspaceOrchestrator` only shares explicitly published `RuleCrystal` records
between registered local workspaces. Every shared record carries the origin
workspace and a policy version; unregistered sources and policy mismatches are
not retrieved.

## Provenance and Security

Every SQLite save appends a SHA-256 record hash to an HMAC-protected chain.
`verify_provenance()` validates both chain continuity and current stored values.
`SecuritySubject` performs local AST and token scanning for literal secrets and
destructive operations before governance can authorize a transition.

New repositories use local per-database HMAC key material when
`SPST_PROVENANCE_KEY` is not configured. Existing legacy-default chains remain
verifiable for compatibility and can be re-signed with
`SQLiteRepository.rotate_provenance_key()` without exposing the secret. Runtime
snapshots store compact context summaries instead of recursively embedding prior
retrieval and inference payloads, keeping audit provenance bounded during
sustained dispatch runs.

## Adaptive HITL

Routine transitions receive high trust and autonomous authorization. Payloads
marked as breaking or architectural changes become low-trust proposals with a
stable approval identifier and diff. The cockpit exposes the pending queue and
calls `/api/approval` to approve or reject without committing or acting before
approval.

## Local MCP Hub

`ToolProvider` accepts local MCP JSON-RPC `initialize`, `tools/list`, and
`tools/call` requests. Its `local.execute` tool requires governance approval,
uses no shell, stays in the configured workspace, and accepts only an explicit
read-only command profile for `python`, `git`, `pytest`, `mypy`, and `ruff`.
