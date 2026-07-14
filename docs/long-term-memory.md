# Long-Term Memory

## Purpose

SPST-UEA now has a persistent long-term memory layer for Codex-mediated operation.

## Capabilities

- Store every SPST-UEA chat turn as an episodic memory.
- Search past memories with lightweight lexical matching.
- Keep memory statistics by kind.
- Consolidate durable high-salience memories and tags.
- Surface retrieved memory context into the intelligence amplification layer.

## Retrieval Policy

Normal runtime retrieval is fail-closed against the current covenant policy.
A record is reusable only when it is active, unexpired, above the selected
confidence floor, and explicitly stamped with the current policy version.

Legacy records without a policy version and records stamped by another policy
remain in storage for audit, but are quarantined from:

- normal search results;
- consolidated durable tags and high-salience context; and
- RuleCrystal distillation.

`LongTermMemoryStore.retrieval_health()` reports eligible, ineligible, and
policy-quarantined counts with explicit reason codes. A caller may pass
`policy_version=None` for an explicit audit-only search across policy versions;
the chat runtime never uses that bypass for operational context.

This policy does not prove that an eligible memory is true. Source quality,
confidence, TTL, and task-specific verification remain separate obligations.

## Storage

The default database is `runtime/spst_long_term_memory.db`.

## Boundary

This memory is local to this workspace. It does not modify Codex's built-in memory or the user's account-level memory.
