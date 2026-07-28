from copy import deepcopy
import hashlib
import json
import re
from typing import Any

from spst_runtime.context_review import validate_context_intervention
from spst_runtime.model_input_binding import ModelInputBindingError, bind_model_input


PROVIDER_REQUEST_BINDING_SCHEMA = "spst-provider-request-binding-v1"
PROVIDER_OBSERVATION_SCHEMA = "spst-provider-observation-v1"
CODEX_CLI_OBSERVATION_SOURCE = "codex_cli_jsonl_model_echo"
PROVIDER_OBSERVATION_SOURCES = frozenset(
    {
        CODEX_CLI_OBSERVATION_SOURCE,
        "https_response_metadata_echo",
        "in_process_provider_echo",
        "local_process_provider_echo",
    }
)

_identifier = re.compile(r"^[A-Za-z0-9._:/-]{1,160}$")


class ProviderObservationError(ValueError):
    """Raised when provider-bound request evidence cannot be constructed."""


def build_provider_request_binding(
    prompt: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind the exact adapter input without persisting prompt or context content."""

    source = context or {}
    if not isinstance(source, dict):
        raise ProviderObservationError("provider_request_context_invalid")
    try:
        model_input = bind_model_input(prompt, source)
    except ModelInputBindingError as error:
        raise ProviderObservationError(str(error)) from error
    intervention = source.get("evidence_context_intervention")
    intervention_sha256: str | None = None
    if intervention is not None:
        valid, reason = validate_context_intervention(intervention)
        if not valid or not isinstance(intervention, dict):
            raise ProviderObservationError(
                reason or "provider_request_context_intervention_invalid"
            )
        intervention_sha256 = str(intervention["intervention_sha256"])

    evaluation = source.get("evaluation", {})
    if not isinstance(evaluation, dict):
        raise ProviderObservationError("provider_request_evaluation_context_invalid")
    delivered_items = model_input.get("delivered_context_items")
    if not isinstance(delivered_items, int) or isinstance(delivered_items, bool):
        raise ProviderObservationError("provider_request_model_input_invalid")
    context_components = [
        value
        for value in (
            model_input.get("context_projection_sha256")
            if delivered_items > 0
            else None,
            intervention_sha256,
        )
        if value is not None
    ]
    unsigned = {
        "schema": PROVIDER_REQUEST_BINDING_SCHEMA,
        "prompt_sha256": _sha256_text(prompt),
        "instructions_sha256": model_input["instructions_sha256"],
        "model_input_sha256": model_input["model_input_sha256"],
        "context_packet_sha256": model_input.get("context_packet_sha256"),
        "context_intervention_sha256": intervention_sha256,
        "context_binding_sha256": (
            _canonical_hash(context_components) if context_components else None
        ),
        "context_present": bool(context_components),
        "evaluation_context_sha256": _canonical_hash(evaluation),
        "adapter_context_sha256": _canonical_hash(source),
    }
    return {**unsigned, "request_binding_sha256": _canonical_hash(unsigned)}


def validate_provider_request_binding(value: Any) -> tuple[bool, str | None]:
    if not isinstance(value, dict):
        return False, "provider_request_binding_invalid"
    digest = value.get("request_binding_sha256")
    unsigned = {
        key: item for key, item in value.items() if key != "request_binding_sha256"
    }
    if (
        value.get("schema") != PROVIDER_REQUEST_BINDING_SCHEMA
        or not _is_sha256(digest)
        or _canonical_hash(unsigned) != digest
    ):
        return False, "provider_request_binding_digest_mismatch"
    if any(
        not _is_sha256(value.get(field))
        for field in (
            "prompt_sha256",
            "instructions_sha256",
            "model_input_sha256",
            "evaluation_context_sha256",
            "adapter_context_sha256",
        )
    ):
        return False, "provider_request_binding_field_invalid"
    optional_digests = (
        value.get("context_packet_sha256"),
        value.get("context_intervention_sha256"),
        value.get("context_binding_sha256"),
    )
    if any(item is not None and not _is_sha256(item) for item in optional_digests):
        return False, "provider_request_binding_context_invalid"
    context_present = value.get("context_present")
    if not isinstance(context_present, bool):
        return False, "provider_request_binding_context_invalid"
    if context_present != (value.get("context_binding_sha256") is not None):
        return False, "provider_request_binding_context_invalid"
    if (
        value.get("context_intervention_sha256") is not None
        and value.get("context_binding_sha256") is None
    ):
        return False, "provider_request_binding_context_invalid"
    return True, None


def build_provider_observation(
    request_binding: dict[str, Any],
    *,
    provider_name: str,
    model_version: str,
    response_id: str,
    response_status: str,
    output_text: str,
    observation_source: str,
    acknowledged_request_binding_sha256: str | None,
) -> dict[str, Any]:
    """Record an adapter-observed response acknowledgement without causal claims."""

    request_valid, request_reason = validate_provider_request_binding(request_binding)
    if not request_valid:
        raise ProviderObservationError(request_reason or "provider_request_binding_invalid")
    reasons: list[str] = []
    if not _safe_identifier(provider_name):
        reasons.append("provider_observation_provider_invalid")
    if not _safe_identifier(model_version):
        reasons.append("provider_observation_model_invalid")
    if not isinstance(response_id, str) or not response_id.strip():
        reasons.append("provider_observation_response_id_missing")
    if response_status != "completed":
        reasons.append("provider_observation_response_incomplete")
    if observation_source not in PROVIDER_OBSERVATION_SOURCES:
        reasons.append("provider_observation_source_invalid")
    if (
        acknowledged_request_binding_sha256
        != request_binding["request_binding_sha256"]
    ):
        reasons.append("provider_request_acknowledgement_mismatch")
    status = "observed" if not reasons else "unresolved"
    reason = reasons[0] if reasons else None
    unsigned = {
        "schema": PROVIDER_OBSERVATION_SCHEMA,
        "status": status,
        "reason": reason,
        "observation_source": observation_source,
        "provider": {
            "name": provider_name,
            "model_version": model_version,
            "identity_cryptographically_verified": False,
        },
        "request": deepcopy(request_binding),
        "response": {
            "response_id_sha256": _sha256_text(response_id.strip()),
            "status": response_status,
            "output_sha256": _sha256_text(output_text),
        },
        "transport": {
            "request_submitted": True,
            "response_received": True,
            "request_binding_acknowledged": not reasons
            or "provider_request_acknowledgement_mismatch" not in reasons,
            "source_authenticated": False,
        },
        "claims": {
            "provider_transport_observed": True,
            "context_uptake_observed": bool(request_binding["context_present"])
            and not reasons,
            "semantic_context_use_verified": False,
            "causal_effect_established": False,
        },
    }
    return {**unsigned, "observation_sha256": _canonical_hash(unsigned)}


def verify_provider_observation(
    value: Any,
    expected_request_binding: dict[str, Any],
    *,
    output_text: str,
    provider_name: str | None = None,
    model_version: str | None = None,
) -> tuple[bool, str | None]:
    integrity_valid, integrity_reason = _validate_observation_integrity(value)
    if not integrity_valid or not isinstance(value, dict):
        return False, integrity_reason
    request_valid, request_reason = validate_provider_request_binding(
        expected_request_binding
    )
    if not request_valid:
        return False, request_reason
    if value.get("status") != "observed" or value.get("reason") is not None:
        return False, str(value.get("reason") or "provider_observation_unresolved")
    if value.get("request") != expected_request_binding:
        return False, "provider_observation_request_mismatch"
    provider = _mapping(value.get("provider"))
    response = _mapping(value.get("response"))
    transport = _mapping(value.get("transport"))
    claims = _mapping(value.get("claims"))
    if provider_name is not None and provider.get("name") != provider_name:
        return False, "provider_observation_provider_mismatch"
    if model_version is not None and provider.get("model_version") != model_version:
        return False, "provider_observation_model_mismatch"
    if response.get("output_sha256") != _sha256_text(output_text):
        return False, "provider_observation_output_mismatch"
    if (
        response.get("status") != "completed"
        or transport.get("request_submitted") is not True
        or transport.get("response_received") is not True
        or transport.get("request_binding_acknowledged") is not True
        or claims.get("provider_transport_observed") is not True
        or claims.get("context_uptake_observed")
        is not bool(expected_request_binding["context_present"])
    ):
        return False, "provider_observation_transport_invalid"
    return True, None


def validate_provider_observation_binding(
    value: Any,
    *,
    expected_output_sha256: str,
    expected_context_intervention_sha256: str | None,
) -> tuple[bool, str | None]:
    """Validate persisted producer evidence without requiring raw model output."""

    integrity_valid, integrity_reason = _validate_observation_integrity(value)
    if not integrity_valid or not isinstance(value, dict):
        return False, integrity_reason
    if value.get("status") != "observed" or value.get("reason") is not None:
        return False, str(value.get("reason") or "provider_observation_unresolved")
    request = _mapping(value.get("request"))
    response = _mapping(value.get("response"))
    transport = _mapping(value.get("transport"))
    claims = _mapping(value.get("claims"))
    if response.get("output_sha256") != expected_output_sha256:
        return False, "provider_observation_output_mismatch"
    if (
        request.get("context_intervention_sha256")
        != expected_context_intervention_sha256
    ):
        return False, "provider_observation_context_mismatch"
    expected_context_present = expected_context_intervention_sha256 is not None
    if request.get("context_present") is not expected_context_present:
        return False, "provider_observation_context_mismatch"
    if (
        transport.get("request_submitted") is not True
        or transport.get("response_received") is not True
        or transport.get("request_binding_acknowledged") is not True
        or claims.get("provider_transport_observed") is not True
        or claims.get("context_uptake_observed") is not expected_context_present
        or claims.get("semantic_context_use_verified") is not False
        or claims.get("causal_effect_established") is not False
    ):
        return False, "provider_observation_claim_boundary_invalid"
    return True, None


def unresolved_provider_observation(reason: str) -> dict[str, Any]:
    return {
        "schema": PROVIDER_OBSERVATION_SCHEMA,
        "status": "unresolved",
        "reason": reason,
        "claims": {
            "provider_transport_observed": False,
            "context_uptake_observed": False,
            "semantic_context_use_verified": False,
            "causal_effect_established": False,
        },
    }


def canonical_provider_value_sha256(value: Any) -> str:
    """Return the canonical digest used inside provider request bindings."""

    return _canonical_hash(value)


def _validate_observation_integrity(value: Any) -> tuple[bool, str | None]:
    if not isinstance(value, dict):
        return False, "provider_observation_missing"
    digest = value.get("observation_sha256")
    unsigned = {key: item for key, item in value.items() if key != "observation_sha256"}
    if (
        value.get("schema") != PROVIDER_OBSERVATION_SCHEMA
        or not _is_sha256(digest)
        or _canonical_hash(unsigned) != digest
    ):
        return False, "provider_observation_digest_mismatch"
    request_valid, request_reason = validate_provider_request_binding(
        value.get("request")
    )
    if not request_valid:
        return False, request_reason
    provider = _mapping(value.get("provider"))
    response = _mapping(value.get("response"))
    transport = _mapping(value.get("transport"))
    claims = _mapping(value.get("claims"))
    if (
        not _safe_identifier(provider.get("name"))
        or not _safe_identifier(provider.get("model_version"))
        or provider.get("identity_cryptographically_verified") is not False
        or not _is_sha256(response.get("response_id_sha256"))
        or not _is_sha256(response.get("output_sha256"))
        or transport.get("source_authenticated") is not False
        or claims.get("semantic_context_use_verified") is not False
        or claims.get("causal_effect_established") is not False
    ):
        return False, "provider_observation_boundary_invalid"
    if value.get("observation_source") not in PROVIDER_OBSERVATION_SOURCES:
        return False, "provider_observation_source_invalid"
    return True, None


def _canonical_hash(value: Any) -> str:
    return _sha256_text(_canonical_json(value))


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ProviderObservationError("provider_observation_not_canonicalizable") from error


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _safe_identifier(value: Any) -> str | None:
    candidate = value if isinstance(value, str) else ""
    return candidate if _identifier.fullmatch(candidate) else None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
