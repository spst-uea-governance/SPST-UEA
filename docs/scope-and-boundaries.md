# Scope and Boundaries

## In Scope

SPST-UEA covers the architecture and evaluation of persistent AI systems, including:

- multi-session continuity,
- long-term memory,
- goal persistence,
- identity constraints,
- state reconstruction,
- reflection,
- governance,
- scheduling,
- model abstraction,
- tool use,
- event-driven execution,
- observability,
- conformance,
- benchmark design,
- and evidence tracking.

## Out of Scope

SPST-UEA does not claim to define or prove:

- human consciousness,
- artificial consciousness,
- phenomenal qualia,
- biological cognition,
- neuroscience,
- a complete philosophy of mind,
- guaranteed AGI,
- guaranteed ASI,
- moral personhood,
- legal personhood,
- or intrinsic sentience.

## Operational Boundary

The system boundary begins where managed subject state is initialized and ends where that state is terminated, archived, or migrated according to policy.

External components include:

- foundation models,
- vector databases,
- graph databases,
- workflow engines,
- external tools,
- user interfaces,
- physical devices,
- and third-party services.

These MAY participate in an implementation but are not themselves SPST-UEA.

## Safety Boundary

SPST-UEA is an architecture standard, not a safety guarantee.

Implementations MUST define:

- permission boundaries,
- tool authorization,
- human intervention paths,
- audit logs,
- state rollback,
- secret handling,
- and failure containment.

## Naming Boundary

The term `subject` is used operationally.

In implementation-facing documents, `Persistent Agent System` MAY be used when a less philosophical term improves clarity.
