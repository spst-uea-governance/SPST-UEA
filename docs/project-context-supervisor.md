# Project Context Supervisor

## Position

The persistent Cockpit Runtime is the formal owner of the ARCH-18 Project
Context JSONL process. It starts the child only when queried, retains the
child across requests, and shuts it down with the Cockpit process. This is a
read-only evidence path; it does not promote Project history to trusted
instructions and does not prove provider uptake or model-quality improvement.

The Project Context worker is intentionally separate from the provider
recovery supervisor. Provider recovery uses authenticated leases and
program-bound recovery authority. Project Context retrieval has no authority
to send provider requests or mutate provider state.

## Formal HTTP paths

The Windows persistent launcher starts the Cockpit Runtime on port 8767 and
warms the child when a reviewed corpus exists:

```powershell
cd runtime
.\run_spst_web_8767.ps1
```

The managed paths are:

- `GET /api/project-context/status` returns process and last-observation state
  without starting the child or hashing/writing the corpus.
- `POST /api/project-context/query` accepts the same JSON object as one JSONL
  request, serializes it through the managed child, and returns the worker
  response plus its canonical SHA-256.
- `POST /api/project-context/shutdown` gracefully closes the child without
  stopping the Cockpit process.

The launcher places Cockpit state in the ignored
`.spst/runtime/spst_cockpit.db`. It does not use or create the historical DBs
under `runtime/`.

Before accepting either a new or existing listener, the launcher captures the
current path-free Repository identity and compares it with the process startup
identity through `GET /api/runtime-identity`. A changed HEAD, index, tracked
file, or non-ignored untracked file makes the process `stale`; a listener from
another worktree is rejected. This endpoint is read-only and does not
initialize the Cockpit database.

## Recovery and exact-once boundary

Requests are serialized through one child. If the child is already dead before
a request, the supervisor may create a new generation and send the new request
once. If failure occurs after a request may have been written, the supervisor
returns `project_context_request_outcome_unresolved`, discards the child, and
does not retry that request. The next independently submitted request may start
a new generation.

This distinction prevents a transport ambiguity from being presented as a
successful exact-once execution. The status response exposes generation,
restart, request, and failure counters and always records
`automatic_inflight_retry: false`.

## Integrity and cache boundary

The child retains ARCH-18's bounded term cache. It rereads and hashes the
complete corpus bytes before every query. Byte changes force full
reverification; digest mismatch, malformed content, and expired freshness
remain fail-closed. The lightweight status path deliberately reports
`integrity_current: null` with `verified_on_query_not_status`, because claiming
current integrity would require the expensive read that the status path is
designed to avoid.

Historical Project content remains `untrusted_evidence_only`. It cannot
override current user instructions, repository policy, or verified execution
evidence.

## Shutdown

Use the paired launcher script:

```powershell
cd runtime
.\stop_spst_web_8767.ps1
```

It first asks the Project Context supervisor to close its child, then stops the
Cockpit listener. The web server also closes the child from its `finally` path
for controlled process shutdown.
