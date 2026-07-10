# Intelligence Amplification Layer

## Purpose

The intelligence amplification layer improves Codex-mediated SPST-UEA operation without requiring an OpenAI API key.

It does not make the underlying model larger. Instead, it increases effective task performance by adding structured reasoning scaffolds around each turn.

## Architecture

Each chat turn can be enriched with:

1. **Intent classification** - identify whether the turn asks for implementation, analysis, operation, or design.
2. **Context retrieval** - reuse recent prompts and audit state from the persisted session.
3. **Task decomposition** - split the prompt into concrete work units.
4. **Reflection prompts** - generate checks for uncertainty, risks, and missing evidence.
5. **Governance focus** - preserve no-key, Codex-mediated boundaries unless the user explicitly asks otherwise.
6. **Amplification score** - estimate how much structure the layer adds to the raw prompt.

## Expected Effect

Compared with normal Codex operation, this layer should make SPST-UEA turns more:

- consistent,
- inspectable,
- state-aware,
- risk-sensitive,
- and action-oriented.

## Boundary

This is not direct GPT API execution. It is a local/Codex-mediated augmentation layer that prepares better context and control signals for Codex to use when answering.
