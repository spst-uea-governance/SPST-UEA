# Always-On Codex-Mediated Mode

## Status

SPST-UEA is configured for always-on operation in this Codex thread.

## Default Mode

- Mode: `codex-chat-mediated`
- Runtime provider: `codex-mediated-local`
- API key required: no
- Default entry point: `python -m spst_runtime.chat_bridge "<prompt>"`
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
