# Runtime Baseline 0.1

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
python -m mypy spst_runtime
```

## Minimal Verification

If development dependencies are not installed, the baseline test suite can still be checked with:

```bash
python -m pytest -q
```

## Run Locally

```bash
python -m spst_runtime --steps 3 --event codex_run
```

To route a prompt through the API-key-free local/Codex-mediated model boundary:

```bash
python -m spst_runtime --steps 3 --event codex_run --prompt "Say hello from SPST-UEA."
```

Optional: to call OpenAI GPT directly from the runtime itself, set `OPENAI_API_KEY` in your shell and pass `--use-gpt`:

```bash
python -m spst_runtime --steps 3 --event gpt_run --use-gpt --prompt "Say hello from SPST-UEA."
```

## Run In A Browser

```bash
python -m spst_runtime.web --host 127.0.0.1 --port 8767
```

Then open `http://127.0.0.1:8767/`.

On Windows, use the persistent launcher:

```powershell
.\run_spst_web_8767.ps1
```

To stop the persistent browser UI:

```powershell
.\stop_spst_web_8767.ps1
```

The browser UI runs in API-key-free local/Codex-mediated mode by default.

## Run From This Codex Chat

For API-key-free operation from this chat, use the Codex-mediated chat bridge:

```bash
python -m spst_runtime.chat_bridge "Your prompt here"
```

Check the persisted always-on session:

```bash
python -m spst_runtime.chat_bridge --status
```

Preview the bounded context packet without changing session or memory state:

```bash
python -m spst_runtime.chat_bridge --context-preview "Your query" --repository-root ..
```

Compile an explicit repository source into the typed evidence-context layer:

```powershell
python -m spst_runtime.evidence_context_bridge compile-file `
  --repository-root .. `
  --producer-receipt <receipt-id> `
  --kind code_fact `
  --title "Bound code fact" `
  --statement "This statement is bound to the selected source bytes." `
  --source-file path/to/source.py
```

The compiler uses the existing long-term-memory DB and revalidates its source on
every preview. A compiled artifact is not delivered until an exact, append-only
human semantic-support review is recorded. Reviewer identity remains
self-attested, and every delivered item remains untrusted evidence. See
`../docs/evidence-derived-context.md` and
`../docs/context-semantic-review-and-utility.md`.

```powershell
python -m spst_runtime.evidence_context_bridge review `
  --repository-root .. `
  --record-id <memory-record-id> `
  --reviewer-id <local-reviewer-id> `
  --decision supported `
  --artifact-sha256 <artifact-sha256> `
  --source-sha256 <source-sha256> `
  --review-note "Why the exact source supports the bounded statement"
```

Only memory records with a verified producer Receipt are eligible; repository
routes additionally require the producer's exact repository identity to match.
Relevance is recomputed under `deterministic-lexical-v2` with a fail-closed
floor; a caller-supplied score cannot promote unrelated memory. A ready packet
is projected into `spst-model-input-binding-v2`, whose digest, reviewed artifact
set, semantic-review set, and delivery status are included in the Routing
Receipt. The API-key-free adapter records
the binding as `scaffold_only` and does not claim that Codex consumed it.
See `../docs/context-mediation.md` for the selection, provenance, budget,
origin-binding, and instruction-authority boundaries.

Run local maintenance:

```bash
python -m spst_runtime.maintenance
```

Maintenance expires and compacts records in the configured memory database.
It does not delete Repository caches, bytecode, source files, or ignored user
artifacts. Dynamic verifier source is held in memory rather than written into
the package tree.

In this thread, requests addressed to SPST-UEA can be routed through that bridge by Codex.

Task-specific quality evidence is separate from routing and contract-proxy
metrics. The Phase 16 local API accepts only stored PBIND references, scores
eight or more same-model/task pairs through an arm-blinded exact-JSON evaluator,
reports a fixed 95% Hoeffding bound, and requires a digest-bound human review:

- `POST /api/paired-quality-evaluations`
- `GET /api/paired-quality-evaluations`
- `POST /api/paired-quality-evaluations/review`

See `../docs/phase16-independent-paired-quality.md`. The offline conformance
fixture proves the mechanism only; it is not GPT-uplift evidence.

After an isolated context experiment and accepted Phase 16 review, derive or
inspect bounded utility without supplying scores:

```powershell
python -m spst_runtime.context_utility_bridge attribute `
  --repository-root .. `
  --evaluation-id <paired-evaluation-id> `
  --artifact-sha256 <artifact-sha256>

python -m spst_runtime.context_utility_bridge status --repository-root ..
```

The result distinguishes beneficial association, harmful association, and an
inconclusive interval. It does not prove provider uptake, causality, or general
GPT quality.

After `python -m pip install -e ".[dev]"`, the console script is also available:

```bash
spst-runtime --steps 3 --event codex_run
```
