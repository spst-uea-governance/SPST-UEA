import asyncio
from copy import deepcopy
import hashlib
import json
import math
from typing import Any

from spst_runtime.evaluation.quality_evidence import (
    CONTRACT_PROXY_SCHEMA,
    CONTRACT_PROXY_SCOPE,
    contract_compliance_proxy,
    validate_contract_compliance_proxy,
)
from spst_runtime.persistence.sqlite_repository import SQLiteRepository


class CalibrationRegistry:
    """Persist comparable local evaluation evidence without automatic promotion."""

    SCHEMA_VERSION = "calibration-registry-v1"
    INDEX_KEY = "runtime:calibration:index:v1"
    RECORD_KEY_PREFIX = "runtime:calibration:record:"
    MINIMUM_SAMPLE_COUNT = 2
    MINIMUM_EFFECT_SIZE = 0.05
    MAXIMUM_VARIANCE = 0.25

    def __init__(self, repository: SQLiteRepository):
        self.repository = repository

    def register(
        self,
        evaluation: dict[str, Any],
        *,
        candidate_id: str | None = None,
        baseline_id: str | None = None,
    ) -> dict[str, Any]:
        """Append compact evaluation evidence and derive a bounded comparison result."""
        self._validate_evaluation(evaluation)
        normalized_candidate = self._normalize_candidate_id(candidate_id)
        requested_baseline = self._normalize_baseline_id(baseline_id)
        with self.repository.locked():
            index = self._load_index()
            records = self._records(index)
            contract = self._contract(evaluation)
            observations = self._observations(evaluation)
            record_id = self._identifier(
                evaluation,
                normalized_candidate,
                requested_baseline,
                contract,
                observations,
            )
            existing = self._find_record(records, record_id)
            if existing is not None:
                return deepcopy(existing)

            baseline = self._select_baseline(records, contract, requested_baseline)
            comparison = self._comparison(
                observations,
                contract,
                baseline,
                requested_baseline,
            )
            role = self._role(baseline, requested_baseline)
            record = {
                "schema_version": self.SCHEMA_VERSION,
                "id": record_id,
                "sequence": len(records) + 1,
                "role": role,
                "candidate_id": normalized_candidate,
                "evaluation_id": str(evaluation["id"]),
                "baseline_id": baseline.get("id") if baseline else requested_baseline,
                "contract": contract,
                "observations": observations,
                "comparison": comparison,
                "claims": self._claims(evaluation, comparison),
                "policy": self._policy(comparison),
                "official_score_policy": self._official_score_policy(evaluation),
            }
            self._save_record(record, index)
            return deepcopy(record)

    def get(self, record_id: str) -> dict[str, Any] | None:
        """Return a detached immutable snapshot of one registered record."""
        with self.repository.locked():
            record = self._find_record(self._records(self._load_index()), record_id)
            return deepcopy(record) if record is not None else None

    def history(self) -> list[dict[str, Any]]:
        """Return compact registry history in append sequence order."""
        with self.repository.locked():
            records = self._records(self._load_index())
            ordered = sorted(records, key=lambda record: int(record.get("sequence", 0)))
            return deepcopy(ordered)

    def latest(self) -> dict[str, Any]:
        records = self.history()
        return records[-1] if records else {}

    def _load_index(self) -> dict[str, Any]:
        stored = asyncio.run(self.repository.load(self.INDEX_KEY))
        if not isinstance(stored, dict):
            return {"schema_version": self.SCHEMA_VERSION, "records": []}
        records = stored.get("records")
        if stored.get("schema_version") != self.SCHEMA_VERSION or not isinstance(records, list):
            raise ValueError("Invalid calibration registry index")
        return stored

    def _records(self, index: dict[str, Any]) -> list[dict[str, Any]]:
        records = index.get("records", [])
        return [record for record in records if isinstance(record, dict)]

    def _save_record(self, record: dict[str, Any], index: dict[str, Any]) -> None:
        records = self._records(index)
        updated_index = {
            "schema_version": self.SCHEMA_VERSION,
            "records": [*records, record],
        }
        asyncio.run(self.repository.save(f"{self.RECORD_KEY_PREFIX}{record['id']}", record))
        asyncio.run(self.repository.save(self.INDEX_KEY, updated_index))

    def _select_baseline(
        self,
        records: list[dict[str, Any]],
        contract: dict[str, Any],
        requested_baseline: str | None,
    ) -> dict[str, Any] | None:
        if requested_baseline:
            return self._find_record(records, requested_baseline)
        for record in records:
            if record.get("role") == "baseline" and record.get("contract") == contract:
                return record
        return None

    def _comparison(
        self,
        observations: dict[str, Any],
        contract: dict[str, Any],
        baseline: dict[str, Any] | None,
        requested_baseline: str | None,
    ) -> dict[str, Any]:
        measurement, metric_scope, semantic_quality = self._measurement(observations)
        if not measurement.get("available", False):
            return self._comparison_result(
                "scaffold_only",
                baseline,
                comparable=False,
                reasons=["task_quality_unavailable"],
                metric_scope=metric_scope,
                semantic_task_quality_established=semantic_quality,
            )
        if baseline is None:
            if requested_baseline:
                return self._comparison_result(
                    "not_comparable",
                    None,
                    comparable=False,
                    reasons=["baseline_not_found"],
                    metric_scope=metric_scope,
                    semantic_task_quality_established=semantic_quality,
                )
            return self._comparison_result(
                "baseline_recorded",
                None,
                comparable=False,
                reasons=["immutable_baseline_recorded"],
                metric_scope=metric_scope,
                semantic_task_quality_established=semantic_quality,
            )

        reasons = self._contract_mismatch_reasons(baseline.get("contract", {}), contract)
        if reasons:
            return self._comparison_result(
                "not_comparable",
                baseline,
                comparable=False,
                reasons=reasons,
                metric_scope=metric_scope,
                semantic_task_quality_established=semantic_quality,
            )

        baseline_scores = self._score_map(baseline.get("observations", {}), metric_scope)
        candidate_scores = self._score_map(observations, metric_scope)
        common_case_ids = sorted(set(baseline_scores) & set(candidate_scores))
        deltas = [
            round(candidate_scores[case_id] - baseline_scores[case_id], 6)
            for case_id in common_case_ids
        ]
        if len(deltas) < self.MINIMUM_SAMPLE_COUNT:
            return self._comparison_result(
                "insufficient_evidence",
                baseline,
                comparable=True,
                reasons=["minimum_sample_count_not_met"],
                sample_count=len(deltas),
                metric_scope=metric_scope,
                semantic_task_quality_established=semantic_quality,
            )

        mean_delta = self._mean(deltas)
        variance = self._variance(deltas, mean_delta)
        if variance > self.MAXIMUM_VARIANCE:
            return self._comparison_result(
                "insufficient_evidence",
                baseline,
                comparable=True,
                reasons=["variance_exceeds_bound"],
                sample_count=len(deltas),
                mean_delta=mean_delta,
                variance=variance,
                metric_scope=metric_scope,
                semantic_task_quality_established=semantic_quality,
            )
        if mean_delta >= self.MINIMUM_EFFECT_SIZE:
            status = "improved"
        elif mean_delta <= -self.MINIMUM_EFFECT_SIZE:
            status = "regressed"
        else:
            status = "neutral"
        return self._comparison_result(
            status,
            baseline,
            comparable=True,
            reasons=[],
            sample_count=len(deltas),
            mean_delta=mean_delta,
            variance=variance,
            metric_scope=metric_scope,
            semantic_task_quality_established=semantic_quality,
        )

    def _comparison_result(
        self,
        status: str,
        baseline: dict[str, Any] | None,
        *,
        comparable: bool,
        reasons: list[str],
        sample_count: int = 0,
        mean_delta: float | None = None,
        variance: float | None = None,
        metric_scope: str | None = None,
        semantic_task_quality_established: bool = False,
    ) -> dict[str, Any]:
        confidence = (
            "bounded"
            if comparable
            and sample_count >= self.MINIMUM_SAMPLE_COUNT
            and variance is not None
            and variance <= self.MAXIMUM_VARIANCE
            else "not_established"
        )
        return {
            "status": status,
            "baseline_id": baseline.get("id") if baseline else None,
            "comparable": comparable,
            "sample_count": sample_count,
            "mean_delta": mean_delta,
            "variance": variance,
            "minimum_effect_size": self.MINIMUM_EFFECT_SIZE,
            "confidence": confidence,
            "metric_scope": metric_scope,
            "semantic_task_quality_established": semantic_task_quality_established,
            "reasons": reasons,
        }

    def _contract(self, evaluation: dict[str, Any]) -> dict[str, Any]:
        suite = self._mapping(evaluation.get("suite"))
        provider = self._mapping(evaluation.get("provider"))
        proxy = self._mapping(evaluation.get("contract_compliance_proxy"))
        semantic_quality = False
        measurement_scope = (
            CONTRACT_PROXY_SCOPE
            if proxy.get("schema") == CONTRACT_PROXY_SCHEMA
            and proxy.get("metric_scope") == CONTRACT_PROXY_SCOPE
            else None
        )
        contract = {
            "suite_version": suite.get("version"),
            "suite_hash": suite.get("hash"),
            "provider_name": provider.get("name"),
            "provider_model_version": provider.get("model_version"),
            "contract_scoring_supported": bool(
                provider.get(
                    "contract_scoring_supported",
                    provider.get("task_scoring_supported", False),
                )
            ),
            "task_scoring_supported": bool(provider.get("task_scoring_supported", False)),
            "measurement_scope": measurement_scope,
            "semantic_task_quality_established": semantic_quality,
        }
        return {**contract, "hash": self._digest(contract)}

    def _observations(self, evaluation: dict[str, Any]) -> dict[str, Any]:
        task_quality = self._mapping(evaluation.get("task_quality"))
        source_proxy = self._mapping(evaluation.get("contract_compliance_proxy"))
        scaffold = self._mapping(evaluation.get("scaffold_contract"))
        latency = self._mapping(evaluation.get("latency"))
        source_cases = evaluation.get("cases")
        cases = (
            [case for case in source_cases if isinstance(case, dict)]
            if isinstance(source_cases, list)
            else []
        )
        source_proxy_valid, source_proxy_reason = validate_contract_compliance_proxy(
            source_proxy
        )
        recomputed_proxy = contract_compliance_proxy(cases, source_proxy_valid)
        proxy_matches_cases = source_proxy_valid and all(
            source_proxy.get(field) == recomputed_proxy.get(field)
            for field in (
                "schema",
                "available",
                "metric_scope",
                "baseline_mean",
                "maximized_mean",
                "paired_delta",
                "pair_count",
                "semantic_task_quality_established",
                "claim_eligible",
            )
        )
        proxy = (
            recomputed_proxy
            if proxy_matches_cases
            else contract_compliance_proxy(cases, False)
        )
        proxy_validation_reason = (
            None
            if proxy_matches_cases
            else source_proxy_reason or "contract_proxy_case_recomputation_mismatch"
        )
        case_contract_scores = []
        rejected_task_score_count = 0
        rejected_contract_score_count = 0
        for case in cases:
            maximized = self._mapping(case.get("maximized"))
            case_id = case.get("case_id")
            if maximized.get("task_score") is not None:
                rejected_task_score_count += 1
            contract_score = self._score_or_none(maximized.get("contract_score"))
            if maximized.get("contract_score") is not None and contract_score is None:
                rejected_contract_score_count += 1
            if (
                proxy_matches_cases
                and isinstance(case_id, str)
                and contract_score is not None
            ):
                case_contract_scores.append(
                    {"case_id": case_id, "maximized_contract_score": contract_score}
                )
        return {
            "status": evaluation.get("status"),
            "task_quality": {
                "available": False,
                "baseline_mean": None,
                "maximized_mean": None,
                "paired_delta": None,
                "semantic_task_quality_established": False,
                "claim_eligible": False,
                "source_available_attested": bool(task_quality.get("available", False)),
                "reason": "independent_quality_evidence_verifier_unavailable",
            },
            "contract_compliance_proxy": {
                "schema": proxy.get("schema"),
                "available": proxy.get("available") is True,
                "metric_scope": proxy.get("metric_scope"),
                "baseline_mean": self._float_or_none(proxy.get("baseline_mean")),
                "maximized_mean": self._float_or_none(proxy.get("maximized_mean")),
                "paired_delta": self._float_or_none(proxy.get("paired_delta")),
                "pair_count": proxy.get("pair_count", 0),
                "semantic_task_quality_established": False,
                "claim_eligible": False,
                "validation_status": "verified" if proxy_matches_cases else "rejected",
                "validation_reason": proxy_validation_reason,
                "case_recomputed": proxy_matches_cases,
            },
            "case_task_scores": [],
            "rejected_task_score_count": rejected_task_score_count,
            "case_contract_scores": case_contract_scores,
            "rejected_contract_score_count": rejected_contract_score_count,
            "scaffold_contract": {
                "baseline_mean": self._float_or_none(scaffold.get("baseline_mean")),
                "maximized_mean": self._float_or_none(scaffold.get("maximized_mean")),
                "paired_delta": self._float_or_none(scaffold.get("paired_delta")),
            },
            "latency": {
                "baseline_mean_ms": self._float_or_none(latency.get("baseline_mean_ms")),
                "maximized_mean_ms": self._float_or_none(latency.get("maximized_mean_ms")),
                "overhead_mean_ms": self._float_or_none(latency.get("overhead_mean_ms")),
            },
        }

    def _claims(
        self,
        evaluation: dict[str, Any],
        comparison: dict[str, Any],
    ) -> dict[str, Any]:
        source_claims = self._mapping(evaluation.get("claims"))
        return {
            "task_quality_uplift_claimed": False,
            "source_uplift_claim_rejected": bool(
                source_claims.get("task_quality_uplift_claimed", False)
            ),
            "measurement_scope": comparison.get("metric_scope"),
            "requires_human_interpretation": True,
            "automatic_adoption": False,
        }

    def _policy(self, comparison: dict[str, Any]) -> dict[str, Any]:
        regressed = comparison.get("status") == "regressed"
        return {
            "automatic_adoption": False,
            "requires_human_approval": regressed,
            "recommended_action": (
                "hold_for_human_approval" if regressed else "record_evidence_only"
            ),
        }

    def _official_score_policy(self, evaluation: dict[str, Any]) -> dict[str, Any]:
        policy = self._mapping(evaluation.get("official_score_policy"))
        return {
            "model_weight_score_unchanged": bool(
                policy.get("model_weight_score_unchanged", True)
            ),
            "official_benchmark_claimed": False,
        }

    def _measurement(
        self,
        observations: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None, bool]:
        proxy = self._mapping(observations.get("contract_compliance_proxy"))
        proxy_valid, _ = validate_contract_compliance_proxy(proxy)
        if proxy_valid and proxy.get("validation_status") == "verified":
            return proxy, CONTRACT_PROXY_SCOPE, False
        return {"available": False}, CONTRACT_PROXY_SCOPE, False

    def _score_map(
        self,
        observations: dict[str, Any],
        metric_scope: str | None,
    ) -> dict[str, float]:
        scores: dict[str, float] = {}
        proxy = self._mapping(observations.get("contract_compliance_proxy"))
        proxy_valid, _ = validate_contract_compliance_proxy(proxy)
        if (
            not proxy_valid
            or proxy.get("validation_status") != "verified"
            or metric_scope != proxy.get("metric_scope")
        ):
            return scores
        for item in observations.get("case_contract_scores", []):
            if not isinstance(item, dict):
                continue
            case_id = item.get("case_id")
            score = self._score_or_none(item.get("maximized_contract_score"))
            if isinstance(case_id, str) and score is not None:
                scores[case_id] = score
        return scores

    def _contract_mismatch_reasons(
        self,
        baseline_contract: dict[str, Any],
        candidate_contract: dict[str, Any],
    ) -> list[str]:
        fields = (
            ("suite_version", "suite_version_mismatch"),
            ("suite_hash", "suite_hash_mismatch"),
            ("provider_name", "provider_name_mismatch"),
            ("provider_model_version", "provider_model_version_mismatch"),
            ("contract_scoring_supported", "contract_scoring_contract_mismatch"),
            ("task_scoring_supported", "task_scoring_contract_mismatch"),
            ("measurement_scope", "measurement_scope_mismatch"),
            (
                "semantic_task_quality_established",
                "semantic_task_quality_contract_mismatch",
            ),
        )
        return [
            reason
            for field, reason in fields
            if baseline_contract.get(field) != candidate_contract.get(field)
        ]

    def _role(
        self,
        baseline: dict[str, Any] | None,
        requested_baseline: str | None,
    ) -> str:
        if baseline is None and requested_baseline is None:
            return "baseline"
        return "candidate"

    def _find_record(
        self,
        records: list[dict[str, Any]],
        record_id: str,
    ) -> dict[str, Any] | None:
        for record in records:
            if record.get("id") == record_id:
                return record
        return None

    def _identifier(
        self,
        evaluation: dict[str, Any],
        candidate_id: str,
        baseline_id: str | None,
        contract: dict[str, Any],
        observations: dict[str, Any],
    ) -> str:
        value = {
            "evaluation_id": evaluation["id"],
            "candidate_id": candidate_id,
            "baseline_id": baseline_id,
            "contract": contract,
            "observations": observations,
        }
        return f"CAL-{self._digest(value)[:16]}"

    def _normalize_candidate_id(self, value: str | None) -> str:
        candidate = str(value or "runtime-default")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
        if candidate and len(candidate) <= 96 and set(candidate) <= allowed:
            return candidate
        return f"candidate-{self._digest(candidate)[:16]}"

    def _normalize_baseline_id(self, value: str | None) -> str | None:
        return str(value) if isinstance(value, str) and value else None

    def _validate_evaluation(self, evaluation: dict[str, Any]) -> None:
        if not isinstance(evaluation, dict) or not evaluation.get("id"):
            raise ValueError("Calibration registry requires a completed evaluation report")
        suite = self._mapping(evaluation.get("suite"))
        provider = self._mapping(evaluation.get("provider"))
        if not suite.get("hash") or not provider.get("name"):
            raise ValueError("Evaluation report lacks a comparable suite or provider contract")

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            result = float(value)
            return result if math.isfinite(result) else None
        return None

    @staticmethod
    def _score_or_none(value: Any) -> float | None:
        if not isinstance(value, float) or not math.isfinite(value):
            return None
        return value if 0.0 <= value <= 1.0 else None

    @staticmethod
    def _mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 6) if values else 0.0

    @staticmethod
    def _variance(values: list[float], mean: float) -> float:
        if not values:
            return 0.0
        return round(sum((value - mean) ** 2 for value in values) / len(values), 6)

    @staticmethod
    def _digest(value: Any) -> str:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
