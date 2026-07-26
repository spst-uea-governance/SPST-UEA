# ARCH-11 Recovery Authority PKI and Program-Bound Supervisor Attestation

ARCH-11 closes two authority gaps left by ARCH-10. A digest-only adoption
artifact identified its fields but did not authenticate who authorized it, and
the parent Program did not retain independently signed evidence of what the
recovery supervisor actually observed. ARCH-11 remains a local, no-network,
no-charge mechanism profile.

## Local authority chain

The authority module uses Ed25519 and a two-level local chain:

```text
offline root private key
  `-- signs operator certificate
        `-- operator private key signs one exact recovery grant
              `-- grant authorizes one supervisor attestation public key
```

`generate_recovery_authority_pki()` creates the root and operator private-key
files with exclusive creation and restrictive file-mode requests. Only the
root public trust anchor and root-signed operator certificate may enter Program
evidence. `generate_supervisor_attestation_key()` creates a separate per-run
private key; the operator grant binds its public key. None of the three private
keys is persisted in the Program or provider SQLite databases.

The Program pre-registers the complete public trust anchor and its digest,
grant schema, supervisor-attestation schema, and v3 process protocol. The
provider store HMAC also binds the trust-anchor digest. Opening a signed store
without the public trust object permits provider-worker operations but cannot
acquire or adopt a recovery lease.

## Exact recovery grant

Each root-validated, operator-signed grant binds:

- Program ID and `program_sha256`;
- active Attempt ID;
- provider-instance digest;
- lease resource and owner;
- existing transport-recovery-authority digest;
- context-intervention digest;
- optional previous owner and expired generation for orphan adoption;
- operator certificate and operator ID;
- one supervisor-attestation public key;
- issuance, expiry, nonce, and the prohibition on automatic retry.

The supervisor verifies the full chain before reconciliation or a new provider
call. The provider store independently verifies the signed lease claims. A
wrong root, altered claim with recomputed hashes, expired grant, wrong owner,
wrong Program, or wrong orphan generation fails closed.

## Program-bound supervisor lifecycle

The supervisor signs and appends these events to the parent Program repository:

1. `authority_accepted`
2. `lease_acquired`
3. one or more `lease_renewed`
4. `heartbeat_stopped`
5. `recovery_returned`
6. `lease_released`

Every event binds the Program, Attempt, provider instance, lease resource,
lease owner, authority grant, prior attestation digest, event details, local
PID, and sequence. The grant-bound supervisor private key signs the complete
event. SQLite provenance remains a separate integrity layer: even a rewrite
accepted by the local provenance key still fails the Ed25519 event signature.

A killed process cannot append a false terminal event. Its attestation chain
therefore remains `incomplete`. A fresh supervisor must use a new grant and, for
an expired live lease, exact orphan-adoption claims. The next session links to
the previous attestation digest and can reach `complete` only after observed
recovery return and lease release.

## Read-only status

The status path can include both provider lease state and Program attestation
without changing either database:

```powershell
python -m spst_runtime.process_recovery_supervisor status `
  --database <program.db> `
  --program-id <program-id> `
  --provider-db <provider.db> `
  --provider-key-file <provider.key> `
  --authority-trust-file <root-trust.json> `
  --resource-id <lease-resource-sha256>
```

## Verified counterexamples

The targeted suite covers signature-preserving digest recomputation after a
claim rewrite, expiry, wrong root, missing signed authority, provider-store
trust downgrade, grant replay, incorrect orphan owner, a rewritten event after
valid local provenance append, read-only status hash preservation, forced
supervisor kill, fresh signed adoption, and a 32-call recovery with no duplicate
provider execution.

## Claim boundary

`operator_certificate_verified` means a key was authorized by the Program-bound
local root. It does not prove the operator ID belongs to a particular real
person. `supervisor_attestation_key_verified` authenticates the event signer to
the operator grant; PID and operating-system process identity remain
unverified. Root compromise, operator-key compromise, certificate revocation,
secure hardware custody, trusted time, remote-provider identity, remote
exactly-once behavior, and GPT quality improvement are not established.
