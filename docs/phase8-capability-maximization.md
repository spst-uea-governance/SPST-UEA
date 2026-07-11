# Phase 8: Capability Maximization Layer

SPST-UEA can improve realized task performance by wrapping a model with
provider-neutral scaffolding. It does not change model weights, official normal
benchmark truth, or the score that a benchmark provider reports for an
unwrapped model.

The phase follows the core ethos: **改造ではなく、開花。シンプルで、美しく。**
It aims to let existing capability express itself more reliably, not to claim
that the underlying model has been altered.

## Purpose

The capability maximization layer turns a raw prompt into an execution contract:

- target domain classification,
- strategy portfolio selection,
- constraint extraction,
- decomposition,
- verifier loop planning,
- self-consistency or alternate-derivation checks,
- retrieval grounding when local memory is relevant,
- and a paired baseline-vs-maximized evidence requirement.

## Boundary

The layer may raise system-conditioned performance for the same model by
improving how the task is framed, checked, and retried. It must not:

- claim that the official normal-model benchmark score changed,
- leak hidden benchmark answers,
- depend on a single LLM vendor,
- require an API key in Codex-mediated local mode,
- or bypass governance and provenance.

## Runtime Integration

`CapabilityMaximizer` runs during `TransitionEngine._infer`, after
`IntelligenceAmplifier` and before the model adapter call. The
`StateTransitionPipeline` passes the compact maximization plan through the
vendor-neutral `ModelAdapter` context. Runtime audit records store summaries
such as mode and strategy count instead of recursively embedding full context.

## Evidence Standard

Any uplift claim should be backed by a paired evaluation:

- same model,
- same tasks,
- normal adapter baseline,
- maximized adapter run,
- identical scoring rubric,
- and a delta report that separates latency/cost overhead from accuracy gain.
