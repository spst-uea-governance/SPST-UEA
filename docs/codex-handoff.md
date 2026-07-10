# Codex Handoff Guide

## Purpose

This document tells an implementation agent how to work on SPST-UEA without altering the frozen architecture.

## Authority Order

When instructions conflict, use this order:

1. accepted RFCs,
2. normative specification,
3. architecture documents,
4. tests,
5. issue description,
6. implementation convenience.

## Implementation Rules

Codex MUST:

- preserve module boundaries,
- keep model providers behind adapters,
- keep state commits separate from inference output,
- add tests for normative behavior,
- avoid hidden global state,
- emit structured logs,
- document public interfaces,
- preserve backward compatibility where specified,
- and record architectural deviations.

Codex MUST NOT:

- invent new core theory,
- commit identity changes directly from model output,
- bypass governance for convenience,
- couple runtime logic to one model provider,
- or mark placeholder behavior as production-ready.

## Initial Implementation Sequence

1. Define typed state schemas.
2. Define event types.
3. Define module protocols.
4. Implement in-memory state store.
5. Implement deterministic state transition coordinator.
6. Implement model adapter protocol.
7. Implement memory adapter protocol.
8. Implement governance decision interface.
9. Implement runtime lifecycle.
10. Add conformance tests.

## Required Outputs Per Task

Each implementation task SHOULD produce:

- code,
- tests,
- documentation,
- migration impact,
- and a short design note.

## Definition of Done

A task is complete only when:

- code passes formatting and type checks,
- tests pass,
- public APIs are documented,
- no architecture rule is violated,
- and the change is traceable to a requirement or RFC.
