# Implementation Notes

## Codex Runtime Enablement

This pass makes the reference runtime executable in Codex without requiring network access or external services.

## What Changed

- Added a deterministic transition pipeline that records the RFC-0003 phase trace.
- Added governance checks that reject direct, unverified model-origin identity commits.
- Added SQLite WAL-backed key/value persistence for state reconstruction tests.
- Added a local CLI entry point via `python -m spst_runtime`.
- Added an OpenAI Responses API model adapter behind the SPST `ModelAdapter` boundary.
- Added an API-key-free local/Codex-mediated adapter for Codex runtime operation.
- Enabled conformance tests for transition ordering, governance rejection, and persistence recovery.
- Added lifecycle scheduling, deep-copy checkpoints, integrity-checked migration,
  public local SDK, and reproducible local benchmark support.

## Boundary Decisions

- Model inference remains behind adapter interfaces; provider-specific OpenAI code is isolated in `spst_runtime.providers.openai_model_adapter`.
- API-key-free operation uses `spst_runtime.providers.local_codex_adapter`.
- Reflection and governance remain separate from state commit.
- Persistence stores structured dictionaries and does not directly mutate subject identity.
- Documentation remains explicit about non-sentience and the API-key-free local
  runtime boundary; no theoretical claim is represented as executable behavior.
