# ARCH-07 Operational Hardening

ARCH-07 hardens the ARCH-06 real paired outcome execution boundary against
concurrent execution, adapter failure, and process interruption. It does not
claim that a provider request was billed, accepted, or completed unless the
provider transport supplies that evidence.

## Fail-Closed Attempt State

Each program gets one durable `spst-real-paired-outcome-attempt-v1` record.
SQLite `INSERT OR IGNORE` is the cross-process single-claim boundary. Claim,
running, and terminal transitions are digest-bound, compare-and-set protected,
and linked to their append-only provenance record hashes.

The attempt is written before `LiveContextPairedEvaluator` can invoke an
adapter. A v2 program execution binds the attempt ID and running-state digest.
The final attempt binds the execution digest. Re-signed changes that do not
match the recorded transition chain fail closed.

## Retry Boundary

No time-based lease stealing or automatic retry is permitted after an attempt
reaches `running`. A killed process may have sent an external request even when
no response or local producer record exists. Retrying that work could duplicate
cost and inflate evidence. Read-only status therefore reports
`recovery_required`; it does not mutate the database or call the adapter.

Ordinary adapter exceptions are reduced to a generic failure reason and the
exception class. Raw exception text is not persisted. The attempt becomes a
terminal failure and later `execute` calls remain blocked.

If a program execution was durably written but its terminal transition was
interrupted, a later explicit `execute` may finalize that existing digest
without invoking the adapter again. It cannot replace the execution record.

## Compatibility and Claim Boundary

ARCH-06 v1 execution records remain readable. They are reported as
`legacy_unbound`, not as attempt-bound operational evidence. ARCH-07 proves the
local claim/transition mechanism and its in-process adversarial fixtures. It
does not prove provider transport attempt counts, cross-host distributed locks,
crash recovery for partly completed remote requests, or model-quality uplift.
