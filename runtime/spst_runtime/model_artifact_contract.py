from __future__ import annotations

import json
import math
import re
from typing import Any


MODEL_ARTIFACT_CONTRACT_SCHEMA = "spst-model-artifact-contract-v1"
_SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,63}$")
_SUPPORTED_VALUE_TYPES = {"boolean", "integer", "null", "number", "string"}


class ModelArtifactContractError(ValueError):
    """Raised when a model artifact contract is unsafe or inconsistent."""


def build_model_artifact_contract(
    expected_json_keys: Any,
    property_types: Any = None,
) -> dict[str, Any]:
    if not isinstance(expected_json_keys, list):
        raise ModelArtifactContractError("artifact_contract_required_keys_invalid")
    if not expected_json_keys:
        return {
            "schema": MODEL_ARTIFACT_CONTRACT_SCHEMA,
            "kind": "text",
            "required_keys": [],
            "property_types": {},
        }
    if (
        len(expected_json_keys) > 32
        or any(
            not isinstance(key, str) or _SAFE_KEY.fullmatch(key) is None
            for key in expected_json_keys
        )
        or len(set(expected_json_keys)) != len(expected_json_keys)
    ):
        raise ModelArtifactContractError("artifact_contract_required_keys_invalid")
    if (
        not isinstance(property_types, dict)
        or set(property_types) != set(expected_json_keys)
        or any(
            not isinstance(value, str) or value not in _SUPPORTED_VALUE_TYPES
            for value in property_types.values()
        )
    ):
        raise ModelArtifactContractError("artifact_contract_property_types_invalid")
    return {
        "schema": MODEL_ARTIFACT_CONTRACT_SCHEMA,
        "kind": "json_object",
        "required_keys": list(expected_json_keys),
        "property_types": {
            key: property_types[key]
            for key in expected_json_keys
        },
    }


def validate_model_artifact_contract(value: Any) -> tuple[bool, str | None]:
    if not isinstance(value, dict):
        return False, "artifact_contract_invalid"
    if value.get("schema") != MODEL_ARTIFACT_CONTRACT_SCHEMA:
        return False, "artifact_contract_schema_mismatch"
    kind = value.get("kind")
    required_keys = value.get("required_keys")
    if set(value) != {"schema", "kind", "required_keys", "property_types"}:
        return False, "artifact_contract_fields_invalid"
    try:
        rebuilt = build_model_artifact_contract(
            required_keys,
            value.get("property_types"),
        )
    except ModelArtifactContractError as error:
        return False, str(error)
    if kind != rebuilt["kind"] or value != rebuilt:
        return False, "artifact_contract_kind_mismatch"
    return True, None


def provider_artifact_schema(contract: dict[str, Any]) -> dict[str, Any]:
    valid, reason = validate_model_artifact_contract(contract)
    if not valid:
        raise ModelArtifactContractError(reason or "artifact_contract_invalid")
    if contract["kind"] == "text":
        return {"type": "string"}
    required_keys = contract["required_keys"]
    return {
        "type": "object",
        "required": required_keys,
        "properties": {
            key: {"type": contract["property_types"][key]}
            for key in required_keys
        },
        "additionalProperties": False,
    }


def serialize_provider_artifact(
    artifact: Any,
    contract: dict[str, Any],
) -> str:
    valid, reason = validate_model_artifact_contract(contract)
    if not valid:
        raise ModelArtifactContractError(reason or "artifact_contract_invalid")
    if contract["kind"] == "text":
        if not isinstance(artifact, str):
            raise ModelArtifactContractError("provider_artifact_text_invalid")
        return artifact
    required_keys = contract["required_keys"]
    if (
        not isinstance(artifact, dict)
        or set(artifact) != set(required_keys)
        or any(
            not _matches_type(artifact[key], contract["property_types"][key])
            for key in required_keys
        )
    ):
        raise ModelArtifactContractError("provider_artifact_json_contract_mismatch")
    return json.dumps(
        artifact,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _matches_type(value: Any, expected_type: str) -> bool:
    if expected_type == "null":
        return value is None
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
        )
    return isinstance(value, str)
