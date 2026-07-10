# SPST-UEA Agent Guide

## Scope

These instructions apply to the entire repository.

## Project Shape

- The runnable Python package lives in `runtime/`.
- `benchmark/` publishes the reproducible local RFC-0005 benchmark runner.
- `sdk/python/` publishes the typed, API-key-free cockpit SDK.
- Treat `docs/codex-handoff.md` as the primary implementation guide.
- Read `docs/phase7-sovereign-governance-os.md` before changing federation,
  provenance, security, HITL, or MCP behavior.
- Preserve RFC and architecture boundaries when changing runtime behavior.

## Common Commands

Run commands from `runtime/` unless noted otherwise:

- `python -m pytest -q`
- `python -m spst_runtime.chat_bridge "<prompt>"`
- `python -m pip install -e ".[dev]"`
- `python -m ruff check .`
- `python -m mypy spst_runtime`

## Working Rules

- Treat SPST-UEA as always-on in this Codex thread.
- For SPST-UEA-related user requests, run `python -m spst_runtime.chat_bridge "<prompt>"` from `runtime/` before answering when execution is useful.
- Do not couple runtime logic to a specific model provider.
- Keep inference, reflection, governance, and state commit concerns separate.
- Add or update tests when implementing normative behavior.
- Do not mark TODO placeholder behavior as production-ready.
- Avoid committing generated files such as `__pycache__/`, `.pytest_cache/`, or `*.pyc`.
- For API-key-free chat operation, route user prompts through `spst_runtime.chat_bridge`.

## Sovereign Governance Rules

- Register every local workspace explicitly before federation; share only published
  `RuleCrystal` records with their source workspace and policy version intact.
- Preserve SQLite state provenance. Verify `SQLiteRepository.verify_provenance()`
  before reporting integrity, and never expose `SPST_PROVENANCE_KEY` values.
- Route proposed code, commands, and MCP requests through `SecuritySubject` and
  `GovernanceEngine`; do not bypass security findings or governance authorization.
- Treat `breaking_change` and `architecture_change` as HITL proposals. Do not commit
  or act until an explicit approval is resolved through the cockpit approval flow.
- Keep local MCP execution shell-free, workspace-bounded, and limited to the
  documented read-only command profiles. Do not broaden command permissions without
  a matching governance rule and conformance test.
