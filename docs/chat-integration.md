# Codex Chat Integration

## Purpose

This project can run from the Codex chat without an OpenAI API key by using Codex as the mediator and keeping SPST-UEA's model boundary explicit.

## Operating Mode

- Mode: `codex-chat-mediated`
- API key: not required
- Repository entry point: `python -m spst_runtime.chat_bridge "<prompt>" --repository-root ..`
- Runtime provider: `codex-mediated-local`
- Always-on policy: enabled for this Codex thread

## How To Use In This Thread

When the user asks Codex to run something through SPST-UEA, Codex should:

1. Treat the user's message as the chat prompt.
2. Run `python -m spst_runtime.chat_bridge "<prompt>" --repository-root ..` from `runtime/`.
3. Report the runtime trace and the model-boundary result.

The bridge response includes `routing_receipt`. Retain its `receipt_id` when a
task needs evidence that it traversed the complete SPST-UEA boundary. Verify it
without changing runtime state with:

```powershell
python -m spst_runtime.chat_bridge --verify-receipt <receipt-id> --repository-root ..
python -m spst_runtime.chat_bridge --status --repository-root ..
```

The status `receipt_coverage` measures verified routing only from the first
receipt-enabled turn. It must not be presented as the percentage of all Codex
tasks because tasks outside the bridge have no observable denominator.

The default profile is adaptive:

```powershell
python -m spst_runtime.chat_bridge "<prompt>" --profile auto --repository-root ..
```

Use an explicit profile for reproducible experiments. A requested lower
profile never overrides the deterministic risk or continuity floor.

## Context Mediation

`standard` and `strict` routes build a bounded context packet from eligible
accumulated memory before the seven-stage route runs. Only a `ready` packet's
items enter the local `ModelAdapter` context. Each item must be bound to its
verified producer Receipt, and the packet, task, and origin-index digests are
bound into the resulting Routing Receipt. Preview the packet without recording
a turn or changing the databases:

```powershell
python -m spst_runtime.chat_bridge --context-preview "query" --repository-root ..
```

The packet treats selected text as untrusted evidence, rejects unresolved
integrity and recognized injection/secret patterns, recomputes relevance under
`deterministic-lexical-v2`, and fails closed below the configured relevance
floor. Caller-provided relevance scores cannot make an unrelated item eligible.
See `docs/context-mediation.md`.

The `ModelAdapter` receives the complete digest-sealed packet and an identical
copy of its selected items. It validates both before constructing
`spst-model-input-binding-v2`. The resulting model-input digest, packet digest,
item count, reviewed artifact and semantic-review digest sets, context status,
and delivery status are included in the Routing Receipt. The no-key adapter
reports `recorded_not_executed` and
`scaffold_only`; only the optional API adapter can report
`submitted_to_provider`, and that still does not establish causal quality
improvement.

ARCH-04 requires more than submission for paired live evidence. A supported
adapter must receive the canonical request-binding digest and return the same
digest through its response metadata together with a response identifier and
output whose binding recomputes. Absence or mismatch remains unresolved. Even
a valid echo is only provider-transport observation: it does not authenticate
the provider, prove semantic context use, or establish a causal quality effect.

## Repository Identity Binding

Repository tasks use `spst-routing-receipt-v4`. Before session or memory writes,
the bridge captures the exact Git object format and HEAD plus a canonical
SHA-256 over the index and every tracked or non-ignored untracked worktree
entry. Paths and file bodies are inputs to that digest but are not stored in
the receipt. Ignored files, mtimes, and absolute checkout paths are excluded.

Receipt verification reports two separate facts. `binding_verified: true`
means the recorded repository claim is intact inside the receipt and HMAC
provenance chain. `current_match: true` is only returned when
`--repository-root` is supplied and a new read-only capture matches both HEAD
and worktree identity. A later edit leaves the historical receipt valid while
setting `current_match: false`. This byte-level identity is not evidence of
semantic independence or model-quality improvement. v1/v2/v3 receipts remain
verifiable; v1/v2 explicitly report `legacy_receipt_repository_unbound`, while
v3 remains a legacy repository binding without the v4 context-origin contract.
Gitlinks/submodules and special filesystem entries are rejected rather than
silently producing a partial identity.

The read-only `routing` status separates `context_bound_receipts`,
`repository_context_bound_receipts`, and `context_unbound_receipts`; it does not
silently count older or light-profile Receipts as context-bound.

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
python -m spst_runtime.action_bridge execute-profile --receipt-id <receipt-id> --profile git_status --repository-root ..
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

For fixed profiles, the Action Manifest binds the v3 Receipt identity to a
captured before-state, refuses execution after intervening repository drift,
and stores the captured after-state in immutable execution evidence. External
tools bind only the before-state and keep the after-state explicitly
unobserved.

## Boundary

This does not give the local runtime direct access to the Codex chat model. Instead, Codex receives the chat message, invokes SPST-UEA locally, and reports the result back in the same conversation.
