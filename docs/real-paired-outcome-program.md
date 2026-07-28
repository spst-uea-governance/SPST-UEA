# ARCH-06: Real Paired Outcome Program

ARCH-06 turns the existing one-shot live paired evaluator into a durable study
program. It pre-registers the complete cohort and condition schedule before any
provider call, applies an explicit network/cost authority, binds the resulting
provider-observed PBIND records to the program, and revalidates the blind human
review and uncertainty result on every read.

The word `Real` identifies the target workload class. It is not a shortcut around
evidence. The runtime continues to distinguish a local in-process fixture from an
observed remote-provider response, and neither provider identity nor workload
provenance becomes cryptographically authenticated merely because a caller labels
it real.

## Pre-registered contract

`RealPairedOutcomeProgram.register()` accepts only:

- at least eight unique, active, consented holdout tasks with exact JSON rubrics;
- one valid, human-supported context intervention;
- distinct baseline and treatment candidate identifiers;
- an adapter-declared provider, model, observation source, execution environment,
  and billing class;
- `test_fixture` or `real_user_workload` as an explicitly self-attested workload
  class;
- an exact adapter-invocation cap and separate authorization flags for external and
  paid calls.

Registration freezes:

- selected task and rubric-contract digests;
- provider/model and observation-source contract;
- context, source-review, and intervention bindings;
- balanced per-task condition order and nonce commitment;
- the target sample count, no-optional-stopping rule, fixed 95% Hoeffding bound,
  blind review requirement, and automatic-promotion prohibition.

The public projection exposes the schedule commitment and balance, not per-task
condition order or the randomization nonce. The full local database remains
operator-accessible, so reviewer blindness is still a human attestation rather
than a cryptographic fact.

## Cost and authority gate

The execution authority uses `spst-provider-execution-authority-v1`:

```json
{
  "schema": "spst-provider-execution-authority-v1",
  "external_provider_calls_authorized": false,
  "paid_provider_calls_authorized": false,
  "maximum_adapter_invocations": 32
}
```

For eight retained pairs, the current two-arm shadow implementation performs 32
adapter `infer()` invocations. Registration rejects a lower cap. `preflight()`
is read-only, makes zero adapter invocations, and blocks an external, paid, or
unknown-billing adapter unless the corresponding authority was explicitly
registered.

The runtime can recompute its adapter-invocation count from the selected Producer
reports. It cannot observe retries or additional transport requests hidden inside
an adapter. Therefore `provider_transport_attempt_count_verified` remains false;
the registered cap must not be presented as a cryptographically verified billing
limit for an opaque remote adapter.

No paid provider was invoked while implementing or verifying ARCH-06.

## API-key-free Codex CLI provider path

`CodexCliAdapter` adds a bounded external-provider path for a locally installed
Codex CLI that is already signed in with ChatGPT. It invokes `codex exec` with
`--ephemeral`, ignores user config and exec-policy rules, uses a read-only
sandbox, and requires JSONL plus schema-constrained structured output. The
adapter removes `OPENAI_API_KEY` and `CODEX_API_KEY` from the child environment
and checks `codex login status` immediately before every inference. Any login
mode other than the exact ChatGPT status fails before the model call.

The model output must echo the exact SPST provider-request binding inside the
schema-constrained response. The acknowledgement envelope and bounded task
artifact are distinct fields. When the registered task supplies a flat,
primitive-valued JSON quality contract, the adapter derives a closed, typed
artifact schema from that contract, requires the complete object, and
canonicalizes it back to task text. It rejects a bare extracted value, a wrong
root type, an additional field, or a mismatched property type rather than
letting acknowledgement framing change the artifact being scored. The adapter
records the returned CLI thread ID, turn-completion event, token usage, artifact
digest, and acknowledgement as `codex_cli_jsonl_model_echo`. Missing, malformed,
failed, mismatched, or replayed events are rejected. This proves a fresh
observable CLI request/response round trip; it does not cryptographically
authenticate OpenAI, the account, model weights, or provider identity.

This transport contract prevents an outer `answer` field from ambiguously
collapsing an inner task object such as `{"answer":"unknown"}` to the bare
string `unknown`. It does not repair historical responses or prove that the
model selected a semantically correct value.

ChatGPT login avoids Platform API-key billing. Before every inference, the adapter
also queries the local Codex app-server `account/read` and
`account/rateLimits/read` surfaces. It fails closed unless the account is a
ChatGPT account, the plan identities match, the included Codex limit remains below
the configured threshold, the limit is not reported as reached, and credits are
explicitly reported as absent, finite, and zero balance. The projected decision is
bound inside the canonical provider observation, so removing or altering it makes
the Producer Evidence ineligible.

This is a local, unauthenticated observation of the installed Codex client state;
it is not a cryptographic billing attestation and cannot rule out every
server-side account-policy change. Its billing class therefore remains
`chatgpt_plan_usage`, not `no_charge`. A Program must use
`spst-provider-execution-authority-v2` and explicitly set
`chatgpt_plan_usage_authorized: true` in addition to external-call authority.
The v1 authority and a v2 false/missing authorization both fail before the first
model call. This authorization permits included-plan quota consumption. The
per-call guard prevents execution when spendable credits are present or the
reported included limit is unavailable, but it is not cryptographic proof that
an invocation was monetarily free.

```json
{
  "schema": "spst-provider-execution-authority-v2",
  "external_provider_calls_authorized": true,
  "paid_provider_calls_authorized": false,
  "chatgpt_plan_usage_authorized": true,
  "maximum_adapter_invocations": 32
}
```

A reviewed `real_user_workload` Program using this source may reach
`reviewed_observed_real_workload`. `real_paired_outcome_cryptographically_verified`
remains false because workload provenance, reviewer independence, provider
identity, and billing state are still not externally authenticated.

## Execution and recomputation

`execute()` accepts the exact registered intervention only. Before the first
adapter call, `LiveContextPairedEvaluator` writes a
`spst-live-context-plan-record-v1`. The program later proves this order from the
SQLite provenance sequence:

```text
program registration < live execution plan < first producer report
```

The stored Program execution does not become authoritative merely by carrying a
digest. Every `get()` recomputes:

- Program and live-plan digests;
- live execution-record digest and Program binding;
- selected Producer reports and PBIND provider-observation sources, Provider
  names, and model versions;
- adapter-attempt count;
- registration/plan/producer provenance ordering;
- paired scoring artifact, source references, uncertainty, and human review via
  `PairedQualityEvidenceLedger`.

Post-hoc replacement of `in_process_provider_echo` with
`https_response_metadata_echo`, even when the modified record is saved into a
new valid local provenance entry, fails the Producer-record recomputation.

## Evidence classes

- `mechanism_validation`: every selected observation came from
  `in_process_provider_echo` or `local_process_provider_echo`. After review, the status is
  `mechanism_validation_only`; it is never counted as real outcome evidence.
- `remote_transport_observed`: every selected observation came from
  `https_response_metadata_echo` and matched the pre-registered provider
  contract. This may support a bounded observed result for a self-attested real
  workload after blind review, but provider identity remains unauthenticated.
- mixed, missing, relabelled, replayed, or mismatched sources: blocked.

`observed_real_workload_outcome` requires all of the following:

- pre-registered `real_user_workload`;
- remote response-metadata observation for every selected arm;
- exact Provider/model/task/intervention binding;
- at least eight valid pairs;
- an accepted digest-bound blind review;
- a revalidated paired measurement.

Even then, `real_paired_outcome_cryptographically_verified` remains false because
the current provider observation and workload declarations are not externally
authenticated.

## Read-only inspection and review

```powershell
python -m spst_runtime.real_paired_outcome_bridge get `
  --evaluation-db <isolated-evaluation.db> `
  --program-id <real-paired-outcome-program-id>

python -m spst_runtime.real_paired_outcome_bridge status `
  --evaluation-db <isolated-evaluation.db>
```

Both commands open SQLite in immutable read-only mode. After inspecting only the
blind surface, append the existing Phase 16 review contract:

```powershell
python -m spst_runtime.real_paired_outcome_bridge review `
  --evaluation-db <isolated-evaluation.db> `
  --program-id <real-paired-outcome-program-id> `
  --reviewer-id <independent-human-reviewer> `
  --decision accepted `
  --scoring-artifact-digest <artifact-digest> `
  --blind-review-surface-sha256 <surface-digest> `
  --arm-mapping-not-accessed `
  --reviewer-independence-attested
```

Registration and provider execution remain Python API operations because the
provider adapter is an injected capability boundary, not a caller-selected shell
or arbitrary network command.

## Honesty boundary

ARCH-06 does not establish:

- model-weight change;
- general GPT or Codex quality improvement;
- causal semantic use of context;
- authenticated provider, reviewer, or workload identity;
- the number of transport retries hidden inside a remote adapter;
- organizational reviewer independence;
- complete Codex tool-call coverage;
- automatic adoption or promotion.

The no-charge conformance adapter proves the Program lifecycle and adversarial
recomputation. It is not a real GPT outcome. A production study still requires a
compatible remote adapter, an explicitly approved no-charge or budgeted execution
authority, an actual consented workload corpus, and an actual blind human review.

## ARCH-07 operational hardening

ARCH-07 adds a single durable execution claim, compare-and-set state
transitions, and fail-closed interruption handling. See
`docs/operational-hardening.md`. A `recovery_required` result is deliberately
not auto-retried because the provider transport attempt count remains unknown.

## ARCH-08 provider transport recovery

For adapters that explicitly support provider-side idempotency and result
reconciliation, ARCH-08 binds every call to a unique Program/Attempt/ordinal
key and requires an exact provider acknowledgement receipt. An interrupted run
can resume only through `RealPairedOutcomeProgram.recover()` with a digest-bound
recovery authority. Submitted calls are reconciled first; completed calls are
replayed without new inference, while unresolved or altered results remain
`recovery_required`. See `docs/provider-transport-recovery.md`.

## ARCH-09 process-isolated recovery

ARCH-09 binds a durable local provider-instance digest into the Program and
runs provider requests and reconciliation through fresh no-network subprocesses.
Its crash test terminates the original client after the first provider commit
and completes recovery from a second client process without a duplicate first
inference. This remains `mechanism_validation`; local process separation does
not establish external provider identity, exactly-once execution, or quality
uplift. See `docs/process-isolated-transport-recovery.md`.

## ARCH-10 authenticated store and leased supervisor

For the local process profile, ARCH-10 pre-registers the provider-store HMAC key
ID, authentication schema, protocol v2, recovery-lease schema, and supervisor
authority schema. Recovery ownership is fenced by owner, generation, and token,
with a heartbeat while the supervisor remains alive. Expired orphan adoption
requires a separate exact authority. These fields authenticate local store rows
against the supplied key file; provider, operator, and remote-service identity
remain unverified. See `docs/authenticated-provider-store-supervisor.md`.

## ARCH-11 signed recovery authority and Program attestation

The v3 process profile pre-registers a public Ed25519 root trust anchor. A
root-signed operator certificate must sign an exact recovery grant before the
Program can reconcile transport state. The grant binds the Program, Attempt,
provider instance, context intervention, existing transport authority, lease,
expiry, and one supervisor attestation key. Signed supervisor lifecycle events
are appended beneath the Program and independently revalidated in every status
projection. Local certificate validity is not external human identity proof.
See `docs/recovery-authority-pki-supervisor-attestation.md`.
