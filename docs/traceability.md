# Traceability Model

## 1. Purpose

Traceability ensures that every stable claim can be connected to requirements, implementation, tests, benchmarks, and evidence.

## 2. Trace Chain

```text
Claim
  ↓
Requirement
  ↓
Specification
  ↓
Implementation
  ↓
Test
  ↓
Benchmark
  ↓
Evidence
  ↓
Decision
```

## 3. Identifier Classes

Recommended identifier prefixes:

- `CLM-` Claim
- `REQ-` Requirement
- `SPEC-` Specification item
- `ARC-` Architecture decision
- `API-` Interface requirement
- `TEST-` Test
- `BENCH-` Benchmark case
- `EVID-` Evidence record
- `RISK-` Risk
- `RFC-` Change proposal

## 4. Minimum Traceability Rule

A stable requirement MUST map to:

- at least one specification item,
- at least one implementation artifact,
- and at least one test.

A stable claim MUST additionally map to evidence.

## 5. Evidence Record

Each evidence record SHOULD contain:

- identifier,
- claim tested,
- experiment design,
- environment,
- artifacts,
- raw result location,
- analysis method,
- result,
- limitations,
- and reviewer.

## 6. Change Impact

Any change to a requirement or specification MUST identify affected:

- implementations,
- tests,
- benchmarks,
- documentation,
- compatibility promises,
- and evidence.
