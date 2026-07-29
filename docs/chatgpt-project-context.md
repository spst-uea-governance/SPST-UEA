# ChatGPT Project Context Integration

## Position

This integration makes an owner-supplied ChatGPT Project snapshot available to
Codex-mediated SPST tasks through deterministic, bounded retrieval. It does not
modify model weights, grant the runtime access to Codex internals, or establish
that an arbitrary JSON file was authentically exported by ChatGPT.

Historical text has `untrusted_evidence_only` authority. Current instructions,
repository policy, current code, and verified execution evidence take priority.

## Data flow

1. The owner supplies or reviews a complete `chatgpt-project-export-v1` JSON
   snapshot.
2. `spst_runtime.project_context_bridge compile` requires an owner-reviewed
   `complete` declaration with exact chat, message, and source counts, then
   validates Project identity, chat URLs, timestamps, roles, and unique IDs.
3. The compiler normalizes Unicode and line endings, splits long records,
   records source and content hashes, and writes a canonical corpus.
4. `status` rechecks the corpus digest, projected record digests, counts, and
   freshness without writing state.
5. `query` applies the existing deterministic relevance gate, rejects suspected
   prompt injection and duplicates, and enforces item and character budgets.
6. The repository skill gives Codex only the resulting task-specific packet.
7. Canonical delivery uses `spst-model-input-binding-v3`. The binder receives
   the reviewed corpus as verification-only input, deterministically replays the
   same query and budget, and rejects any packet, selected item, Project
   identity, source record, or corpus mismatch. Only selected items are
   projected into canonical model input; the whole corpus is not delivered.

Generated corpora belong under `.spst/project-context/` and are gitignored.

## Commands

Run from `runtime/`:

```powershell
python -m spst_runtime.project_context_bridge compile `
  --snapshot <owner-reviewed-snapshot.json> `
  --corpus ..\.spst\project-context\corpus.json

python -m spst_runtime.project_context_bridge status `
  --corpus ..\.spst\project-context\corpus.json

python -m spst_runtime.project_context_bridge query `
  --corpus ..\.spst\project-context\corpus.json `
  --query "<sanitized task>"
```

Exit code 0 means `ready` or a valid `empty` query. Exit code 2 means blocked.
Invalid snapshots and unreadable inputs also produce a structured `blocked`
result with exit code 2; a failed compile does not write the destination corpus.

## Model Input Binding Boundary

The packet digest detects accidental changes but is not an origin signature: a
caller could alter a packet and calculate a new digest. V3 therefore requires
the exact owner-reviewed corpus and recomputes the packet before binding it.
Binding evidence records the corpus digest, Project id, bounded completeness,
and the explicit authenticity status
`owner_supplied_not_independently_verified`. This proves deterministic lineage
inside the supplied evidence set, not that ChatGPT cryptographically signed the
snapshot and not that a provider model used the context.

## Evidence boundary

The corpus SHA-256 detects changes relative to its compiled manifest. Nested
record hashes and a regenerated segment projection detect inconsistent record
or projection edits. These are integrity checks, not a ChatGPT signature. The
corpus explicitly records source authenticity as
`owner_supplied_not_independently_verified`.

Browser UI inspection may establish an inventory, but virtualized page content
that was not durably captured is not a complete snapshot. In that state the
integration must report unavailable rather than filling gaps from inference.
