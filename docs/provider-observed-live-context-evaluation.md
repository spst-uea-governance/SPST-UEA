# ARCH-04: Provider-Observed Context Uptake and Blinded Live Paired Evaluation

ARCH-04 tests a narrower proposition than "the model used the context": whether
an observable provider response acknowledged the exact request that contained
the governed context. The result is transport evidence, not a claim about
hidden model reasoning or general quality.

## Exact request and response binding

`spst-provider-request-binding-v1` commits to the exact prompt, instructions,
canonical model input, context packet/intervention, evaluation contract, and
all remaining adapter context. The adapter sends its digest with the request.
An eligible response must echo that digest and provide a response identifier,
status, provider/model declaration, and output text. These fields form
`spst-provider-observation-v1` and are recomputed before a producer record may
become v5 PBIND.

The runtime fails closed on a missing or altered acknowledgement, changed
output, incomplete record, unsupported observation source, or reused response
identifier. The schema accepts HTTPS response-metadata echo and observable
in-process echo as source classes, but this repository currently exercises only
the in-process class. Neither class cryptographically proves provider identity
by itself.

## Live paired protocol

`LiveContextPairedEvaluator` requires at least eight registered rubric tasks and
an adapter declaring provider-observation support. It performs fresh control
and treatment calls, with a balanced nonce-committed selected-condition order
per task. That order is included in the exact provider request and selected
v5 PBIND, so changing and re-signing only the stored schedule does not validate.
The
baseline explicitly binds no context intervention; the treatment binds the
exact human-supported intervention. Both producer runs and all 16 observations
must verify before paired scoring proceeds.

The current implementation reuses the existing two-arm Operational Shadow
runner. Consequently, it makes two adapter attempts for each condition report:
32 attempts for eight retained pairs, of which 16 are selected evidence. This
is explicit in the execution record and is a cost/latency limitation, not extra
sample evidence. This project did not invoke a network provider or incur API
charges during ARCH-04 verification.

The scoring artifact contains anonymous slots and provider-observation
commitments, but no arm labels. While review is pending, the public projection
also withholds the source-pair mapping and measured direction. Only the
`spst-blind-review-surface-v1` digest and anonymous task artifacts are available
to the reviewer.

Inspect that surface through the read-only command:

```powershell
python -m spst_runtime.paired_quality_bridge get `
  --evaluation-db <isolated-evaluation.db> `
  --evaluation-id <paired-evaluation-id>
```

After a human has reviewed only that surface, append the decision:

```powershell
python -m spst_runtime.paired_quality_bridge review `
  --evaluation-db <isolated-evaluation.db> `
  --evaluation-id <paired-evaluation-id> `
  --reviewer-id <independent-human-reviewer> `
  --decision accepted `
  --scoring-artifact-digest <artifact-digest> `
  --blind-review-surface-sha256 <surface-digest> `
  --arm-mapping-not-accessed `
  --reviewer-independence-attested
```

The review is append-only, but reviewer identity, blindness, and independence
remain self-attested. ARCH-04 utility attribution also rejects use of the same
reviewer identifier for semantic-context and outcome review.

## Claim boundary

A successful run may establish that the exact bound request was acknowledged
at the adapter/provider response boundary and that its paired outputs were
scored and reviewed under this local protocol. It does not establish:

- cryptographically authenticated provider identity;
- semantic use of the supplied context inside a model;
- causal context utility or general model-quality improvement;
- model-weight change;
- reviewer identity, blindness, or organizational independence;
- automatic adoption or promotion.

The conformance suite uses a deterministic in-process observable provider so it
can exercise success and adversarial paths without credentials, network calls,
or charges. Its measured benefit is fixture behavior, not GPT evidence. A real
provider study still requires an independently approved dataset, credentials,
cost authority, provider support for exact acknowledgement, and actual human
review. ARCH-06 adds the durable pre-registration, cost gate, Program-level
recomputation, and fixture-versus-remote evidence classification needed to
conduct that study without upgrading the present fixture into real evidence;
see `real-paired-outcome-program.md`.
