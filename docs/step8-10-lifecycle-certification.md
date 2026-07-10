# Step 8-10 Lifecycle Certification

## Step 8: Governance Decision Interface

`GovernanceEngine.authorize(action)` remains the mandatory authorization gate
for state transitions and protected identity changes. The lifecycle
orchestrator records governance decisions in state metadata and persists them
as audit records.

## Step 9: Runtime Lifecycle and ACID Persistence

`RuntimeOrchestrator` commits the resulting subject state and audit envelope to
`SQLiteRepository`, which enables WAL mode on every connection. Persistence is
reconstructed from saved state rather than hidden process globals.

## Step 10: E2E Conformance Certification

The orchestrator now wires the lifecycle phases:

1. retrieve memory context via `MemoryProvider`
2. infer through the vendor-neutral `ModelProvider`
3. reflect candidate state
4. govern the transition action
5. commit state and audit to SQLite
6. act by publishing `runtime.action` and storing memory

The final conformance test certifies trace recording, structured transition
logs, governance audit persistence, runtime action publication, and memory
settlement in one end-to-end cycle.
