# Operational Subject Model

## 1. Subject State

The canonical subject state is represented as:

```text
S(t) = {
  identity,
  goals,
  memory_refs,
  policies,
  runtime_state,
  environment_model,
  reflection_state,
  governance_state,
  provenance,
  version
}
```

## 2. Reconstruction Function

A compliant runtime reconstructs state using a function conceptually equivalent to:

```text
S(t+1) = R(S(t), O(t), M(t), G(t), C(t), P(t))
```

Where:

- `O(t)` is the current observation,
- `M(t)` is retrieved memory,
- `G(t)` is active goal state,
- `C(t)` is the applicable constraint set,
- `P(t)` is governance policy,
- and `R` is the reconstruction process.

The function need not be mathematically continuous or implemented as one function.

## 3. State Classes

### Identity State

Contains:

- system identifier,
- role,
- mission,
- commitments,
- invariant constraints,
- approved self-description,
- and provenance.

### Goal State

Contains:

- goal identifier,
- description,
- priority,
- parent goal,
- dependencies,
- status,
- creation source,
- authorization,
- and completion criteria.

### Memory State

Contains references and retrieval metadata, not necessarily all memory payloads.

### Governance State

Contains:

- applicable policy set,
- permissions,
- active restrictions,
- risk state,
- unresolved violations,
- and escalation status.

### Runtime State

Contains:

- lifecycle status,
- pending events,
- active tasks,
- retry state,
- current execution lease,
- and checkpoint metadata.

## 4. State Integrity

Every committed state MUST:

- have a unique version,
- identify its predecessor,
- include provenance,
- pass schema validation,
- pass governance validation,
- and be durably persisted before dependent actions are treated as committed.

## 5. Continuity

Continuity is measured through the preservation and reconstruction of relevant state, not through exact byte identity.

## 6. Identity Invariants

Identity invariants MAY change only through an explicit authorized transition.

Ordinary inference output MUST NOT directly mutate identity invariants.
