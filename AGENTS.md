# SPST-UEA Agent Guide

## Scope

These instructions apply to the entire repository.

## Project Shape

- The runnable Python package lives in `runtime/`.
- `benchmark/` publishes the reproducible local RFC-0005 benchmark runner.
- `sdk/python/` publishes the typed, API-key-free cockpit SDK.
- Treat `docs/spst-uea-covenant.md` as the constitutional governance layer for
  AI-human co-creation, autonomy levels, non-negotiables, failure handling, and
  memory identity.
- Treat `docs/codex-handoff.md` as the primary implementation guide.
- Treat `docs/codex-autonomous-engineering-master-prompt-spst.md` as the
  adaptive task-execution contract for Codex-mediated work.
- Read `docs/phase7-sovereign-governance-os.md` before changing federation,
  provenance, security, HITL, or MCP behavior.
- Preserve RFC and architecture boundaries when changing runtime behavior.

## Common Commands

Run commands from `runtime/` unless noted otherwise:

- `python -m pytest -q`
- `python -m spst_runtime.chat_bridge "<sanitized-contract>" --profile auto`
- `python -m spst_runtime.chat_bridge --verify-receipt <receipt-id>`
- `python -m spst_runtime.chat_bridge --status`
- `python -m spst_runtime.action_bridge execute-profile --receipt-id <receipt-id> --profile git_status --workspace-root ..`
- `python -m spst_runtime.action_bridge verify --action-id <action-id>`
- `python -m spst_runtime.action_bridge receipt-status --receipt-id <receipt-id>`
- `python -m pip install -e ".[dev]"`
- `python -m ruff check .`
- `python -m mypy spst_runtime`

## Working Rules

- Treat adaptive SPST-UEA routing as always-on in this Codex thread. Always-on
  does not mean applying the same orchestration or memory cost to every task.
- Preserve the core ethos: "改造ではなく、開花。シンプルで、美しく。"
  Prefer scaffolds that open latent capability over claims that models were
  intrinsically modified, and keep implementations legible enough to audit.
- Preserve Honesty First: distinguish verified evidence, local inference, and
  aspirational roadmap language.
- For SPST-UEA-related user requests, run
  `python -m spst_runtime.chat_bridge "<sanitized-contract>" --profile auto`
  from `runtime/` before answering when execution is useful. Let deterministic
  risk and continuity floors select `light`, `standard`, or `strict`.
- Do not pass secrets, credentials, private attachment bodies, or other raw
  sensitive content to the bridge. Routed prompts are persisted in local
  session state in every profile and additionally in policy-filtered long-term
  memory for `standard` and `strict`.
- Do not claim that a task traversed SPST-UEA unless its routing receipt passes
  `--verify-receipt`. Use `--status` for a read-only health path.
- A verified receipt proves routing, profile, trace, governance, memory action,
  session binding, and provenance. Bind supported post-route `git`, `pytest`,
  `ruff`, and `mypy` profiles through `spst_runtime.action_bridge` when
  action-level execution evidence is required.
- Treat R2 and unresolved external Action Manifests as HITL-pending. An approval
  record does not make an external Codex tool execution observable or verified.
- Do not claim that `apply_patch`, arbitrary shell, browser, or connector calls
  were cryptographically execution-bound. The repository cannot intercept
  those Codex App tools; report them as outside verified action coverage and
  keep `global_codex_tool_coverage` unmeasured.
- Do not couple runtime logic to a specific model provider.
- Keep inference, reflection, governance, and state commit concerns separate.
- Add or update tests when implementing normative behavior.
- Do not mark TODO placeholder behavior as production-ready.
- Avoid committing generated files such as `__pycache__/`, `.pytest_cache/`, or `*.pyc`.
- For API-key-free chat operation, route sanitized task contracts through
  `spst_runtime.chat_bridge`.

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
