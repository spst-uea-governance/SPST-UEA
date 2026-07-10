# High-Level Architecture

## 1. Architectural Goal

The SPST-UEA architecture enables a persistent AI system to maintain operational continuity across sessions, interruptions, model substitutions, and changing environments.

## 2. Core Layers

```text
Applications
    ↓
Cognitive Services
    ↓
Subject Runtime
    ↓
Persistence and Governance
    ↓
Model / Tool / Storage Adapters
    ↓
External Infrastructure
```

## 3. Core Components

### 3.1 Subject State Manager

Owns the canonical versioned subject state.

Responsibilities:

- state validation,
- state versioning,
- transition commits,
- conflict detection,
- snapshots,
- and reconstruction checkpoints.

### 3.2 Identity Manager

Maintains identity-relevant invariants, roles, policies, provenance, and continuity constraints.

### 3.3 Goal Manager

Maintains goals, priorities, dependencies, state, provenance, and termination conditions.

### 3.4 Memory Manager

Provides typed storage and retrieval for episodic, semantic, procedural, policy, and audit memory.

### 3.5 Reflection Engine

Evaluates plans, actions, outcomes, uncertainty, contradictions, and state integrity.

### 3.6 Governance Engine

Applies policy, authorization, safety constraints, escalation rules, and recovery actions.

### 3.7 Scheduler

Selects which work item, reflection cycle, maintenance task, or event handler runs next.

### 3.8 Event Bus

Transports typed events between modules.

### 3.9 Model Adapter Layer

Provides provider-independent access to inference engines.

### 3.10 Tool Adapter Layer

Provides governed access to external tools and environments.

### 3.11 Evaluation Engine

Computes operational metrics and emits evaluation records.

## 4. Mandatory Boundaries

A compliant implementation MUST separate:

- model output from committed state,
- retrieved memory from validated memory,
- proposed goals from approved goals,
- reflection proposals from authoritative updates,
- and tool intent from tool authorization.

## 5. State Transition Pattern

```text
Observe
  ↓
Retrieve Context
  ↓
Generate Candidate
  ↓
Evaluate Candidate
  ↓
Apply Governance
  ↓
Commit State Transition
  ↓
Act
  ↓
Record Outcome
```

## 6. Replaceability

An implementation SHOULD allow replacement of:

- inference model,
- memory backend,
- workflow engine,
- scheduler,
- embedding model,
- graph store,
- and tool execution environment.

## 7. Non-Normative Reference Stack

A reference implementation MAY use:

- Python,
- FastAPI,
- LangGraph,
- Temporal,
- Mem0 or an equivalent memory layer,
- Neo4j,
- PostgreSQL,
- and sandboxed tool execution.

These technologies are not required by the standard.
