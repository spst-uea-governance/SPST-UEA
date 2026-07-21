from decimal import Decimal
import hashlib
import json
import math
import re
from typing import Any, cast


CONTRACT_PROXY_SCHEMA = "spst-contract-compliance-proxy-v1"
CONTRACT_PROXY_SCOPE = "required_marker_and_json_shape_coverage"
TASK_QUALITY_SCHEMA = "spst-task-quality-evidence-v1"
TASK_QUALITY_UNAVAILABLE_REASON = (
    "independent_blinded_paired_outcome_measurement_unavailable"
)
TASK_QUALITY_REQUIRED_EVIDENCE = (
    "same_model_same_tasks",
    "verified_distinct_producer_runs",
    "arm_blinded_task_specific_scoring",
    "independent_evaluator_identity",
    "producer_bound_scoring_artifacts",
    "minimum_paired_sample_and_uncertainty_bound",
    "human_review_before_adoption",
)
QUALITY_RUBRIC_SCHEMA = "task-specific-json-rubric-v1"
SCORING_MATERIAL_SCHEMA = "producer-scoring-material-v1"
BLINDED_SCORER_SCHEMA = "independent-exact-json-scorer-v1"
MAX_SCORING_ARTIFACT_BYTES = 1_000_000
_CRITERION_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def contract_compliance_proxy(
    cases: list[dict[str, Any]],
    scoring_supported: bool,
) -> dict[str, Any]:
    """Summarize deterministic contract checks without calling them task quality."""
    baseline_scores = [_contract_score(case, "baseline") for case in cases]
    maximized_scores = [_contract_score(case, "maximized") for case in cases]
    available = scoring_supported is True and bool(cases) and all(
        _is_score(score) for score in [*baseline_scores, *maximized_scores]
    )
    if not available:
        return {
            "schema": CONTRACT_PROXY_SCHEMA,
            "available": False,
            "metric_scope": CONTRACT_PROXY_SCOPE,
            "baseline_mean": None,
            "maximized_mean": None,
            "paired_delta": None,
            "pair_count": 0,
            "semantic_task_quality_established": False,
            "claim_eligible": False,
        }

    baseline = [float(score) for score in baseline_scores]
    maximized = [float(score) for score in maximized_scores]
    baseline_mean = _mean(baseline)
    maximized_mean = _mean(maximized)
    return {
        "schema": CONTRACT_PROXY_SCHEMA,
        "available": True,
        "metric_scope": CONTRACT_PROXY_SCOPE,
        "baseline_mean": baseline_mean,
        "maximized_mean": maximized_mean,
        "paired_delta": round(maximized_mean - baseline_mean, 6),
        "pair_count": len(cases),
        "semantic_task_quality_established": False,
        "claim_eligible": False,
    }


def unresolved_task_quality(proxy: dict[str, Any]) -> dict[str, Any]:
    """Return a fail-closed quality claim until independent paired evidence exists."""
    proxy_valid, _ = validate_contract_compliance_proxy(proxy)
    return {
        "schema": TASK_QUALITY_SCHEMA,
        "available": False,
        "baseline_mean": None,
        "maximized_mean": None,
        "paired_delta": None,
        "pair_count": 0,
        "semantic_task_quality_established": False,
        "claim_eligible": False,
        "reason": TASK_QUALITY_UNAVAILABLE_REASON,
        "proxy_scores_available": proxy_valid,
        "proxy_metric_scope": proxy.get("metric_scope"),
        "required_evidence": list(TASK_QUALITY_REQUIRED_EVIDENCE),
    }


def proxy_calibration(proxy: dict[str, Any], *, blocked: bool = False) -> dict[str, Any]:
    """Calibrate the proxy while preserving the boundary around semantic quality."""
    proxy_valid, _ = validate_contract_compliance_proxy(proxy)
    if blocked:
        status = "blocked"
    elif not proxy_valid:
        status = "scaffold_only"
    else:
        delta = float(proxy["paired_delta"])
        status = "improved" if delta > 0 else "regressed" if delta < 0 else "neutral"
    return {
        "status": status,
        "measurement_class": "contract_compliance_proxy",
        "metric_scope": CONTRACT_PROXY_SCOPE,
        "semantic_task_quality_established": False,
        "task_quality_claim_eligible": False,
        "automatic_adoption": False,
        "requires_human_interpretation": True,
    }


def quality_claim_boundary(proxy: dict[str, Any]) -> dict[str, Any]:
    """Expose why a positive proxy delta cannot become an uplift claim."""
    proxy_valid, proxy_reason = validate_contract_compliance_proxy(proxy)
    reasons = [
        "contract_proxy_not_semantic_task_quality",
        TASK_QUALITY_UNAVAILABLE_REASON,
    ]
    if not proxy_valid:
        reasons.insert(0, proxy_reason or "contract_proxy_invalid")
    return {
        "eligible": False,
        "status": "blocked",
        "metric_scope": proxy.get("metric_scope", CONTRACT_PROXY_SCOPE),
        "reasons": reasons,
    }


def validate_contract_compliance_proxy(proxy: Any) -> tuple[bool, str | None]:
    """Validate a self-contained proxy before it can influence calibration status."""
    if not isinstance(proxy, dict):
        return False, "contract_proxy_not_mapping"
    if proxy.get("schema") != CONTRACT_PROXY_SCHEMA:
        return False, "contract_proxy_schema_mismatch"
    if proxy.get("metric_scope") != CONTRACT_PROXY_SCOPE:
        return False, "contract_proxy_scope_mismatch"
    if proxy.get("available") is not True:
        return False, "contract_proxy_unavailable"
    if proxy.get("semantic_task_quality_established") is not False:
        return False, "contract_proxy_semantic_claim_forbidden"
    if proxy.get("claim_eligible") is not False:
        return False, "contract_proxy_claim_eligibility_forbidden"

    pair_count = proxy.get("pair_count")
    if isinstance(pair_count, bool) or not isinstance(pair_count, int) or pair_count <= 0:
        return False, "contract_proxy_pair_count_invalid"
    baseline = proxy.get("baseline_mean")
    maximized = proxy.get("maximized_mean")
    delta = proxy.get("paired_delta")
    if not _is_score(baseline) or not _is_score(maximized) or not _is_delta(delta):
        return False, "contract_proxy_score_invalid"
    baseline_value = cast(float, baseline)
    maximized_value = cast(float, maximized)
    delta_value = cast(float, delta)
    if round(maximized_value - baseline_value, 6) != delta_value:
        return False, "contract_proxy_delta_mismatch"
    return True, None


def normalize_quality_rubric(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Normalize a bounded exact-JSON rubric or reject the whole contract."""
    if value is None:
        return None, None
    if not isinstance(value, dict) or value.get("schema") != QUALITY_RUBRIC_SCHEMA:
        return None, "quality_rubric_schema_invalid"
    criteria = value.get("criteria")
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 16:
        return None, "quality_rubric_criteria_invalid"

    normalized: list[dict[str, Any]] = []
    criterion_ids: set[str] = set()
    total_weight = 0.0
    for criterion in criteria:
        if not isinstance(criterion, dict) or set(criterion) != {
            "id",
            "json_pointer",
            "expected",
            "weight",
        }:
            return None, "quality_rubric_criterion_shape_invalid"
        criterion_id = criterion.get("id")
        pointer = criterion.get("json_pointer")
        weight = criterion.get("weight")
        expected = _typed_scalar(criterion.get("expected"))
        if not isinstance(criterion_id, str) or not _CRITERION_ID.fullmatch(criterion_id):
            return None, "quality_rubric_criterion_id_invalid"
        if criterion_id in criterion_ids:
            return None, "quality_rubric_criterion_id_duplicate"
        if not _valid_json_pointer(pointer):
            return None, "quality_rubric_json_pointer_invalid"
        if expected is None:
            return None, "quality_rubric_expected_scalar_invalid"
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            return None, "quality_rubric_weight_invalid"
        normalized_weight = float(weight)
        if not math.isfinite(normalized_weight) or not 0.0 < normalized_weight <= 1.0:
            return None, "quality_rubric_weight_invalid"
        normalized_weight = round(normalized_weight, 9)
        total_weight += normalized_weight
        criterion_ids.add(criterion_id)
        normalized.append(
            {
                "id": criterion_id,
                "json_pointer": pointer,
                "expected": expected,
                "weight": normalized_weight,
            }
        )
    if not math.isclose(total_weight, 1.0, abs_tol=1e-9):
        return None, "quality_rubric_weights_must_sum_to_one"
    normalized_rubric = {
        "schema": QUALITY_RUBRIC_SCHEMA,
        "criteria": sorted(normalized, key=lambda item: item["id"]),
    }
    valid, reason = validate_quality_rubric(normalized_rubric)
    return (normalized_rubric, None) if valid else (None, reason)


def validate_quality_rubric(value: Any) -> tuple[bool, str | None]:
    """Validate the already-normalized private rubric stored by the corpus."""
    if not isinstance(value, dict) or value.get("schema") != QUALITY_RUBRIC_SCHEMA:
        return False, "quality_rubric_schema_invalid"
    criteria = value.get("criteria")
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 16:
        return False, "quality_rubric_criteria_invalid"
    ids: list[str] = []
    total_weight = 0.0
    for criterion in criteria:
        if not isinstance(criterion, dict) or set(criterion) != {
            "id",
            "json_pointer",
            "expected",
            "weight",
        }:
            return False, "quality_rubric_criterion_shape_invalid"
        criterion_id = criterion.get("id")
        pointer = criterion.get("json_pointer")
        weight = criterion.get("weight")
        if not isinstance(criterion_id, str) or not _CRITERION_ID.fullmatch(criterion_id):
            return False, "quality_rubric_criterion_id_invalid"
        if not _valid_json_pointer(pointer):
            return False, "quality_rubric_json_pointer_invalid"
        if not _valid_typed_scalar(criterion.get("expected")):
            return False, "quality_rubric_expected_scalar_invalid"
        if isinstance(weight, bool) or not isinstance(weight, float):
            return False, "quality_rubric_weight_invalid"
        if not math.isfinite(weight) or not 0.0 < weight <= 1.0:
            return False, "quality_rubric_weight_invalid"
        ids.append(criterion_id)
        total_weight += weight
    if len(ids) != len(set(ids)) or ids != sorted(ids):
        return False, "quality_rubric_criterion_order_invalid"
    if not math.isclose(total_weight, 1.0, abs_tol=1e-9):
        return False, "quality_rubric_weights_must_sum_to_one"
    return True, None


def quality_rubric_digest(rubric: Any) -> str | None:
    valid, _ = validate_quality_rubric(rubric)
    return _digest(rubric) if valid else None


def build_producer_scoring_material(text: str, rubric: Any) -> dict[str, Any]:
    """Project only rubric-addressed JSON scalars while producer output is available."""
    rubric_digest = quality_rubric_digest(rubric)
    output_digest = hashlib.sha256(str(text).encode("utf-8")).hexdigest()
    base: dict[str, Any] = {
        "schema": SCORING_MATERIAL_SCHEMA,
        "status": "unresolved",
        "reason": "quality_rubric_invalid",
        "rubric_digest": rubric_digest,
        "output_digest": output_digest,
        "observations": [],
    }
    if rubric_digest is None:
        return _with_material_digest(base)
    if not isinstance(text, str):
        return _with_material_digest({**base, "reason": "artifact_format_unresolved"})
    if len(text.encode("utf-8")) > MAX_SCORING_ARTIFACT_BYTES:
        return _with_material_digest({**base, "reason": "artifact_size_limit_exceeded"})
    try:
        parsed = json.loads(
            text,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_non_finite,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (DuplicateJSONKeyError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        return _with_material_digest({**base, "reason": "artifact_format_unresolved"})

    observations: list[dict[str, Any]] = []
    for criterion in rubric["criteria"]:
        found, raw_value = _resolve_json_pointer(parsed, criterion["json_pointer"])
        observed = _typed_scalar(raw_value) if found else None
        observations.append(
            {
                "criterion_id": criterion["id"],
                "status": "observed" if observed is not None else "missing_or_unsupported",
                "observed": observed,
            }
        )
    return _with_material_digest(
        {
            **base,
            "status": "ready",
            "reason": None,
            "observations": observations,
        }
    )


def validate_producer_scoring_material(value: Any) -> tuple[bool, str | None]:
    """Validate self-contained producer material before binding or scoring it."""
    if not isinstance(value, dict) or value.get("schema") != SCORING_MATERIAL_SCHEMA:
        return False, "scoring_material_schema_invalid"
    if value.get("status") not in {"ready", "unresolved"}:
        return False, "scoring_material_status_invalid"
    if not _valid_sha256(value.get("output_digest")):
        return False, "scoring_material_output_digest_invalid"
    rubric_digest = value.get("rubric_digest")
    if rubric_digest is not None and not _valid_sha256(rubric_digest):
        return False, "scoring_material_rubric_digest_invalid"
    material_digest = value.get("material_digest")
    unsigned = {key: item for key, item in value.items() if key != "material_digest"}
    if not _valid_sha256(material_digest) or material_digest != _digest(unsigned):
        return False, "scoring_material_digest_mismatch"
    observations = value.get("observations")
    if not isinstance(observations, list):
        return False, "scoring_material_observations_invalid"
    if value.get("status") == "unresolved":
        if value.get("reason") not in {
            "artifact_format_unresolved",
            "artifact_size_limit_exceeded",
            "quality_rubric_invalid",
        } or observations:
            return False, "scoring_material_unresolved_invalid"
        return True, None
    if value.get("reason") is not None or not _valid_sha256(rubric_digest):
        return False, "scoring_material_ready_invalid"
    seen: set[str] = set()
    for observation in observations:
        if not isinstance(observation, dict) or set(observation) != {
            "criterion_id",
            "status",
            "observed",
        }:
            return False, "scoring_material_observation_shape_invalid"
        criterion_id = observation.get("criterion_id")
        status = observation.get("status")
        observed = observation.get("observed")
        if not isinstance(criterion_id, str) or not _CRITERION_ID.fullmatch(criterion_id):
            return False, "scoring_material_criterion_id_invalid"
        if criterion_id in seen:
            return False, "scoring_material_criterion_duplicate"
        if status == "observed" and not _valid_typed_scalar(observed):
            return False, "scoring_material_observed_invalid"
        if status == "missing_or_unsupported" and observed is not None:
            return False, "scoring_material_missing_value_invalid"
        if status not in {"observed", "missing_or_unsupported"}:
            return False, "scoring_material_observation_status_invalid"
        seen.add(criterion_id)
    return True, None


class IndependentExactJsonScorer:
    """Score anonymous JSON projections without receiving producer or arm identity."""

    SCHEMA = BLINDED_SCORER_SCHEMA

    @property
    def identity(self) -> dict[str, str]:
        specification = {
            "schema": self.SCHEMA,
            "input": "anonymous_rubric_scoped_scalar_projection",
            "score": "weighted_exact_semantic_scalar_match",
        }
        return {
            "evaluator_id": "spst-independent-exact-json-scorer",
            "version": "v1",
            "implementation_digest": _digest(specification),
        }

    def score(
        self,
        rubric: dict[str, Any],
        anonymous_materials: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return fixed scores; packets deliberately contain no task, arm, or run labels."""
        valid_rubric, reason = validate_quality_rubric(rubric)
        if not valid_rubric:
            raise ValueError(reason or "quality_rubric_invalid")
        rubric_digest = quality_rubric_digest(rubric)
        results: list[dict[str, Any]] = []
        for packet in anonymous_materials:
            if not isinstance(packet, dict) or set(packet) != {"slot_id", "material"}:
                raise ValueError("anonymous_scoring_packet_invalid")
            slot_id = packet.get("slot_id")
            material = packet.get("material")
            if not isinstance(slot_id, str) or not slot_id.startswith("slot-"):
                raise ValueError("anonymous_slot_invalid")
            valid_material, material_reason = validate_producer_scoring_material(material)
            if (
                not valid_material
                or not isinstance(material, dict)
                or material.get("status") != "ready"
            ):
                raise ValueError(material_reason or "scoring_material_unresolved")
            if material.get("rubric_digest") != rubric_digest:
                raise ValueError("scoring_material_rubric_mismatch")
            observations = {
                observation["criterion_id"]: observation
                for observation in material["observations"]
            }
            if set(observations) != {criterion["id"] for criterion in rubric["criteria"]}:
                raise ValueError("scoring_material_criteria_mismatch")
            criterion_results: list[dict[str, Any]] = []
            score = 0.0
            for criterion in rubric["criteria"]:
                observation = observations[criterion["id"]]
                matched = (
                    observation["status"] == "observed"
                    and observation["observed"] == criterion["expected"]
                )
                if matched:
                    score += criterion["weight"]
                criterion_results.append(
                    {
                        "criterion_id": criterion["id"],
                        "weight": criterion["weight"],
                        "matched": matched,
                        "expected_digest": _digest(criterion["expected"]),
                        "observed_digest": (
                            _digest(observation["observed"])
                            if observation["status"] == "observed"
                            else None
                        ),
                    }
                )
            results.append(
                {
                    "slot_id": slot_id,
                    "score": round(score, 6),
                    "criteria": criterion_results,
                    "material_digest": material["material_digest"],
                }
            )
        return sorted(results, key=lambda item: item["slot_id"])


def paired_hoeffding_interval(
    deltas: list[float],
    *,
    confidence_level: float = 0.95,
) -> dict[str, Any]:
    """Return a distribution-free two-sided bound for deltas in [-1, 1]."""
    if (
        isinstance(confidence_level, bool)
        or not isinstance(confidence_level, float)
        or not 0.0 < confidence_level < 1.0
        or not deltas
        or any(not _is_delta(value) for value in deltas)
    ):
        return {
            "available": False,
            "method": "hoeffding_bounded_paired_delta",
            "confidence_level": confidence_level,
            "lower_bound": None,
            "upper_bound": None,
            "half_width": None,
        }
    mean_delta = sum(deltas) / len(deltas)
    alpha = 1.0 - confidence_level
    half_width = math.sqrt(2.0 * math.log(2.0 / alpha) / len(deltas))
    return {
        "available": True,
        "method": "hoeffding_bounded_paired_delta",
        "confidence_level": confidence_level,
        "lower_bound": round(max(-1.0, mean_delta - half_width), 6),
        "upper_bound": round(min(1.0, mean_delta + half_width), 6),
        "half_width": round(half_width, 6),
    }


def _is_score(value: Any) -> bool:
    return isinstance(value, float) and math.isfinite(value) and 0.0 <= value <= 1.0


def _is_delta(value: Any) -> bool:
    return isinstance(value, float) and math.isfinite(value) and -1.0 <= value <= 1.0


def _contract_score(case: Any, arm: str) -> Any:
    if not isinstance(case, dict):
        return None
    result = case.get(arm)
    return result.get("contract_score") if isinstance(result, dict) else None


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 6) if values else 0.0


def _typed_scalar(value: Any) -> list[Any] | None:
    if value is None:
        return ["null", None]
    if isinstance(value, bool):
        return ["boolean", value]
    if isinstance(value, str):
        return ["string", value] if len(value.encode("utf-8")) <= 256 else None
    if isinstance(value, Decimal):
        return ["number", _decimal_identity(value)] if value.is_finite() else None
    if isinstance(value, int):
        return ["number", _decimal_identity(Decimal(value))]
    if isinstance(value, float) and math.isfinite(value):
        return ["number", _decimal_identity(Decimal(str(value)))]
    return None


def _valid_typed_scalar(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != 2:
        return False
    kind, item = value
    if kind == "null":
        return item is None
    if kind == "boolean":
        return isinstance(item, bool)
    if kind == "string":
        return isinstance(item, str) and len(item.encode("utf-8")) <= 256
    if kind == "number":
        return isinstance(item, str) and bool(re.fullmatch(r"[01]:[0-9]+:-?[0-9]+", item))
    return False


def _decimal_identity(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("non-finite number")
    if value.is_zero():
        return "0:0:0"
    sign, digits, raw_exponent = value.as_tuple()
    if not isinstance(raw_exponent, int):
        raise ValueError("non-finite number")
    exponent = raw_exponent
    normalized_digits = list(digits)
    while len(normalized_digits) > 1 and normalized_digits[-1] == 0:
        normalized_digits.pop()
        exponent += 1
    return f"{sign}:{''.join(str(digit) for digit in normalized_digits)}:{exponent}"


def _valid_json_pointer(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 512:
        return False
    segments = value[1:].split("/")
    if not 1 <= len(segments) <= 8:
        return False
    for segment in segments:
        if not segment or len(segment) > 64 or re.search(r"~(?![01])", segment):
            return False
    return True


def _resolve_json_pointer(value: Any, pointer: str) -> tuple[bool, Any]:
    current = value
    for encoded in pointer[1:].split("/"):
        segment = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            return False, None
    return True, current


class DuplicateJSONKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJSONKeyError(key)
        result[key] = value
    return result


def _reject_non_finite(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def _with_material_digest(value: dict[str, Any]) -> dict[str, Any]:
    return {**value, "material_digest": _digest(value)}


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def _digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
