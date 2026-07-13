# Adaptive Task Profiles and Memory Lifecycle

## Purpose

SPST-UEA MUST NOT apply the same orchestration cost or persistence policy to
every task. The chat bridge therefore selects the least costly profile that
satisfies deterministic continuity and safety floors.

| Profile | Intended task | Long-term memory | Governance |
|---|---|---|---|
| `light` | short, low-risk, non-continuous | no read or write | baseline audit |
| `standard` | repository, evidence, test, or multi-step work | policy-filtered, 30-day episodic TTL | standard |
| `strict` | destructive, production, billing, secret, or architectural risk | policy-filtered, 90-day review TTL | strict |

`--profile light` is not an authority bypass. Risk signals always raise the
profile to `strict`; continuity signals always raise it to at least `standard`.
Every profile retains the complete seven-stage trace and an HMAC-provenance
routing receipt.

## Memory Eligibility

Long-term retrieval accepts only active, unexpired records at or above the
profile confidence floor. Records with a different policy version are rejected.
Legacy records without a policy version remain eligible at a lower ranking and
are explicitly labelled `legacy_unversioned`; they are not silently relabelled
as current policy.

Each new record carries:

- source;
- confidence;
- policy version;
- optional expiry;
- lifecycle status;
- optional superseding RuleCrystal.

Retirement is logical rather than physical. The record stays in the
HMAC-protected audit chain but is removed from retrieval and fingerprint
indexes. This preserves accountability while preventing stale instructions
from influencing later inference. Physical deletion remains a separate,
approval-gated retention operation.

High-salience episodic records can be distilled into `rule_crystal` records.
The source is then retired with a `superseded_by` binding to the crystal. Expiry
and duplicate compaction use the same auditable retirement mechanism.

## Receipt and Measurement Boundary

`spst-routing-receipt-v2` binds the selected profile and memory action. The
verifier remains compatible with persisted `spst-routing-receipt-v1` records.
Status reports verified `profile_counts` and `memory_action_counts`; invalid or
tampered receipts do not contribute.

These counts measure orchestration behavior, not GPT answer quality. SPST-UEA
still cannot observe Codex tasks that bypass the bridge, and it cannot infer a
quality delta without independently verified paired outcomes.
