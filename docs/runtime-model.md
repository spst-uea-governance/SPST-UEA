# Runtime Model

## 1. Runtime Responsibilities

The runtime is responsible for:

- lifecycle management,
- event ingestion,
- scheduling,
- state reconstruction,
- state transition control,
- checkpointing,
- retries,
- observability,
- module coordination,
- recovery,
- and termination.

## 2. Lifecycle

```text
Uninitialized
  ↓
Initializing
  ↓
Active
  ↙       ↘
Paused   Degraded
  ↓         ↓
Recovering
  ↓
Active
  ↓
Migrating or Terminating
  ↓
Archived
```

## 3. Event Types

The runtime SHOULD support typed events including:

- observation received,
- memory updated,
- goal created,
- goal changed,
- state reconstruction requested,
- reflection requested,
- governance violation detected,
- plan approved,
- action completed,
- action failed,
- checkpoint created,
- recovery initiated,
- and migration completed.

## 4. Scheduler Policy

The scheduler MUST consider:

- priority,
- urgency,
- dependencies,
- resource limits,
- governance constraints,
- timeouts,
- and starvation prevention.

## 5. Checkpointing

Checkpoints SHOULD include:

- state version,
- pending events,
- goal state,
- governance state,
- memory references,
- adapter versions,
- and reconstruction metadata.

## 6. Recovery

Recovery MUST NOT assume that the previous model process remains available.

The runtime SHOULD be able to restore from persisted state using a compatible model adapter.

## 7. Migration

Migration is the transfer of managed continuity to a new runtime, model, or infrastructure environment.

A migration MUST produce:

- export manifest,
- schema version,
- compatibility report,
- integrity hash,
- import result,
- and post-migration continuity evaluation.

## 8. Termination

Termination MUST be explicit, auditable, and policy-governed.

The runtime SHOULD distinguish:

- temporary pause,
- operational shutdown,
- archival,
- migration,
- and irreversible deletion.
