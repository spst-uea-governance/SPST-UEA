# Receipt-Bound Action Manifests

## Purpose

Routing receipts prove that a task contract traversed the SPST-UEA chat bridge,
but they do not, by themselves, prove that later tool calls were governed or
executed. The Action Manifest boundary adds a child evidence chain for supported
local actions without pretending that SPST-UEA can observe every Codex tool.

```text
verified routing receipt
  -> receipt repository identity
  -> immutable action manifest
  -> before_repository_identity
  -> derived R0 / R1 / R2 risk
  -> governance decision
  -> optional append-only human approval
  -> fixed, shell-free runtime execution
  -> compact execution evidence + after_repository_identity
  -> read-only verification from receipt or action id
```

The manifest, approval, and evidence records are stored in the same SQLite HMAC
provenance chain as the parent routing receipt. Each child record binds the
parent receipt or preceding record by identifier, record hash, chain hash, and
provenance sequence.

## Repository Transition Contract

New manifests require a repository-bound v3 parent receipt. At prepare time,
the runtime captures `before_repository_identity` and rejects the manifest if
it differs from the parent receipt identity. Immediately before fixed-profile
execution, the runtime recaptures the repository and refuses to run if the
state changed after manifest preparation.

The immutable manifest cannot truthfully contain a future after-state. It
therefore binds `after_repository_identity_source: action_evidence`; the actual
`after_repository_identity` is stored in the HMAC-chained evidence record that
binds the action id and manifest record hash. Read-only verification assembles:

```text
parent Receipt identity == Manifest before identity
Manifest before identity == immediate pre-execution identity
Manifest -> Action Evidence -> after identity
```

All current fixed profiles have `expected_effect: preserve`. A changed
after-state remains execution evidence, but reports
`unexpected_repository_mutation` and cannot be successful. Failure to capture
the after-state is recorded as `after_repository_identity_unresolved`, not
silently treated as success. Legacy manifests without this contract remain
verifiable as `legacy_unbound`, but new actions cannot be prepared from v1/v2
receipts that lack repository identity.

## Runtime-Executed Profiles

Only the existing fixed verification profiles can produce
`execution_verified: true`:

| Profile | Risk | Profile-owned execution root | Notes |
|---|---|---|---|
| `git_head`, `git_status`, `git_diff_check` | `R0` | repository root | Derived read-only inspection |
| `pytest`, `ruff`, `mypy` | `R1` | `runtime/` | Local quality execution; tools may create reversible caches |

Run one profile through the bound executor:

```powershell
python -m spst_runtime.action_bridge execute-profile `
  --receipt-id <receipt-id> `
  --profile git_status `
  --repository-root ..
```

The caller supplies only the repository boundary. The immutable profile
definition selects the execution root: Git profiles run at the repository root,
while `pytest`, `ruff`, and `mypy` run from `runtime/`. The bridge rejects a
runtime directory or unrelated directory presented as the repository root with
`spst-action-bridge-error-v1` / `repository_root_invalid`, before creating a
manifest.

The persisted evidence contains the profile contract version, relative
execution root, repository and resolved-root digests, return code, duration,
output digest, command digest, executor identity, and provenance bindings. It
does not persist raw stdout, stderr, arbitrary command text, or the absolute
repository path. A failed command can have a verified execution binding while
`successful` remains false. Legacy manifests remain verifiable; an unexecuted
legacy fixed profile can run only when its stored workspace digest matches the
root resolved by the current profile definition.

## External Codex Tools and HITL

SPST-UEA cannot intercept Codex App tools such as `apply_patch`, arbitrary
shell calls, browser actions, or connector calls. Such an operation can be
prepared as a manifest using only hashes. Its workspace must be the same Git
top level bound by the parent receipt:

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
counts. Its before-state is bound, but no after-state is fabricated.

After the external tool returns, the caller may append a bounded result
attestation. For R2 actions, the approval record must already be present and
approved:

```powershell
python -m spst_runtime.action_bridge attest-external `
  --action-id <action-id> `
  --result-status completed `
  --returncode 0 `
  --result-evidence-sha256 <canonical-result-bundle-digest> `
  --workspace-root ..
```

The runtime validates the workspace binding, captures the repository identity
at attestation time, and appends the caller-supplied status, return code, and
result digest to the HMAC provenance chain. The digest can represent a
canonical bundle such as a Git object identifier plus command-output digest,
but the runtime cannot prove that the caller constructed it truthfully.

The resulting status is `external_execution_attested_unverified`, with
`external_attestation.integrity_verified: true` and
`source_authenticated: false`. `execution_verified`, `verified`, and
`successful` remain false. The recorded after-state is an attestation-time
snapshot; `causal_link_verified` and `execution_window_verified` remain false,
so it does not prove that the external action caused the observed change or
that no transient changes occurred. Duplicate attestations, workspace
mismatches, inconsistent result/return-code pairs, missing R2 approval, and
tampered records are rejected.

## Read-Only Verification

Verify one action or list child actions for a receipt without changing the
database:

```powershell
python -m spst_runtime.action_bridge verify --action-id <action-id>
python -m spst_runtime.action_bridge receipt-status --receipt-id <receipt-id>
python -m spst_runtime.chat_bridge --verify-receipt <receipt-id> --repository-root ..
```

Receipt verification now includes an `actions` summary. The summary counts
only valid manifest bindings and independently distinguishes prepared,
HITL-pending, externally unobserved, execution-verified, and successful
actions. `global_codex_tool_coverage` remains `null` because the runtime has no
denominator for Codex tool calls that bypass the action bridge.
The summary separately counts transition-bound, after-state-verified,
preserved, changed, attested, and unresolved actions. External attestations
have separate `external_attested_actions` and
`repository_transition_attested_actions` counters and remain unresolved for
verified-transition purposes.

## Honesty and Security Boundaries

- A manifest proves an immutable planned-action binding, not execution.
- A v3 parent receipt, Manifest before-state, and Evidence after-state form a
  verified sequence for runtime fixed profiles. They do not observe transient
  changes that occur and are fully reverted between captures.
- Ignored files and OS metadata excluded by the repository identity contract
  are also outside transition coverage.
- `execution_verified: true` requires the runtime fixed-profile executor and a
  valid compact evidence record after the manifest.
- External operation names and argument digests are caller attestations. They
  never become verified execution evidence.
- External result digests and attestation-time repository snapshots add
  tamper-evident supplemental evidence, but they do not authenticate the
  caller, observe the tool invocation, or establish a causal transition.
- An HMAC approval record proves local record integrity, not the real-world
  identity of the human actor.
- The fixed executor uses no shell and has bounded profiles, but it is not an
  OS-level sandbox. Test code can still have side effects unless separately
  contained by the host environment.
- Action binding does not establish answer-quality improvement or model changes.
