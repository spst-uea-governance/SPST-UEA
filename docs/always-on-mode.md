# Always-On Codex-Mediated Mode

## Status

SPST-UEA is configured for always-on operation in this Codex thread.

## Default Mode

- Mode: `codex-chat-mediated`
- Runtime provider: `codex-mediated-local`
- API key required: no
- Repository entry point: `python -m spst_runtime.chat_bridge "<prompt>" --repository-root ..`
- Browser endpoint: `/api/run` also routes through the chat bridge.

## Operational Rule

When a user request is about SPST-UEA behavior, runtime operation, GPT integration, subject-state processing, governance, or evaluation, Codex should route the request through the SPST-UEA chat bridge before answering.

## Boundary

This mode does not replace Codex's own model with SPST-UEA. Instead, Codex acts as the mediator: it receives the chat message, invokes SPST-UEA locally, and reports the runtime result back to the user.

## Live Verification

A healthy always-on turn includes:

- `mode: codex-chat-mediated`
- `requires_api_key: false`
- `provider: codex-mediated-local`
- trace: `observe -> retrieve -> infer -> reflect -> govern -> commit -> act`
- persistent `turn_count`
- session `evaluation.esi`
- governance `latest_audit.authorized`
- intelligence `amplification.amplification_score`
- long-term `memory.stats.total_records`

Every completed bridge turn also emits a versioned routing receipt. The
receipt binds the prompt digest (never the prompt body), event, mode, provider,
complete seven-stage trace, governance decision, session turn, memory record,
and persisted session-state hash. The receipt is stored in the session SQLite
provenance chain and can be checked independently:

```powershell
python -m spst_runtime.chat_bridge --verify-receipt <receipt-id> --repository-root ..
```

`python -m spst_runtime.chat_bridge --status` uses an immutable SQLite
read-only connection. It does not create the database, provenance key, WAL, or
other sidecar files. The `routing` section reports verified receipts and
receipt coverage from the first receipt-enabled turn. Overall Codex-task
coverage remains `null` because SPST-UEA cannot observe tasks that bypass the
bridge. `task_quality_delta` also remains `null` until the same task has
independently verified normal and SPST-routed outcomes.

Repository-bound turns use `spst-routing-receipt-v4` and bind HEAD plus a
canonical index/tracked/untracked worktree digest alongside the adaptive
`light`, `standard`, or `strict` profile. `binding_verified` protects the
recorded historical identity; `current_match` requires a read-only recapture
with `--repository-root`. v4 also binds the mediated context packet, task, and
per-record origin index. Existing v1/v2/v3 receipts remain verifiable as legacy
evidence with their narrower contracts.
See `docs/adaptive-task-profiles.md` for risk floors and memory lifecycle.

Supported post-route local verification commands can be attached as immutable
Action Manifests and compact execution evidence with
`spst_runtime.action_bridge`. Receipt verification lists those child actions.
This does not create visibility into Codex tools that bypass the bridge;
`global_codex_tool_coverage` therefore remains `null`. See
`docs/action-manifest-binding.md`.
