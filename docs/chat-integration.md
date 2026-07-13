# Codex Chat Integration

## Purpose

This project can run from the Codex chat without an OpenAI API key by using Codex as the mediator and keeping SPST-UEA's model boundary explicit.

## Operating Mode

- Mode: `codex-chat-mediated`
- API key: not required
- Entry point: `python -m spst_runtime.chat_bridge "<prompt>"`
- Runtime provider: `codex-mediated-local`
- Always-on policy: enabled for this Codex thread

## How To Use In This Thread

When the user asks Codex to run something through SPST-UEA, Codex should:

1. Treat the user's message as the chat prompt.
2. Run `python -m spst_runtime.chat_bridge "<prompt>"` from `runtime/`.
3. Report the runtime trace and the model-boundary result.

The bridge response includes `routing_receipt`. Retain its `receipt_id` when a
task needs evidence that it traversed the complete SPST-UEA boundary. Verify it
without changing runtime state with:

```powershell
python -m spst_runtime.chat_bridge --verify-receipt <receipt-id>
python -m spst_runtime.chat_bridge --status
```

The status `receipt_coverage` measures verified routing only from the first
receipt-enabled turn. It must not be presented as the percentage of all Codex
tasks because tasks outside the bridge have no observable denominator.

The default profile is adaptive:

```powershell
python -m spst_runtime.chat_bridge "<prompt>" --profile auto
```

Use an explicit profile for reproducible experiments. A requested lower
profile never overrides the deterministic risk or continuity floor.

## Boundary

This does not give the local runtime direct access to the Codex chat model. Instead, Codex receives the chat message, invokes SPST-UEA locally, and reports the result back in the same conversation.
