import asyncio
from copy import deepcopy
import hashlib
import json
from typing import Any

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
        task_quality = observations["task_quality"]
        if not task_quality["available"]:
            return self._comparison_result(
                "scaffold_only",
                baseline,
                comparable=False,
                reasons=["task_quality_unavailable"],
            )
        if baseline is None:
            if requested_baseline:
                return self._comparison_result(
                    "not_comparable",
                    None,
                    comparable=False,
                    reasons=["baseline_not_found"],
                )
            return self._comparison_result(
                "baseline_recorded",
                None,
                comparable=False,
                reasons=["immutable_baseline_recorded"],
            )

        reasons = self._contract_mismatch_reasons(baseline.get("contract", {}), contract)
        if reasons:
            return self._comparison_result(
                "not_comparable",
                baseline,
                comparable=False,
                reasons=reasons,
            )

        baseline_scores = self._score_map(baseline.get("observations", {}))
        candidate_scores = self._score_map(observations)
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
            "reasons": reasons,
        }

    def _contract(self, evaluation: dict[str, Any]) -> dict[str, Any]:
        suite = self._mapping(evaluation.get("suite"))
        provider = self._mapping(evaluation.get("provider"))
        contract = {
            "suite_version": suite.get("version"),
            "suite_hash": suite.get("hash"),
            "provider_name": provider.get("name"),
            "provider_model_version": provider.get("model_version"),
            "task_scoring_supported": bool(provider.get("task_scoring_supported", False)),
        }
        return {**contract, "hash": self._digest(contract)}

    def _observations(self, evaluation: dict[str, Any]) -> dict[str, Any]:
        task_quality = self._mapping(evaluation.get("task_quality"))
        scaffold = self._mapping(evaluation.get("scaffold_contract"))
        latency = self._mapping(evaluation.get("latency"))
        case_scores = []
        for case in evaluation.get("cases", []):
            if not isinstance(case, dict):
                continue
            maximized = self._mapping(case.get("maximized"))
            case_id = case.get("case_id")
            task_score = self._float_or_none(maximized.get("task_score"))
            if isinstance(case_id, str) and task_score is not None:
                case_scores.append({"case_id": case_id, "maximized_task_score": task_score})
        return {
            "status": evaluation.get("status"),
            "task_quality": {
                "available": bool(task_quality.get("available", False)),
                "baseline_mean": self._float_or_none(task_quality.get("baseline_mean")),
                "maximized_mean": self._float_or_none(task_quality.get("maximized_mean")),
                "paired_delta": self._float_or_none(task_quality.get("paired_delta")),
            },
            "case_task_scores": case_scores,
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
            "task_quality_uplift_claimed": bool(
                comparison.get("status") == "improved"
                and source_claims.get("task_quality_uplift_claimed", False)
            ),
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

    def _score_map(self, observations: dict[str, Any]) -> dict[str, float]:
        scores: dict[str, float] = {}
        for item in observations.get("case_task_scores", []):
            if not isinstance(item, dict):
                continue
            case_id = item.get("case_id")
            score = self._float_or_none(item.get("maximized_task_score"))
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
            ("task_scoring_supported", "task_scoring_contract_mismatch"),
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
            return float(value)
        return None

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
