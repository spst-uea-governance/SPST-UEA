# Phase 2 IA/LTM Pipeline Fusion

Phase 2 fuses the intelligence amplification layer and long-term memory with
the deterministic seven-step transition pipeline.

## Intelligence Amplification

`TransitionEngine._infer` now starts `IntelligenceAmplifier` automatically. It
adds deterministic intent classification, task decomposition, retrieved memory
context, reflection checks, governance focus, and an amplification score to
`candidate.metadata["intelligence_amplification"]`.

`TransitionEngine._reflect` records `candidate.metadata["phase2_reflection"]`
with an ESI-style score and deterministic corrective flags. Low-scoring turns
are marked as `corrected` rather than silently accepted.

## Long-Term Memory

`MemoryProvider` now bridges SQLite-backed key/value memory with
`LongTermMemoryStore`. It supports:

- direct long-term memory seeding via `remember_long_term`
- associative retrieval via `search_long_term`
- duplicate-safe provider search
- automatic distillation from committed action records into long-term memory

The runtime remains API-key-free by default and keeps inference behind the
Codex-mediated model provider boundary.
