# Runtime Release Identity

## Position

The persistent Cockpit listener must belong to the exact Repository state from
which its Python process was started. Port ownership, `/health`, API schemas,
and a responsive Project Context child are insufficient: an older process can
continue serving after HEAD, the index, tracked bytes, or non-ignored untracked
bytes change on disk.

`RuntimeReleaseIdentity` captures the canonical path-free Repository identity
once for each web-process generation. `GET /api/runtime-identity` recaptures the
current Repository through the same byte-level implementation and reports:

- `ready` only when startup and current identities are identical;
- `stale` when current bytes are readable but no longer match;
- `blocked` when either startup or current identity cannot be captured safely.

The response includes HEAD, worktree and identity digests, dirty state, and
match fields. It never returns the repository path and never writes persistent
state.

## Launcher enforcement

`runtime/run_spst_web_8767.ps1` independently captures the requested
Repository identity before accepting a listener. It then requires all of the
following:

1. the release-identity endpoint exists and has the expected schema;
2. the process reports `ready` and `current_match: true`;
3. the process startup identity equals the launcher's requested identity;
4. the process current identity equals the same requested identity.

This rejects an old binary, a process from another worktree, and a process
whose source changed after startup. The launcher does not terminate an unknown
or stale listener automatically; use the explicit stop path before starting a
new generation.

## Request admission enforcement

Release identity is enforced after startup as well as by the launcher. If the
current HEAD, index, tracked bytes, or non-ignored untracked bytes no longer
match the process startup identity, `GET /api/run` and capability-increasing
`POST` operations return HTTP 503 with `spst-runtime-release-admission-v1`.
Rejection occurs before chat dispatch, Cockpit request handling, or
request-body processing.

The static UI, `/health`, `/api/runtime-identity`, and read-only GET surfaces
remain available for liveness and diagnosis. They do not authorize execution
from the stale process. Restart the Runtime from the exact current Repository
state to restore executable admission.

The exact `POST /api/project-context/shutdown` path remains available as the
sole stale-process recovery exception so the launcher can close the managed
read-only child before replacing the parent. Near-match paths, Project Context
queries, dispatch, approvals, and every other POST remain gated.

Concurrent admission checks use single-flight capture. Requests that overlap
one complete Repository capture receive independent copies of that same
observation; a request arriving after completion always starts a new capture.
There is no TTL cache or metadata-only shortcut, so concurrency reduction does
not create a configured stale-acceptance interval. Unexpected comparison errors
are converted to a path-free `blocked` result.

## Boundaries

The identity is a local Git and filesystem integrity observation, not a code
signature, trusted build attestation, authenticated host identity, or proof of
which instructions the Python interpreter executed. Git-ignored runtime state
is intentionally outside the worktree digest. External coordination is still
required to establish hosted CI, signed releases, artifact provenance, and
workflow-file identity.
