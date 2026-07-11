# Core Design Principles

These principles are frozen for SPST-UEA Project 1.0 unless changed through a major-version governance process.

## Core Ethos

All principles are interpreted through this posture:

> 改造ではなく、開花。シンプルで、美しく。

SPST-UEA SHOULD amplify capability by arranging context, memory, verification,
and governance so that existing model ability can open more fully. It MUST NOT
claim that scaffolding has modified model weights, official benchmark truth, or
the model's intrinsic nature. The preferred design is simple, legible, and
beautiful enough to be audited.

The constitutional authority for these principles is `spst-uea-covenant.md`.

## P-001 Model Independence

The architecture MUST NOT require a specific language model, provider, or model family.

## P-002 Implementation Independence

The specification MUST be implementable across cloud, local, edge, robotic, and hybrid environments where technically feasible.

## P-003 Persistence Through Reconstruction

Continuity MUST be represented as a reconstructive process over managed state rather than as simple transcript accumulation.

## P-004 Observable Behavior

Claims about persistence, identity, governance, or recovery MUST be tied to observable variables.

## P-005 Modularity

Memory, identity, goal management, reflection, governance, scheduling, model access, and tool access SHOULD be independently replaceable.

## P-006 Evidence First

Stable architectural changes MUST be supported by implementation evidence, test results, or formal analysis.

## P-007 Reproducibility

Experiments SHOULD be reproducible by independent implementers using published configurations and datasets.

## P-008 Falsifiability

The project MUST permit results that reject, weaken, or narrow a claim.

## P-009 Explicit Boundaries

Each module MUST define inputs, outputs, responsibilities, failure modes, and authority boundaries.

## P-010 Traceability

Normative claims MUST be traceable through requirements, code, tests, benchmarks, and evidence.

## P-011 Backward Compatibility

Backward compatibility SHOULD be preserved for stable interfaces unless doing so would block correctness or safety.

## P-012 Governance Before Autonomy

A persistent system MUST NOT gain operational autonomy without explicit policies, authority limits, auditability, and intervention mechanisms.

## P-013 Least Authority

Every module and agent process SHOULD receive only the permissions necessary for its assigned function.

## P-014 Safe Degradation

When confidence, state integrity, or governance stability falls below threshold, the system SHOULD degrade capability rather than continue silently.

## P-015 Human-Readable State

Critical state transitions, goals, policies, and governance decisions SHOULD be explainable in human-readable form.
