import asyncio
from copy import deepcopy
import hashlib
import json
import re
import secrets
from typing import Any, Callable

from spst_runtime.context_review import validate_producer_context_binding
from spst_runtime.engines.governance_engine import GovernanceEngine
from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.evaluation.producer_evidence import (
    CONTEXT_SCHEMA_VERSION,
    SCHEMA_VERSION as PRODUCER_SCORING_SCHEMA_VERSION,
    verified_producer_binding,
)
from spst_runtime.evaluation.quality_evidence import (
    IndependentExactJsonScorer,
    paired_hoeffding_interval,
    quality_rubric_digest,
    validate_producer_scoring_material,
)
from spst_runtime.persistence.sqlite_repository import SQLiteRepository


class PairedQualityEvidenceLedger:
    """Create producer-bound, arm-blinded paired scores with explicit HITL review."""

    SCHEMA_VERSION = "paired-quality-evidence-v1"
    SCORING_ARTIFACT_SCHEMA = "human-reviewable-blinded-scoring-artifact-v1"
    INDEX_KEY = "runtime:paired_quality:index:v1"
    RECORD_KEY_PREFIX = "runtime:paired_quality:record:"
    REVIEW_KEY_PREFIX = "runtime:paired_quality:review:"
    MINIMUM_PAIRED_SAMPLES = 8
    CONFIDENCE_LEVEL = 0.95
    REVIEW_SCOPE = "paired_quality_scoring_artifact"
    METRIC_SCOPE = "task_specific_exact_json_quality_on_registered_local_corpus"
    _identifier = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
    _sha256 = re.compile(r"^[0-9a-f]{64}$")

    def __init__(
        self,
        repository: SQLiteRepository,
        corpus: OperationalEvaluationCorpus,
        *,
        scorer: IndependentExactJsonScorer | None = None,
        governance_engine: GovernanceEngine | None = None,
        nonce_factory: Callable[[], str] | None = None,
    ):
        self.repository = repository
        self.corpus = corpus
        self.scorer = scorer or IndependentExactJsonScorer()
        self.governance_engine = governance_engine or GovernanceEngine()
        self.nonce_factory = nonce_factory or (lambda: secrets.token_hex(32))

    def evaluate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Score stored producer bindings; caller-supplied scores are never accepted."""
        normalized = self._normalize_evaluation(payload)
        provenance = self.repository.verify_provenance()
        resolved_pairs, resolution_reasons = self._resolve_pairs(normalized["pairs"])
        reasons = [*normalized["errors"], *resolution_reasons]
        if len(resolved_pairs) < self.MINIMUM_PAIRED_SAMPLES:
            reasons.append("minimum_paired_samples_not_met")

        scoring_artifact: dict[str, Any] = {}
        paired_scores: list[dict[str, Any]] = []
        measurement = self._unavailable_measurement(len(resolved_pairs))
        blocking_reasons = [
            reason for reason in reasons if reason != "minimum_paired_samples_not_met"
        ]
        if resolved_pairs and not blocking_reasons:
            scoring_artifact, paired_scores = self._score_pairs(resolved_pairs)
            measurement = self._measurement(paired_scores)

        evaluator_independent = self._evaluator_independent(resolved_pairs)
        if not evaluator_independent:
            reasons.append("independent_evaluator_identity_required")
        if not provenance.get("valid", False):
            reasons.append("provenance_invalid")
        reasons = self._dedupe(reasons)
        sufficient = not reasons and measurement.get("available") is True
        governance = self._evaluation_governance(
            provenance_valid=bool(provenance.get("valid", False)),
            contract_valid=not normalized["errors"],
            evidence_valid=not resolution_reasons,
            evaluator_independent=evaluator_independent,
            minimum_sample_met=len(resolved_pairs) >= self.MINIMUM_PAIRED_SAMPLES,
            uncertainty_available=measurement.get("uncertainty", {}).get("available")
            is True,
        )
        if not governance.get("authorized", False) or not provenance.get("valid", False):
            status = "blocked"
        elif sufficient:
            status = "pending_human_review"
        else:
            status = "insufficient_evidence"

        record: dict[str, Any] = {
            "schema_version": self.SCHEMA_VERSION,
            "kind": "evaluation",
            "initial_status": status,
            "reasons": reasons,
            "source_pairs": self._source_pairs(resolved_pairs, paired_scores),
            "scoring_artifact": scoring_artifact,
            "measurement": measurement,
            "human_review": {
                "status": "pending" if status == "pending_human_review" else "not_applicable",
                "review_scope": self.REVIEW_SCOPE,
                "human_identity_cryptographically_verified": False,
            },
            "task_quality": self._task_quality(
                status=status,
                measurement=measurement,
                review=None,
            ),
            "claims": {
                "task_quality_uplift_claimed": False,
                "official_benchmark_claimed": False,
                "general_model_quality_claimed": False,
                "automatic_promotion": False,
                "same_model_same_tasks": bool(resolved_pairs) and not resolution_reasons,
                "verified_distinct_producer_runs": bool(resolved_pairs)
                and not resolution_reasons,
                "arm_blinded_scoring": bool(scoring_artifact),
                "organizational_evaluator_independence_verified": False,
            },
            "governance": self._compact_governance(governance),
        }
        record["id"] = f"PQE-{self._digest(record)[:16]}"
        if not provenance.get("valid", False):
            return self._with_provenance(record, provenance, "not_persisted")
        return self._persist_evaluation(record)

    def review(self, evaluation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append a self-attested human review of one immutable scoring artifact."""
        current = self.get(evaluation_id)
        if current is None:
            return self._not_found(evaluation_id)
        if current.get("status") != "pending_human_review":
            return self._operation_blocked(current, "quality_evaluation_not_pending")
        normalized = self._normalize_review(payload)
        artifact = self._mapping(current.get("scoring_artifact"))
        artifact_digest_matches = (
            normalized["scoring_artifact_digest"] == artifact.get("artifact_digest")
        )
        provenance = self.repository.verify_provenance()
        governance = self._review_governance(
            provenance_valid=bool(provenance.get("valid", False)),
            review_valid=not normalized["errors"],
            artifact_digest_matches=artifact_digest_matches,
        )
        if (
            normalized["errors"]
            or not artifact_digest_matches
            or not provenance.get("valid", False)
            or not governance.get("authorized", False)
        ):
            blocked = self._operation_blocked(
                current,
                self._dedupe(
                    [
                        *normalized["errors"],
                        *([] if artifact_digest_matches else ["review_artifact_digest_mismatch"]),
                    ]
                )[0]
                if normalized["errors"] or not artifact_digest_matches
                else "quality_review_not_authorized",
            )
            blocked["governance"] = self._compact_governance(governance)
            return self._with_provenance(blocked, provenance, "not_persisted")

        event = {
            "schema_version": self.SCHEMA_VERSION,
            "kind": "review",
            "evaluation_id": evaluation_id,
            "decision": normalized["decision"],
            "reviewer_id": normalized["reviewer_id"],
            "reviewer_kind": "human",
            "review_scope": self.REVIEW_SCOPE,
            "reviewed_scoring_artifact_digest": normalized[
                "scoring_artifact_digest"
            ],
            "identity_assurance": "self_attested",
            "human_identity_cryptographically_verified": False,
            "governance": self._compact_governance(governance),
        }
        event["id"] = f"PQREV-{self._digest(event)[:16]}"
        return self._append_review(event)

    def get(self, evaluation_id: str) -> dict[str, Any] | None:
        with self.repository.locked():
            records = self._records(self._load_index())
        evaluation = next(
            (
                record
                for record in records
                if record.get("kind") == "evaluation" and record.get("id") == evaluation_id
            ),
            None,
        )
        if evaluation is None:
            return None
        reviews = [
            record
            for record in records
            if record.get("kind") == "review"
            and record.get("evaluation_id") == evaluation_id
        ]
        return self._projection(evaluation, reviews)

    def history(self) -> list[dict[str, Any]]:
        with self.repository.locked():
            records = self._records(self._load_index())
        evaluations = sorted(
            [record for record in records if record.get("kind") == "evaluation"],
            key=lambda item: int(item.get("sequence", 0)),
        )
        return [
            self._projection(
                evaluation,
                [
                    record
                    for record in records
                    if record.get("kind") == "review"
                    and record.get("evaluation_id") == evaluation.get("id")
                ],
            )
            for evaluation in evaluations
        ]

    def coverage(self) -> dict[str, Any]:
        records = self.history()
        by_status: dict[str, int] = {}
        for record in records:
            status = str(record.get("status") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
        return {
            "schema_version": self.SCHEMA_VERSION,
            "total_count": len(records),
            "reviewed_count": by_status.get("reviewed_evidence", 0),
            "claim_eligible_count": sum(
                self._mapping(record.get("task_quality")).get("claim_eligible") is True
                for record in records
            ),
            "by_status": by_status,
        }

    def _resolve_pairs(
        self,
        pairs: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        resolved: list[dict[str, Any]] = []
        reasons: list[str] = []
        task_ids: set[str] = set()
        binding_ids: set[str] = set()
        model_contract: tuple[Any, ...] | None = None
        for pair in pairs:
            task_id = pair["task_id"]
            if task_id in task_ids:
                reasons.append("duplicate_paired_task")
                continue
            task_ids.add(task_id)
            baseline, baseline_reason = self._resolve_reference(
                pair["baseline"], expected_arm="baseline"
            )
            candidate, candidate_reason = self._resolve_reference(
                pair["candidate"], expected_arm="maximized"
            )
            if baseline_reason or candidate_reason:
                reasons.extend(
                    reason
                    for reason in (baseline_reason, candidate_reason)
                    if reason is not None
                )
                continue
            assert baseline is not None and candidate is not None
            if baseline["binding"]["task_id"] != task_id or candidate["binding"][
                "task_id"
            ] != task_id:
                reasons.append("paired_task_mismatch")
                continue
            if baseline["report_id"] == candidate["report_id"]:
                reasons.append("distinct_producer_runs_required")
                continue
            if baseline["model_contract"] != candidate["model_contract"]:
                reasons.append("same_model_same_task_contract_required")
                continue
            if model_contract is None:
                model_contract = baseline["model_contract"][:3]
            elif baseline["model_contract"][:3] != model_contract:
                reasons.append("cross_pair_model_contract_mismatch")
                continue
            pair_binding_ids = {
                baseline["binding"]["binding_id"],
                candidate["binding"]["binding_id"],
            }
            if len(pair_binding_ids) != 2 or binding_ids & pair_binding_ids:
                reasons.append("producer_binding_reused")
                continue
            binding_ids.update(pair_binding_ids)
            if not self._semantically_distinct(baseline, candidate):
                reasons.append("candidate_evidence_not_distinct")
                continue
            rubric = self.corpus.scoring_contract(task_id)
            rubric_digest = quality_rubric_digest(rubric)
            if rubric_digest is None:
                reasons.append("task_quality_rubric_missing")
                continue
            if any(
                item["binding"].get("quality_rubric_digest") != rubric_digest
                or item["material"].get("rubric_digest") != rubric_digest
                for item in (baseline, candidate)
            ):
                reasons.append("producer_rubric_binding_mismatch")
                continue
            resolved.append(
                {
                    "task_id": task_id,
                    "rubric": rubric,
                    "baseline": baseline,
                    "candidate": candidate,
                }
            )
        return resolved, self._dedupe(reasons)

    def _resolve_reference(
        self,
        reference: dict[str, str],
        *,
        expected_arm: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        report_id = reference.get("producer_run_id")
        binding_id = reference.get("binding_id")
        report = asyncio.run(
            self.repository.load(f"runtime:operational_shadow:{report_id}")
        )
        if not isinstance(report, dict) or report.get("id") != report_id:
            return None, "producer_run_not_found"
        binding = verified_producer_binding(report, str(binding_id))
        if binding is None:
            return None, "producer_binding_invalid"
        if binding.get("arm") != expected_arm:
            return None, "producer_arm_mismatch"
        if binding.get("producer_status") != "completed":
            return None, "producer_not_completed"
        if binding.get("verification_status") != "passed":
            return None, "producer_verification_not_passed"
        binding_schema = binding.get("schema_version")
        if binding_schema not in {
            PRODUCER_SCORING_SCHEMA_VERSION,
            CONTEXT_SCHEMA_VERSION,
        }:
            return None, "producer_scoring_binding_required"
        if binding_schema == CONTEXT_SCHEMA_VERSION:
            context_valid, _ = validate_producer_context_binding(
                binding.get("context_intervention")
            )
            if (
                binding.get("context_intervention_status") != "ready"
                or not context_valid
            ):
                return None, "producer_context_intervention_invalid"
        if binding.get("scoring_material_status") != "ready":
            return None, "producer_scoring_material_unresolved"
        run_instance_id = binding.get("producer_run_instance_id")
        execution_id = binding.get("producer_execution_id")
        if (
            not isinstance(run_instance_id, str)
            or not run_instance_id.startswith("PRUN-")
            or not self._safe_identifier(run_instance_id)
            or not isinstance(execution_id, str)
            or not execution_id.startswith("PEX-")
            or not self._safe_identifier(execution_id)
        ):
            return None, "producer_execution_identity_required"
        case = next(
            (
                item
                for item in report.get("cases", [])
                if isinstance(item, dict) and item.get("case_id") == binding.get("task_id")
            ),
            None,
        )
        result = self._mapping(case).get(expected_arm)
        result = self._mapping(result)
        material = result.get("scoring_material")
        valid_material, _ = validate_producer_scoring_material(material)
        if (
            not valid_material
            or not isinstance(material, dict)
            or material.get("status") != "ready"
        ):
            return None, "producer_scoring_material_invalid"
        if (
            material.get("material_digest") != binding.get("scoring_material_digest")
            or material.get("output_digest") != binding.get("artifact_digest")
        ):
            return None, "producer_scoring_material_binding_mismatch"
        provider = self._mapping(report.get("provider"))
        suite = self._mapping(report.get("suite"))
        if not all(
            isinstance(value, str) and value
            for value in (
                provider.get("name"),
                provider.get("model_version"),
                suite.get("version"),
                self._mapping(case).get("prompt_digest"),
            )
        ):
            return None, "producer_model_contract_incomplete"
        return (
            {
                "report_id": report_id,
                "reference": deepcopy(reference),
                "binding": binding,
                "material": deepcopy(material),
                "model_contract": (
                    provider.get("name"),
                    provider.get("model_version"),
                    suite.get("version"),
                    self._mapping(case).get("prompt_digest"),
                ),
            },
            None,
        )

    @staticmethod
    def _semantically_distinct(
        baseline: dict[str, Any],
        candidate: dict[str, Any],
    ) -> bool:
        baseline_binding = baseline["binding"]
        candidate_binding = candidate["binding"]
        if (
            baseline_binding.get("semantic_configuration_status") != "resolved"
            or candidate_binding.get("semantic_configuration_status") != "resolved"
            or baseline_binding.get("semantic_configuration_digest")
            == candidate_binding.get("semantic_configuration_digest")
        ):
            return False
        return (
            baseline_binding.get("producer_run_instance_id")
            != candidate_binding.get("producer_run_instance_id")
            and baseline_binding.get("producer_execution_id")
            != candidate_binding.get("producer_execution_id")
        )

    def _score_pairs(
        self,
        pairs: list[dict[str, Any]],
        fixed_slots: dict[str, tuple[str, str]] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        nonce = self.nonce_factory()
        task_artifacts: list[dict[str, Any]] = []
        paired_scores: list[dict[str, Any]] = []
        for pair in pairs:
            task_id = pair["task_id"]
            if fixed_slots and task_id in fixed_slots:
                baseline_slot, candidate_slot = fixed_slots[task_id]
            else:
                baseline_slot = self._slot_id(
                    nonce, task_id, pair["baseline"]["binding"]["binding_id"]
                )
                candidate_slot = self._slot_id(
                    nonce, task_id, pair["candidate"]["binding"]["binding_id"]
                )
            packets = [
                {
                    "slot_id": baseline_slot,
                    "material": deepcopy(pair["baseline"]["material"]),
                },
                {
                    "slot_id": candidate_slot,
                    "material": deepcopy(pair["candidate"]["material"]),
                },
            ]
            scores = self.scorer.score(pair["rubric"], sorted(packets, key=lambda x: x["slot_id"]))
            score_by_slot = {result["slot_id"]: result["score"] for result in scores}
            baseline_score = float(score_by_slot[baseline_slot])
            candidate_score = float(score_by_slot[candidate_slot])
            mapping_commitment = self._digest(
                {
                    baseline_slot: pair["baseline"]["binding"]["binding_id"],
                    candidate_slot: pair["candidate"]["binding"]["binding_id"],
                }
            )
            task_artifacts.append(
                {
                    "task_id": task_id,
                    "rubric_digest": quality_rubric_digest(pair["rubric"]),
                    "mapping_commitment": mapping_commitment,
                    "scores": scores,
                }
            )
            paired_scores.append(
                {
                    "task_id": task_id,
                    "baseline_slot_id": baseline_slot,
                    "candidate_slot_id": candidate_slot,
                    "baseline_score": baseline_score,
                    "candidate_score": candidate_score,
                    "delta": round(candidate_score - baseline_score, 6),
                }
            )
        artifact: dict[str, Any] = {
            "schema": self.SCORING_ARTIFACT_SCHEMA,
            "evaluator": self.scorer.identity,
            "arm_blinded": True,
            "scored_before_unblinding": True,
            "contains_arm_labels": False,
            "task_artifacts": sorted(task_artifacts, key=lambda item: item["task_id"]),
        }
        artifact["artifact_digest"] = self._digest(artifact)
        return artifact, sorted(paired_scores, key=lambda item: item["task_id"])

    def _measurement(self, paired_scores: list[dict[str, Any]]) -> dict[str, Any]:
        baseline = [float(item["baseline_score"]) for item in paired_scores]
        candidate = [float(item["candidate_score"]) for item in paired_scores]
        deltas = [float(item["delta"]) for item in paired_scores]
        uncertainty = paired_hoeffding_interval(
            deltas,
            confidence_level=self.CONFIDENCE_LEVEL,
        )
        return {
            "available": bool(paired_scores) and uncertainty.get("available") is True,
            "metric_scope": self.METRIC_SCOPE,
            "sample_count": len(paired_scores),
            "minimum_paired_samples": self.MINIMUM_PAIRED_SAMPLES,
            "baseline_mean": self._mean(baseline),
            "candidate_mean": self._mean(candidate),
            "paired_delta": self._mean(deltas),
            "paired_scores": paired_scores,
            "uncertainty": uncertainty,
            "positive_effect_supported": (
                isinstance(uncertainty.get("lower_bound"), float)
                and uncertainty["lower_bound"] > 0.0
            ),
        }

    def _projection(
        self,
        evaluation: dict[str, Any],
        reviews: list[dict[str, Any]],
    ) -> dict[str, Any]:
        view = deepcopy(evaluation)
        revalidation_valid, revalidation_reason = self._revalidate(evaluation)
        ordered_reviews = sorted(reviews, key=lambda item: int(item.get("sequence", 0)))
        review = ordered_reviews[0] if ordered_reviews else None
        initial_status = str(evaluation.get("initial_status") or "blocked")
        if not revalidation_valid:
            status = "blocked"
        elif review is not None and initial_status == "pending_human_review":
            status = (
                "reviewed_evidence"
                if review.get("decision") == "accepted"
                else "rejected_by_human"
            )
        else:
            status = initial_status
        measurement = self._mapping(view.get("measurement"))
        view["status"] = status
        view["revalidation"] = {
            "valid": revalidation_valid,
            "reason": revalidation_reason,
        }
        view["human_review"] = (
            {
                "status": review.get("decision"),
                "review_id": review.get("id"),
                "reviewer_id": review.get("reviewer_id"),
                "reviewer_kind": review.get("reviewer_kind"),
                "review_scope": review.get("review_scope"),
                "reviewed_scoring_artifact_digest": review.get(
                    "reviewed_scoring_artifact_digest"
                ),
                "identity_assurance": review.get("identity_assurance"),
                "human_identity_cryptographically_verified": False,
            }
            if review is not None
            else view.get("human_review")
        )
        view["task_quality"] = self._task_quality(
            status=status,
            measurement=measurement,
            review=review,
        )
        claims = self._mapping(view.get("claims"))
        view["claims"] = {
            **claims,
            "task_quality_uplift_claimed": False,
            "automatic_promotion": False,
        }
        return view

    def _revalidate(self, evaluation: dict[str, Any]) -> tuple[bool, str | None]:
        provenance = self.repository.verify_provenance()
        if not provenance.get("valid", False):
            return False, "provenance_invalid"
        artifact = self._mapping(evaluation.get("scoring_artifact"))
        artifact_digest = artifact.get("artifact_digest")
        unsigned_artifact = {key: value for key, value in artifact.items() if key != "artifact_digest"}
        if not self._valid_sha256(artifact_digest) or artifact_digest != self._digest(
            unsigned_artifact
        ):
            return False, "scoring_artifact_digest_mismatch"
        source_pairs = evaluation.get("source_pairs")
        if not isinstance(source_pairs, list):
            return False, "source_pairs_invalid"
        normalized_pairs = [
            {
                "task_id": item.get("task_id"),
                "baseline": self._mapping(item.get("baseline_reference")),
                "candidate": self._mapping(item.get("candidate_reference")),
            }
            for item in source_pairs
            if isinstance(item, dict)
        ]
        resolved, reasons = self._resolve_pairs(normalized_pairs)
        if reasons or len(resolved) != len(source_pairs):
            return False, reasons[0] if reasons else "source_pair_count_mismatch"
        fixed_slots = {
            item["task_id"]: (
                item["baseline_slot_id"],
                item["candidate_slot_id"],
            )
            for item in source_pairs
        }
        recomputed_artifact, paired_scores = self._score_pairs(resolved, fixed_slots)
        if recomputed_artifact != artifact:
            return False, "scoring_artifact_recomputation_mismatch"
        if self._measurement(paired_scores) != evaluation.get("measurement"):
            return False, "paired_measurement_recomputation_mismatch"
        return True, None

    def _task_quality(
        self,
        *,
        status: str,
        measurement: dict[str, Any],
        review: dict[str, Any] | None,
    ) -> dict[str, Any]:
        available = status == "reviewed_evidence" and review is not None
        if available:
            reason = None
        elif status == "pending_human_review":
            reason = "human_review_pending"
        elif status == "rejected_by_human":
            reason = "human_review_rejected"
        else:
            reason = "paired_quality_evidence_incomplete"
        return {
            "schema": "spst-task-quality-evidence-v1",
            "available": available,
            "metric_scope": self.METRIC_SCOPE,
            "baseline_mean": measurement.get("baseline_mean") if available else None,
            "maximized_mean": measurement.get("candidate_mean") if available else None,
            "paired_delta": measurement.get("paired_delta") if available else None,
            "pair_count": measurement.get("sample_count") if available else 0,
            "uncertainty": measurement.get("uncertainty") if available else None,
            "semantic_task_quality_established": available,
            "claim_eligible": available
            and measurement.get("positive_effect_supported") is True,
            "reason": reason,
            "automatic_promotion": False,
            "generalization_beyond_registered_corpus": False,
        }

    def _persist_evaluation(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.repository.locked():
            index = self._load_index()
            records = self._records(index)
            stored = {**record, "sequence": len(records) + 1}
            updated = {
                "schema_version": self.SCHEMA_VERSION,
                "records": [*records, stored],
            }
            asyncio.run(
                self.repository.save(f"{self.RECORD_KEY_PREFIX}{record['id']}", stored)
            )
            asyncio.run(self.repository.save(self.INDEX_KEY, updated))
        projection = self.get(str(record["id"])) or deepcopy(stored)
        return self._with_provenance(
            projection,
            self.repository.verify_provenance(),
            "stored",
        )

    def _append_review(self, event: dict[str, Any]) -> dict[str, Any]:
        with self.repository.locked():
            index = self._load_index()
            records = self._records(index)
            if any(
                record.get("kind") == "review"
                and record.get("evaluation_id") == event["evaluation_id"]
                for record in records
            ):
                current = self.get(str(event["evaluation_id"]))
                assert current is not None
                return self._operation_blocked(current, "quality_review_already_recorded")
            stored = {**event, "sequence": len(records) + 1}
            updated = {
                "schema_version": self.SCHEMA_VERSION,
                "records": [*records, stored],
            }
            asyncio.run(self.repository.save(f"{self.REVIEW_KEY_PREFIX}{event['id']}", stored))
            asyncio.run(self.repository.save(self.INDEX_KEY, updated))
        projection = self.get(str(event["evaluation_id"]))
        assert projection is not None
        return self._with_provenance(
            projection,
            self.repository.verify_provenance(),
            "stored",
        )

    def _load_index(self) -> dict[str, Any]:
        stored = asyncio.run(self.repository.load(self.INDEX_KEY))
        if not isinstance(stored, dict):
            return {"schema_version": self.SCHEMA_VERSION, "records": []}
        if stored.get("schema_version") != self.SCHEMA_VERSION or not isinstance(
            stored.get("records"), list
        ):
            raise ValueError("Invalid paired quality evidence index")
        return stored

    @staticmethod
    def _records(index: dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in index.get("records", []) if isinstance(item, dict)]

    def _normalize_evaluation(self, payload: Any) -> dict[str, Any]:
        source = payload if isinstance(payload, dict) else {}
        errors: list[str] = []
        if set(source) != {"pairs"}:
            errors.append("paired_evaluation_payload_shape_invalid")
        values = source.get("pairs")
        if not isinstance(values, list) or not values:
            return {"pairs": [], "errors": [*errors, "paired_evaluation_pairs_required"]}
        pairs: list[dict[str, Any]] = []
        for value in values:
            if not isinstance(value, dict) or set(value) != {"task_id", "baseline", "candidate"}:
                errors.append("paired_evaluation_pair_shape_invalid")
                continue
            task_id = self._safe_identifier(value.get("task_id"))
            baseline = self._normalize_reference(value.get("baseline"))
            candidate = self._normalize_reference(value.get("candidate"))
            if not task_id or baseline is None or candidate is None:
                errors.append("paired_evaluation_reference_invalid")
                continue
            pairs.append(
                {"task_id": task_id, "baseline": baseline, "candidate": candidate}
            )
        return {"pairs": pairs, "errors": self._dedupe(errors)}

    def _normalize_reference(self, value: Any) -> dict[str, str] | None:
        if not isinstance(value, dict) or set(value) != {"producer_run_id", "binding_id"}:
            return None
        run_id = self._safe_identifier(value.get("producer_run_id"))
        binding_id = self._safe_identifier(value.get("binding_id"))
        if not run_id or not binding_id:
            return None
        return {"producer_run_id": run_id, "binding_id": binding_id}

    def _normalize_review(self, payload: Any) -> dict[str, Any]:
        source = payload if isinstance(payload, dict) else {}
        errors: list[str] = []
        if set(source) != {
            "reviewer_id",
            "reviewer_kind",
            "review_scope",
            "decision",
            "scoring_artifact_digest",
        }:
            errors.append("quality_review_payload_shape_invalid")
        reviewer_id = self._safe_identifier(source.get("reviewer_id"))
        if not reviewer_id:
            errors.append("quality_review_reviewer_id_invalid")
        if source.get("reviewer_kind") != "human":
            errors.append("quality_review_human_reviewer_required")
        if source.get("review_scope") != self.REVIEW_SCOPE:
            errors.append("quality_review_scope_invalid")
        decision = source.get("decision")
        if decision not in {"accepted", "rejected"}:
            errors.append("quality_review_decision_invalid")
            decision = "invalid"
        digest = source.get("scoring_artifact_digest")
        if not self._valid_sha256(digest):
            errors.append("quality_review_artifact_digest_invalid")
            digest = ""
        return {
            "reviewer_id": reviewer_id,
            "decision": decision,
            "scoring_artifact_digest": digest,
            "errors": self._dedupe(errors),
        }

    def _evaluation_governance(self, **facts: bool) -> dict[str, Any]:
        return self.governance_engine.decide(
            {
                "type": "paired_quality_evaluation",
                "event_type": "paired_quality_evaluation",
                "source": "paired_quality_evidence_ledger",
                "provenance_scope": "sqlite",
                "provenance_verified": facts["provenance_valid"],
                "payload": {"local_only": True, "evidence_only": True, **facts},
                "security_assessment": {"blocked": False, "findings": []},
            }
        )

    def _review_governance(self, **facts: bool) -> dict[str, Any]:
        return self.governance_engine.decide(
            {
                "type": "paired_quality_review",
                "event_type": "paired_quality_review",
                "source": "paired_quality_evidence_ledger",
                "provenance_scope": "sqlite",
                "provenance_verified": facts["provenance_valid"],
                "payload": {
                    "local_only": True,
                    "evidence_only": True,
                    "human_decision_recorded": True,
                    **facts,
                },
                "security_assessment": {"blocked": False, "findings": []},
            }
        )

    def _source_pairs(
        self,
        resolved: list[dict[str, Any]],
        paired_scores: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        task_scores = {
            item["task_id"]: item
            for item in paired_scores
        }
        return [
            {
                "task_id": pair["task_id"],
                "baseline_reference": pair["baseline"]["reference"],
                "candidate_reference": pair["candidate"]["reference"],
                "baseline_binding_id": pair["baseline"]["binding"]["binding_id"],
                "candidate_binding_id": pair["candidate"]["binding"]["binding_id"],
                "baseline_slot_id": task_scores.get(pair["task_id"], {}).get(
                    "baseline_slot_id"
                ),
                "candidate_slot_id": task_scores.get(pair["task_id"], {}).get(
                    "candidate_slot_id"
                ),
            }
            for pair in resolved
        ]

    def _evaluator_independent(self, pairs: list[dict[str, Any]]) -> bool:
        identity = self.scorer.identity
        evaluator_id = identity.get("evaluator_id")
        implementation_digest = identity.get("implementation_digest")
        if not self._safe_identifier(evaluator_id) or not self._valid_sha256(
            implementation_digest
        ):
            return False
        producer_values = {
            str(value)
            for pair in pairs
            for value in pair["baseline"]["model_contract"][:2]
        }
        return evaluator_id not in producer_values

    def _unavailable_measurement(self, sample_count: int) -> dict[str, Any]:
        return {
            "available": False,
            "metric_scope": self.METRIC_SCOPE,
            "sample_count": sample_count,
            "minimum_paired_samples": self.MINIMUM_PAIRED_SAMPLES,
            "baseline_mean": None,
            "candidate_mean": None,
            "paired_delta": None,
            "paired_scores": [],
            "uncertainty": paired_hoeffding_interval([], confidence_level=self.CONFIDENCE_LEVEL),
            "positive_effect_supported": False,
        }

    @staticmethod
    def _slot_id(nonce: str, task_id: str, binding_id: str) -> str:
        return f"slot-{PairedQualityEvidenceLedger._digest([nonce, task_id, binding_id])[:16]}"

    def _persist_status(self, value: dict[str, Any], status: str) -> dict[str, Any]:
        result = deepcopy(value)
        result["persistence"] = {"status": status}
        return result

    @staticmethod
    def _with_provenance(
        value: dict[str, Any],
        provenance: dict[str, Any],
        persistence_status: str,
    ) -> dict[str, Any]:
        result = deepcopy(value)
        result["persistence"] = {
            "status": persistence_status,
            "provenance_valid": provenance.get("valid", False),
        }
        result["state_provenance"] = {
            key: provenance.get(key)
            for key in ("valid", "entries", "latest_hash", "key_source")
            if key in provenance
        }
        return result

    @staticmethod
    def _compact_governance(value: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value.get(key)
            for key in ("authorized", "trust_level", "requires_human_approval", "reasons")
        }

    @staticmethod
    def _operation_blocked(current: dict[str, Any], reason: str) -> dict[str, Any]:
        result = deepcopy(current)
        result["operation"] = {"status": "blocked", "reason": reason}
        return result

    def _not_found(self, evaluation_id: str) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "id": evaluation_id,
            "status": "not_found",
            "task_quality": self._task_quality(
                status="not_found",
                measurement=self._unavailable_measurement(0),
                review=None,
            ),
        }

    def _safe_identifier(self, value: Any) -> str:
        candidate = str(value or "")
        return candidate if self._identifier.fullmatch(candidate) else ""

    def _valid_sha256(self, value: Any) -> bool:
        return isinstance(value, str) and bool(self._sha256.fullmatch(value))

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 6) if values else None

    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))

    @staticmethod
    def _digest(value: Any) -> str:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
