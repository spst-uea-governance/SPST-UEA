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

ARCH-04 adds a stricter live path for adapters that return an exact request
binding acknowledgement. Inspect the blind pending artifact without changing
the evaluation database, then append a human review only after inspecting that
surface:

```powershell
python -m spst_runtime.paired_quality_bridge get `
  --evaluation-db <isolated-evaluation.db> `
  --evaluation-id <paired-evaluation-id>

python -m spst_runtime.paired_quality_bridge review `
  --evaluation-db <isolated-evaluation.db> `
  --evaluation-id <paired-evaluation-id> `
  --reviewer-id <independent-human-reviewer> `
  --decision accepted `
  --scoring-artifact-digest <artifact-digest> `
  --blind-review-surface-sha256 <surface-digest> `
  --arm-mapping-not-accessed `
  --reviewer-independence-attested
```

The pending projection withholds source-pair mappings and measured direction.
The flags are self-attestations; they do not authenticate the reviewer. Exact
provider acknowledgement proves only observed transport of the bound request,
not semantic use, causality, or general GPT improvement. The bundled
conformance path uses a no-charge in-process provider; no bundled network
adapter currently emits this observation contract. See
`../docs/provider-observed-live-context-evaluation.md`.

ARCH-06 wraps that live path in a durable pre-registered Program. The Python API
freezes the selected rubric cohort, reviewed intervention, provider/model and
observation source, randomized order commitment, no-optional-stopping rule,
adapter-invocation cap, and separate external/paid authority. Call `preflight()`
before `execute()`; preflight changes no state and makes no provider calls.

Inspect a Program without changing its database:

```powershell
python -m spst_runtime.real_paired_outcome_bridge get `
  --evaluation-db <isolated-evaluation.db> `
  --program-id <program-id>

python -m spst_runtime.real_paired_outcome_bridge status `
  --evaluation-db <isolated-evaluation.db>
```

The in-process conformance adapter remains `mechanism_validation_only`. No paid
provider is invoked by the bundled Program. A remote metadata echo can establish
observed transport, not authenticated provider identity, causal context use, or
general model-quality uplift. See `../docs/real-paired-outcome-program.md`.

The optional `CodexCliAdapter` can execute an ephemeral, read-only Codex CLI
round trip under confirmed ChatGPT login without exposing API-key environment
variables. It records a schema-constrained request-binding echo from the CLI
JSONL stream. Because the CLI cannot attest quota, credits, or overage state,
this path is classified as `chatgpt_plan_usage` and requires an explicit v2
plan-usage authority before any inference. It is never silently treated as
`no_charge`; see `../docs/real-paired-outcome-program.md`.

Freeze a task-local action denominator before preparing any Action Manifest,
then inspect coverage through the immutable read-only path:

```powershell
python -m spst_runtime.execution_coverage_bridge register `
  --session-db <isolated-session.db> `
  --receipt-id <receipt-id> `
  --plan-file <plan.json> `
  --repository-root ..

python -m spst_runtime.execution_coverage_bridge status `
  --session-db <isolated-session.db> `
  --receipt-id <receipt-id>
```

Only exact, ordered, same-workspace post-plan Action Manifests satisfy slots.
External result attestations remain execution-unverified. The denominator is a
caller declaration whose completeness cannot be authenticated, so this does
not measure all Codex tool calls. See
`../docs/governed-execution-coverage.md`.

After `python -m pip install -e ".[dev]"`, the console script is also available:

```bash
spst-runtime --steps 3 --event codex_run
```

## ARCH-07 operational hardening

Real paired outcome execution now acquires an atomic one-attempt claim before
adapter invocation. Concurrent callers cannot start a second run, ordinary
adapter failures become terminal without storing raw error text, and interrupted
runs surface as read-only `recovery_required` state with automatic retry
disabled. See `../docs/operational-hardening.md`.

## ARCH-08 provider transport recovery

Adapters may opt into the ARCH-08 path only by declaring both transport
idempotency and reconciliation and implementing `reconcile_transport`. Each
call then receives a Program/Attempt/ordinal-bound idempotency request and must
return an exact provider transport receipt. `RealPairedOutcomeProgram.recover()`
requires an explicit recovery-authority artifact, reconciles uncertain results,
and replays completed results without another inference. The no-charge test
adapter validates this mechanism; the bundled OpenAI adapter does not claim the
capability. See `../docs/provider-transport-recovery.md`.

## ARCH-09 process-isolated transport recovery

`ProcessIsolatedTransportAdapter` executes a no-network provider simulator in
fresh Python worker processes and stores results in a dedicated SQLite file.
The process integration test terminates the first client only after the
provider commit marker exists, then performs `recover()` from another client
PID. It requires 32 logical executions for 32 scheduled calls, one
reconciliation, and no 33rd inference request. This validates local crash
recovery mechanics only; it does not authenticate an external provider or
prove exactly-once semantics. See
`../docs/process-isolated-transport-recovery.md`.

## ARCH-10 authenticated provider store and recovery supervisor

The local process provider now HMAC-authenticates its instance, result,
reconciliation, and recovery-lease rows with a key file outside SQLite. The
Program binds the key ID and protocol schemas before execution. Recovery runs
under a fenced lease whose heartbeat prevents live-owner takeover; after a hard
kill, a fresh supervisor needs both expiry and an exact orphan-adoption
authority. Key rotation re-authenticates existing rows and invalidates the old
key. This remains local mechanism validation. See
`../docs/authenticated-provider-store-supervisor.md`.

## ARCH-11 recovery authority PKI and supervisor attestation

The optional v3 local-process profile binds an Ed25519 root trust anchor into
the Program and provider-store identity. A root-signed operator certificate
signs an exact Program/Attempt/provider/lease recovery grant, which in turn
authorizes a separate supervisor event-signing key. Recovery appends a signed,
hash-linked authority/lease/heartbeat/result/release lifecycle to the Program
repository. `process_recovery_supervisor status` can project that lifecycle
through a read-only Program and provider path. Local certificate verification
does not establish real-world operator identity or hardware-backed key custody.
See `../docs/recovery-authority-pki-supervisor-attestation.md`.

## ARCH-12 authority state and rollback defense

The optional v4 local-process profile requires a current root-signed authority
state and rollback anchor at every live recovery boundary. The grant binds the
state generation, revocation snapshot, authenticated time floor, custody
evidence, and anchor. Store lease acquisition and Program recovery reject
missing or older live state, revocation, clock rollback below the floor, and
state/anchor rewrites. Historical event validation may use the grant-embedded
snapshot, while new supervisor sessions must not lower the accepted generation.
The built-in local-file custody profile keeps trusted time, hardware custody,
and full rollback resistance explicitly false. See
`../docs/recovery-authority-state-rollback-defense.md`.
