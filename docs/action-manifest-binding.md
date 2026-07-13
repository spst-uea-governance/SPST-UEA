# Receipt-Bound Action Manifests

## Purpose

Routing receipts prove that a task contract traversed the SPST-UEA chat bridge,
but they do not, by themselves, prove that later tool calls were governed or
executed. The Action Manifest boundary adds a child evidence chain for supported
local actions without pretending that SPST-UEA can observe every Codex tool.

```text
verified routing receipt
  -> immutable action manifest
  -> derived R0 / R1 / R2 risk
  -> governance decision
  -> optional append-only human approval
  -> fixed, shell-free runtime execution
  -> compact execution evidence
  -> read-only verification from receipt or action id
```

The manifest, approval, and evidence records are stored in the same SQLite HMAC
provenance chain as the parent routing receipt. Each child record binds the
parent receipt or preceding record by identifier, record hash, chain hash, and
provenance sequence.

## Runtime-Executed Profiles

Only the existing fixed verification profiles can produce
`execution_verified: true`:

| Profile | Risk | Notes |
|---|---|---|
| `git_head`, `git_status`, `git_diff_check` | `R0` | Derived read-only inspection |
| `pytest`, `ruff`, `mypy` | `R1` | Local quality execution; tools may create reversible caches |

Run one profile through the bound executor:

```powershell
python -m spst_runtime.action_bridge execute-profile `
  --receipt-id <receipt-id> `
  --profile git_status `
  --workspace-root ..
```

The persisted evidence contains the profile, return code, duration, output
digest, command digest, workspace digest, executor identity, and provenance
bindings. It does not persist raw stdout, stderr, arbitrary command text, or the
workspace path. A failed command can have a verified execution binding while
`successful` remains false.

## External Codex Tools and HITL

SPST-UEA cannot intercept Codex App tools such as `apply_patch`, arbitrary
shell calls, browser actions, or connector calls. Such an operation can be
prepared as a manifest using only hashes:

```powershell
python -m spst_runtime.action_bridge prepare-external `
  --receipt-id <receipt-id> `
  --operation deploy `
  --tool-name codex.shell_command `
  --arguments-sha256 <canonical-arguments-digest> `
  --workspace-root ..
```

Known high-impact operations and all unknown operations are `R2` and remain
`pending_human_approval`. An explicit approval or rejection is appended rather
than rewriting the manifest:

```powershell
python -m spst_runtime.action_bridge approve `
  --action-id <action-id> `
  --decision approve `
  --actor <local-human-label>
```

The actor label is stored only as a digest and is an attestation, not an
authenticated human identity. Approval does not make an unobserved external
tool execution verifiable. Its status remains
`external_execution_unobserved`, and it is excluded from verified execution
counts.

## Read-Only Verification

Verify one action or list child actions for a receipt without changing the
database:

```powershell
python -m spst_runtime.action_bridge verify --action-id <action-id>
python -m spst_runtime.action_bridge receipt-status --receipt-id <receipt-id>
python -m spst_runtime.chat_bridge --verify-receipt <receipt-id>
```

Receipt verification now includes an `actions` summary. The summary counts
only valid manifest bindings and independently distinguishes prepared,
HITL-pending, externally unobserved, execution-verified, and successful
actions. `global_codex_tool_coverage` remains `null` because the runtime has no
denominator for Codex tool calls that bypass the action bridge.

## Honesty and Security Boundaries

- A manifest proves an immutable planned-action binding, not execution.
- `execution_verified: true` requires the runtime fixed-profile executor and a
  valid compact evidence record after the manifest.
- External operation names and argument digests are caller attestations. They
  never become verified execution evidence.
- An HMAC approval record proves local record integrity, not the real-world
  identity of the human actor.
- The fixed executor uses no shell and has bounded profiles, but it is not an
  OS-level sandbox. Test code can still have side effects unless separately
  contained by the host environment.
- Action binding does not establish answer-quality improvement or model changes.
