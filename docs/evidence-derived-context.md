# ARCH-02: Evidence-Derived Context Compiler

ARCH-02 converts explicit local evidence into typed, bounded context records. It
uses the existing long-term-memory database and context-mediation path; it does
not create a second memory authority or grant model output permission to act.

## Problem

Raw chat memory is useful for continuity but is not a durable code or test fact.
Binding every record to the complete historical worktree also makes an otherwise
valid fact stale after an unrelated edit. Conversely, accepting a different
worktree merely because text still looks relevant would reuse unsupported
evidence.

The compiler separates two claims:

1. a verified Routing Receipt identifies the governed producer task;
2. a source-specific binding identifies the evidence that supports the context.

Both must verify. The resulting statement remains
`asserted_not_independently_established` and has
`untrusted_evidence_only` authority.

## Artifact types

- `architecture_decision`
- `code_fact`
- `test_evidence`
- `failure_lesson`
- `rule_crystal`

Free-form or unknown kinds fail closed. Every artifact binds its title,
statement, tags, source, producer Receipt, policy version, confidence, expiry,
lifecycle, and canonical model-facing projection into an artifact SHA-256.

## Supported evidence sources

### Repository file

The relative path, file byte count, SHA-256, Git blob object ID, and object
format are recorded. An unrelated repository edit does not invalidate this
source. Changing, deleting, replacing, or moving the bound file does.

Absolute paths, traversal, `.git` access, symlinks, missing files, and files over
1 MB are rejected. Absolute local paths are never stored in an artifact.

### Git commit

The resolved commit, tree, subject digest, and object format are recorded. The
commit must remain an ancestor of current `HEAD`. This source represents
historical evidence; it does not assert that every statement remains true of
newer code.

### Verified Action result

The Action Manifest, execution evidence, output digest, and after-repository
identity must pass `ActionManifestLedger.verify()`. External post-hoc
attestations are not execution-verified and are ineligible. `test_evidence`
additionally requires a successful `pytest`, `ruff`, `mypy`, or
`git_diff_check` fixed profile.

Because an Action result cannot safely identify which code bytes its conclusion
depends on, its after-repository identity must exactly match the current
repository. Any later repository change makes it stale.

## Compile commands

First create a repository-bound Routing Receipt for the task. Then run one of:

```powershell
python -m spst_runtime.evidence_context_bridge compile-file `
  --repository-root .. `
  --producer-receipt <receipt-id> `
  --kind architecture_decision `
  --title "Context source policy" `
  --statement "Repository-file evidence is revalidated by content." `
  --source-file docs/evidence-derived-context.md `
  --tag arch-02
```

```powershell
python -m spst_runtime.evidence_context_bridge compile-commit `
  --repository-root .. `
  --producer-receipt <receipt-id> `
  --kind architecture_decision `
  --title "Recorded architecture commit" `
  --statement "This commit records the reviewed architecture decision." `
  --revision HEAD
```

```powershell
python -m spst_runtime.evidence_context_bridge compile-action `
  --repository-root .. `
  --producer-receipt <receipt-id> `
  --kind test_evidence `
  --title "Runtime tests passed" `
  --statement "The bound fixed test profile completed successfully." `
  --action-id <action-id>
```

Compilation writes only to the configured existing long-term-memory database.
Use `--session-db` and `--memory-db` for isolated environments.

Verify one persisted record without changing either database:

```powershell
python -m spst_runtime.evidence_context_bridge verify `
  --repository-root .. `
  --record-id <memory-record-id>
```

Structural verification is necessary but no longer sufficient for delivery.
Record an exact semantic-support decision before normal preview can select the
artifact:

```powershell
python -m spst_runtime.evidence_context_bridge review `
  --repository-root .. `
  --record-id <memory-record-id> `
  --reviewer-id <local-reviewer-id> `
  --decision supported `
  --artifact-sha256 <artifact-sha256> `
  --source-sha256 <source-sha256> `
  --review-note "The bound source supports the bounded statement."
```

The review is append-only and binds the complete memory record, projection,
artifact, source, producer Receipt, and policy. The runtime records reviewer
identity as self-attested; it does not authenticate the person or establish
reviewer independence. An `unsupported` decision is retained and excludes the
artifact.

Normal read-only retrieval remains:

```powershell
python -m spst_runtime.chat_bridge `
  --context-preview "context source policy" `
  --repository-root ..
```

The preview revalidates candidate artifacts and augments the receipt-derived
origin index in memory, then applies the semantic-review gate. It does not
update access times, repair records, create indexes, or write provenance
entries.

## Lifecycle and failure behavior

Artifacts carry the current memory policy, confidence, and an explicit TTL.
Low-confidence, expired, policy-mismatched, retired, or source-stale artifacts
are excluded by the existing mediator.

`--supersedes <artifact-sha256>` creates and self-verifies the replacement
before retiring the exactly matched active predecessor. A failed replacement
does not retire the predecessor. Physical deletion is not performed.

Representative rejection reasons include:

- `artifact_producer_receipt_unverified`
- `artifact_source_file_digest_mismatch`
- `artifact_source_commit_not_ancestor`
- `artifact_source_action_repository_stale`
- `artifact_projection_mismatch`
- `artifact_digest_mismatch`
- `artifact_kind_unsupported`
- `artifact_repository_root_missing`
- `artifact_semantic_review_missing`
- `artifact_semantic_review_unsupported`
- `semantic_review_digest_mismatch`

## Evidence boundary

ARCH-02 proves deterministic compilation, source integrity, producer provenance,
policy eligibility, and model-input delivery binding. It does not prove the
semantic truth of a human- or Codex-authored statement, general GPT quality
improvement, model-weight change, or causal performance uplift. Those require
separate review and paired evaluation evidence.

ARCH-03 supplies that separate review and a bounded context-utility attribution
path without changing the immutable ARCH-02 artifact claim. See
`context-semantic-review-and-utility.md`.
