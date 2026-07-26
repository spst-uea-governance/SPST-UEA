import asyncio
from copy import deepcopy
import hashlib
import json
import re
from typing import Any

from spst_runtime.context_review import (
    ContextSemanticReviewError,
    ContextSemanticReviewLedger,
    producer_context_binding,
)
from spst_runtime.evaluation.paired_quality import PairedQualityEvidenceLedger
from spst_runtime.evaluation.producer_evidence import (
    CONTEXT_SCHEMA_VERSION,
    UPTAKE_SCHEMA_VERSION,
    verified_producer_binding,
)
from spst_runtime.evidence_context import EvidenceContextVerifier
from spst_runtime.live_pairing import (
    validate_live_pair_execution,
    validate_live_pair_execution_set,
)
from spst_runtime.persistence.sqlite_repository import SQLiteRepository


CONTEXT_UTILITY_SCHEMA = "spst-context-utility-attribution-v1"
PROVIDER_OBSERVED_CONTEXT_UTILITY_SCHEMA = "spst-context-utility-attribution-v2"
CONTEXT_UTILITY_LEDGER_SCHEMA = "context-utility-attribution-ledger-v1"
CONTEXT_UTILITY_INDEX_KEY = "runtime:context_utility:index:v1"
CONTEXT_UTILITY_RECORD_PREFIX = "runtime:context_utility:record:"
CONTEXT_EXPERIMENT_PROFILE = "isolated_context_intervention_v1"
CONTEXT_UTILITY_METRIC_SCOPE = (
    "reviewed_paired_task_quality_association_on_registered_local_corpus"
)
PROVIDER_OBSERVED_CONTEXT_UTILITY_METRIC_SCOPE = (
    "provider_observed_blind_reviewed_paired_task_quality_association_on_registered_local_corpus"
)

_identifier = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class ContextUtilityAttributionError(ValueError):
    """Raised when context utility cannot be attributed from verified evidence."""


class ContextUtilityAttributionLedger:
    """Derive context utility from Phase 16 evidence without accepting caller scores."""

    def __init__(
        self,
        repository: SQLiteRepository,
        paired_quality: PairedQualityEvidenceLedger,
        semantic_reviews: ContextSemanticReviewLedger,
        evidence_verifier: EvidenceContextVerifier,
    ):
        self.repository = repository
        self.paired_quality = paired_quality
        self.semantic_reviews = semantic_reviews
        self.evidence_verifier = evidence_verifier

    def attribute(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized, reason = self._normalize(payload)
        if reason is not None:
            return self._blocked(reason)
        base, reasons = self._derive(
            normalized["evaluation_id"],
            normalized["artifact_sha256"],
        )
        if reasons or base is None:
            return self._blocked(reasons[0] if reasons else "context_utility_unavailable")
        record_id = f"CUA-{_canonical_hash(base)[:16]}"
        with self.repository.locked():
            index = self._load_index()
            records = self._records(index)
            existing = next((item for item in records if item.get("id") == record_id), None)
            if existing is not None:
                return self.get(record_id) or self._blocked(
                    "context_utility_existing_record_unavailable"
                )
            if any(
                item.get("evaluation", {}).get("evaluation_id")
                == normalized["evaluation_id"]
                and item.get("context", {}).get("artifact_sha256")
                == normalized["artifact_sha256"]
                for item in records
                if isinstance(item, dict)
            ):
                return self._blocked("context_utility_attribution_already_recorded")
            stored = {**base, "id": record_id, "sequence": len(records) + 1}
            updated = {
                "schema_version": CONTEXT_UTILITY_LEDGER_SCHEMA,
                "records": [*records, stored],
            }
            asyncio.run(
                self.repository.save(
                    f"{CONTEXT_UTILITY_RECORD_PREFIX}{record_id}",
                    stored,
                )
            )
            asyncio.run(self.repository.save(CONTEXT_UTILITY_INDEX_KEY, updated))
        return self.get(record_id) or self._blocked(
            "context_utility_persisted_record_unavailable"
        )

    def get(self, attribution_id: str) -> dict[str, Any] | None:
        try:
            records = self._records(self._load_index())
        except ContextUtilityAttributionError:
            return None
        stored = next((item for item in records if item.get("id") == attribution_id), None)
        if stored is None:
            return None
        evaluation = _mapping(stored.get("evaluation"))
        context = _mapping(stored.get("context"))
        current, reasons = self._derive(
            str(evaluation.get("evaluation_id", "")),
            str(context.get("artifact_sha256", "")),
        )
        stored_base = {
            key: value for key, value in stored.items() if key not in {"id", "sequence"}
        }
        valid = current is not None and not reasons and current == stored_base
        projection = deepcopy(stored)
        if not valid:
            projection["status"] = "blocked"
            projection["utility"] = self._unavailable_utility(
                reasons[0] if reasons else "context_utility_recomputation_mismatch"
            )
        projection["revalidation"] = {
            "valid": valid,
            "reason": None
            if valid
            else reasons[0]
            if reasons
            else "context_utility_recomputation_mismatch",
        }
        return self._with_provenance(projection)

    def history(self) -> list[dict[str, Any]]:
        try:
            records = self._records(self._load_index())
        except ContextUtilityAttributionError:
            return []
        return [
            result
            for item in sorted(records, key=lambda value: int(value.get("sequence", 0)))
            if (result := self.get(str(item.get("id", "")))) is not None
        ]

    def coverage(self) -> dict[str, Any]:
        records = self.history()
        directions: dict[str, int] = {}
        for record in records:
            direction = str(_mapping(record.get("utility")).get("direction") or "unavailable")
            directions[direction] = directions.get(direction, 0) + 1
        return {
            "schema_version": CONTEXT_UTILITY_LEDGER_SCHEMA,
            "total_count": len(records),
            "available_count": sum(
                _mapping(record.get("utility")).get("available") is True
                for record in records
            ),
            "by_direction": dict(sorted(directions.items())),
        }

    def _derive(
        self,
        evaluation_id: str,
        artifact_sha256: str,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        reasons: list[str] = []
        evaluation_provenance = self.repository.verify_provenance()
        memory_provenance = self.semantic_reviews.repository.verify_provenance()
        if evaluation_provenance.get("valid") is not True:
            reasons.append("context_utility_evaluation_provenance_invalid")
        if memory_provenance.get("valid") is not True:
            reasons.append("context_utility_memory_provenance_invalid")
        paired = self.paired_quality.get(evaluation_id)
        if paired is None:
            return None, [*reasons, "context_utility_paired_evaluation_missing"]
        if paired.get("status") != "reviewed_evidence":
            reasons.append("context_utility_paired_evaluation_not_reviewed")
        if _mapping(paired.get("revalidation")).get("valid") is not True:
            reasons.append("context_utility_paired_evaluation_invalid")
        outcome_review = _mapping(paired.get("human_review"))
        if (
            outcome_review.get("status") != "accepted"
            or outcome_review.get("identity_assurance") != "self_attested"
            or outcome_review.get("human_identity_cryptographically_verified") is not False
        ):
            reasons.append("context_utility_outcome_review_invalid")
        task_quality = _mapping(paired.get("task_quality"))
        if task_quality.get("available") is not True:
            reasons.append("context_utility_task_quality_unavailable")
        measurement = _mapping(paired.get("measurement"))
        sample_count = measurement.get("sample_count")
        if (
            not isinstance(sample_count, int)
            or isinstance(sample_count, bool)
            or sample_count < self.paired_quality.MINIMUM_PAIRED_SAMPLES
        ):
            reasons.append("context_utility_minimum_samples_not_met")
        uncertainty = _mapping(measurement.get("uncertainty"))
        if (
            uncertainty.get("available") is not True
            or uncertainty.get("method") != "hoeffding_bounded_paired_delta"
            or uncertainty.get("confidence_level") != self.paired_quality.CONFIDENCE_LEVEL
            or not isinstance(uncertainty.get("lower_bound"), float)
            or not isinstance(uncertainty.get("upper_bound"), float)
        ):
            reasons.append("context_utility_uncertainty_invalid")

        try:
            record = self.semantic_reviews.find_artifact_record(artifact_sha256)
            structural = self.evidence_verifier.verify_record(record)
            if structural.get("verified") is not True:
                raise ContextSemanticReviewError(str(structural.get("reason")))
            semantic_support = self.semantic_reviews.verify_record(record, structural)
            if semantic_support.get("verified") is not True:
                raise ContextSemanticReviewError(str(semantic_support.get("reason")))
            intervention = self.semantic_reviews.build_intervention(record, structural)
            expected_context = producer_context_binding(intervention)
        except (ContextSemanticReviewError, KeyError, TypeError, ValueError) as error:
            return None, [*reasons, f"context_utility_semantic_support_invalid:{error}"]

        source_pairs = paired.get("source_pairs")
        if not isinstance(source_pairs, list):
            reasons.append("context_utility_source_pairs_invalid")
            source_pairs = []
        if isinstance(sample_count, int) and len(source_pairs) != sample_count:
            reasons.append("context_utility_pair_count_mismatch")
        task_ids: set[str] = set()
        provider_pair_evidence: list[dict[str, Any]] = []
        provider_contract: bool | None = None
        for pair in source_pairs:
            reason = self._pair_reason(pair, expected_context, task_ids)
            if reason is not None:
                reasons.append(reason)
                continue
            provider_evidence, provider_reason = self._provider_pair_evidence(
                pair,
                expected_context,
            )
            if provider_reason is not None:
                reasons.append(provider_reason)
                continue
            pair_provider_observed = provider_evidence is not None
            if provider_contract is None:
                provider_contract = pair_provider_observed
            elif provider_contract is not pair_provider_observed:
                reasons.append("context_utility_provider_observation_mixed")
                continue
            if provider_evidence is not None:
                provider_pair_evidence.append(provider_evidence)
        provider_observed = bool(source_pairs) and provider_contract is True
        if provider_observed:
            live_executions = [
                execution
                for evidence in provider_pair_evidence
                for execution in evidence.get("live_pair_executions", [])
                if isinstance(execution, dict)
            ]
            live_valid, live_reason = validate_live_pair_execution_set(
                live_executions,
                expected_task_ids=sorted(task_ids),
                expected_context_intervention_sha256=expected_context[
                    "intervention_sha256"
                ],
            )
            if not live_valid:
                reasons.append(
                    live_reason or "context_utility_live_pair_execution_invalid"
                )
            semantic_reviewer_id = _mapping(
                semantic_support.get("semantic_review")
            ).get("reviewer_id")
            if (
                outcome_review.get("blind_review_surface_enforced") is not True
                or outcome_review.get("arm_mapping_not_accessed_attested") is not True
                or outcome_review.get("reviewer_independence_attested") is not True
                or outcome_review.get("reviewer_blindness_cryptographically_verified")
                is not False
                or outcome_review.get("reviewer_independence_verified") is not False
            ):
                reasons.append("context_utility_blind_review_invalid")
            if outcome_review.get("reviewer_id") == semantic_reviewer_id:
                reasons.append("context_utility_reviewer_role_separation_required")
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            return None, reasons

        lower = float(uncertainty["lower_bound"])
        upper = float(uncertainty["upper_bound"])
        if lower > 0.0:
            direction = "beneficial_association_supported"
        elif upper < 0.0:
            direction = "harmful_association_supported"
        else:
            direction = "direction_inconclusive"
        scoring_artifact = _mapping(paired.get("scoring_artifact"))
        evaluation_binding = {
            "schema": "spst-context-utility-paired-evaluation-binding-v1",
            "evaluation_id": paired["id"],
            "scoring_artifact_digest": scoring_artifact.get("artifact_digest"),
            "outcome_review_id": outcome_review.get("review_id"),
            "reviewed_scoring_artifact_digest": outcome_review.get(
                "reviewed_scoring_artifact_digest"
            ),
            "source_pairs": source_pairs,
            "measurement": measurement,
        }
        context_binding = {
            "schema": "spst-context-utility-intervention-binding-v1",
            **expected_context,
        }
        provider_observation_binding = {
            "schema": "spst-context-utility-provider-observation-set-v1",
            "pairs": provider_pair_evidence,
        }
        base = {
            "schema": (
                PROVIDER_OBSERVED_CONTEXT_UTILITY_SCHEMA
                if provider_observed
                else CONTEXT_UTILITY_SCHEMA
            ),
            "kind": "context_utility_attribution",
            "status": "attributed",
            "evaluation": {
                "evaluation_id": paired["id"],
                "evaluation_binding_sha256": _canonical_hash(evaluation_binding),
                "scoring_artifact_digest": scoring_artifact["artifact_digest"],
                "outcome_review_id": outcome_review["review_id"],
                "outcome_identity_assurance": outcome_review["identity_assurance"],
                "human_identity_cryptographically_verified": False,
                "sample_count": sample_count,
                "minimum_sample_count": self.paired_quality.MINIMUM_PAIRED_SAMPLES,
            },
            "context": {
                "artifact_sha256": artifact_sha256,
                "semantic_review_id": expected_context["semantic_review_id"],
                "semantic_review_sha256": expected_context[
                    "semantic_review_sha256"
                ],
                "intervention_sha256": expected_context["intervention_sha256"],
                "producer_context_binding_sha256": expected_context[
                    "producer_context_binding_sha256"
                ],
                "context_binding_sha256": _canonical_hash(context_binding),
                "delivery_status": "submitted_to_adapter",
                "provider_uptake_verified": False,
            },
            "utility": {
                "available": True,
                "metric_scope": (
                    PROVIDER_OBSERVED_CONTEXT_UTILITY_METRIC_SCOPE
                    if provider_observed
                    else CONTEXT_UTILITY_METRIC_SCOPE
                ),
                "direction": direction,
                "baseline_mean": measurement.get("baseline_mean"),
                "context_mean": measurement.get("candidate_mean"),
                "paired_delta": measurement.get("paired_delta"),
                "sample_count": sample_count,
                "minimum_sample_count": self.paired_quality.MINIMUM_PAIRED_SAMPLES,
                "uncertainty": uncertainty,
                "causal_context_utility_established": False,
                "reason_causal_claim_withheld": "provider_uptake_and_hidden_state_not_verified",
                "generalization_beyond_registered_corpus": False,
            },
            "claims": {
                "general_model_quality_claimed": False,
                "model_weight_change_claimed": False,
                "causal_superiority_claimed": False,
                "automatic_context_adoption": False,
                "human_identity_cryptographically_verified": False,
                "provider_uptake_verified": False,
            },
        }
        if provider_observed:
            observation_set_sha256 = _canonical_hash(provider_observation_binding)
            _mapping(base["evaluation"]).update(
                {
                    "blind_review_surface_sha256": outcome_review[
                        "blind_review_surface_sha256"
                    ],
                    "reviewer_blindness_assurance": "surface_enforced_self_attested",
                    "provider_observation_set_sha256": observation_set_sha256,
                }
            )
            _mapping(base["context"]).update(
                {
                    "delivery_status": "provider_transport_observed",
                    "provider_uptake_observed": True,
                    "provider_uptake_verified": False,
                    "provider_observation_count": len(provider_pair_evidence) * 2,
                    "provider_observation_set_sha256": observation_set_sha256,
                }
            )
            _mapping(base["utility"]).update(
                {
                    "metric_scope": PROVIDER_OBSERVED_CONTEXT_UTILITY_METRIC_SCOPE,
                    "reason_causal_claim_withheld": (
                        "provider_identity_reviewer_blindness_and_hidden_state_not_cryptographically_verified"
                    ),
                }
            )
            _mapping(base["claims"]).update(
                {
                    "provider_uptake_observed": True,
                    "provider_uptake_verified": False,
                    "blind_review_surface_enforced": True,
                    "reviewer_blindness_cryptographically_verified": False,
                }
            )
        return base, []

    def _pair_reason(
        self,
        pair: Any,
        expected_context: dict[str, Any],
        task_ids: set[str],
    ) -> str | None:
        if not isinstance(pair, dict):
            return "context_utility_pair_invalid"
        task_id = pair.get("task_id")
        if not isinstance(task_id, str) or not task_id or task_id in task_ids:
            return "context_utility_pair_task_invalid"
        task_ids.add(task_id)
        baseline_reference = _mapping(pair.get("baseline_reference"))
        candidate_reference = _mapping(pair.get("candidate_reference"))
        baseline_report = asyncio.run(
            self.repository.load(
                f"runtime:operational_shadow:{baseline_reference.get('producer_run_id')}"
            )
        )
        candidate_report = asyncio.run(
            self.repository.load(
                f"runtime:operational_shadow:{candidate_reference.get('producer_run_id')}"
            )
        )
        if not isinstance(baseline_report, dict) or not isinstance(candidate_report, dict):
            return "context_utility_producer_run_missing"
        if any(
            _mapping(report.get("paired")).get("comparison_profile")
            != CONTEXT_EXPERIMENT_PROFILE
            for report in (baseline_report, candidate_report)
        ):
            return "context_utility_isolated_experiment_required"
        baseline = verified_producer_binding(
            baseline_report,
            str(baseline_reference.get("binding_id", "")),
        )
        candidate = verified_producer_binding(
            candidate_report,
            str(candidate_reference.get("binding_id", "")),
        )
        if baseline is None or candidate is None:
            return "context_utility_producer_binding_invalid"
        if baseline.get("task_id") != task_id or candidate.get("task_id") != task_id:
            return "context_utility_task_binding_mismatch"
        if baseline.get("arm") != "baseline" or candidate.get("arm") != "maximized":
            return "context_utility_arm_binding_mismatch"
        if (
            baseline.get("schema_version") == CONTEXT_SCHEMA_VERSION
            or "context_intervention" in baseline
        ):
            return "context_utility_baseline_contains_context"
        baseline_case = next(
            (
                value
                for value in baseline_report.get("cases", [])
                if isinstance(value, dict) and value.get("case_id") == task_id
            ),
            None,
        )
        candidate_case = next(
            (
                value
                for value in candidate_report.get("cases", [])
                if isinstance(value, dict) and value.get("case_id") == task_id
            ),
            None,
        )
        baseline_result = _mapping(_mapping(baseline_case).get("baseline"))
        candidate_result = _mapping(_mapping(candidate_case).get("maximized"))
        if (
            baseline_result.get("evaluation_profile") != "context_control"
            or candidate_result.get("evaluation_profile") != "context_control"
            or baseline_result.get("plan") != {}
            or candidate_result.get("plan") != {}
        ):
            return "context_utility_isolated_configuration_mismatch"
        if (
            candidate.get("schema_version")
            not in {CONTEXT_SCHEMA_VERSION, UPTAKE_SCHEMA_VERSION}
            or candidate.get("context_intervention_status") != "ready"
            or candidate.get("context_intervention") != expected_context
        ):
            return "context_utility_candidate_context_mismatch"
        return None

    def _provider_pair_evidence(
        self,
        pair: dict[str, Any],
        expected_context: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        baseline_reference = _mapping(pair.get("baseline_reference"))
        candidate_reference = _mapping(pair.get("candidate_reference"))
        bindings: list[dict[str, Any]] = []
        for reference, arm in (
            (baseline_reference, "baseline"),
            (candidate_reference, "maximized"),
        ):
            report = asyncio.run(
                self.repository.load(
                    f"runtime:operational_shadow:{reference.get('producer_run_id')}"
                )
            )
            if not isinstance(report, dict):
                return None, "context_utility_producer_run_missing"
            binding = verified_producer_binding(
                report,
                str(reference.get("binding_id", "")),
            )
            if binding is None:
                return None, "context_utility_producer_binding_invalid"
            bindings.append(binding)

        uptake_flags = [
            binding.get("schema_version") == UPTAKE_SCHEMA_VERSION
            for binding in bindings
        ]
        if not any(uptake_flags):
            return None, None
        if not all(uptake_flags):
            return None, "context_utility_provider_observation_pair_incomplete"

        live_executions: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        for binding, condition, arm in zip(
            bindings,
            ("control", "treatment"),
            ("baseline", "maximized"),
            strict=True,
        ):
            live_execution = _mapping(binding.get("live_pair_execution"))
            live_valid, live_reason = validate_live_pair_execution(
                live_execution,
                expected_task_id=str(pair.get("task_id") or ""),
                expected_condition=condition,
                expected_selected_arm=arm,
                expected_context_intervention_sha256=expected_context[
                    "intervention_sha256"
                ],
            )
            if (
                binding.get("live_pair_execution_status") != "verified"
                or not live_valid
            ):
                return None, live_reason or "context_utility_live_pair_execution_invalid"
            live_executions.append(live_execution)

        for binding in bindings:
            observation = _mapping(binding.get("provider_observation"))
            if binding.get("provider_observation_status") != "verified":
                return None, "context_utility_provider_observation_invalid"
            observations.append(observation)
        baseline_observation, candidate_observation = observations
        baseline_request = _mapping(baseline_observation.get("request"))
        candidate_request = _mapping(candidate_observation.get("request"))
        if (
            baseline_request.get("context_present") is not False
            or baseline_request.get("context_intervention_sha256") is not None
        ):
            return None, "context_utility_baseline_provider_context_present"
        if (
            candidate_request.get("context_present") is not True
            or candidate_request.get("context_intervention_sha256")
            != expected_context["intervention_sha256"]
        ):
            return None, "context_utility_candidate_provider_context_mismatch"
        response_sha256s = [
            str(_mapping(observation.get("response")).get("response_id_sha256") or "")
            for observation in observations
        ]
        if len(set(response_sha256s)) != 2 or not all(
            _is_sha256(value) for value in response_sha256s
        ):
            return None, "context_utility_provider_response_replay_detected"
        return {
            "task_id": pair["task_id"],
            "baseline_observation_sha256": baseline_observation[
                "observation_sha256"
            ],
            "candidate_observation_sha256": candidate_observation[
                "observation_sha256"
            ],
            "baseline_response_id_sha256": response_sha256s[0],
            "candidate_response_id_sha256": response_sha256s[1],
            "baseline_execution_position": live_executions[0][
                "condition_position"
            ],
            "candidate_execution_position": live_executions[1][
                "condition_position"
            ],
            "randomization_nonce_sha256": live_executions[0][
                "randomization_nonce_sha256"
            ],
            "execution_manifest_sha256": live_executions[0][
                "manifest_sha256"
            ],
            "live_pair_executions": live_executions,
        }, None

    def _load_index(self) -> dict[str, Any]:
        stored = asyncio.run(self.repository.load(CONTEXT_UTILITY_INDEX_KEY))
        if stored is None:
            return {"schema_version": CONTEXT_UTILITY_LEDGER_SCHEMA, "records": []}
        if (
            not isinstance(stored, dict)
            or stored.get("schema_version") != CONTEXT_UTILITY_LEDGER_SCHEMA
            or not isinstance(stored.get("records"), list)
        ):
            raise ContextUtilityAttributionError("context_utility_index_invalid")
        return stored

    @staticmethod
    def _records(index: dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in index.get("records", []) if isinstance(item, dict)]

    @staticmethod
    def _normalize(payload: Any) -> tuple[dict[str, str], str | None]:
        source = payload if isinstance(payload, dict) else {}
        if set(source) != {"evaluation_id", "artifact_sha256"}:
            return {}, "context_utility_payload_shape_invalid"
        evaluation_id = str(source.get("evaluation_id") or "")
        artifact_sha256 = source.get("artifact_sha256")
        if not _identifier.fullmatch(evaluation_id):
            return {}, "context_utility_evaluation_id_invalid"
        if not _is_sha256(artifact_sha256):
            return {}, "context_utility_artifact_digest_invalid"
        return {
            "evaluation_id": evaluation_id,
            "artifact_sha256": str(artifact_sha256),
        }, None

    def _blocked(self, reason: str) -> dict[str, Any]:
        return {
            "schema": CONTEXT_UTILITY_SCHEMA,
            "status": "blocked",
            "reason": reason,
            "utility": self._unavailable_utility(reason),
            "claims": {
                "general_model_quality_claimed": False,
                "model_weight_change_claimed": False,
                "causal_superiority_claimed": False,
                "automatic_context_adoption": False,
            },
            "persistence": {"status": "not_persisted"},
        }

    @staticmethod
    def _unavailable_utility(reason: str) -> dict[str, Any]:
        return {
            "available": False,
            "metric_scope": CONTEXT_UTILITY_METRIC_SCOPE,
            "direction": "unavailable",
            "baseline_mean": None,
            "context_mean": None,
            "paired_delta": None,
            "sample_count": 0,
            "minimum_sample_count": PairedQualityEvidenceLedger.MINIMUM_PAIRED_SAMPLES,
            "uncertainty": None,
            "causal_context_utility_established": False,
            "reason": reason,
            "generalization_beyond_registered_corpus": False,
        }

    def _with_provenance(self, value: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(value)
        provenance = self.repository.verify_provenance()
        memory_provenance = self.semantic_reviews.repository.verify_provenance()
        result["persistence"] = {
            "status": "stored",
            "provenance_valid": provenance.get("valid") is True,
        }
        result["state_provenance"] = {
            "evaluation": {
                key: provenance.get(key)
                for key in ("valid", "entries", "latest_hash", "key_source")
            },
            "memory": {
                key: memory_provenance.get(key)
                for key in ("valid", "entries", "latest_hash", "key_source")
            },
        }
        return result


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _canonical_hash(value: Any) -> str:
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
