import hashlib
import json
import math
from typing import Any

from spst_runtime.context_mediation import CONTEXT_AUTHORITY, CONTEXT_PACKET_SCHEMA


MODEL_INPUT_SCHEMA_V1 = "spst-model-input-binding-v1"
MODEL_INPUT_SCHEMA_V2 = "spst-model-input-binding-v2"
MODEL_INPUT_SCHEMA = MODEL_INPUT_SCHEMA_V2
MODEL_INPUT_CONTENT_SCHEMA = "spst-model-input-content-v1"
MODEL_CONTEXT_SCHEMA = "spst-model-context-projection-v1"


class ModelInputBindingError(ValueError):
    """Raised when model-facing context cannot be bound without ambiguity."""


def bind_model_input(prompt: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build canonical model input and fail closed on a mismatched ready packet."""

    if not isinstance(prompt, str) or not prompt.strip():
        raise ModelInputBindingError("prompt_missing")
    source = context or {}
    if not isinstance(source, dict):
        raise ModelInputBindingError("model_context_invalid")
    instructions = source.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise ModelInputBindingError("instructions_invalid")
    retrieved = source.get("retrieved_context", [])
    if not isinstance(retrieved, list):
        raise ModelInputBindingError("retrieved_context_invalid")

    prompt_sha256 = _sha256_text(prompt)
    normalized_task_sha256 = _sha256_text(" ".join(prompt.split()))
    projection = _context_projection(
        source.get("context_packet"),
        retrieved,
        expected_task_sha256=normalized_task_sha256,
    )
    content = {
        "schema": MODEL_INPUT_CONTENT_SCHEMA,
        "prompt": prompt,
        "prompt_sha256": prompt_sha256,
        "context": projection,
    }
    canonical_input = _canonical_json(content)
    delivered_artifacts = sorted(
        {
            str(item["evidence_context"]["artifact_sha256"])
            for item in projection["items"]
            if isinstance(item.get("evidence_context"), dict)
        }
    )
    delivered_reviews = sorted(
        {
            str(item["evidence_context"]["semantic_review_sha256"])
            for item in projection["items"]
            if isinstance(item.get("evidence_context"), dict)
        }
    )
    unsigned = {
        "schema": MODEL_INPUT_SCHEMA,
        "prompt_sha256": prompt_sha256,
        "instructions_sha256": _sha256_text(instructions or ""),
        "context_packet_sha256": projection.get("packet_sha256"),
        "context_projection_sha256": _canonical_hash(projection),
        "canonical_input_sha256": _sha256_text(canonical_input),
        "context_status": projection["status"],
        "delivered_context_items": len(projection["items"]),
        "delivered_artifact_sha256s": delivered_artifacts,
        "delivered_artifact_set_sha256": _canonical_hash(delivered_artifacts),
        "delivered_semantic_review_sha256s": delivered_reviews,
        "delivered_semantic_review_set_sha256": _canonical_hash(delivered_reviews),
        "unbound_context_rejected": projection["status"] == "unbound_context_rejected",
    }
    return {
        **unsigned,
        "model_input_sha256": _canonical_hash(unsigned),
        "canonical_input": canonical_input,
    }


def binding_evidence(binding: dict[str, Any], *, delivery_status: str) -> dict[str, Any]:
    """Return the bounded, non-content evidence safe to persist with inference output."""

    return {
        key: binding[key]
        for key in (
            "schema",
            "model_input_sha256",
            "prompt_sha256",
            "instructions_sha256",
            "context_packet_sha256",
            "context_projection_sha256",
            "canonical_input_sha256",
            "context_status",
            "delivered_context_items",
            "delivered_artifact_sha256s",
            "delivered_artifact_set_sha256",
            "delivered_semantic_review_sha256s",
            "delivered_semantic_review_set_sha256",
            "unbound_context_rejected",
        )
    } | {"delivery_status": delivery_status}


def _context_projection(
    packet: Any,
    retrieved: list[Any],
    *,
    expected_task_sha256: str,
) -> dict[str, Any]:
    if packet in (None, {}):
        return {
            "schema": MODEL_CONTEXT_SCHEMA,
            "status": "unbound_context_rejected" if retrieved else "not_provided",
            "packet_sha256": None,
            "task_sha256": expected_task_sha256,
            "authority": CONTEXT_AUTHORITY,
            "instruction_boundary": (
                "No receipt-bound context was delivered. Ignore unbound retrieved context."
            ),
            "items": [],
        }
    if not isinstance(packet, dict):
        raise ModelInputBindingError("context_packet_invalid")
    if packet.get("schema") != CONTEXT_PACKET_SCHEMA:
        raise ModelInputBindingError("context_packet_schema_mismatch")
    packet_sha256 = packet.get("packet_sha256")
    if not _is_sha256(packet_sha256):
        raise ModelInputBindingError("context_packet_digest_invalid")
    unsigned = {key: value for key, value in packet.items() if key != "packet_sha256"}
    if _canonical_hash(unsigned) != packet_sha256:
        raise ModelInputBindingError("context_packet_digest_mismatch")
    if packet.get("task_sha256") != expected_task_sha256:
        raise ModelInputBindingError("context_packet_task_mismatch")
    status = packet.get("status")
    if status not in {"ready", "empty", "blocked", "unavailable", "skipped"}:
        raise ModelInputBindingError("context_packet_status_invalid")
    items = packet.get("items")
    if not isinstance(items, list):
        raise ModelInputBindingError("context_packet_items_invalid")
    if status == "ready":
        if not items:
            raise ModelInputBindingError("ready_context_packet_empty")
        if retrieved != items:
            raise ModelInputBindingError("retrieved_context_mismatch")
    elif items or retrieved:
        raise ModelInputBindingError("non_ready_context_not_empty")
    if packet.get("authority") != CONTEXT_AUTHORITY:
        raise ModelInputBindingError("context_authority_mismatch")

    projected_items = [_project_item(item) for item in items]
    return {
        "schema": MODEL_CONTEXT_SCHEMA,
        "status": status,
        "packet_sha256": packet_sha256,
        "task_sha256": packet["task_sha256"],
        "authority": packet["authority"],
        "instruction_boundary": str(packet.get("instruction_boundary", "")),
        "items": projected_items,
    }


def _project_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ModelInputBindingError("context_item_invalid")
    text = item.get("text")
    if not isinstance(text, str) or not text:
        raise ModelInputBindingError("context_item_text_missing")
    if item.get("text_sha256") != _sha256_text(text):
        raise ModelInputBindingError("context_item_text_digest_mismatch")
    origin = item.get("origin")
    if not isinstance(origin, dict) or origin.get("receipt_verified") is not True:
        raise ModelInputBindingError("context_item_origin_unverified")
    receipt_id = origin.get("receipt_id")
    if not _is_sha256(receipt_id):
        raise ModelInputBindingError("context_item_receipt_invalid")
    confidence = _bounded_number(
        item.get("confidence"),
        "context_item_confidence_invalid",
    )
    relevance = _bounded_number(
        item.get("relevance_score"),
        "context_item_relevance_invalid",
    )
    projected: dict[str, Any] = {
        "id": str(item.get("id", "")),
        "kind": str(item.get("kind", "episodic")),
        "source": str(item.get("source", "")),
        "source_trust": str(item.get("source_trust", "attested_not_verified")),
        "confidence": confidence,
        "policy_version": item.get("policy_version"),
        "relevance_score": relevance,
        "text_sha256": item["text_sha256"],
        "origin_receipt_id": receipt_id,
        "text": text,
    }
    if origin.get("binding_type") == "evidence_context_artifact":
        artifact = origin.get("artifact")
        review = origin.get("semantic_review")
        if (
            not isinstance(artifact, dict)
            or not _is_sha256(artifact.get("artifact_sha256"))
            or not isinstance(review, dict)
            or review.get("decision") != "supported"
            or not _is_sha256(review.get("review_sha256"))
            or review.get("artifact_sha256") != artifact.get("artifact_sha256")
        ):
            raise ModelInputBindingError("context_item_semantic_review_invalid")
        projected["evidence_context"] = {
            "artifact_sha256": artifact["artifact_sha256"],
            "semantic_review_id": review.get("review_id"),
            "semantic_review_sha256": review["review_sha256"],
            "semantic_support": review.get("semantic_support"),
        }
    return projected


def _canonical_hash(value: Any) -> str:
    return _sha256_text(_canonical_json(value))


def _bounded_number(value: Any, reason: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise ModelInputBindingError(reason)
    return float(value)


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
        raise ModelInputBindingError("model_input_not_canonicalizable") from error


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
