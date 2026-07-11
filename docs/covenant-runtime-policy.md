# Covenant Runtime Policy

This document describes how `docs/spst-uea-covenant.md` is connected to runtime
governance. The goal is to make the Covenant executable without expanding
SPST-UEA beyond its local, Codex-mediated cognitive governance OS boundary.

## Architecture

```text
Event payload
  -> TransitionEngine
  -> StateTransitionPipeline governance action
  -> CovenantPolicyEngine.evaluate(action)
  -> GovernanceEngine.decide(action)
  -> commit / HITL pending / rejection
```

## Runtime Component

`CovenantPolicyEngine` is the deterministic policy interpreter for the Covenant.
It attaches an auditable `covenant_policy` envelope to each governed transition:

- `policy_version`
- `active_clauses`
- `autonomy_level`
- `authorized`
- `requires_human_approval`
- `violations`
- `escalations`
- `reasons`
- `boundary`

## Clause Mapping

| Covenant clause | Runtime behavior |
|---|---|
| Sovereign Human Dignity | Blocks payloads marked as human harm, coercion, rights violation, or authority override. |
| Absolute Auditability | Blocks hidden channels, concealed traces, unaudited actions, and backdoor paths. |
| Strict Authority Boundaries | Blocks self-replication and escalates external/protected boundary access. |
| Informed Consent | Escalates destructive, permanent, external-transaction, breaking, or architectural changes to HITL. |
| Honesty First | Blocks unsupported claims of model weight modification, sentience, unsupported benchmark uplift, or direct GPT execution without API context. |
| Evidence Before Assertions | Preserves governance evidence in `governance.covenant_policy`. |
| Memory & Identity Doctrine | Keeps identity and authority inside local governance continuity rather than model output mutation. |

## Autonomy Level Derivation

| Level | Trigger |
|---|---|
| `L0` | `analysis_only` or `read_only` payloads |
| `L1` | destructive, permanent, external, protected-boundary, or self-replication flags |
| `L2` | routine bounded local implementation and documentation |
| `L3` | `system_tick` autonomous loop events |
| `L4` | local dynamic tool generation or rule crystallization |

Explicit `autonomy_level` values are accepted only when they normalize to
`L0` through `L4`; otherwise the deterministic derivation is used.

## Outcomes

- **Authorized**: routine local transitions continue to commit.
- **HITL pending**: destructive or external-boundary transitions pause before
  commit and appear in the approval queue.
- **Rejected**: non-negotiable violations are blocked before commit.

This policy is intentionally small. It does not try to become a general legal or
ethical reasoner; it maps the Covenant's non-negotiables into deterministic,
testable runtime gates.
