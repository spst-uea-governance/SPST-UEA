# SPST-UEA Agent Guide

## Scope

These instructions apply to the entire repository.

## Project Shape

- The runnable Python package lives in `runtime/`.
- Treat `docs/codex-handoff.md` as the primary implementation guide.
- Preserve RFC and architecture boundaries when changing runtime behavior.

## Common Commands

Run commands from `runtime/` unless noted otherwise:

- `python -m pytest -q`
- `python -m spst_runtime.chat_bridge "<prompt>"`
- `python -m pip install -e ".[dev]"`
- `python -m ruff check .`
- `python -m mypy spst_runtime`

## Working Rules

- Treat SPST-UEA as always-on in this Codex thread.
- For SPST-UEA-related user requests, run `python -m spst_runtime.chat_bridge "<prompt>"` from `runtime/` before answering when execution is useful.
- Do not couple runtime logic to a specific model provider.
- Keep inference, reflection, governance, and state commit concerns separate.
- Add or update tests when implementing normative behavior.
- Do not mark TODO placeholder behavior as production-ready.
- Avoid committing generated files such as `__pycache__/`, `.pytest_cache/`, or `*.pyc`.
- For API-key-free chat operation, route user prompts through `spst_runtime.chat_bridge`.
