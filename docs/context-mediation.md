# Context Mediation Layer

## Purpose

The Context Mediation Layer sits between accumulated local state and the
`ModelAdapter` boundary. It prevents the runtime from treating the complete
memory database as a prompt and emits one deterministic, bounded context packet
for the current task.

The layer does not certify that remembered text is true. Every selected item is
labelled `untrusted_evidence_only`; source and confidence are stored
attestations, not independent proof.

## Selection Contract

A memory item is eligible only when all of the following hold:

- the record is active and unexpired;
- confidence meets the packet floor;
- the record and retrieval attestation match the current policy version;
- source, record identity, and retrieval attestation are complete;
- deterministic lexical relevance is recomputed from the current query and
  bound record text instead of trusting a caller-provided score;
- recomputed relevance meets the packet floor and the required informative
  match count;
- memory provenance is valid;
- an existing routing history has a valid session chain and a verified latest
  receipt;
- the individual memory record is named by a provenance-verified producer
  Routing Receipt whose policy version matches the record;
- for repository-bound tasks, that producer Receipt is repository-bound and
  its exact recorded HEAD/worktree identity matches the current capture;
- the item contains no recognized secret, control-character, or prompt-
  injection pattern;
- the complete item fits the per-item and total character budgets.

Items are deduplicated by Unicode/whitespace-normalized text or canonical JSON
structure. This is a surface-equivalence filter, not a claim of general
semantic equivalence. Meaning that cannot be established safely remains
`semantic_claim: not_established`.

Default limits are five items, 1,500 characters per item, and 4,000 characters
in total. The default relevance floor is `0.35` under
`deterministic-lexical-v2`. English stop words do not create relevance, ASCII
words and structured paths/identifiers are matched as tokens, and Japanese
text uses bounded contiguous n-grams. A structured exact match or a sufficiently
strong multi-token match is required; any positive character overlap is no
longer enough. Oversized items are rejected rather than truncated because a
partial record can change its meaning.

## Runtime Order

For `standard` and `strict` chat routes, the bridge now performs this order:

1. capture the repository identity when requested;
2. open session and memory databases through immutable read-only connections;
3. verify provenance and prior routing state;
4. derive a digest-sealed origin index from independently verified Receipts;
5. retrieve accumulated memory and reject every item without a valid origin;
6. pass only a `ready` packet's items and the complete digest-sealed packet to
   the transition and `ModelAdapter` boundary;
7. execute the normal seven-stage route;
8. build a canonical model-input projection, reject a packet/item mismatch,
   and record its digest and delivery status;
9. persist the current turn and bind the packet, task, origin-index, and model-
   input evidence into the `spst-routing-receipt-v4`.

`light` routes skip long-term-memory access and carry no selected items.

## Read-Only Preview

Preview the exact packet without recording a turn or changing either database:

```powershell
python -m spst_runtime.chat_bridge `
  --context-preview "repository verification context" `
  --repository-root ..
```

Optional bounds:

```powershell
python -m spst_runtime.chat_bridge `
  --context-preview "repository verification context" `
  --context-max-items 3 `
  --context-max-chars 2500 `
  --repository-root ..
```

The output includes the task digest, selection and rejection counts, source
attribution, memory/session provenance summaries, routing summary, repository
identity, per-item producer Receipt/provenance bindings, and canonical packet
and origin-index digests. It does not include provenance key material or
absolute repository paths. Directly inserted or legacy-unreceipted records stay
in the database for audit but are not model-facing context.

## Model Input Binding

`spst-model-input-binding-v2` binds the normalized task, complete context-
packet digest, bounded model-facing item projection, instructions digest,
canonical model input, and the exact reviewed artifact and semantic-review
digest sets. The adapter rejects packet digest changes, task
mismatches, non-ready items, altered item text, and disagreement between the
packet items and `retrieved_context`.

Evidence-context items additionally require an exact supported semantic review.
The review is source- and record-bound but remains a self-attested human
decision, not authenticated identity or semantic truth.

The optional OpenAI adapter submits the canonical input and includes the model-
input and context-packet digests in request metadata. Tests use a captured local
HTTP stub and do not claim that a live OpenAI request was made. The API-key-free
adapter records the same binding with `delivery_status: recorded_not_executed`
and `inference_scope: scaffold_only`; it must not be described as proof that
the Codex model consumed the context.

## Boundary

This layer improves context discipline and auditability. It does not prove
answer-quality improvement, semantic truth, model-weight changes, or that the
Codex application itself consumed the packet. `submitted_to_provider` proves
only that the supported adapter constructed and submitted that request; it is
not proof that a model used every item causally. External Codex tool execution
and Codex internals remain outside runtime observability.
