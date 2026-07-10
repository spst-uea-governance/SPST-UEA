# Phase 7: Runtime Completion

Phase 7 closes the remaining executable reference-runtime gaps in the frozen
architecture without changing the model-provider or governance boundaries.

## Lifecycle and Recovery

`RuntimeEngine` now owns an auditable RFC-0003 lifecycle state machine,
priority scheduler, deep-copy checkpoints, recovery flow, and explicit
archival termination. Lifecycle transitions emit `runtime.lifecycle` events.

## Migration

`MigrationService` exports a schema-versioned manifest with adapter metadata,
canonical SHA-256 integrity hash, compatibility report, and continuity ESI.
Imports reject altered payloads before reconstructing a `SubjectState`.

## Public Integration

`spst_runtime.sdk.LocalRuntimeClient` and `sdk/python` expose the local cockpit
contract without external network access or API keys. `benchmark/run_benchmark.py`
publishes the RFC-0005 report fields with isolated SQLite/WAL storage.

## Operational Boundary

The completion layer is a local Codex-mediated cognitive runtime. It does not
replace Codex's internal model, use account-level memory, or make direct GPT
API requests in the default mode.
