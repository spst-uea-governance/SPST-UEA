# Long-Term Memory

## Purpose

SPST-UEA now has a persistent long-term memory layer for Codex-mediated operation.

## Capabilities

- Store every SPST-UEA chat turn as an episodic memory.
- Search past memories with lightweight lexical matching.
- Keep memory statistics by kind.
- Consolidate durable high-salience memories and tags.
- Surface retrieved memory context into the intelligence amplification layer.

## Storage

The default database is `runtime/spst_long_term_memory.db`.

## Boundary

This memory is local to this workspace. It does not modify Codex's built-in memory or the user's account-level memory.
