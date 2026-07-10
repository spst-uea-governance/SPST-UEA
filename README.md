# SPST-UEA

SPST-UEA is a specification-first runtime project. The runnable reference implementation is in `runtime/`, with architecture and RFC context in `docs/` and `rfc/`.

## Quick Start

```bash
cd runtime
python -m pytest -q
```

For local development:

```bash
cd runtime
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
python -m mypy spst_runtime
```

## Repository Map

- `runtime/` - Python reference runtime package and tests.
- `docs/` - architecture, governance, runtime model, and Codex handoff guidance.
- `rfc/` - normative RFC drafts and templates.
- `benchmark/` - reproducible RFC-0005 local benchmark runner.
- `sdk/` - typed, API-key-free Python cockpit SDK.

## Codex Notes

Before changing behavior, read `docs/codex-handoff.md`. Implementation work should preserve module boundaries, keep providers behind adapters, and avoid bypassing governance for convenience.

## Current Runtime Status

The runtime can be executed locally from `runtime/`:

```bash
python -m spst_runtime --steps 3 --event codex_run
```

Implementation notes for the Codex enablement pass are recorded in `docs/implementation-notes.md`.
