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

## Prompt Persistence and Redaction

The receipt contains a SHA-256 prompt digest rather than the prompt body, but
that does not mean the prompt is absent from all local state. Every profile
persists the routed prompt in the local session database. `standard` and
`strict` also persist it in policy-filtered long-term memory; `light` skips only
the long-term-memory read/write path.

Never route secrets, credentials, private attachment bodies, or unnecessary raw
personal data. Route a sanitized execution contract containing only the task
objective, constraints, acceptance conditions, and risk signals. When the
contract is sanitized, report accurately that the receipt binds that contract,
not the complete raw user message.

See `docs/codex-autonomous-engineering-master-prompt-spst.md` for the complete
Codex task policy.

## Receipt-Bound Actions

For supported fixed local profiles, bind the post-route operation to its parent
receipt and execute it through the governed action bridge:

```powershell
python -m spst_runtime.action_bridge execute-profile --receipt-id <receipt-id> --profile git_status --workspace-root ..
python -m spst_runtime.action_bridge verify --action-id <action-id>
python -m spst_runtime.action_bridge receipt-status --receipt-id <receipt-id>
```

`chat_bridge --verify-receipt` also returns the receipt's child-action summary.
Only fixed profiles executed inside this boundary can set
`execution_verified: true`. External Codex tools can have a planned manifest
and HITL decision, but remain `external_execution_unobserved` because the local
runtime cannot intercept their actual invocation. See
`docs/action-manifest-binding.md`. `global_codex_tool_coverage` remains `null`;
the runtime cannot observe a denominator for bypassed Codex tool calls.

## Boundary

This does not give the local runtime direct access to the Codex chat model. Instead, Codex receives the chat message, invokes SPST-UEA locally, and reports the result back in the same conversation.
