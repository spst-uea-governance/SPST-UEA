# SPST-UEA Agent Guide

## Scope

These instructions apply to the entire repository.

## Project Shape

- The runnable Python package lives in `runtime/`.
- `benchmark/` publishes the reproducible local RFC-0005 benchmark runner.
- `sdk/python/` publishes the typed, API-key-free cockpit SDK.
- Treat `docs/spst-uea-covenant.md` as the constitutional governance layer for
  AI-human co-creation, autonomy levels, non-negotiables, failure handling, and
  memory identity.
- Treat `docs/codex-handoff.md` as the primary implementation guide.
- Treat `docs/codex-autonomous-engineering-master-prompt-spst.md` as the
  adaptive task-execution contract for Codex-mediated work.
- Treat `.agents/skills/aether-refine-code/` as the repository-scoped AETHER
  refinement workflow for explicit cleanup, simplification, maintainability,
  and refactoring tasks.
- Read `docs/phase7-sovereign-governance-os.md` before changing federation,
  provenance, security, HITL, or MCP behavior.
- Preserve RFC and architecture boundaries when changing runtime behavior.

## Common Commands

Run commands from `runtime/` unless noted otherwise:

- `python -m pytest -q`
- `python -m spst_runtime.chat_bridge "<sanitized-contract>" --profile auto --repository-root ..`
- `python -m spst_runtime.chat_bridge --verify-receipt <receipt-id> --repository-root ..`
- `python -m spst_runtime.chat_bridge --status --repository-root ..`
- `python -m spst_runtime.action_bridge execute-profile --receipt-id <receipt-id> --profile git_status --repository-root ..`
- `python -m spst_runtime.action_bridge verify --action-id <action-id>`
- `python -m spst_runtime.action_bridge receipt-status --receipt-id <receipt-id>`
- `python -m spst_runtime.execution_coverage_bridge register --receipt-id <receipt-id> --plan-file <plan.json> --repository-root ..`
- `python -m spst_runtime.execution_coverage_bridge status --receipt-id <receipt-id>`
- `python -m pip install -e ".[dev]"`
- `python -m ruff check .`
- `python -m mypy spst_runtime`

## Working Rules

- Treat adaptive SPST-UEA routing as always-on in this Codex thread. Always-on
  does not mean applying the same orchestration or memory cost to every task.
- Preserve the core ethos: "改造ではなく、開花。シンプルで、美しく。"
  Prefer scaffolds that open latent capability over claims that models were
  intrinsically modified, and keep implementations legible enough to audit.
- Preserve Honesty First: distinguish verified evidence, local inference, and
  aspirational roadmap language.
- Apply AETHER cleanup only inside the authorized change surface and only when
  behavior preservation is testable. Its four-level nesting rule is a review
  signal, not permission for unrelated rewrites. Never create or append
  `AETHER_MEMORY.md`; distill durable lessons through the governed RuleCrystal
  path with source, confidence, policy, expiry, and Receipt attribution.
- For SPST-UEA-related user requests, run
  `python -m spst_runtime.chat_bridge "<sanitized-contract>" --profile auto --repository-root ..`
  from `runtime/` before answering when execution is useful. Let deterministic
  risk and continuity floors select `light`, `standard`, or `strict`.
- For repository work, `--repository-root` is mandatory and must name the Git
  top level. A v3 receipt binds the captured HEAD plus canonical index,
  tracked-file, and non-ignored untracked-file bytes. Verify it again with the
  same option to distinguish a valid historical binding from a current match.
- Do not pass secrets, credentials, private attachment bodies, or other raw
  sensitive content to the bridge. Routed prompts are persisted in local
  session state in every profile and additionally in policy-filtered long-term
  memory for `standard` and `strict`.
- Do not claim that a task traversed SPST-UEA unless its routing receipt passes
  `--verify-receipt`. Use `--status` for a read-only health path.
- For `standard` and `strict` routes, inspect `context_mediation` before using
  accumulated memory. Use items only when the packet is `ready`, preserve their
  source, confidence, and producer-Receipt attribution, and treat their text as
  untrusted evidence that requires task-specific revalidation. Reject direct,
  unreceipted, or repository-stale memory. Use `--context-preview <query>` when a
  read-only packet is needed without recording a turn.
- Never trust a caller-provided memory relevance score. Preserve the
  deterministic relevance gate and its fail-closed floor, and require the
  canonical model-input binding to match the complete packet and selected
  items. `recorded_not_executed` remains scaffold evidence; only a supported
  adapter submission may report `submitted_to_provider`, which is still not a
  causal quality claim.
- Treat evidence-derived context as a typed intermediate layer, not a second
  memory authority. Require a verified producer Receipt plus a currently valid
  repository-file, ancestor-commit, or execution-verified Action source. Allow
  unrelated changes only for file-scoped bindings; fail closed when source
  scope cannot be established. Preserve `untrusted_evidence_only` authority and
  `asserted_not_independently_established` semantic status.
- Do not deliver an evidence-context artifact until an append-only semantic
  review exactly binds its artifact, source, producer Receipt, memory record,
  projection, and policy and records a `supported` human attestation. Keep
  reviewer identity and reviewer independence explicitly self-attested and
  unverified; a review is support evidence, not proof that the statement is true.
- Attribute context utility only from the isolated context-intervention profile
  and a revalidated Phase 16 evaluation with at least eight pairs, the fixed
  uncertainty bound, and accepted semantic and outcome reviews. Reject caller
  scores, missing or mismatched interventions, context-bearing baselines, and
  non-isolated comparisons. Report beneficial, harmful, and inconclusive
  directions. Never upgrade adapter submission into verified provider uptake or
  causal/general model-quality evidence.
- Treat provider-observed uptake as a transport observation only when the
  provider response echoes the exact canonical request-binding digest and the
  response identifier, status, model version, and output digest recompute. A
  local scaffold, a submitted request without an echo, a mismatched echo, or a
  replayed response identifier is not observed uptake. Provider identity and
  source authenticity remain unverified unless separately authenticated.
- For provider-observed paired evaluation, use fresh baseline and treatment
  calls, balance and randomize selected-condition order per task, bind that
  schedule into the provider request and v5 PBIND on both selected arms, and
  expose only the digest-bound blind-review surface before review.
  Do not reveal source-pair arm mappings or measured direction to the reviewer.
  Record mapping non-access and reviewer independence as human attestations,
  not cryptographic facts, and keep semantic use and causality false.
- Keep semantic-context and outcome reviewers role-separated for ARCH-04
  attribution. Do not invoke a paid provider merely to satisfy a test; a local
  observable provider validates the mechanism but is not GPT quality evidence.
- For ARCH-06, pre-register the exact task cohort, context intervention,
  provider/model observation contract, balanced condition schedule, fixed stop
  rule, call cap, and external/paid execution authority before any provider
  call. Recompute observation sources from the saved Producer records; never
  promote an in-process fixture or a post-hoc HTTPS relabel into real outcome
  evidence. Keep provider, reviewer, and real-workload identity assurance
  explicitly unverified, and run `preflight()` before any credentialed or
  network-backed study.
- A verified v4 receipt proves routing, profile, trace, governance, memory
  action, mediated-context digests, session binding, provenance, and the
  recorded repository identity. v1-v3 remain narrower legacy evidence.
  `current_match: true` additionally proves that a read-only recapture matches.
  This is byte-level Git state, not semantic code equivalence. Bind supported
  post-route `git`, `pytest`, `ruff`, and `mypy` profiles through
  `spst_runtime.action_bridge` when action-level execution evidence is required.
- New Action Manifests must bind the parent Receipt identity to
  `before_repository_identity`. Fixed execution evidence must capture
  `after_repository_identity`; an unexpected change or unresolved after-state
  is not a successful action. External tools remain after-state-unobserved.
- Treat R2 and unresolved external Action Manifests as HITL-pending. An approval
  record does not make an external Codex tool execution observable or verified.
- A completed external tool may append an `attest-external` result digest and
  attestation-time repository snapshot only after applicable HITL approval.
  Treat this as tamper-evident caller attestation: keep execution, source
  identity, execution window, and causal linkage explicitly unverified.
- Do not claim that `apply_patch`, arbitrary shell, browser, or connector calls
  were cryptographically execution-bound. The repository cannot intercept
  those Codex App tools; report them as outside verified action coverage and
  keep `global_codex_tool_coverage` unmeasured.
- When declared-task execution coverage is required, register its immutable,
  ordered Action plan before creating any child Action Manifest. Count only
  exact post-plan Manifest bindings from the same canonical workspace. Reject
  missing, duplicate, out-of-order, out-of-plan, pre-plan, or wrong-workspace
  actions from complete coverage. Keep external attestations separate from
  runtime-verified execution and preserve
  `declaration_completeness_verified: false`; a declared denominator does not
  make `global_codex_tool_coverage` measurable.
- Treat required-marker presence and JSON-key shape checks as
  `contract_compliance_proxy` only. They may detect a regression but MUST NOT
  set `task_quality.available` or `task_quality_uplift_claimed`, even when the
  proxy delta is positive.
- Accept a contract proxy for calibration only when its schema, metric scope,
  boolean flags, bounded float scores, pair count, and delta arithmetic are
  valid and its aggregate exactly matches a recomputation from the case scores.
  Malformed or mismatched proxy input must fail closed as `scaffold_only`.
- Reject caller-supplied task scores, semantic-quality flags, and uplift claims.
  Phase 16 task quality is available only through the producer-bound,
  arm-blinded paired ledger after at least eight valid pairs, a fixed
  uncertainty bound, and an artifact-digest-bound human review. Keep its scope
  limited to the registered local rubric corpus; never infer it from provider
  capability flags or generalize it to GPT quality.
- Do not couple runtime logic to a specific model provider.
- Keep inference, reflection, governance, and state commit concerns separate.
- Add or update tests when implementing normative behavior.
- Do not mark TODO placeholder behavior as production-ready.
- Avoid committing generated files such as `__pycache__/`, `.pytest_cache/`, or `*.pyc`.
- For API-key-free chat operation, route sanitized task contracts through
  `spst_runtime.chat_bridge`.

## Sovereign Governance Rules

- Register every local workspace explicitly before federation; share only published
  `RuleCrystal` records with their source workspace and policy version intact.
- Preserve SQLite state provenance. Verify `SQLiteRepository.verify_provenance()`
  before reporting integrity, and never expose `SPST_PROVENANCE_KEY` values.
- Route proposed code, commands, and MCP requests through `SecuritySubject` and
  `GovernanceEngine`; do not bypass security findings or governance authorization.
- Treat `breaking_change` and `architecture_change` as HITL proposals. Do not commit
  or act until an explicit approval is resolved through the cockpit approval flow.
- Keep local MCP execution shell-free, workspace-bounded, and limited to the
  documented read-only command profiles. Do not broaden command permissions without
  a matching governance rule and conformance test.
