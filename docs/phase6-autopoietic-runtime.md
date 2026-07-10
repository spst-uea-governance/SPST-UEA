# Phase 6 Autopoietic Runtime

Phase 6 implements a life-like operational layer without claiming biological
life, consciousness, or sentience.

## Self-Model

`AutopoiesisEngine` records a bounded self-model:

- identity: `SPST-UEA local autonomous runtime`
- boundary: `not_biological_not_sentient`
- mode: `codex-chat-mediated`
- API key requirement: `false`

## Homeostasis

The engine evaluates viability from governance authorization, reflection state,
self-repair state, and trace integrity. Stable runs record
`metadata["autopoiesis"]["homeostasis"]["status"] == "stable"`.

## Memory Settlement

`RuntimeOrchestrator` stores an `autopoiesis:<version>` memory record on each
action cycle so self-model, drives, and homeostasis become inspectable local
runtime state.

This phase makes SPST-UEA more self-maintaining and observable; it does not make
the system a living organism or an independently conscious entity.
