# Phase 4 Web Cockpit API

Phase 4 connects the local web server to live `RuntimeOrchestrator` and
`RuntimeLoop` instances.

## Endpoints

- `GET /api/status` returns runtime status, tick count, ESI score, last trace,
  API-key requirement status, and memory statistics.
- `GET /api/goals` returns current goal records and statuses.
- `POST /api/dispatch` accepts local prompt and optional goal payloads, runs the
  seven-step pipeline, and returns trace, governance, goal, and amplification
  metadata.
- `GET /api/memory/search?q=...` searches the local memory provider and long-term
  memory bridge.

## Cockpit

The root HTML dashboard now polls cockpit status and goals, displays pipeline
steps, and allows local prompt dispatch without requiring an external API key.

Importing `spst_runtime.web` does not open or create a cockpit database. The
process-default `CockpitRuntime` is initialized lazily on the first request
that does not supply an explicit runtime. Its database remains
`spst_cockpit.db` by default and can be isolated with
`SPST_COCKPIT_DB_PATH`; an explicit constructor `db_path` takes precedence.

The API remains fully local and Codex-mediated by default.
