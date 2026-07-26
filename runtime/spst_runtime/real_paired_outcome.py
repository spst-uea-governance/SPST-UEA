import asyncio
from copy import deepcopy
import json
import re
import secrets
from typing import Any, Callable

from spst_runtime.context_review import validate_context_intervention
from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.evaluation.paired_quality import PairedQualityEvidenceLedger
from spst_runtime.interfaces.model_adapter import ModelAdapter
from spst_runtime.live_context_evaluation import (
    LIVE_CONTEXT_EVALUATION_SCHEMA,
    LIVE_CONTEXT_PLAN_RECORD_SCHEMA,
    LiveContextPairedEvaluator,
)
from spst_runtime.live_pairing import (
    build_live_pair_plan,
    canonical_live_pair_hash,
    validate_live_pair_plan,
)
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.process_transport import build_recovery_lease_resource_id
from spst_runtime.provider_observation import PROVIDER_OBSERVATION_SOURCES
from spst_runtime.provider_transport import (
    IdempotentTransportAdapter,
    PROVIDER_RECOVERY_AUTHORITY_SCHEMA,
    PROVIDER_TRANSPORT_RECEIPT_SCHEMA,
    PROVIDER_TRANSPORT_REQUEST_SCHEMA,
    ProviderTransportLedger,
    ProviderTransportOutcomeUnknown,
    ProviderTransportRecoveryRequired,
)
from spst_runtime.recovery_authority import (
    RECOVERY_AUTHORITY_GRANT_SCHEMA,
    RECOVERY_AUTHORITY_TRUST_SCHEMA,
    SUPERVISOR_ATTESTATION_SCHEMA,
    SupervisorAttestationLedger,
    validate_recovery_authority_trust_anchor,
    verify_recovery_authority_grant,
)
from spst_runtime.real_paired_outcome_attempt import (
    ACTIVE_ATTEMPT_STATES,
    PROGRAM_ATTEMPT_PREFIX,
    REAL_PAIRED_OUTCOME_ATTEMPT_SCHEMA,
    RealPairedOutcomeAttemptLedger,
)


REAL_PAIRED_OUTCOME_PROGRAM_SCHEMA = "spst-real-paired-outcome-program-v1"
REAL_PAIRED_OUTCOME_EXECUTION_SCHEMA = "spst-real-paired-outcome-execution-v2"
LEGACY_REAL_PAIRED_OUTCOME_EXECUTION_SCHEMA = "spst-real-paired-outcome-execution-v1"
REAL_PAIRED_OUTCOME_INDEX_SCHEMA = "spst-real-paired-outcome-index-v1"
PROVIDER_EXECUTION_AUTHORITY_SCHEMA = "spst-provider-execution-authority-v1"
PROGRAM_INDEX_KEY = "runtime:real_paired_outcome:index:v1"
PROGRAM_RECORD_PREFIX = "runtime:real_paired_outcome:program:"
PROGRAM_EXECUTION_PREFIX = "runtime:real_paired_outcome:execution:"
WORKLOAD_CLASSES = frozenset({"test_fixture", "real_user_workload"})
EXECUTION_ENVIRONMENTS = frozenset(
    {"in_process", "local_process", "external_network"}
)
BILLING_CLASSES = frozenset({"no_charge", "paid", "unknown"})
MECHANISM_OBSERVATION_SOURCE = "in_process_provider_echo"
PROCESS_MECHANISM_OBSERVATION_SOURCE = "local_process_provider_echo"
REMOTE_OBSERVATION_SOURCE = "https_response_metadata_echo"
MAXIMUM_TRANSPORT_RECONCILIATION_MULTIPLIER = 2

_identifier = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_sha256 = re.compile(r"^[0-9a-f]{64}$")


class RealPairedOutcomeProgram:
    """Pre-register and revalidate a bounded provider-observed paired study."""

    EXPECTED_ATTEMPTS_PER_PAIR = 4
    METRIC_SCOPE = PairedQualityEvidenceLedger.METRIC_SCOPE

    def __init__(
        self,
        repository: SQLiteRepository,
        corpus: OperationalEvaluationCorpus,
        model_adapter: ModelAdapter | None = None,
        *,
        plan_nonce_factory: Callable[[], str] | None = None,
        scoring_nonce_factory: Callable[[], str] | None = None,
        attempt_nonce_factory: Callable[[], str] | None = None,
    ):
        self.repository = repository
        self.corpus = corpus
        self.model_adapter = model_adapter
        self.plan_nonce_factory = plan_nonce_factory or (lambda: secrets.token_hex(32))
        self.scoring_nonce_factory = scoring_nonce_factory
        self.paired_quality = PairedQualityEvidenceLedger(repository, corpus)
        self.attempts = RealPairedOutcomeAttemptLedger(
            repository,
            nonce_factory=attempt_nonce_factory,
        )

    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Freeze the cohort, provider contract, cost authority, and order before calls."""

        if self.repository.read_only:
            return self._blocked("real_paired_outcome_store_read_only")
        source = payload if isinstance(payload, dict) else {}
        if set(source) != {
            "baseline_candidate_id",
            "candidate_id",
            "context_intervention",
            "execution_authority",
            "study",
            "task_ids",
        }:
            return self._blocked("real_paired_outcome_registration_shape_invalid")
        if self.model_adapter is None:
            return self._blocked("real_paired_outcome_adapter_required")

        intervention = source.get("context_intervention")
        intervention_valid, intervention_reason = validate_context_intervention(
            intervention
        )
        if not intervention_valid or not isinstance(intervention, dict):
            return self._blocked(
                intervention_reason or "real_paired_outcome_intervention_invalid"
            )
        task_ids = self._task_ids(source.get("task_ids"))
        if task_ids is None:
            return self._blocked("real_paired_outcome_task_set_invalid")
        if len(task_ids) < PairedQualityEvidenceLedger.MINIMUM_PAIRED_SAMPLES:
            return self._blocked("real_paired_outcome_minimum_pairs_not_met")
        tasks, corpus_manifest = self.corpus.load_for_shadow(
            split="holdout",
            task_ids=task_ids,
        )
        if sorted(str(task["task_id"]) for task in tasks) != task_ids:
            return self._blocked("real_paired_outcome_task_set_unavailable")
        if any(not isinstance(task.get("quality_rubric"), dict) for task in tasks):
            return self._blocked("real_paired_outcome_quality_rubric_required")

        study = self._study(source.get("study"))
        if study is None:
            return self._blocked("real_paired_outcome_study_invalid")
        authority = self._authority(source.get("execution_authority"))
        if authority is None:
            return self._blocked("real_paired_outcome_execution_authority_invalid")
        expected_calls = len(task_ids) * self.EXPECTED_ATTEMPTS_PER_PAIR
        if authority["maximum_adapter_invocations"] < expected_calls:
            return self._blocked("real_paired_outcome_adapter_invocation_cap_too_low")

        provider, provider_reason = self._adapter_contract(self.model_adapter)
        if provider is None:
            return self._blocked(
                provider_reason or "real_paired_outcome_provider_contract_invalid"
            )
        candidate_id = self._safe_identifier(source.get("candidate_id"))
        baseline_candidate_id = self._safe_identifier(
            source.get("baseline_candidate_id")
        )
        if not candidate_id or not baseline_candidate_id:
            return self._blocked("real_paired_outcome_candidate_id_invalid")
        if candidate_id == baseline_candidate_id:
            return self._blocked("real_paired_outcome_candidate_ids_not_distinct")

        plan = build_live_pair_plan(
            task_ids,
            self.plan_nonce_factory(),
            str(intervention["intervention_sha256"]),
        )
        evaluation_contract = {
            "metric_scope": self.METRIC_SCOPE,
            "minimum_paired_samples": PairedQualityEvidenceLedger.MINIMUM_PAIRED_SAMPLES,
            "confidence_level": PairedQualityEvidenceLedger.CONFIDENCE_LEVEL,
            "uncertainty_method": "hoeffding_bounded_paired_delta",
            "human_review_required": True,
            "blind_review_required": True,
            "automatic_promotion": False,
        }
        unsigned = {
            "schema": REAL_PAIRED_OUTCOME_PROGRAM_SCHEMA,
            "kind": "program_registration",
            "study": study,
            "corpus": {
                "task_ids": task_ids,
                "task_count": len(task_ids),
                "task_set_sha256": plan["task_set_sha256"],
                "selected_manifest_sha256": corpus_manifest["hash"],
                "task_contracts": [
                    {
                        "task_id": str(item["id"]),
                        "contract_sha256": str(item["contract_hash"]),
                    }
                    for item in sorted(
                        corpus_manifest["tasks"],
                        key=lambda task: str(task["id"]),
                    )
                ],
                "consent_scope": OperationalEvaluationCorpus.CONSENT_SCOPE,
            },
            "context": {
                "intervention_sha256": intervention["intervention_sha256"],
                "binding_sha256": intervention["binding"]["binding_sha256"],
                "semantic_review_sha256": intervention["binding"][
                    "semantic_review_sha256"
                ],
            },
            "provider": provider,
            "candidates": {
                "baseline_candidate_id": baseline_candidate_id,
                "candidate_id": candidate_id,
            },
            "execution_authority": authority,
            "expected_adapter_invocations": expected_calls,
            "execution_plan": plan,
            "operational_hardening": {
                "attempt_schema": REAL_PAIRED_OUTCOME_ATTEMPT_SCHEMA,
                "atomic_single_claim": True,
                "automatic_retry_after_unknown_attempt": False,
                "timeout_lease_steal_permitted": False,
                "provider_transport_request_schema": (
                    PROVIDER_TRANSPORT_REQUEST_SCHEMA
                    if provider["transport_idempotency_supported"]
                    else None
                ),
                "provider_transport_receipt_schema": (
                    PROVIDER_TRANSPORT_RECEIPT_SCHEMA
                    if provider["transport_idempotency_supported"]
                    else None
                ),
                "provider_recovery_authority_schema": (
                    PROVIDER_RECOVERY_AUTHORITY_SCHEMA
                    if provider["transport_idempotency_supported"]
                    else None
                ),
                "maximum_transport_reconciliation_queries": (
                    expected_calls * MAXIMUM_TRANSPORT_RECONCILIATION_MULTIPLIER
                    if provider["transport_idempotency_supported"]
                    else 0
                ),
                "process_isolated_recovery_supported": provider.get(
                    "process_isolated_recovery_supported", False
                ),
                "process_transport_protocol": provider.get(
                    "process_transport_protocol"
                ),
                "transport_instance_sha256": provider.get(
                    "transport_instance_sha256"
                ),
                "provider_store_authenticated": provider.get(
                    "provider_store_authenticated", False
                ),
                "provider_store_authentication_schema": provider.get(
                    "provider_store_authentication_schema"
                ),
                "provider_store_authentication_key_id": provider.get(
                    "provider_store_authentication_key_id"
                ),
                "leased_recovery_supervisor_supported": provider.get(
                    "leased_recovery_supervisor_supported", False
                ),
                "recovery_lease_schema": provider.get("recovery_lease_schema"),
                "recovery_supervisor_authority_schema": provider.get(
                    "recovery_supervisor_authority_schema"
                ),
                "signed_recovery_authority_supported": provider.get(
                    "signed_recovery_authority_supported", False
                ),
                "recovery_authority_grant_schema": provider.get(
                    "recovery_authority_grant_schema"
                ),
                "recovery_authority_trust_schema": provider.get(
                    "recovery_authority_trust_schema"
                ),
                "recovery_authority_trust_anchor_sha256": provider.get(
                    "recovery_authority_trust_anchor_sha256"
                ),
                "supervisor_attestation_schema": provider.get(
                    "supervisor_attestation_schema"
                ),
            },
            "evaluation_contract": evaluation_contract,
            "claims": self._claim_boundary(),
        }
        program_sha256 = self._digest(unsigned)
        record = {
            **unsigned,
            "program_sha256": program_sha256,
            "id": f"RPOP-{program_sha256[:16]}",
        }
        with self.repository.locked():
            index = self._load_index()
            program_ids = self._program_ids(index)
            if record["id"] in program_ids:
                return self._blocked("real_paired_outcome_program_exists")
            asyncio.run(
                self.repository.save(
                    f"{PROGRAM_RECORD_PREFIX}{record['id']}",
                    record,
                )
            )
            asyncio.run(
                self.repository.save(
                    PROGRAM_INDEX_KEY,
                    {
                        "schema": REAL_PAIRED_OUTCOME_INDEX_SCHEMA,
                        "program_ids": [*program_ids, record["id"]],
                    },
                )
            )
        return self.get(str(record["id"])) or self._blocked(
            "real_paired_outcome_registration_unreadable"
        )

    def preflight(self, program_id: str) -> dict[str, Any]:
        """Check corpus, adapter, and authority without writing state or making calls."""

        registration = self._registration(program_id)
        registration_reason = self._registration_reason(registration, program_id)
        if registration_reason or registration is None:
            return self._blocked(
                registration_reason or "real_paired_outcome_program_not_found"
            )
        provenance = self.repository.verify_provenance()
        if provenance.get("valid") is not True:
            return self._blocked("provenance_invalid")
        corpus_reason = self._current_corpus_reason(registration)
        if corpus_reason:
            return self._blocked(corpus_reason)
        if self._execution(program_id) is not None:
            return self._blocked("real_paired_outcome_execution_already_recorded")
        attempt = self._attempt(program_id)
        attempt_reason = self._attempt_reason(attempt, registration)
        if attempt_reason:
            return self._blocked(attempt_reason)
        if attempt is not None:
            return self._blocked("real_paired_outcome_execution_recovery_required")
        if self.model_adapter is None:
            return self._blocked("real_paired_outcome_adapter_required")
        adapter, adapter_reason = self._adapter_contract(self.model_adapter)
        if adapter is None or adapter != registration["provider"]:
            return self._blocked(
                adapter_reason or "real_paired_outcome_provider_contract_mismatch"
            )
        authority_reason = self._execution_authority_reason(registration, adapter)
        if authority_reason:
            return self._blocked(authority_reason)
        return {
            "schema": REAL_PAIRED_OUTCOME_PROGRAM_SCHEMA,
            "id": program_id,
            "status": "ready",
            "expected_adapter_invocations": registration[
                "expected_adapter_invocations"
            ],
            "external_provider_calls_authorized": registration[
                "execution_authority"
            ]["external_provider_calls_authorized"],
            "paid_provider_calls_authorized": registration["execution_authority"][
                "paid_provider_calls_authorized"
            ],
            "provider": deepcopy(registration["provider"]),
            "state_changed": False,
            "adapter_invocations_executed": 0,
            "claims": self._claim_boundary(),
        }

    def execute(
        self,
        program_id: str,
        *,
        context_intervention: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute one pre-registered study without accepting post-hoc plan changes."""

        if self.repository.read_only:
            return self._blocked("real_paired_outcome_store_read_only")
        registration = self._registration(program_id)
        registration_reason = self._registration_reason(registration, program_id)
        if registration_reason or registration is None:
            return self._blocked(
                registration_reason or "real_paired_outcome_program_not_found"
            )
        valid_intervention, intervention_reason = validate_context_intervention(
            context_intervention
        )
        if not valid_intervention:
            return self._blocked(
                intervention_reason or "real_paired_outcome_intervention_invalid"
            )
        if (
            context_intervention.get("intervention_sha256")
            != registration["context"]["intervention_sha256"]
        ):
            return self._blocked("real_paired_outcome_intervention_mismatch")
        current_reason = self._current_corpus_reason(registration)
        if current_reason:
            return self._blocked(current_reason)
        provenance = self.repository.verify_provenance()
        if provenance.get("valid") is not True:
            return self._blocked("provenance_invalid")
        attempt = self._attempt(program_id)
        attempt_reason = self._attempt_reason(attempt, registration)
        if attempt_reason:
            return self._blocked(attempt_reason)
        execution = self._execution(program_id)
        execution_reason = self._execution_reason(execution, registration, attempt)
        if execution_reason:
            return self._blocked(execution_reason)
        if execution is not None:
            if isinstance(attempt, dict) and attempt.get("state") == "running":
                _, terminal_reason = self.attempts.complete(
                    registration,
                    attempt,
                    outcome=(
                        "completed"
                        if execution.get("status") == "pending_human_review"
                        else "blocked"
                    ),
                    execution_sha256=str(execution["execution_sha256"]),
                    reason=next(iter(execution.get("reasons", [])), None),
                )
                if terminal_reason:
                    return self._blocked(terminal_reason)
                return self.get(program_id) or self._blocked(
                    "real_paired_outcome_execution_unreadable"
                )
            return self._blocked("real_paired_outcome_execution_already_recorded")
        if attempt is not None:
            return self.get(program_id) or self._blocked(
                "real_paired_outcome_execution_recovery_required"
            )
        if self.model_adapter is None:
            return self._blocked("real_paired_outcome_adapter_required")
        adapter, adapter_reason = self._adapter_contract(self.model_adapter)
        if adapter is None or adapter != registration["provider"]:
            return self._blocked(
                adapter_reason or "real_paired_outcome_provider_contract_mismatch"
            )
        authority_reason = self._execution_authority_reason(registration, adapter)
        if authority_reason:
            return self._blocked(authority_reason)

        claim, created, claim_reason = self.attempts.claim(registration)
        if claim_reason:
            return self._blocked(claim_reason)
        if not created or claim is None:
            return self.get(program_id) or self._blocked(
                "real_paired_outcome_execution_recovery_required"
            )
        running, running_reason = self.attempts.mark_running(registration, claim)
        if running_reason or running is None:
            return self._blocked(
                running_reason or "real_paired_outcome_attempt_transition_failed"
            )

        transport_ledger: ProviderTransportLedger | None = None
        execution_adapter = self.model_adapter
        if registration["provider"].get("transport_idempotency_supported") is True:
            transport_ledger = ProviderTransportLedger(
                self.repository,
                registration,
                running,
            )
            execution_adapter = IdempotentTransportAdapter(
                self.model_adapter,
                transport_ledger,
            )
        evaluator = LiveContextPairedEvaluator(
            self.repository,
            self.corpus,
            execution_adapter,
            scoring_nonce_factory=self.scoring_nonce_factory,
        )
        try:
            live = evaluator.run(
                context_intervention=context_intervention,
                candidate_id=str(registration["candidates"]["candidate_id"]),
                baseline_candidate_id=str(
                    registration["candidates"]["baseline_candidate_id"]
                ),
                task_ids=list(registration["corpus"]["task_ids"]),
                execution_plan=deepcopy(registration["execution_plan"]),
            )
        except (ProviderTransportOutcomeUnknown, ProviderTransportRecoveryRequired):
            return self.get(program_id) or self._blocked(
                "real_paired_outcome_execution_recovery_required"
            )
        except Exception as error:
            _, failure_reason = self.attempts.complete(
                registration,
                running,
                outcome="failed",
                execution_sha256=None,
                reason="real_paired_outcome_adapter_execution_failed",
                failure_type=type(error).__name__,
            )
            if failure_reason:
                return self._blocked(failure_reason)
            return self.get(program_id) or self._blocked(
                "real_paired_outcome_adapter_execution_failed"
            )
        return self._record_execution(
            registration,
            running,
            live,
            transport_ledger=transport_ledger,
        )

    def attest_supervisor(
        self,
        program_id: str,
        authority_grant: dict[str, Any],
        supervisor_private_key_file: str,
        *,
        event: str,
        details: dict[str, Any],
        authority_state: dict[str, Any] | None = None,
        rollback_anchor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        registration = self._registration(program_id)
        registration_reason = self._registration_reason(registration, program_id)
        if registration_reason or registration is None:
            raise ValueError(
                registration_reason or "real_paired_outcome_program_not_found"
            )
        if registration["provider"].get(
            "signed_recovery_authority_supported"
        ) is not True:
            raise ValueError("signed_recovery_authority_not_registered")
        return SupervisorAttestationLedger(
            self.repository,
            registration,
            authority_state=authority_state,
            rollback_anchor=rollback_anchor,
        ).append(
            authority_grant,
            supervisor_private_key_file,
            event=event,
            details=details,
        )

    def recover(
        self,
        program_id: str,
        *,
        context_intervention: dict[str, Any],
        recovery_authority: dict[str, Any],
        supervisor_authority: dict[str, Any] | None = None,
        authority_state: dict[str, Any] | None = None,
        rollback_anchor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Explicitly reconcile provider transport before resuming an interrupted run."""

        if self.repository.read_only:
            return self._blocked("real_paired_outcome_store_read_only")
        registration = self._registration(program_id)
        registration_reason = self._registration_reason(registration, program_id)
        if registration_reason or registration is None:
            return self._blocked(
                registration_reason or "real_paired_outcome_program_not_found"
            )
        valid_intervention, intervention_reason = validate_context_intervention(
            context_intervention
        )
        if not valid_intervention:
            return self._blocked(
                intervention_reason or "real_paired_outcome_intervention_invalid"
            )
        if (
            context_intervention.get("intervention_sha256")
            != registration["context"]["intervention_sha256"]
        ):
            return self._blocked("real_paired_outcome_intervention_mismatch")
        if self.repository.verify_provenance().get("valid") is not True:
            return self._blocked("provenance_invalid")
        current_reason = self._current_corpus_reason(registration)
        if current_reason:
            return self._blocked(current_reason)
        attempt = self._attempt(program_id)
        attempt_reason = self._attempt_reason(attempt, registration)
        if attempt_reason or not isinstance(attempt, dict):
            return self._blocked(
                attempt_reason or "real_paired_outcome_attempt_missing"
            )
        if attempt.get("state") != "running":
            return self._blocked("real_paired_outcome_recovery_not_running")
        execution = self._execution(program_id)
        execution_reason = self._execution_reason(execution, registration, attempt)
        if execution_reason:
            return self._blocked(execution_reason)
        if execution is not None:
            return self.execute(
                program_id,
                context_intervention=context_intervention,
            )
        if registration["provider"].get("transport_idempotency_supported") is not True:
            return self._blocked("provider_transport_idempotency_capability_required")
        if self.model_adapter is None:
            return self._blocked("real_paired_outcome_adapter_required")
        adapter, adapter_reason = self._adapter_contract(self.model_adapter)
        if adapter is None or adapter != registration["provider"]:
            return self._blocked(
                adapter_reason or "real_paired_outcome_provider_contract_mismatch"
            )
        authority_reason = self._execution_authority_reason(registration, adapter)
        if authority_reason:
            return self._blocked(authority_reason)

        signed_authority = registration["provider"].get(
            "signed_recovery_authority_supported"
        ) is True
        if signed_authority:
            resource_id = build_recovery_lease_resource_id(
                program_id=program_id,
                attempt_id=str(attempt["attempt_id"]),
                provider_instance_sha256=str(
                    registration["provider"]["transport_instance_sha256"]
                ),
            )
            _, signed_reason = verify_recovery_authority_grant(
                supervisor_authority,
                self._mapping(
                    registration["provider"].get(
                        "recovery_authority_trust_anchor"
                    )
                ),
                expected={
                    "program_id": program_id,
                    "program_sha256": registration["program_sha256"],
                    "attempt_id": attempt["attempt_id"],
                    "provider_instance_sha256": registration["provider"][
                        "transport_instance_sha256"
                    ],
                    "lease_resource_id": resource_id,
                    "transport_recovery_authority_sha256": recovery_authority.get(
                        "authority_sha256"
                    ),
                    "context_intervention_sha256": context_intervention.get(
                        "intervention_sha256"
                    ),
                },
                authority_state=authority_state,
                rollback_anchor=rollback_anchor,
            )
            if signed_reason:
                return self._blocked(signed_reason)
        elif supervisor_authority is not None:
            return self._blocked("signed_recovery_authority_not_registered")

        ledger = ProviderTransportLedger(self.repository, registration, attempt)
        expected_calls = int(registration["expected_adapter_invocations"])
        maximum_queries = int(
            registration["operational_hardening"][
                "maximum_transport_reconciliation_queries"
            ]
        )
        replay_results, reconciliation_reason = ledger.reconcile_existing(
            self.model_adapter,
            maximum_queries=maximum_queries,
        )
        if reconciliation_reason:
            return {
                **(
                    self.get(program_id)
                    or self._blocked(
                        "real_paired_outcome_execution_recovery_required"
                    )
                ),
                "recovery": {
                    "status": "held",
                    "reason": reconciliation_reason,
                    "new_adapter_invocations": 0,
                    "transport": ledger.summary(expected_calls=expected_calls),
                },
            }
        if self._execution(program_id) is not None:
            return self.execute(
                program_id,
                context_intervention=context_intervention,
            )
        recovery, recovery_reason = ledger.claim_recovery(
            recovery_authority,
            expected_calls=expected_calls,
        )
        if recovery_reason or recovery is None:
            return {
                **(
                    self.get(program_id)
                    or self._blocked(
                        "real_paired_outcome_execution_recovery_required"
                    )
                ),
                "recovery": {
                    "status": "held",
                    "reason": recovery_reason or "provider_recovery_claim_failed",
                    "new_adapter_invocations": 0,
                    "transport": ledger.summary(expected_calls=expected_calls),
                },
            }

        execution_adapter = IdempotentTransportAdapter(
            self.model_adapter,
            ledger,
            replay_results=replay_results,
        )
        evaluator = LiveContextPairedEvaluator(
            self.repository,
            self.corpus,
            execution_adapter,
            scoring_nonce_factory=self.scoring_nonce_factory,
        )
        try:
            live = evaluator.run(
                context_intervention=context_intervention,
                candidate_id=str(registration["candidates"]["candidate_id"]),
                baseline_candidate_id=str(
                    registration["candidates"]["baseline_candidate_id"]
                ),
                task_ids=list(registration["corpus"]["task_ids"]),
                execution_plan=deepcopy(registration["execution_plan"]),
                resume_registered_plan=True,
            )
        except (ProviderTransportOutcomeUnknown, ProviderTransportRecoveryRequired):
            terminal_reason = ledger.complete_recovery(
                recovery,
                outcome="transport_interrupted",
                execution_sha256=None,
                expected_calls=expected_calls,
            )
            return {
                **(
                    self.get(program_id)
                    or self._blocked(
                        "real_paired_outcome_execution_recovery_required"
                    )
                ),
                "recovery": {
                    "status": "interrupted",
                    "reason": terminal_reason
                    or "provider_transport_outcome_unknown",
                    "transport": ledger.summary(expected_calls=expected_calls),
                },
            }
        recovered = self._record_execution(
            registration,
            attempt,
            live,
            transport_ledger=ledger,
        )
        stored_execution = self._execution(program_id)
        execution_sha256 = (
            str(stored_execution.get("execution_sha256"))
            if isinstance(stored_execution, dict)
            else None
        )
        terminal_reason = ledger.complete_recovery(
            recovery,
            outcome="execution_completed",
            execution_sha256=execution_sha256,
            expected_calls=expected_calls,
        )
        if terminal_reason:
            return {
                **recovered,
                "recovery": {
                    "status": "blocked",
                    "reason": terminal_reason,
                    "transport": ledger.summary(expected_calls=expected_calls),
                },
            }
        return self.get(program_id) or recovered

    def _record_execution(
        self,
        registration: dict[str, Any],
        running_attempt: dict[str, Any],
        live: dict[str, Any],
        *,
        transport_ledger: ProviderTransportLedger | None = None,
    ) -> dict[str, Any]:
        program_id = str(registration["id"])
        (
            observation_sources,
            observed_providers,
            producer_record_keys,
            recomputed_calls,
        ) = (
            self._execution_observations(live)
        )
        evidence_class, source_reason = self._evidence_class(observation_sources)
        expected_source = registration["provider"]["provider_observation_source"]
        expected_observed_provider = self._expected_observed_provider(registration)
        actual_calls = self._mapping(live.get("execution")).get(
            "total_adapter_attempts"
        )
        expected_calls = registration["expected_adapter_invocations"]
        transport_required = (
            registration["provider"].get("transport_idempotency_supported") is True
        )
        transport_summary = (
            transport_ledger.summary(expected_calls=expected_calls)
            if transport_ledger is not None
            else None
        )
        order_evidence = self._registration_order_evidence(
            program_id,
            str(self._mapping(live.get("execution")).get("experiment_id") or ""),
            producer_record_keys,
            attempt_id=str(running_attempt["attempt_id"]),
        )
        reasons = [
            str(live.get("reason") or "")
            if live.get("status") != "pending_human_review"
            else "",
            source_reason or "",
            (
                "real_paired_outcome_provider_observation_source_mismatch"
                if observation_sources != [expected_source]
                else ""
            ),
            (
                "real_paired_outcome_provider_identity_mismatch"
                if observed_providers != [expected_observed_provider]
                else ""
            ),
            (
                "real_paired_outcome_adapter_invocation_count_mismatch"
                if actual_calls != expected_calls or recomputed_calls != actual_calls
                else ""
            ),
            (
                "real_paired_outcome_registration_order_unverified"
                if order_evidence.get("verified") is not True
                else ""
            ),
            (
                "provider_transport_receipt_set_incomplete"
                if transport_required
                and (
                    not isinstance(transport_summary, dict)
                    or transport_summary.get("receipt_set_complete") is not True
                )
                else ""
            ),
        ]
        reasons = self._dedupe(reasons)
        execution_status = (
            "pending_human_review"
            if not reasons and live.get("status") == "pending_human_review"
            else "blocked"
        )
        execution_unsigned = {
            "schema": REAL_PAIRED_OUTCOME_EXECUTION_SCHEMA,
            "kind": "program_execution",
            "program_id": program_id,
            "program_sha256": registration["program_sha256"],
            "attempt_id": running_attempt["attempt_id"],
            "attempt_running_sha256": running_attempt["attempt_sha256"],
            "status": execution_status,
            "reasons": reasons,
            "evaluation_id": self._mapping(live.get("evaluation")).get("id"),
            "experiment_id": self._mapping(live.get("execution")).get(
                "experiment_id"
            ),
            "execution_manifest_sha256": self._mapping(live.get("execution")).get(
                "execution_manifest_sha256"
            ),
            "execution_record_sha256": self._mapping(live.get("execution")).get(
                "execution_record_sha256"
            ),
            "plan_record_sha256": self._mapping(live.get("execution")).get(
                "plan_record_sha256"
            ),
            "expected_adapter_invocations": expected_calls,
            "actual_adapter_invocations": actual_calls,
            "provider_observation_sources": observation_sources,
            "observed_providers": observed_providers,
            "evidence_class": evidence_class,
            "producer_record_keys": producer_record_keys,
            "registration_order": order_evidence,
            "provider_transport": (
                transport_summary
            ),
        }
        execution = {
            **execution_unsigned,
            "execution_sha256": self._digest(execution_unsigned),
        }
        inserted = asyncio.run(
            self.repository.save_if_absent(
                f"{PROGRAM_EXECUTION_PREFIX}{program_id}", execution
            )
        )
        if not inserted:
            stored = self._execution(program_id)
            if stored != execution:
                return self._blocked("real_paired_outcome_execution_write_conflict")
        _, terminal_reason = self.attempts.complete(
            registration,
            running_attempt,
            outcome=(
                "completed" if execution_status == "pending_human_review" else "blocked"
            ),
            execution_sha256=str(execution["execution_sha256"]),
            reason=next(iter(reasons), None),
        )
        if terminal_reason:
            return self._blocked(terminal_reason)
        return self.get(program_id) or self._blocked(
            "real_paired_outcome_execution_unreadable"
        )

    def review(self, program_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append the existing exact blind-review decision for one program."""

        if self.repository.read_only:
            return self._blocked("real_paired_outcome_store_read_only")
        execution = self._execution(program_id)
        if not isinstance(execution, dict):
            return self._blocked("real_paired_outcome_execution_missing")
        if execution.get("status") != "pending_human_review":
            return self._blocked("real_paired_outcome_not_pending_review")
        evaluation_id = execution.get("evaluation_id")
        if not self._safe_identifier(evaluation_id):
            return self._blocked("real_paired_outcome_evaluation_missing")
        reviewed = self.paired_quality.review(str(evaluation_id), payload)
        operation = self._mapping(reviewed.get("operation"))
        if operation.get("status") == "blocked":
            return {
                **(self.get(program_id) or self._blocked("real_paired_outcome_not_found")),
                "operation": deepcopy(operation),
            }
        return self.get(program_id) or self._blocked(
            "real_paired_outcome_review_unreadable"
        )

    def get(self, program_id: str) -> dict[str, Any] | None:
        registration = self._registration(program_id)
        if registration is None:
            return None
        registration_reason = self._registration_reason(registration, program_id)
        provenance = self.repository.verify_provenance()
        current_reason = (
            registration_reason
            or (None if provenance.get("valid") is True else "provenance_invalid")
            or self._current_corpus_reason(registration)
        )
        attempt = self._attempt(program_id)
        attempt_reason = self._attempt_reason(attempt, registration)
        transport_reason = (
            ProviderTransportLedger(
                self.repository,
                registration,
                attempt,
            ).integrity_reason(
                expected_calls=int(registration["expected_adapter_invocations"])
            )
            if isinstance(attempt, dict)
            and registration["provider"].get("transport_idempotency_supported") is True
            else None
        )
        attestation_summary = (
            SupervisorAttestationLedger(self.repository, registration).summary()
            if registration["provider"].get(
                "signed_recovery_authority_supported"
            )
            is True
            else None
        )
        attestation_reason = (
            str(attestation_summary.get("reason"))
            if isinstance(attestation_summary, dict)
            and attestation_summary.get("integrity_verified") is not True
            else None
        )
        execution = self._execution(program_id)
        execution_reason = self._execution_reason(execution, registration, attempt)
        if (
            current_reason
            or attempt_reason
            or transport_reason
            or attestation_reason
            or execution_reason
        ):
            return self._projection(
                registration,
                execution,
                attempt,
                evaluation=None,
                status="blocked",
                reason=(
                    current_reason
                    or attempt_reason
                    or transport_reason
                    or attestation_reason
                    or execution_reason
                ),
                provenance=provenance,
            )
        if execution is None:
            if isinstance(attempt, dict) and attempt.get("state") in ACTIVE_ATTEMPT_STATES:
                return self._projection(
                    registration,
                    None,
                    attempt,
                    evaluation=None,
                    status="recovery_required",
                    reason="real_paired_outcome_execution_recovery_required",
                    provenance=provenance,
                )
            if isinstance(attempt, dict) and attempt.get("terminal_outcome") == "failed":
                return self._projection(
                    registration,
                    None,
                    attempt,
                    evaluation=None,
                    status="blocked",
                    reason=str(
                        attempt.get("reason")
                        or "real_paired_outcome_adapter_execution_failed"
                    ),
                    provenance=provenance,
                )
            return self._projection(
                registration,
                None,
                attempt,
                evaluation=None,
                status="registered",
                reason=None,
                provenance=provenance,
            )
        if isinstance(attempt, dict) and attempt.get("state") in ACTIVE_ATTEMPT_STATES:
            return self._projection(
                registration,
                execution,
                attempt,
                evaluation=None,
                status="recovery_required",
                reason="real_paired_outcome_execution_finalization_required",
                provenance=provenance,
            )
        evaluation_id = execution.get("evaluation_id")
        evaluation = (
            self.paired_quality.get(str(evaluation_id))
            if self._safe_identifier(evaluation_id)
            else None
        )
        if evaluation is None:
            status, reason = "blocked", "real_paired_outcome_evaluation_missing"
        elif execution.get("status") == "blocked":
            status = "blocked"
            reason = next(
                iter(execution.get("reasons", [])),
                "real_paired_outcome_execution_blocked",
            )
        elif evaluation.get("status") == "pending_human_review":
            status, reason = "pending_human_review", None
        elif evaluation.get("status") == "reviewed_evidence":
            if execution.get("evidence_class") == "mechanism_validation":
                status = "mechanism_validation_only"
                reason = (
                    "local_process_provider_is_not_real_outcome_evidence"
                    if registration["provider"].get(
                        "provider_observation_source"
                    )
                    == PROCESS_MECHANISM_OBSERVATION_SOURCE
                    else "in_process_provider_is_not_real_outcome_evidence"
                )
            elif registration["study"]["workload_class"] == "test_fixture":
                status = "reviewed_fixture_outcome"
                reason = "fixture_workload_is_not_real_outcome_evidence"
            else:
                status, reason = "reviewed_observed_real_workload", None
        elif evaluation.get("status") == "rejected_by_human":
            status, reason = "rejected_by_human", "human_review_rejected"
        else:
            status = "blocked"
            reason = str(
                self._mapping(evaluation.get("revalidation")).get("reason")
                or "real_paired_outcome_evaluation_invalid"
            )
        return self._projection(
            registration,
            execution,
            attempt,
            evaluation=evaluation,
            status=status,
            reason=reason,
            provenance=provenance,
        )

    def history(self) -> list[dict[str, Any]]:
        return [
            record
            for program_id in self._program_ids(self._load_index())
            if (record := self.get(program_id)) is not None
        ]

    def coverage(self) -> dict[str, Any]:
        records = self.history()
        by_status: dict[str, int] = {}
        for record in records:
            status = str(record.get("status") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
        hardened = sum(
            self._mapping(record.get("operational_hardening")).get("assurance")
            in {"attempt_bound", "attempt_and_transport_bound"}
            for record in records
        )
        legacy_unbound = sum(
            self._mapping(record.get("operational_hardening")).get("assurance")
            == "legacy_unbound"
            for record in records
        )
        transport_bound = sum(
            self._mapping(
                self._mapping(record.get("operational_hardening")).get(
                    "provider_transport"
                )
            ).get("receipt_set_complete")
            is True
            for record in records
        )
        return {
            "schema": REAL_PAIRED_OUTCOME_PROGRAM_SCHEMA,
            "total_programs": len(records),
            "reviewed_observed_real_workload_count": by_status.get(
                "reviewed_observed_real_workload", 0
            ),
            "cryptographically_verified_real_outcome_count": 0,
            "attempt_bound_execution_count": hardened,
            "transport_bound_execution_count": transport_bound,
            "recovery_required_count": by_status.get("recovery_required", 0),
            "legacy_unbound_execution_count": legacy_unbound,
            "by_status": by_status,
        }

    def _projection(
        self,
        registration: dict[str, Any],
        execution: dict[str, Any] | None,
        attempt: dict[str, Any] | None,
        *,
        evaluation: dict[str, Any] | None,
        status: str,
        reason: str | None,
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        plan = registration["execution_plan"]
        measurement = self._mapping(
            self._mapping(evaluation).get("measurement")
        )
        task_quality = self._mapping(
            self._mapping(evaluation).get("task_quality")
        )
        remote_observed = (
            isinstance(execution, dict)
            and execution.get("evidence_class") == "remote_transport_observed"
        )
        real_workload = registration["study"]["workload_class"] == "real_user_workload"
        reviewed = self._mapping(evaluation).get("status") == "reviewed_evidence"
        observed_real_outcome = remote_observed and real_workload and reviewed
        attempt_bound = (
            isinstance(attempt, dict)
            and attempt.get("state") == "terminal"
            and isinstance(execution, dict)
            and attempt.get("execution_sha256") == execution.get("execution_sha256")
        )
        transport_ledger = (
            ProviderTransportLedger(self.repository, registration, attempt)
            if isinstance(attempt, dict)
            and registration["provider"].get("transport_idempotency_supported") is True
            else None
        )
        transport_summary = (
            transport_ledger.summary(
                expected_calls=int(registration["expected_adapter_invocations"])
            )
            if transport_ledger is not None
            else None
        )
        transport_bound = (
            attempt_bound
            and isinstance(transport_summary, dict)
            and transport_summary.get("receipt_set_complete") is True
        )
        recovery_projection = (
            transport_ledger.recovery_projection()
            if transport_ledger is not None
            else None
        )
        process_isolated_recovery_observed = bool(
            transport_bound
            and registration["provider"].get(
                "process_isolated_recovery_supported"
            )
            is True
            and isinstance(recovery_projection, dict)
            and int(recovery_projection.get("cycle_count") or 0) > 0
            and recovery_projection.get("latest_outcome") == "execution_completed"
            and recovery_projection.get("verified") is True
        )
        assurance = (
            "attempt_and_transport_bound"
            if transport_bound
            else "attempt_bound"
            if attempt_bound
            else "recovery_required"
            if isinstance(attempt, dict) and attempt.get("state") in ACTIVE_ATTEMPT_STATES
            else "terminal_failure"
            if isinstance(attempt, dict) and attempt.get("terminal_outcome") == "failed"
            else "legacy_unbound"
            if execution is not None
            else "not_started"
        )
        return {
            "schema": REAL_PAIRED_OUTCOME_PROGRAM_SCHEMA,
            "id": registration["id"],
            "status": status,
            "reason": reason,
            "program_sha256": registration["program_sha256"],
            "study": deepcopy(registration["study"]),
            "corpus": deepcopy(registration["corpus"]),
            "context": deepcopy(registration["context"]),
            "provider": deepcopy(registration["provider"]),
            "candidates": deepcopy(registration["candidates"]),
            "execution_authority": deepcopy(registration["execution_authority"]),
            "operational_hardening": {
                "assurance": assurance,
                "attempt_bound": attempt_bound,
                "attempt": (
                    {
                        key: deepcopy(attempt.get(key))
                        for key in (
                            "attempt_id",
                            "state",
                            "transition_index",
                            "adapter_invocations_may_have_started",
                            "automatic_retry_permitted",
                            "terminal_outcome",
                            "reason",
                            "failure_type",
                        )
                    }
                    if attempt is not None
                    else None
                ),
                "transition_provenance": self.attempts.provenance_projection(attempt),
                "automatic_retry_permitted": False,
                "timeout_lease_steal_permitted": False,
                "provider_attempt_count_after_interruption_verified": False,
                "provider_transport": deepcopy(transport_summary),
                "recovery_authority": deepcopy(recovery_projection),
                "process_isolated_recovery_observed": (
                    process_isolated_recovery_observed
                ),
                "provider_store_authentication": {
                    "supported": registration["provider"].get(
                        "provider_store_authenticated", False
                    ),
                    "schema": registration["provider"].get(
                        "provider_store_authentication_schema"
                    ),
                    "key_id": registration["provider"].get(
                        "provider_store_authentication_key_id"
                    ),
                    "external_provider_identity_authenticated": False,
                },
                "recovery_supervisor": {
                    "leased": registration["provider"].get(
                        "leased_recovery_supervisor_supported", False
                    ),
                    "lease_schema": registration["provider"].get(
                        "recovery_lease_schema"
                    ),
                    "authority_schema": registration["provider"].get(
                        "recovery_supervisor_authority_schema"
                    ),
                    "signed_authority_supported": registration["provider"].get(
                        "signed_recovery_authority_supported", False
                    ),
                    "trust_anchor_sha256": registration["provider"].get(
                        "recovery_authority_trust_anchor_sha256"
                    ),
                    "attestation": (
                        SupervisorAttestationLedger(
                            self.repository,
                            registration,
                        ).summary()
                        if registration["provider"].get(
                            "signed_recovery_authority_supported"
                        )
                        is True
                        else None
                    ),
                    "operator_identity_verified": False,
                },
            },
            "preregistration": {
                "registered_before_adapter_invocations": (
                    self._mapping(execution).get("registration_order", {}).get(
                        "verified"
                    )
                    if execution is not None
                    else True
                ),
                "execution_manifest_sha256": plan["manifest_sha256"],
                "randomization_nonce_sha256": plan["randomization_nonce_sha256"],
                "condition_order_balance": deepcopy(plan["condition_order_balance"]),
                "per_task_order_disclosed": False,
                "stop_rule": {
                    "target_pair_count": registration["corpus"]["task_count"],
                    "minimum_pair_count": PairedQualityEvidenceLedger.MINIMUM_PAIRED_SAMPLES,
                    "optional_stopping_permitted": False,
                },
            },
            "execution": (
                {
                    key: deepcopy(execution.get(key))
                    for key in (
                        "status",
                        "reasons",
                        "attempt_id",
                        "attempt_running_sha256",
                        "evaluation_id",
                        "experiment_id",
                        "execution_manifest_sha256",
                        "execution_record_sha256",
                        "plan_record_sha256",
                        "expected_adapter_invocations",
                        "actual_adapter_invocations",
                        "provider_observation_sources",
                        "observed_providers",
                        "evidence_class",
                        "registration_order",
                        "provider_transport",
                    )
                }
                if execution is not None
                else None
            ),
            "blind_review": {
                "surface": deepcopy(
                    self._mapping(evaluation).get("blind_review_surface", {})
                ),
                "source_pairs_disclosed": bool(
                    self._mapping(evaluation).get("source_pairs")
                ),
                "human_review": deepcopy(
                    self._mapping(evaluation).get("human_review", {})
                ),
                "reviewer_identity_cryptographically_verified": False,
                "reviewer_independence_verified": False,
            },
            "outcome": {
                "measurement_available": task_quality.get("available") is True,
                "observed_real_workload_outcome": observed_real_outcome,
                "real_paired_outcome_cryptographically_verified": False,
                "metric_scope": measurement.get("metric_scope"),
                "sample_count": measurement.get("sample_count") if reviewed else 0,
                "baseline_mean": measurement.get("baseline_mean") if reviewed else None,
                "treatment_mean": measurement.get("candidate_mean") if reviewed else None,
                "paired_delta": measurement.get("paired_delta") if reviewed else None,
                "uncertainty": measurement.get("uncertainty") if reviewed else None,
                "positive_effect_supported": (
                    measurement.get("positive_effect_supported") if reviewed else False
                ),
                "workload_provenance_assurance": "self_attested",
                "workload_provenance_verified": False,
                "provider_identity_cryptographically_verified": False,
                "adapter_invocation_count_recomputed": (
                    self._mapping(execution).get("actual_adapter_invocations")
                    if execution is not None
                    else 0
                ),
                "provider_transport_attempt_count_verified": False,
                "provider_transport_attempt_count_observed": bool(transport_bound),
                "process_isolated_recovery_mechanism_observed": (
                    process_isolated_recovery_observed
                ),
            },
            "revalidation": {
                "program_valid": status != "blocked",
                "paired_evaluation_valid": self._mapping(
                    self._mapping(evaluation).get("revalidation")
                ).get("valid"),
                "provenance_valid": provenance.get("valid") is True,
            },
            "claims": self._claim_boundary(),
            "state_provenance": {
                key: provenance.get(key)
                for key in ("valid", "entries", "latest_hash", "key_source")
                if key in provenance
            },
        }

    def _registration_reason(
        self,
        value: dict[str, Any] | None,
        expected_program_id: str,
    ) -> str | None:
        if not isinstance(value, dict):
            return "real_paired_outcome_program_not_found"
        if value.get("schema") != REAL_PAIRED_OUTCOME_PROGRAM_SCHEMA:
            return "real_paired_outcome_program_schema_invalid"
        digest = value.get("program_sha256")
        unsigned = {
            key: item
            for key, item in value.items()
            if key not in {"id", "program_sha256"}
        }
        if not self._valid_sha256(digest) or self._digest(unsigned) != digest:
            return "real_paired_outcome_program_digest_mismatch"
        if value.get("id") != expected_program_id or expected_program_id != f"RPOP-{digest[:16]}":
            return "real_paired_outcome_program_id_mismatch"
        plan_valid, plan_reason = validate_live_pair_plan(value.get("execution_plan"))
        if not plan_valid:
            return plan_reason or "real_paired_outcome_plan_invalid"
        corpus = self._mapping(value.get("corpus"))
        context = self._mapping(value.get("context"))
        plan = self._mapping(value.get("execution_plan"))
        if (
            plan.get("task_ids") != corpus.get("task_ids")
            or plan.get("task_set_sha256") != corpus.get("task_set_sha256")
            or plan.get("context_intervention_sha256")
            != context.get("intervention_sha256")
        ):
            return "real_paired_outcome_program_binding_mismatch"
        if self._authority(value.get("execution_authority")) is None:
            return "real_paired_outcome_execution_authority_invalid"
        provider = self._mapping(value.get("provider"))
        hardening = self._mapping(value.get("operational_hardening"))
        transport_fields_present = any(
            field in provider
            for field in (
                "transport_idempotency_supported",
                "transport_reconciliation_supported",
            )
        )
        if transport_fields_present:
            transport_supported = provider.get("transport_idempotency_supported")
            if (
                not isinstance(transport_supported, bool)
                or provider.get("transport_reconciliation_supported")
                is not transport_supported
            ):
                return "provider_transport_capability_binding_invalid"
            expected_queries = (
                int(value.get("expected_adapter_invocations") or 0)
                * MAXIMUM_TRANSPORT_RECONCILIATION_MULTIPLIER
                if transport_supported
                else 0
            )
            if (
                hardening.get("provider_transport_request_schema")
                != (PROVIDER_TRANSPORT_REQUEST_SCHEMA if transport_supported else None)
                or hardening.get("provider_transport_receipt_schema")
                != (PROVIDER_TRANSPORT_RECEIPT_SCHEMA if transport_supported else None)
                or hardening.get("provider_recovery_authority_schema")
                != (PROVIDER_RECOVERY_AUTHORITY_SCHEMA if transport_supported else None)
                or hardening.get("maximum_transport_reconciliation_queries")
                != expected_queries
            ):
                return "provider_transport_program_binding_mismatch"
        process_fields_present = any(
            field in provider
            for field in (
                "process_isolated_recovery_supported",
                "process_transport_protocol",
                "transport_instance_sha256",
                "provider_store_authenticated",
                "provider_store_authentication_schema",
                "provider_store_authentication_key_id",
                "leased_recovery_supervisor_supported",
                "recovery_lease_schema",
                "recovery_supervisor_authority_schema",
                "signed_recovery_authority_supported",
                "recovery_authority_grant_schema",
                "recovery_authority_trust_schema",
                "recovery_authority_trust_anchor",
                "recovery_authority_trust_anchor_sha256",
                "supervisor_attestation_schema",
            )
        )
        if process_fields_present:
            local_process = provider.get("execution_environment") == "local_process"
            authentication_fields = (
                "provider_store_authenticated",
                "provider_store_authentication_schema",
                "provider_store_authentication_key_id",
                "leased_recovery_supervisor_supported",
                "recovery_lease_schema",
                "recovery_supervisor_authority_schema",
            )
            authenticated_process = any(
                field in provider for field in authentication_fields
            )
            signed_authority_fields = (
                "signed_recovery_authority_supported",
                "recovery_authority_grant_schema",
                "recovery_authority_trust_schema",
                "recovery_authority_trust_anchor",
                "recovery_authority_trust_anchor_sha256",
                "supervisor_attestation_schema",
            )
            signed_process = any(
                field in provider for field in signed_authority_fields
            ) and provider.get("signed_recovery_authority_supported") is True
            if local_process:
                if (
                    provider.get("process_isolated_recovery_supported") is not True
                    or not self._valid_sha256(
                        provider.get("transport_instance_sha256")
                    )
                    or provider.get("transport_idempotency_supported") is not True
                ):
                    return "process_transport_capability_binding_invalid"
                if authenticated_process:
                    stateful_signed_process = bool(
                        signed_process
                        and self._mapping(
                            provider.get("recovery_authority_trust_anchor")
                        ).get("authority_state_required")
                        is True
                    )
                    expected_protocol = (
                        "subprocess-stdio-sqlite-hmac-lease-pki-state-v4"
                        if stateful_signed_process
                        else (
                            "subprocess-stdio-sqlite-hmac-lease-pki-v3"
                            if signed_process
                            else "subprocess-stdio-sqlite-hmac-lease-v2"
                        )
                    )
                    if (
                        provider.get("process_transport_protocol")
                        != expected_protocol
                        or provider.get("provider_store_authenticated") is not True
                        or provider.get("provider_store_authentication_schema")
                        != "spst-process-provider-store-authentication-v1"
                        or not self._valid_sha256(
                            provider.get("provider_store_authentication_key_id")
                        )
                        or provider.get("leased_recovery_supervisor_supported")
                        is not True
                        or provider.get("recovery_lease_schema")
                        != "spst-process-recovery-lease-v1"
                        or provider.get("recovery_supervisor_authority_schema")
                        != "spst-process-recovery-supervisor-authority-v1"
                    ):
                        return "process_transport_capability_binding_invalid"
                    if signed_process:
                        trust_anchor = provider.get(
                            "recovery_authority_trust_anchor"
                        )
                        if (
                            validate_recovery_authority_trust_anchor(
                                trust_anchor
                            )
                            is not None
                            or provider.get("recovery_authority_grant_schema")
                            != RECOVERY_AUTHORITY_GRANT_SCHEMA
                            or provider.get("recovery_authority_trust_schema")
                            != RECOVERY_AUTHORITY_TRUST_SCHEMA
                            or provider.get("recovery_authority_trust_anchor_sha256")
                            != self._mapping(trust_anchor).get(
                                "trust_anchor_sha256"
                            )
                            or provider.get("supervisor_attestation_schema")
                            != SUPERVISOR_ATTESTATION_SCHEMA
                        ):
                            return "process_recovery_authority_capability_invalid"
                    elif any(
                        provider.get(field) is not None
                        for field in signed_authority_fields
                        if field != "signed_recovery_authority_supported"
                    ):
                        return "process_recovery_authority_scope_invalid"
                elif (
                    provider.get("process_transport_protocol")
                    != "subprocess-stdio-sqlite-v1"
                ):
                    return "process_transport_capability_binding_invalid"
            elif (
                provider.get("process_isolated_recovery_supported") is not False
                or provider.get("process_transport_protocol") is not None
                or provider.get("transport_instance_sha256") is not None
                or provider.get("provider_store_authenticated") is not False
                or provider.get("provider_store_authentication_schema") is not None
                or provider.get("provider_store_authentication_key_id") is not None
                or provider.get("leased_recovery_supervisor_supported") is not False
                or provider.get("recovery_lease_schema") is not None
                or provider.get("recovery_supervisor_authority_schema") is not None
                or provider.get("signed_recovery_authority_supported") is not False
                or provider.get("recovery_authority_grant_schema") is not None
                or provider.get("recovery_authority_trust_schema") is not None
                or provider.get("recovery_authority_trust_anchor") is not None
                or provider.get("recovery_authority_trust_anchor_sha256") is not None
                or provider.get("supervisor_attestation_schema") is not None
            ):
                return "process_transport_capability_scope_invalid"
            if (
                hardening.get("process_isolated_recovery_supported")
                != provider.get("process_isolated_recovery_supported")
                or hardening.get("process_transport_protocol")
                != provider.get("process_transport_protocol")
                or hardening.get("transport_instance_sha256")
                != provider.get("transport_instance_sha256")
            ):
                return "process_transport_program_binding_mismatch"
            if authenticated_process and any(
                hardening.get(field) != provider.get(field)
                for field in authentication_fields
            ):
                return "process_transport_program_binding_mismatch"
            if signed_process and any(
                hardening.get(field) != provider.get(field)
                for field in signed_authority_fields
                if field != "recovery_authority_trust_anchor"
            ):
                return "process_recovery_authority_program_binding_mismatch"
        return None

    def _execution_reason(
        self,
        value: dict[str, Any] | None,
        registration: dict[str, Any],
        attempt: dict[str, Any] | None,
    ) -> str | None:
        if value is None:
            return None
        schema = value.get("schema")
        if schema not in {
            REAL_PAIRED_OUTCOME_EXECUTION_SCHEMA,
            LEGACY_REAL_PAIRED_OUTCOME_EXECUTION_SCHEMA,
        }:
            return "real_paired_outcome_execution_schema_invalid"
        digest = value.get("execution_sha256")
        unsigned = {
            key: item for key, item in value.items() if key != "execution_sha256"
        }
        if not self._valid_sha256(digest) or self._digest(unsigned) != digest:
            return "real_paired_outcome_execution_digest_mismatch"
        if (
            value.get("program_id") != registration.get("id")
            or value.get("program_sha256") != registration.get("program_sha256")
            or value.get("execution_manifest_sha256")
            != self._mapping(registration.get("execution_plan")).get("manifest_sha256")
        ):
            return "real_paired_outcome_execution_binding_mismatch"
        if schema == REAL_PAIRED_OUTCOME_EXECUTION_SCHEMA:
            if not isinstance(attempt, dict):
                return "real_paired_outcome_attempt_missing"
            running_sha256 = (
                attempt.get("attempt_sha256")
                if attempt.get("state") == "running"
                else attempt.get("running_attempt_sha256")
            )
            if (
                value.get("attempt_id") != attempt.get("attempt_id")
                or value.get("attempt_running_sha256") != running_sha256
            ):
                return "real_paired_outcome_attempt_execution_binding_mismatch"
        elif attempt is not None:
            return "real_paired_outcome_legacy_execution_attempt_conflict"
        transport_required = (
            registration["provider"].get("transport_idempotency_supported") is True
        )
        if transport_required:
            if not isinstance(attempt, dict):
                return "real_paired_outcome_attempt_missing"
            transport_summary = ProviderTransportLedger(
                self.repository,
                registration,
                attempt,
            ).summary(expected_calls=int(registration["expected_adapter_invocations"]))
            if (
                transport_summary.get("receipt_set_complete") is not True
                or value.get("provider_transport") != transport_summary
            ):
                return "provider_transport_execution_binding_mismatch"
        elif value.get("provider_transport") is not None:
            return "provider_transport_unregistered_evidence"
        live_record_reason = self._live_record_reason(value, registration)
        if live_record_reason:
            return live_record_reason
        actual_sources, actual_providers, actual_keys, actual_calls = (
            self._execution_observations(
            {
                "execution": {
                    "experiment_id": value.get("experiment_id"),
                }
            }
            )
        )
        if (
            actual_sources != value.get("provider_observation_sources")
            or actual_providers != value.get("observed_providers")
            or actual_keys != value.get("producer_record_keys")
            or actual_calls != value.get("actual_adapter_invocations")
            or actual_calls != registration.get("expected_adapter_invocations")
        ):
            return "real_paired_outcome_execution_recomputation_mismatch"
        if actual_sources != [registration["provider"]["provider_observation_source"]]:
            return "real_paired_outcome_provider_observation_source_mismatch"
        if actual_providers != [self._expected_observed_provider(registration)]:
            return "real_paired_outcome_provider_identity_mismatch"
        evidence_class, reason = self._evidence_class(
            actual_sources
        )
        if reason or evidence_class != value.get("evidence_class"):
            return reason or "real_paired_outcome_evidence_class_mismatch"
        order = self._registration_order_evidence(
            str(registration["id"]),
            str(value.get("experiment_id") or ""),
            value.get("producer_record_keys"),
            attempt_id=(
                str(value.get("attempt_id"))
                if schema == REAL_PAIRED_OUTCOME_EXECUTION_SCHEMA
                else None
            ),
        )
        if order != value.get("registration_order") or order.get("verified") is not True:
            return "real_paired_outcome_registration_order_unverified"
        if (
            schema == REAL_PAIRED_OUTCOME_EXECUTION_SCHEMA
            and isinstance(attempt, dict)
            and attempt.get("state") == "terminal"
            and attempt.get("execution_sha256") != value.get("execution_sha256")
        ):
            return "real_paired_outcome_attempt_execution_binding_mismatch"
        return None

    def _live_record_reason(
        self,
        execution: dict[str, Any],
        registration: dict[str, Any],
    ) -> str | None:
        experiment_id = execution.get("experiment_id")
        if not self._safe_identifier(experiment_id):
            return "real_paired_outcome_experiment_id_invalid"
        plan_record = asyncio.run(
            self.repository.load(f"runtime:live_context_plan:{experiment_id}")
        )
        if not isinstance(plan_record, dict):
            return "real_paired_outcome_live_plan_missing"
        plan_digest = plan_record.get("record_sha256")
        plan_unsigned = {
            key: item for key, item in plan_record.items() if key != "record_sha256"
        }
        if (
            plan_record.get("schema") != LIVE_CONTEXT_PLAN_RECORD_SCHEMA
            or not self._valid_sha256(plan_digest)
            or canonical_live_pair_hash(plan_unsigned) != plan_digest
            or plan_record.get("manifest") != registration.get("execution_plan")
            or plan_digest != execution.get("plan_record_sha256")
        ):
            return "real_paired_outcome_live_plan_mismatch"
        live_record = asyncio.run(
            self.repository.load(f"runtime:live_context_execution:{experiment_id}")
        )
        if not isinstance(live_record, dict):
            return "real_paired_outcome_live_execution_missing"
        live_digest = live_record.get("record_sha256")
        live_unsigned = {
            key: item for key, item in live_record.items() if key != "record_sha256"
        }
        if (
            live_record.get("schema") != LIVE_CONTEXT_EVALUATION_SCHEMA
            or not self._valid_sha256(live_digest)
            or canonical_live_pair_hash(live_unsigned) != live_digest
            or live_record.get("manifest") != registration.get("execution_plan")
            or live_record.get("evaluation_id") != execution.get("evaluation_id")
            or live_digest != execution.get("execution_record_sha256")
        ):
            return "real_paired_outcome_live_execution_mismatch"
        return None

    def _current_corpus_reason(self, registration: dict[str, Any]) -> str | None:
        corpus = self._mapping(registration.get("corpus"))
        task_ids = corpus.get("task_ids")
        if not isinstance(task_ids, list):
            return "real_paired_outcome_task_set_invalid"
        tasks, manifest = self.corpus.load_for_shadow(
            split="holdout",
            task_ids=list(task_ids),
        )
        if sorted(str(task["task_id"]) for task in tasks) != task_ids:
            return "real_paired_outcome_task_set_stale"
        current_contracts = [
            {
                "task_id": str(item.get("id")),
                "contract_sha256": str(item.get("contract_hash")),
            }
            for item in sorted(
                manifest.get("tasks", []),
                key=lambda task: str(task.get("id")),
            )
            if isinstance(item, dict)
        ]
        if (
            manifest.get("hash") != corpus.get("selected_manifest_sha256")
            or current_contracts != corpus.get("task_contracts")
        ):
            return "real_paired_outcome_corpus_manifest_mismatch"
        return None

    def _execution_authority_reason(
        self,
        registration: dict[str, Any],
        adapter: dict[str, Any],
    ) -> str | None:
        authority = registration["execution_authority"]
        if (
            adapter["execution_environment"] == "external_network"
            and authority["external_provider_calls_authorized"] is not True
        ):
            return "external_provider_calls_not_authorized"
        if (
            adapter["billing_class"] != "no_charge"
            and authority["paid_provider_calls_authorized"] is not True
        ):
            return "paid_or_unknown_provider_calls_not_authorized"
        if authority["maximum_adapter_invocations"] < registration[
            "expected_adapter_invocations"
        ]:
            return "real_paired_outcome_adapter_invocation_cap_too_low"
        health = self.model_adapter.health() if self.model_adapter is not None else {}
        if health.get("ok") is not True:
            return "real_paired_outcome_provider_unavailable"
        return None

    def _adapter_contract(
        self,
        adapter: ModelAdapter,
    ) -> tuple[dict[str, Any] | None, str | None]:
        health = adapter.health()
        capabilities = adapter.get_capabilities()
        if capabilities.get("supports_provider_observation") is not True:
            return None, "provider_observation_capability_required"
        observation_source = capabilities.get("provider_observation_source")
        execution_environment = capabilities.get("execution_environment")
        billing_class = capabilities.get("billing_class")
        transport_idempotency = (
            capabilities.get("supports_transport_idempotency") is True
        )
        transport_reconciliation = (
            capabilities.get("supports_transport_reconciliation") is True
        )
        process_isolated_recovery = (
            capabilities.get("supports_process_isolated_recovery") is True
        )
        process_transport_protocol = capabilities.get("process_transport_protocol")
        transport_instance_sha256 = capabilities.get("transport_instance_sha256")
        authenticated_provider_store = (
            capabilities.get("supports_authenticated_provider_store") is True
        )
        provider_store_authentication_schema = capabilities.get(
            "provider_store_authentication_schema"
        )
        provider_store_authentication_key_id = capabilities.get(
            "provider_store_authentication_key_id"
        )
        leased_recovery_supervisor = (
            capabilities.get("supports_leased_recovery_supervisor") is True
        )
        recovery_lease_schema = capabilities.get("recovery_lease_schema")
        recovery_supervisor_authority_schema = capabilities.get(
            "recovery_supervisor_authority_schema"
        )
        signed_recovery_authority = (
            capabilities.get("supports_signed_recovery_authority") is True
        )
        recovery_authority_grant_schema = capabilities.get(
            "recovery_authority_grant_schema"
        )
        recovery_authority_trust_schema = capabilities.get(
            "recovery_authority_trust_schema"
        )
        recovery_authority_trust_anchor = capabilities.get(
            "recovery_authority_trust_anchor"
        )
        recovery_authority_trust_anchor_sha256 = capabilities.get(
            "recovery_authority_trust_anchor_sha256"
        )
        supervisor_attestation_schema = capabilities.get(
            "supervisor_attestation_schema"
        )
        health_transport_instance = health.get("provider_instance_sha256")
        health_authentication_key_id = health.get(
            "provider_store_authentication_key_id"
        )
        health_recovery_authority_trust_sha256 = health.get(
            "recovery_authority_trust_anchor_sha256"
        )
        provider_name = health.get("provider") or health.get("active_provider")
        model_version = health.get("model_version") or health.get("model")
        if observation_source not in PROVIDER_OBSERVATION_SOURCES:
            return None, "real_paired_outcome_observation_source_required"
        if execution_environment not in EXECUTION_ENVIRONMENTS:
            return None, "real_paired_outcome_execution_environment_required"
        if billing_class not in BILLING_CLASSES:
            return None, "real_paired_outcome_billing_class_required"
        if transport_idempotency is not transport_reconciliation:
            return None, "provider_transport_capability_incomplete"
        if transport_idempotency and not callable(
            getattr(adapter, "reconcile_transport", None)
        ):
            return None, "provider_transport_reconciliation_method_required"
        if execution_environment == "local_process":
            stateful_recovery_authority = bool(
                signed_recovery_authority
                and self._mapping(recovery_authority_trust_anchor).get(
                    "authority_state_required"
                )
                is True
            )
            expected_process_protocol = (
                "subprocess-stdio-sqlite-hmac-lease-pki-state-v4"
                if stateful_recovery_authority
                else (
                    "subprocess-stdio-sqlite-hmac-lease-pki-v3"
                    if signed_recovery_authority
                    else "subprocess-stdio-sqlite-hmac-lease-v2"
                )
            )
            if (
                not transport_idempotency
                or not process_isolated_recovery
                or process_transport_protocol
                != expected_process_protocol
                or not self._valid_sha256(transport_instance_sha256)
                or health_transport_instance != transport_instance_sha256
                or not authenticated_provider_store
                or provider_store_authentication_schema
                != "spst-process-provider-store-authentication-v1"
                or not self._valid_sha256(provider_store_authentication_key_id)
                or health_authentication_key_id
                != provider_store_authentication_key_id
                or not leased_recovery_supervisor
                or recovery_lease_schema != "spst-process-recovery-lease-v1"
                or recovery_supervisor_authority_schema
                != "spst-process-recovery-supervisor-authority-v1"
            ):
                return None, "process_transport_capability_binding_invalid"
            trust_reason = (
                validate_recovery_authority_trust_anchor(
                    recovery_authority_trust_anchor
                )
                if signed_recovery_authority
                else None
            )
            if signed_recovery_authority:
                if (
                    trust_reason
                    or recovery_authority_grant_schema
                    != RECOVERY_AUTHORITY_GRANT_SCHEMA
                    or recovery_authority_trust_schema
                    != RECOVERY_AUTHORITY_TRUST_SCHEMA
                    or not self._valid_sha256(
                        recovery_authority_trust_anchor_sha256
                    )
                    or self._mapping(recovery_authority_trust_anchor).get(
                        "trust_anchor_sha256"
                    )
                    != recovery_authority_trust_anchor_sha256
                    or health_recovery_authority_trust_sha256
                    != recovery_authority_trust_anchor_sha256
                    or health.get("recovery_authority_trust_verified") is not True
                    or supervisor_attestation_schema
                    != SUPERVISOR_ATTESTATION_SCHEMA
                ):
                    return None, "process_recovery_authority_capability_invalid"
            elif any(
                value is not None
                for value in (
                    recovery_authority_grant_schema,
                    recovery_authority_trust_schema,
                    recovery_authority_trust_anchor,
                    recovery_authority_trust_anchor_sha256,
                    supervisor_attestation_schema,
                    health_recovery_authority_trust_sha256,
                )
            ):
                return None, "process_recovery_authority_scope_invalid"
        elif (
            process_isolated_recovery
            or authenticated_provider_store
            or leased_recovery_supervisor
            or signed_recovery_authority
            or any(
            value is not None
            for value in (
                process_transport_protocol,
                transport_instance_sha256,
                health_transport_instance,
                provider_store_authentication_schema,
                provider_store_authentication_key_id,
                health_authentication_key_id,
                recovery_lease_schema,
                recovery_supervisor_authority_schema,
                recovery_authority_grant_schema,
                recovery_authority_trust_schema,
                recovery_authority_trust_anchor,
                recovery_authority_trust_anchor_sha256,
                supervisor_attestation_schema,
                health_recovery_authority_trust_sha256,
            )
            )
        ):
            return None, "process_transport_capability_scope_invalid"
        if not self._safe_identifier(provider_name) or not self._safe_identifier(
            model_version
        ):
            return None, "real_paired_outcome_provider_identity_invalid"
        return (
            {
                "name": str(provider_name),
                "model_version": str(model_version),
                "provider_observation_source": str(observation_source),
                "execution_environment": str(execution_environment),
                "billing_class": str(billing_class),
                "transport_idempotency_supported": transport_idempotency,
                "transport_reconciliation_supported": transport_reconciliation,
                "requires_api_key": bool(health.get("requires_api_key", False)),
                "adapter_declaration_authenticated": False,
                "provider_identity_cryptographically_verified": False,
                "process_isolated_recovery_supported": process_isolated_recovery,
                "process_transport_protocol": (
                    str(process_transport_protocol)
                    if process_transport_protocol is not None
                    else None
                ),
                "transport_instance_sha256": (
                    str(transport_instance_sha256)
                    if transport_instance_sha256 is not None
                    else None
                ),
                "provider_store_authenticated": authenticated_provider_store,
                "provider_store_authentication_schema": (
                    str(provider_store_authentication_schema)
                    if provider_store_authentication_schema is not None
                    else None
                ),
                "provider_store_authentication_key_id": (
                    str(provider_store_authentication_key_id)
                    if provider_store_authentication_key_id is not None
                    else None
                ),
                "leased_recovery_supervisor_supported": (
                    leased_recovery_supervisor
                ),
                "recovery_lease_schema": (
                    str(recovery_lease_schema)
                    if recovery_lease_schema is not None
                    else None
                ),
                "recovery_supervisor_authority_schema": (
                    str(recovery_supervisor_authority_schema)
                    if recovery_supervisor_authority_schema is not None
                    else None
                ),
                "signed_recovery_authority_supported": (
                    signed_recovery_authority
                ),
                "recovery_authority_grant_schema": (
                    str(recovery_authority_grant_schema)
                    if recovery_authority_grant_schema is not None
                    else None
                ),
                "recovery_authority_trust_schema": (
                    str(recovery_authority_trust_schema)
                    if recovery_authority_trust_schema is not None
                    else None
                ),
                "recovery_authority_trust_anchor": (
                    deepcopy(recovery_authority_trust_anchor)
                    if isinstance(recovery_authority_trust_anchor, dict)
                    else None
                ),
                "recovery_authority_trust_anchor_sha256": (
                    str(recovery_authority_trust_anchor_sha256)
                    if recovery_authority_trust_anchor_sha256 is not None
                    else None
                ),
                "supervisor_attestation_schema": (
                    str(supervisor_attestation_schema)
                    if supervisor_attestation_schema is not None
                    else None
                ),
            },
            None,
        )

    def _execution_observations(
        self,
        live: dict[str, Any],
    ) -> tuple[list[str], list[dict[str, str]], list[str], int]:
        execution = self._mapping(live.get("execution"))
        experiment_id = execution.get("experiment_id")
        if not self._safe_identifier(experiment_id):
            return [], [], [], 0
        record = asyncio.run(
            self.repository.load(f"runtime:live_context_execution:{experiment_id}")
        )
        pairs = self._mapping(record).get("pairs")
        if not isinstance(pairs, list):
            return [], [], [], 0
        sources: list[str] = []
        observed_providers: list[dict[str, str]] = []
        record_keys: list[str] = []
        adapter_attempts = 0
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            for arm in ("baseline", "candidate"):
                reference = self._mapping(pair.get(arm))
                run_id = reference.get("producer_run_id")
                binding_id = reference.get("binding_id")
                if not self._safe_identifier(run_id) or not self._safe_identifier(
                    binding_id
                ):
                    continue
                record_key = f"runtime:operational_shadow:{run_id}"
                report = asyncio.run(self.repository.load(record_key))
                cases = self._mapping(report).get("cases")
                if isinstance(cases, list):
                    adapter_attempts += len(cases) * 2
                bindings = self._mapping(report).get("producer_evidence")
                if not isinstance(bindings, list):
                    continue
                binding = next(
                    (
                        item
                        for item in bindings
                        if isinstance(item, dict) and item.get("binding_id") == binding_id
                    ),
                    None,
                )
                observation = self._mapping(
                    self._mapping(binding).get("provider_observation")
                )
                source = observation.get("observation_source")
                if isinstance(source, str):
                    sources.append(source)
                provider = self._mapping(observation.get("provider"))
                provider_name = provider.get("name")
                model_version = provider.get("model_version")
                if (
                    isinstance(source, str)
                    and isinstance(provider_name, str)
                    and isinstance(model_version, str)
                ):
                    observed_providers.append(
                        {
                            "name": provider_name,
                            "model_version": model_version,
                            "provider_observation_source": source,
                        }
                    )
                record_keys.append(record_key)
        return (
            sorted(set(sources)),
            self._dedupe_mappings(observed_providers),
            sorted(set(record_keys)),
            adapter_attempts,
        )

    @staticmethod
    def _expected_observed_provider(registration: dict[str, Any]) -> dict[str, str]:
        provider = registration["provider"]
        return {
            "name": str(provider["name"]),
            "model_version": str(provider["model_version"]),
            "provider_observation_source": str(
                provider["provider_observation_source"]
            ),
        }

    @staticmethod
    def _dedupe_mappings(values: list[dict[str, str]]) -> list[dict[str, str]]:
        keyed = {
            json.dumps(value, sort_keys=True, separators=(",", ":")): value
            for value in values
        }
        return [deepcopy(keyed[key]) for key in sorted(keyed)]

    def _registration_order_evidence(
        self,
        program_id: str,
        experiment_id: str,
        producer_record_keys: Any,
        *,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        keys = (
            [str(item) for item in producer_record_keys]
            if isinstance(producer_record_keys, list)
            and all(isinstance(item, str) for item in producer_record_keys)
            else []
        )
        entries = self.repository.provenance_entries()
        sequences: dict[str, list[int]] = {}
        for entry in entries:
            sequences.setdefault(str(entry["record_key"]), []).append(
                int(entry["sequence"])
            )
        registration_values = sequences.get(f"{PROGRAM_RECORD_PREFIX}{program_id}", [])
        plan_values = sequences.get(f"runtime:live_context_plan:{experiment_id}", [])
        producer_values = [
            min(sequences.get(key, [0])) for key in keys if sequences.get(key)
        ]
        registration_sequence = min(registration_values) if registration_values else None
        plan_sequence = min(plan_values) if plan_values else None
        first_producer_sequence = min(producer_values) if producer_values else None
        base_verified = (
            isinstance(registration_sequence, int)
            and isinstance(plan_sequence, int)
            and isinstance(first_producer_sequence, int)
            and registration_sequence < plan_sequence < first_producer_sequence
        )
        if attempt_id is None:
            return {
                "verified": base_verified,
                "registration_sequence": registration_sequence,
                "plan_sequence": plan_sequence,
                "first_producer_sequence": first_producer_sequence,
                "producer_record_count": len(keys),
            }
        attempt_values = sequences.get(f"{PROGRAM_ATTEMPT_PREFIX}{program_id}", [])
        claim_sequence = attempt_values[0] if attempt_values else None
        running_sequence = attempt_values[1] if len(attempt_values) > 1 else None
        verified = (
            base_verified
            and isinstance(registration_sequence, int)
            and isinstance(claim_sequence, int)
            and isinstance(running_sequence, int)
            and isinstance(plan_sequence, int)
            and registration_sequence < claim_sequence < running_sequence < plan_sequence
        )
        return {
            "verified": verified,
            "registration_sequence": registration_sequence,
            "attempt_id": attempt_id,
            "attempt_claim_sequence": claim_sequence,
            "attempt_running_sequence": running_sequence,
            "plan_sequence": plan_sequence,
            "first_producer_sequence": first_producer_sequence,
            "producer_record_count": len(keys),
        }

    @staticmethod
    def _evidence_class(sources: Any) -> tuple[str, str | None]:
        if not isinstance(sources, list) or not sources:
            return "unresolved", "real_paired_outcome_observation_sources_missing"
        if len(sources) != 1:
            return "unresolved", "real_paired_outcome_observation_sources_mixed"
        if sources[0] in {
            MECHANISM_OBSERVATION_SOURCE,
            PROCESS_MECHANISM_OBSERVATION_SOURCE,
        }:
            return "mechanism_validation", None
        if sources[0] == REMOTE_OBSERVATION_SOURCE:
            return "remote_transport_observed", None
        return "unresolved", "real_paired_outcome_observation_source_invalid"

    def _registration(self, program_id: str) -> dict[str, Any] | None:
        if not self._safe_identifier(program_id):
            return None
        stored = asyncio.run(
            self.repository.load(f"{PROGRAM_RECORD_PREFIX}{program_id}")
        )
        return stored if isinstance(stored, dict) else None

    def _execution(self, program_id: str) -> dict[str, Any] | None:
        if not self._safe_identifier(program_id):
            return None
        stored = asyncio.run(
            self.repository.load(f"{PROGRAM_EXECUTION_PREFIX}{program_id}")
        )
        return stored if isinstance(stored, dict) else None

    def _attempt(self, program_id: str) -> dict[str, Any] | None:
        return self.attempts.get(program_id)

    def _attempt_reason(
        self,
        value: dict[str, Any] | None,
        registration: dict[str, Any],
    ) -> str | None:
        if value is None:
            return None
        return self.attempts.validation_reason(
            value, registration
        ) or self.attempts.provenance_reason(value)

    def _load_index(self) -> dict[str, Any]:
        stored = asyncio.run(self.repository.load(PROGRAM_INDEX_KEY))
        if stored is None:
            return {"schema": REAL_PAIRED_OUTCOME_INDEX_SCHEMA, "program_ids": []}
        if (
            not isinstance(stored, dict)
            or stored.get("schema") != REAL_PAIRED_OUTCOME_INDEX_SCHEMA
            or not isinstance(stored.get("program_ids"), list)
        ):
            raise ValueError("real_paired_outcome_index_invalid")
        return stored

    @staticmethod
    def _program_ids(index: dict[str, Any]) -> list[str]:
        values = index.get("program_ids", [])
        return [str(item) for item in values if isinstance(item, str)]

    @staticmethod
    def _task_ids(value: Any) -> list[str] | None:
        if not isinstance(value, list) or not value:
            return None
        normalized = sorted(str(item) for item in value)
        if (
            len(normalized) != len(value)
            or len(normalized) != len(set(normalized))
            or any(not _identifier.fullmatch(item) for item in normalized)
        ):
            return None
        return normalized

    @staticmethod
    def _study(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict) or set(value) != {"workload_class"}:
            return None
        workload_class = value.get("workload_class")
        if workload_class not in WORKLOAD_CLASSES:
            return None
        return {
            "workload_class": workload_class,
            "workload_source_assurance": "operator_self_attested",
            "workload_source_cryptographically_verified": False,
        }

    @staticmethod
    def _authority(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict) or set(value) != {
            "external_provider_calls_authorized",
            "maximum_adapter_invocations",
            "paid_provider_calls_authorized",
            "schema",
        }:
            return None
        maximum = value.get("maximum_adapter_invocations")
        if (
            value.get("schema") != PROVIDER_EXECUTION_AUTHORITY_SCHEMA
            or not isinstance(value.get("external_provider_calls_authorized"), bool)
            or not isinstance(value.get("paid_provider_calls_authorized"), bool)
            or not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or maximum < 1
            or maximum > 4096
        ):
            return None
        return deepcopy(value)

    @staticmethod
    def _claim_boundary() -> dict[str, bool]:
        return {
            "model_weight_change_claimed": False,
            "general_model_quality_claimed": False,
            "causal_context_utility_established": False,
            "provider_identity_cryptographically_verified": False,
            "reviewer_identity_cryptographically_verified": False,
            "workload_provenance_cryptographically_verified": False,
            "provider_transport_attempt_count_verified": False,
            "automatic_promotion": False,
        }

    @staticmethod
    def _blocked(reason: str) -> dict[str, Any]:
        return {
            "schema": REAL_PAIRED_OUTCOME_PROGRAM_SCHEMA,
            "status": "blocked",
            "reason": reason,
            "outcome": {
                "measurement_available": False,
                "observed_real_workload_outcome": False,
                "real_paired_outcome_cryptographically_verified": False,
            },
            "claims": RealPairedOutcomeProgram._claim_boundary(),
        }

    @staticmethod
    def _safe_identifier(value: Any) -> str:
        candidate = str(value or "")
        return candidate if _identifier.fullmatch(candidate) else ""

    @staticmethod
    def _valid_sha256(value: Any) -> bool:
        return isinstance(value, str) and bool(_sha256.fullmatch(value))

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))

    @staticmethod
    def _digest(value: Any) -> str:
        return canonical_live_pair_hash(value)
