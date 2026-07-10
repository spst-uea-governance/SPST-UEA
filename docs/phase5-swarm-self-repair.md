# Phase 5 Swarm Self-Repair

Phase 5 adds local multi-subject federation and deterministic self-repair.

## Multi-Agent Federation

`SubjectRepository` can create and store independent subjects such as planner,
executor, and verifier roles. `RuntimeOrchestrator.dispatch_subject()` routes
`inter_subject_message` events through `EventBus` and updates per-subject swarm
metadata without shared mutable globals.

Each subject still runs through the same seven-step transition pipeline and is
persisted under `runtime:subject:<subject_id>` when committed.

## Self-Repair Sandbox

`ToolProvider` implements a local secure tool boundary for deterministic
diagnosis and repair planning. When reflection detects low ESI, or a payload
simulates execution failure, the transition engine records a `self_repair`
envelope, requests governance authorization, runs diagnosis and repair actions,
and normalizes the reflection state before commit.

No external API key or networked tool execution is required.
