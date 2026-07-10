# Phase 3 Autonomous Goal Loop

Phase 3 turns the runtime loop into a local, API-key-free autonomous goal loop.

## Goal Engine

`GoalEngine` evaluates pending goals and deterministically decomposes the active
goal into `next_actions`. It uses the existing intelligence amplification
metadata when available, but does not call an external model provider.

## Goal Manager

`GoalManager` normalizes goal state, advances goals from `pending` to
`in_progress` to `completed`, and persists each goal through `SQLiteRepository`
using `runtime:goal:<goal_id>` keys.

## Runtime Loop

`RuntimeLoop.step()` now creates a `system_tick` event when no explicit event is
provided. Each tick dispatches through `RuntimeOrchestrator`, which retrieves
memory, runs the seven-step pipeline, publishes an action event, persists audit
state, stores memory, and runs deterministic maintenance.

The default path remains local and Codex-mediated; no API key is required.
