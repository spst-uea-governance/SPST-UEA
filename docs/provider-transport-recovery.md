# ARCH-08 Provider Transport Idempotency and Recovery Authority

ARCH-08 narrows the unresolved interruption boundary left by ARCH-07. It does
not automatically retry a model request and does not infer success from a local
idempotency-key digest. It enables explicit recovery only for adapters that
declare both provider transport idempotency and result reconciliation and expose
the required reconciliation method.

## Bound call lifecycle

For every adapter invocation the runtime:

1. derives a unique idempotency key from the Program, execution manifest,
   running Attempt, call ordinal, and logical provider-request binding;
2. persists a `prepared` transport record;
3. atomically moves it to `submitted` before calling the adapter;
4. requires a provider-returned receipt acknowledging the exact key, exact
   provider request, response identifier, model, status, and output digest;
5. atomically moves the record to `completed` only after that receipt verifies.

A transport exception or missing/mismatched receipt leaves the record submitted
and the Program `recovery_required`. The ordinary `execute()` path never invokes
the adapter again for that call.

## Explicit recovery

`RealPairedOutcomeProgram.recover()` requires a digest-bound recovery-authority
artifact. It first queries the adapter's reconciliation method for every
submitted or previously completed call. Each query is durably recorded as an
issued-to-terminal CAS transition and is explicitly marked as a non-inference
operation.

Recovery resumes only when every uncertain call returns the exact bound result.
Completed calls are replayed from reconciled results in memory; they do not
create new inference calls. Prepared but unsubmitted calls and later scheduled
calls may then execute under the same fixed Program plan. A fixed query cap
prevents unbounded polling.

The same pre-registered live plan may be reused only in this recovery path and
only when the stored plan is byte-for-byte equal to the expected digest-bound
plan. A different plan remains blocked.

## Fail-closed cases

Recovery remains held when any of these occur:

- the adapter declares only one of idempotency or reconciliation;
- the reconciliation method is missing;
- the provider does not acknowledge the exact idempotency key;
- the key, request binding, response, model, output, or receipt is mismatched;
- the provider outcome is still unavailable;
- the query cap is exhausted;
- the recovery authority is missing, altered, or bound to another Attempt;
- a recovery or transport record is altered even when its outer digest is
  recomputed;
- a concurrent recovery already owns the durable recovery claim.

## Evidence and claim boundary

The local conformance adapter proves the state machine, CAS transitions,
provider-echo validation, no-duplicate-call behavior, and recovery replay
mechanism without a network or paid provider.

`provider_idempotency_observed` means that the configured adapter returned the
exact required receipt. Provider identity remains unauthenticated, and
`exactly_once_execution_proven` remains false because a remote provider's hidden
execution cannot be inferred from local records alone. The bundled OpenAI
adapter does not declare this recovery capability. A real external adapter must
implement and test a documented provider-side idempotency and result-lookup
contract before it can enter the ARCH-08 path.

ARCH-09 adds a stricter local experiment that kills the submitting client after
a separate provider process commits its result, then reconciles from a fresh
client process. See `docs/process-isolated-transport-recovery.md`. It reduces
the in-process simulation gap but does not change the external-provider claim
boundary above.

ARCH-10 additionally HMAC-authenticates the local provider store and serializes
recovery ownership with heartbeat-renewed fencing leases. See
`docs/authenticated-provider-store-supervisor.md`. This protects against a
database-only rewrite and live recovery races, but not compromise of both the
database and its external key file.
