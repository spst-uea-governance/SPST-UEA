# ARCH-10 Authenticated Provider Store and Leased Recovery Supervisor

ARCH-10 narrows two local failure boundaries left by ARCH-09: a writer could
rewrite provider metadata while recomputing unkeyed digests, and two recovery
clients could race after an interrupted process. It remains a no-network,
no-API-key, no-charge mechanism-validation profile.

## Provider-store authentication

`DurableProcessProviderStore` keeps HMAC key material outside the SQLite
database. The default file is `<provider-db>.provider_key`; callers may provide
an explicit key-file path. The key value is never stored in SQLite or printed by
the worker.

The current key ID and generation are stored in the database and the following
records are HMAC-SHA-256 authenticated:

- provider-instance identity;
- the complete provider result row, including request and execution counts;
- every reconciliation record;
- every recovery lease and fencing generation.

The adapter binds the key ID, authentication schema, provider-instance digest,
protocol version, and lease schemas into the pre-registered Program. Store
integrity is checked again by provider health before execution or recovery.
Legacy rows without an authentication tag remain readable at the SQLite level
but fail integrity verification and cannot become promotion evidence.

`rotate_authentication_key()` refuses an invalid source store or an existing
target key file. It re-authenticates result, reconciliation, lease, and instance
records in one database transaction, increments the key generation, and leaves
the previous key file untouched for operator-controlled retention. The old key
cannot verify the rotated database.

## Durable recovery lease

A recovery lease is bound to the Program, Attempt, and provider instance through
a SHA-256 resource identifier. It contains an opaque token digest, owner,
generation, acquisition and expiry times, previous owner, adoption count, key
generation, and HMAC tag.

- A second owner cannot acquire an active lease.
- The current owner renews the lease through a fenced heartbeat.
- Owner, generation, and raw token must all match for renewal or release.
- A killed supervisor stops heartbeats; the lease then expires durably.
- Expiry alone does not authorize takeover. Orphan adoption requires an exact
  digest-bound authority for the previous owner, expired generation, resource,
  and provider instance.
- Rewriting an expiry or owner without the key invalidates the HMAC and cannot
  create an adoptable lease.

The supervisor releases the lease only after `recover()` returns and the
heartbeat stops successfully. An exception or process kill leaves the lease
active until expiry instead of pretending the recovery outcome is known.

## Process topology

```text
recovery operator authority
          |
          v
fresh recovery supervisor process
  |-- acquire authenticated lease
  |-- maintain fenced heartbeat
  |-- invoke RealPairedOutcomeProgram.recover()
  |      `-- fresh authenticated provider worker processes
  `-- release lease only after observed return
```

The supervisor CLI is:

```powershell
python -m spst_runtime.process_recovery_supervisor recover `
  --database <isolated-program.db> `
  --provider-db <isolated-provider.db> `
  --provider-key-file <provider.key> `
  --program-id <program-id> `
  --intervention-file <intervention.json> `
  --recovery-authority-file <provider-recovery-authority.json> `
  --lease-owner <owner-id> `
  --lease-ttl-ms 5000
```

Read-only lease status uses the same module with `status`, the provider paths,
and `--resource-id`. It does not create a missing key or database.

## Counterexamples

The targeted suite verifies:

- unkeyed metadata rewrite after recomputing ordinary digests;
- a wrong key file and use of the pre-rotation key;
- rotation of existing authenticated rows;
- concurrent acquisition while the heartbeat keeps a lease active;
- takeover after expiry without authority;
- stale owner/token use after adoption;
- lease expiry rewriting without the HMAC key;
- forced supervisor termination followed by authority-bound orphan adoption;
- no duplicate provider inference across the recovered 32-call schedule.

## Claim boundary

`provider_store_authenticity_verified` means the local database rows verify
against the supplied external key file. It does not authenticate an external
provider organization, a human operator, the operating-system account, or key
custody. An actor able to rewrite both the database and key file can establish a
new local trust root. The process experiment does not prove remote-provider
exactly-once behavior, GPT quality improvement, or production availability.

ARCH-11 adds an optional Program-bound public-key authority and signed
supervisor lifecycle above this HMAC/lease layer. It does not retroactively
upgrade ARCH-10 records. See
`docs/recovery-authority-pki-supervisor-attestation.md`.
