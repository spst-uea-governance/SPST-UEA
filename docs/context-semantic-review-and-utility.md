# ARCH-03: Human-Reviewed Semantic Support and Context Utility

ARCH-03 closes two gaps left intentionally open by ARCH-02: a source-bound
artifact was not semantically reviewed, and delivery evidence did not measure
whether the exact context was associated with a better or worse task outcome.

## Semantic-support gate

Compilation remains structural. The artifact still says
`asserted_not_independently_established`; review does not rewrite it. A separate
append-only record binds all of the following:

- artifact SHA-256 and kind;
- statement and source SHA-256;
- producer Routing Receipt;
- complete memory-record binding and projection SHA-256;
- policy version, decision, note, and review timestamp.

Only `supported` makes the artifact eligible. Missing, unsupported, duplicated,
tampered, source-stale, producer-mismatched, record-mismatched, or policy-stale
review evidence fails closed. The selected item remains
`untrusted_evidence_only` with
`human_supported_source_verified_untrusted` source trust.

`reviewer_kind: human` is a caller declaration. The local HMAC chain protects
the stored decision after recording but does not authenticate the human.
Consequently both `human_identity_cryptographically_verified` and
`reviewer_independence_verified` remain `false`.

The v2 model-input binding records the exact delivered artifact and semantic
review SHA-256 sets in the Routing Receipt. This proves which reviewed context
the runtime bound to the adapter input; API-key-free
`recorded_not_executed` evidence still does not prove provider consumption.

## Isolated context experiment

`OperationalShadowRunner.run(context_experiment=True)` gives both arms the same
`context_control` evaluation profile and suppresses the normal capability plan.
The treatment arm alone receives a reviewed context intervention. The producer
stores only its exact binding, not an unbounded copy of the source. A v4 PBIND
binds artifact, source, review, record, projection, intervention, delivery to
the adapter argument, task, execution identity, output, verification, and
scoring material.

This profile avoids attributing the normal capability-maximization plan to
context. Utility derivation rejects ordinary or otherwise confounded shadow
runs, a context-bearing control, an absent or different treatment, altered
PBINDs, and caller-supplied scores.

## Utility attribution

`ContextUtilityAttributionLedger` reloads the reviewed Phase 16 evaluation and
both producer reports. It requires:

- at least eight valid paired tasks;
- distinct producer executions under the same recorded provider/model/task
  contract;
- arm-blinded exact-JSON scoring;
- the fixed two-sided 95% Hoeffding interval;
- an accepted outcome scoring-artifact review;
- a current supported semantic review for the exact context artifact;
- the isolated no-context control and exact context treatment binding.

No score, delta, uncertainty, or semantic flag is accepted from the attribution
caller. The interval determines one of three results:

- lower bound above zero: `beneficial_association_supported`;
- upper bound below zero: `harmful_association_supported`;
- otherwise: `direction_inconclusive`.

The word association is deliberate. The runtime proves that the reviewed
context was submitted to the adapter argument and that the paired outputs were
scored as recorded. It does not observe hidden provider state or prove that an
external model consumed the context. Therefore
`causal_context_utility_established`, `provider_uptake_verified`, general model
quality, model-weight change, and automatic adoption all remain false.

## Read-only inspection

Normal context preview applies the gate without writing either DB:

```powershell
python -m spst_runtime.chat_bridge `
  --context-preview "reviewed context" `
  --repository-root ..
```

Utility history is also read-only:

```powershell
python -m spst_runtime.context_utility_bridge status --repository-root ..
```

The conformance fixture uses an isolated deterministic adapter to prove
beneficial, harmful, and inconclusive paths. Those fixture results validate the
mechanism, not SPST or GPT uplift in production.

## ARCH-04 provider-observed extension

ARCH-04 upgrades the transport boundary without upgrading the causal claim. An
eligible adapter must return a provider observation that echoes the SHA-256 of
the exact prompt, instructions, canonical model input, context intervention,
evaluation contract, and remaining adapter context. The runtime also binds the
provider/model declaration, response identifier, completion status, and exact
output digest. Missing, mismatched, altered, or replayed observations fail
closed; a no-key local Codex scaffold is ineligible.

Both selected conditions are fresh calls and use a balanced, nonce-committed
per-task order bound into the provider request and v5 PBIND.
Both must produce v5 PBIND records. Before outcome review, the projection
withholds source-pair mappings and the measured direction and exposes only a
digest-bound blind surface. The reviewer must self-attest that the arm mapping
was not accessed and that they are independent; the semantic-context reviewer
and outcome reviewer must have different identifiers.

After an accepted review, utility attribution may report
`provider_uptake_observed: true`. It deliberately retains
`provider_uptake_verified: false`, because the local evidence does not
cryptographically authenticate the provider, prove that hidden model state used
the context semantically, or establish causality. See
`provider-observed-live-context-evaluation.md`.
