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

## Boundary

This does not give the local runtime direct access to the Codex chat model. Instead, Codex receives the chat message, invokes SPST-UEA locally, and reports the result back in the same conversation.
