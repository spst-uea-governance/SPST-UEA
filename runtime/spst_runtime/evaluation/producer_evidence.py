from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from typing import Any

from spst_runtime.evaluation.quality_evidence import (
    validate_producer_scoring_material,
)


SCHEMA_VERSION = "producer-evidence-v3"
LEGACY_SCHEMA_VERSION = "producer-evidence-v2"
ARMS = ("baseline", "maximized")
ARTIFACT_SEMANTIC_PROFILE = "contract-json-v1"
MAX_CANONICAL_ARTIFACT_BYTES = 1_000_000
_SEMANTIC_REASONS = {
    "artifact_contract_missing",
    "artifact_contract_keys_invalid",
    "artifact_contract_keys_missing",
    "artifact_format_unresolved",
    "artifact_json_duplicate_key",
    "artifact_json_object_required",
    "artifact_size_limit_exceeded",
    "free_text_semantics_unresolved",
    "semantic_artifact_not_recorded",
    "semantic_artifact_record_invalid",
}


def canonical_artifact_semantics(
    text: str,
    expected_json_keys: Any,
) -> dict[str, Any]:
    """Canonicalize only contract-scoped JSON; fail closed for unsafe semantics."""
    keys = _expected_keys(expected_json_keys)
    base = {
        "profile": ARTIFACT_SEMANTIC_PROFILE,
        "status": "unresolved",
        "reason": None,
        "semantic_digest": None,
    }
    if keys is None:
        return {**base, "reason": "artifact_contract_keys_invalid"}
    if not keys:
        return {**base, "reason": "artifact_contract_missing"}
    if not isinstance(text, str):
        return {**base, "reason": "artifact_format_unresolved"}
    if len(text.encode("utf-8")) > MAX_CANONICAL_ARTIFACT_BYTES:
        return {**base, "reason": "artifact_size_limit_exceeded"}
    try:
        parsed = json.loads(
            text,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_non_finite_number,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except json.JSONDecodeError:
        return {**base, "reason": "artifact_format_unresolved"}
    except DuplicateJSONKeyError:
        return {**base, "reason": "artifact_json_duplicate_key"}
    except (RecursionError, TypeError, ValueError):
        return {**base, "reason": "artifact_format_unresolved"}
    if not isinstance(parsed, dict):
        return {**base, "reason": "artifact_json_object_required"}
    if any(key not in parsed for key in keys):
        return {**base, "reason": "artifact_contract_keys_missing"}

    projection = {key: parsed[key] for key in keys}
    try:
        semantic_digest = _digest(_semantic_node(projection))
    except (RecursionError, TypeError, ValueError):
        return {**base, "reason": "artifact_format_unresolved"}
    if _contains_free_text(projection):
        return {
            **base,
            "reason": "free_text_semantics_unresolved",
            "semantic_digest": semantic_digest,
        }
    return {
        **base,
        "status": "resolved",
        "reason": None,
        "semantic_digest": semantic_digest,
    }


def build_producer_evidence(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive compact task/arm bindings without retaining prompt or model text."""
    producer_run_id = report.get("id")
    if not isinstance(producer_run_id, str) or not producer_run_id:
        return []
    suite = _mapping(report.get("suite"))
    provider = _mapping(report.get("provider"))
    candidate_by_arm = {
        "baseline": report.get("baseline_candidate_id"),
        "maximized": report.get("candidate_id"),
    }
    bindings: list[dict[str, Any]] = []
    cases = report.get("cases")
    for case in cases if isinstance(cases, list) else []:
        if not isinstance(case, dict):
            continue
        task_id = case.get("case_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        for arm in ARMS:
            result = _mapping(case.get(arm))
            candidate_id = candidate_by_arm[arm]
            artifact_digest = result.get("output_digest")
            if not isinstance(candidate_id, str) or not candidate_id:
                continue
            if not isinstance(artifact_digest, str) or not artifact_digest:
                continue
            configuration_digest = _digest(
                {
                    "suite": {
                        "version": suite.get("version"),
                        "hash": suite.get("hash"),
                        "split": suite.get("split"),
                    },
                    "provider": {
                        "name": provider.get("name"),
                        "model_version": provider.get("model_version"),
                        "task_scoring_supported": provider.get(
                            "task_scoring_supported"
                        ),
                    },
                    "task": {
                        "id": task_id,
                        "domain": case.get("domain"),
                        "prompt_digest": case.get("prompt_digest"),
                    },
                    "arm": arm,
                    "plan": _mapping(result.get("plan")),
                }
            )
            semantic_configuration = _semantic_configuration(
                suite=suite,
                provider=provider,
                case=case,
                result=result,
            )
            artifact_semantics = _artifact_semantic_binding(
                result.get("artifact_semantics")
            )
            scoring_material_present = "scoring_material" in result
            scoring_material = _scoring_material_binding(
                result.get("scoring_material")
            )
            binding = {
                "schema_version": (
                    SCHEMA_VERSION if scoring_material_present else LEGACY_SCHEMA_VERSION
                ),
                "producer_run_id": producer_run_id,
                "task_id": task_id,
                "candidate_id": candidate_id,
                "arm": arm,
                "configuration_digest": configuration_digest,
                "semantic_configuration_status": semantic_configuration["status"],
                "semantic_configuration_reason": semantic_configuration["reason"],
                "semantic_configuration_digest": semantic_configuration[
                    "semantic_digest"
                ],
                "artifact_digest": artifact_digest,
                "artifact_semantic_profile": artifact_semantics["profile"],
                "artifact_semantic_status": artifact_semantics["status"],
                "artifact_semantic_reason": artifact_semantics["reason"],
                "artifact_semantic_digest": artifact_semantics["semantic_digest"],
                "producer_status": result.get("status"),
                "verification_status": _mapping(result.get("verification")).get(
                    "status"
                ),
            }
            if scoring_material_present:
                binding.update(
                    {
                        **scoring_material,
                        "producer_run_instance_id": report.get(
                            "producer_run_instance_id"
                        ),
                        "producer_execution_id": result.get(
                            "producer_execution_id"
                        ),
                    }
                )
            binding["binding_id"] = f"PBIND-{_digest(binding)[:16]}"
            bindings.append(binding)
    return sorted(bindings, key=lambda item: (str(item["task_id"]), str(item["arm"])))


def verified_producer_binding(
    report: dict[str, Any],
    binding_id: str,
) -> dict[str, Any] | None:
    """Return a binding only when persisted and recomputed forms match exactly."""
    expected = next(
        (
            binding
            for binding in build_producer_evidence(report)
            if binding.get("binding_id") == binding_id
        ),
        None,
    )
    stored_bindings = report.get("producer_evidence")
    stored_values = stored_bindings if isinstance(stored_bindings, list) else []
    stored = next(
        (
            binding
            for binding in stored_values
            if isinstance(binding, dict) and binding.get("binding_id") == binding_id
        ),
        None,
    )
    if expected is None or stored != expected:
        return None
    return deepcopy(expected)


def _semantic_configuration(
    *,
    suite: dict[str, Any],
    provider: dict[str, Any],
    case: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    plan = _mapping(result.get("plan"))
    domains = plan.get("domains", [])
    if not isinstance(domains, list) or not all(
        isinstance(domain, str) for domain in domains
    ):
        return _unresolved_configuration("semantic_configuration_plan_invalid")
    mode = plan.get("mode")
    if mode is not None and not isinstance(mode, str):
        return _unresolved_configuration("semantic_configuration_plan_invalid")
    counts = (plan.get("strategy_count"), plan.get("verification_check_count"))
    if any(
        value is not None
        and (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        )
        for value in counts
    ):
        return _unresolved_configuration("semantic_configuration_plan_invalid")
    semantic_value = {
        "suite": {
            "version": suite.get("version"),
            "split": suite.get("split"),
        },
        "provider": {
            "name": provider.get("name"),
            "model_version": provider.get("model_version"),
            "task_scoring_supported": provider.get("task_scoring_supported"),
        },
        "task_contract": {
            "domain": case.get("domain"),
            "prompt_digest": case.get("prompt_digest"),
        },
        "plan": {
            "mode": mode,
            "domains": sorted(set(domains)),
            "strategy_count": plan.get("strategy_count"),
            "verification_check_count": plan.get("verification_check_count"),
        },
    }
    required = (
        semantic_value["suite"]["version"],
        semantic_value["suite"]["split"],
        semantic_value["provider"]["name"],
        semantic_value["provider"]["model_version"],
        semantic_value["task_contract"]["domain"],
        semantic_value["task_contract"]["prompt_digest"],
    )
    if not all(isinstance(value, str) and value for value in required):
        return _unresolved_configuration("semantic_configuration_contract_incomplete")
    if not isinstance(semantic_value["provider"]["task_scoring_supported"], bool):
        return _unresolved_configuration("semantic_configuration_contract_incomplete")
    return {
        "status": "resolved",
        "reason": None,
        "semantic_digest": _digest(semantic_value),
    }


def _artifact_semantic_binding(value: Any) -> dict[str, Any]:
    semantics = _mapping(value)
    profile = semantics.get("profile")
    status = semantics.get("status")
    reason = semantics.get("reason")
    semantic_digest = semantics.get("semantic_digest")
    if profile != ARTIFACT_SEMANTIC_PROFILE:
        return {
            "profile": ARTIFACT_SEMANTIC_PROFILE,
            "status": "unresolved",
            "reason": "semantic_artifact_not_recorded",
            "semantic_digest": None,
        }
    if status == "resolved" and _valid_sha256(semantic_digest) and reason is None:
        return {
            "profile": profile,
            "status": status,
            "reason": None,
            "semantic_digest": semantic_digest,
        }
    if status == "unresolved" and reason in _SEMANTIC_REASONS:
        return {
            "profile": profile,
            "status": status,
            "reason": reason,
            "semantic_digest": semantic_digest if _valid_sha256(semantic_digest) else None,
        }
    return {
        "profile": ARTIFACT_SEMANTIC_PROFILE,
        "status": "unresolved",
        "reason": "semantic_artifact_record_invalid",
        "semantic_digest": None,
    }


def _scoring_material_binding(value: Any) -> dict[str, Any]:
    valid, reason = validate_producer_scoring_material(value)
    material = _mapping(value)
    if not valid:
        return {
            "scoring_material_status": "invalid",
            "scoring_material_reason": reason or "scoring_material_invalid",
            "scoring_material_digest": None,
            "quality_rubric_digest": None,
        }
    return {
        "scoring_material_status": material.get("status"),
        "scoring_material_reason": material.get("reason"),
        "scoring_material_digest": material.get("material_digest"),
        "quality_rubric_digest": material.get("rubric_digest"),
    }


def _expected_keys(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    if not all(isinstance(key, str) and key for key in value):
        return None
    if len(set(value)) != len(value):
        return None
    return sorted(value)


def _reject_non_finite_number(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


class DuplicateJSONKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateJSONKeyError(key)
        value[key] = item
    return value


def _semantic_node(value: Any) -> Any:
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["boolean", value]
    if isinstance(value, Decimal):
        return ["number", _decimal_identity(value)]
    if isinstance(value, str):
        return ["string", value]
    if isinstance(value, list):
        return ["array", [_semantic_node(item) for item in value]]
    if isinstance(value, dict):
        return [
            "object",
            [[key, _semantic_node(value[key])] for key in sorted(value)],
        ]
    raise TypeError("unsupported JSON semantic value")


def _decimal_identity(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("non-finite number")
    if value.is_zero():
        return "0"
    sign, digits, raw_exponent = value.as_tuple()
    if not isinstance(raw_exponent, int):
        raise ValueError("non-finite number")
    exponent = raw_exponent
    normalized_digits = list(digits)
    while len(normalized_digits) > 1 and normalized_digits[-1] == 0:
        normalized_digits.pop()
        exponent += 1
    return f"{sign}:{''.join(str(digit) for digit in normalized_digits)}:{exponent}"


def _contains_free_text(value: Any) -> bool:
    if isinstance(value, str):
        return True
    if isinstance(value, list):
        return any(_contains_free_text(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_free_text(item) for item in value.values())
    return False


def _unresolved_configuration(reason: str) -> dict[str, Any]:
    return {"status": "unresolved", "reason": reason, "semantic_digest": None}


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
