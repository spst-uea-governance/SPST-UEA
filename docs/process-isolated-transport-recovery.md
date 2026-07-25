# ARCH-09 Process-Isolated Transport Recovery

ARCH-09 moves the no-charge ARCH-08 simulator across real operating-system
process boundaries. It tests whether a durable provider result survives the
loss of the client process that submitted it and whether a fresh client can
reconcile that result without executing the logical inference twice.

## Process topology

```text
test controller
  |-- client process A: RealPairedOutcomeProgram.execute()
  |     `-- provider worker process: durable SQLite commit, delayed response
  |-- terminate client A after the provider commit marker
  `-- client process B: RealPairedOutcomeProgram.recover()
        |-- fresh reconciliation worker
        `-- fresh inference workers for calls not already completed
```

The workers use a JSON-over-stdio protocol and a dedicated provider SQLite
database. They do not open a network socket, read an API key, or invoke a paid
provider. `OPENAI_API_KEY` is removed from each worker environment.

## Durable identity and idempotency

The provider database creates one persistent random instance digest. The
adapter binds that digest, `local_process` execution environment, protocol
version, provider name, and model version into the pre-registered Program.
A recovery caller that substitutes another provider database fails the adapter
contract before issuing a reconciliation query.

The provider table has one row per ARCH-08 idempotency key. Concurrent worker
processes serialize with `BEGIN IMMEDIATE`. A repeated exact request returns the
stored result and increments its request count but never increments the logical
execution count. Reuse of the key with another request or transport binding is
rejected.

## Crash experiment

The integration test waits until a provider worker has:

1. validated the exact transport and request binding;
2. inserted the result and receipt in the durable provider database;
3. closed the transaction; and
4. written a content-digest marker containing only process and result hashes.

It then forcibly terminates client process A before the delayed response is
returned. Client process B reads the same Program and Attempt, presents an
explicit ARCH-08 recovery authority, reconciles the submitted first call, and
continues the fixed schedule. The provider status must end with 32 distinct
records, 32 logical executions, 32 inference requests, and one successful
reconciliation. A 33rd inference request is a failure.

## Fail-closed cases

- exit before provider commit leaves no provider result and recovery performs
  no new inference;
- a substituted provider-instance digest is rejected before reconciliation;
- a modified durable result makes adapter health unavailable before query;
- concurrent identical requests may produce two request observations but only
  one logical provider execution;
- malformed worker output, worker timeout, non-zero worker exit, receipt
  mismatch, or provider-store integrity failure cannot become completed
  transport evidence.

## Claim boundary

`process_isolated_recovery_mechanism_observed` means that the local subprocess
crash-and-recovery experiment completed with its bound receipt set. It does not
authenticate the simulated provider, prove that an external provider enforces
exactly-once execution, or establish GPT quality improvement. The provider
database uses local digests and a bound instance identifier, not an external
trust anchor. This remains `mechanism_validation`, not real outcome evidence.
