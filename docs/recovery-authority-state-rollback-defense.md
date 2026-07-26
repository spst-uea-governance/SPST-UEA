# ARCH-12 Revocation, Authenticated Time Floor, and Rollback Defense

ARCH-12 extends the optional ARCH-11 local recovery authority profile. It does
not turn a local clock, file, or software key into trusted hardware. It adds a
root-signed current authority state that a live recovery must present in
addition to its Program-bound operator grant.

## Root-signed authority state

An authority state binds a strictly positive generation, the previous state
digest, a validity window, a minimum accepted wall-clock time, sorted operator
certificate and grant revocation sets, and key-custody evidence. A separate
root-signed rollback anchor binds the same state digest and generation plus the
previous anchor digest.

For the v4 process profile, the trust anchor binds:

- the authority-state and rollback-anchor schemas;
- a minimum accepted state generation;
- the key-custody policy;
- the requirement that live grant validation supply the current state and
  anchor rather than trusting only the copies embedded in the grant.

The operator-signed grant binds the exact state, anchor, generation, time floor,
and custody-evidence digests. Embedded copies remain usable only to validate
historical signed supervisor events. A live lease acquisition or recovery call
must independently provide the current root-signed state and anchor.

## Failure behavior

Recovery fails closed for a missing current state, a revoked operator or grant,
a state generation below the Program-bound floor, an external state older than
the grant-bound state, a local wall clock below the authenticated time floor,
an expired state, a rewritten state or anchor, a state/anchor mismatch, or a
key-custody policy that the supplied evidence cannot satisfy. The supervisor
attestation ledger also rejects a new recovery session whose authority-state
generation is lower than an already accepted session.

The supervisor CLI accepts the new live inputs:

```powershell
python -m spst_runtime.process_recovery_supervisor recover `
  --authority-trust-file <root-trust.json> `
  --authority-state-file <current-authority-state.json> `
  --rollback-anchor-file <current-rollback-anchor.json> `
  --authority-grant-file <program-bound-grant.json> `
  --supervisor-signing-key-file <per-run-private-key.pem> `
  <other program, provider, intervention, and lease arguments>
```

`status` accepts the state and anchor files through the same options and keeps
the provider and Program databases read-only.

## Verified counterexamples

The targeted tests cover missing live state, certificate revocation, grant
revocation, a clock below the signed floor, altered state, altered anchor,
state-generation downgrade, unattested local-file custody under a required
hardware policy, provider lease acquisition without current state, read-only
provider status byte preservation, and a complete v4 process recovery bound to
the authority state and anchor.

## Claim boundary

The root signature authenticates who issued the state and anchor. It does not
prove that the stated time came from a trusted clock. The local dual-copy
anchor detects independent state or anchor replacement, but an attacker able
to roll back both copies and all Program evidence remains outside the proved
scope. The current key-custody builder records local-file custody with OS,
hardware, and external attestation flags false; it deliberately cannot satisfy
a policy that requires verified OS or hardware custody. Accordingly the
runtime reports `trusted_time_source_verified: false`,
`hardware_key_custody_verified: false`, and
`full_rollback_resistance_verified: false`.
