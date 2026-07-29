# ARCH-18 Low-Latency Evidence Plane

## Position

ARCH-18 reduces local SPST routing and evidence-retrieval overhead without
removing repository binding, provenance verification, corpus integrity checks,
freshness checks, relevance scoring, or output hashing. It is an operational
latency change. It does not establish model-quality improvement and does not
include provider or human latency.

## Implemented paths

- Query-only relevance compilation is reused across all candidate records.
- Candidate matching projects directly onto query terms instead of building a
  complete candidate lexicon. The canonical scoring formula and result fields
  are unchanged.
- Long-term-memory exact records are loaded in SQLite batches rather than by an
  N+1 connection pattern.
- A provided Repository identity is top-level validated without performing and
  discarding a second full worktree capture.
- Each worktree digest pass reads entries concurrently while preserving sorted
  framing, two complete content passes, per-file before/after metadata checks,
  Git index/untracked manifest rechecks, and fail-closed exceptions.
- `VerifiedProjectCorpus` retains a bounded lazy term-to-segment index. The
  loader rereads the complete corpus bytes and compares their SHA-256 before
  reusing a handle. A changed file must pass full verification as a new corpus.
- The handle rechecks the corpus freshness deadline on every query. Its term
  cache is limited to 512 terms; the file-handle cache is limited to four exact
  path-and-byte-digest entries.
- `project_context_bridge serve` provides a long-lived JSONL transport and
  checks the corpus file before every request. Packet selection and packet
  SHA-256 are regenerated for every request.
- The persistent Cockpit Runtime owns that JSONL process through
  `ProjectContextSupervisor`; see `project-context-supervisor.md`. A dead child
  may be restarted before a new request, but an unresolved in-flight request is
  never retried automatically.

## Fail-closed boundaries

The fast path is not a persistent trust assertion. It is valid only inside the
process that performed full corpus verification. Byte changes miss the cache;
invalid digests, inconsistent projections, stale corpora, malformed requests,
and repository changes remain blocking conditions. The process cache is not a
signature and is not shared across machines.

Repository capture intentionally does not use size/mtime as a content proxy.
Every tracked and untracked file is content-hashed twice. Parallel completion
order cannot affect the digest because framing follows the canonical sorted
path order.

## Reproducible benchmark

Run from the repository root with a previously issued Receipt. The benchmark
copies session, memory, and provenance-key sidecars into a temporary directory;
it does not route test records into the live databases.

```powershell
python .\benchmark\run_low_latency_benchmark.py `
  --repository . `
  --runtime .\runtime `
  --corpus .\.spst\project-context\corpus.json `
  --receipt-id <receipt-id> `
  --samples 5 `
  --output <isolated-output.json>
```

Compare results only on the same machine and corpus. Keep cold strict query,
warm verified query, Repository identity, read-only status, Receipt
verification, and Standard route as separate measurements. A repeated-query
warm result demonstrates the cache mechanism; it is not an estimate for novel
queries with no shared terms.

## Required regression evidence

- strict and verified queries return byte-identical canonical Packet hashes;
- byte-tampered corpora cannot reuse a verified handle;
- a handle becomes blocked after its freshness deadline;
- lexical projection matches full-lexicon intersection on structured, Latin,
  Japanese, stop-word, negative, and path-like cases;
- parallel Repository identity is deterministic and changes on content edits;
- bulk SQLite reads cross the 900-parameter batch boundary without writes;
- malformed JSONL requests do not poison subsequent valid requests.
