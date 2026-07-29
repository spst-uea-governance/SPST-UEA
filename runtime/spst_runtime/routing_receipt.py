import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from spst_runtime.always_on import CONFIG
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.repository_identity import (
    compare_repository_identity,
    validate_repository_identity,
)


ROUTING_RECEIPT_SCHEMA_V1 = "spst-routing-receipt-v1"
ROUTING_RECEIPT_SCHEMA_V2 = "spst-routing-receipt-v2"
ROUTING_RECEIPT_SCHEMA_V3 = "spst-routing-receipt-v3"
ROUTING_RECEIPT_SCHEMA_V4 = "spst-routing-receipt-v4"
ROUTING_RECEIPT_SCHEMA = ROUTING_RECEIPT_SCHEMA_V4
ROUTING_RECEIPT_SCHEMAS = (
    ROUTING_RECEIPT_SCHEMA_V4,
    ROUTING_RECEIPT_SCHEMA_V3,
    ROUTING_RECEIPT_SCHEMA_V2,
    ROUTING_RECEIPT_SCHEMA_V1,
)
ROUTING_RECEIPT_PREFIX = "routing_receipt:v"
CONTEXT_ORIGIN_INDEX_SCHEMA = "spst-context-origin-index-v1"
SESSION_STATE_KEY = "codex_chat_session"
COMPLETE_TRACE = (
    "observe",
    "retrieve",
    "infer",
    "reflect",
    "govern",
    "commit",
    "act",
)


def _canonical_hash(value: Any) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _receipt_key(receipt_id: str, schema: str) -> str:
    versions = {
        ROUTING_RECEIPT_SCHEMA_V1: "v1",
        ROUTING_RECEIPT_SCHEMA_V2: "v2",
        ROUTING_RECEIPT_SCHEMA_V3: "v3",
        ROUTING_RECEIPT_SCHEMA_V4: "v4",
    }
    version = versions.get(schema)
    if version is None:
        raise ValueError("Unsupported routing receipt schema.")
    return f"routing_receipt:{version}:{receipt_id}"


def build_routing_receipt(
    *,
    prompt: str,
    event: str,
    steps: int,
    pipeline: dict[str, Any],
    session_turn: int,
    session_state_hash: str,
    memory_record_id: str,
) -> dict[str, Any]:
    """Bind one completed SPST route to its persisted session and pipeline evidence."""
    trace = tuple(str(step) for step in pipeline.get("last_trace", []))
    if trace != COMPLETE_TRACE:
        raise ValueError("Routing receipts require the complete SPST trace.")

    governance = pipeline.get("governance", {})
    if not isinstance(governance, dict) or governance.get("authorized") is not True:
        raise ValueError("Routing receipts require an authorized governance decision.")

    model_inference = pipeline.get("model_inference", {})
    model_provider = model_inference.get("provider") if isinstance(model_inference, dict) else None
    if model_provider != CONFIG.provider:
        raise ValueError("Routing receipts require the configured Codex-mediated provider.")
    payload = {
        "route": {
            "event": event,
            "mode": CONFIG.mode,
            "provider": CONFIG.provider,
            "requires_api_key": CONFIG.requires_api_key,
            "steps": steps,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        },
        "pipeline": {
            "trace": list(trace),
            "version": pipeline.get("version"),
            "model_provider": model_provider,
            "evidence_id": pipeline.get("evidence", {}).get("id"),
            "reflection_approved": pipeline.get("reflection", {}).get("approved"),
        },
        "governance": {
            "authorized": True,
            "approval_id": governance.get("approval_id"),
            "requires_human_approval": governance.get("requires_human_approval", False),
            "trust_level": governance.get("trust_level"),
        },
        "session": {
            "turn": session_turn,
            "memory_record_id": memory_record_id,
            "state_record_hash": session_state_hash,
        },
    }
    signed_content = {"schema": ROUTING_RECEIPT_SCHEMA_V1, "payload": payload}
    return {
        **signed_content,
        "receipt_id": _canonical_hash(signed_content),
    }


def build_profiled_routing_receipt(
    *,
    prompt: str,
    event: str,
    steps: int,
    pipeline: dict[str, Any],
    session_turn: int,
    session_state_hash: str,
    execution_profile: dict[str, Any],
    memory_binding: dict[str, Any],
    repository_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind adaptive execution, memory policy, and optional repository identity."""
    trace = tuple(str(step) for step in pipeline.get("last_trace", []))
    if trace != COMPLETE_TRACE:
        raise ValueError("Routing receipts require the complete SPST trace.")
    governance = pipeline.get("governance", {})
    if not isinstance(governance, dict) or governance.get("authorized") is not True:
        raise ValueError("Routing receipts require an authorized governance decision.")
    model_inference = pipeline.get("model_inference", {})
    model_provider = model_inference.get("provider") if isinstance(model_inference, dict) else None
    if model_provider != CONFIG.provider:
        raise ValueError("Routing receipts require the configured Codex-mediated provider.")
    model_input_binding = (
        model_inference.get("model_input_binding") if isinstance(model_inference, dict) else None
    )

    payload = {
        "route": {
            "event": event,
            "mode": CONFIG.mode,
            "provider": CONFIG.provider,
            "requires_api_key": CONFIG.requires_api_key,
            "steps": steps,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "execution_profile": execution_profile,
        },
        "pipeline": {
            "trace": list(trace),
            "version": pipeline.get("version"),
            "model_provider": model_provider,
            "evidence_id": pipeline.get("evidence", {}).get("id"),
            "reflection_approved": pipeline.get("reflection", {}).get("approved"),
        },
        "governance": {
            "authorized": True,
            "approval_id": governance.get("approval_id"),
            "requires_human_approval": governance.get("requires_human_approval", False),
            "trust_level": governance.get("trust_level"),
        },
        "session": {
            "turn": session_turn,
            "memory_binding": memory_binding,
            "state_record_hash": session_state_hash,
        },
    }
    if isinstance(model_input_binding, dict):
        model_input_fields: tuple[str, ...] = (
            "schema",
            "model_input_sha256",
            "prompt_sha256",
            "instructions_sha256",
            "context_packet_sha256",
            "context_projection_sha256",
            "canonical_input_sha256",
            "context_status",
            "delivered_context_items",
            "unbound_context_rejected",
            "delivery_status",
        )
        if model_input_binding.get("schema") in {
            "spst-model-input-binding-v2",
            "spst-model-input-binding-v3",
        }:
            model_input_fields = (
                *model_input_fields,
                "delivered_artifact_sha256s",
                "delivered_artifact_set_sha256",
                "delivered_semantic_review_sha256s",
                "delivered_semantic_review_set_sha256",
            )
        if model_input_binding.get("schema") == "spst-model-input-binding-v3":
            model_input_fields = (
                *model_input_fields,
                "project_context_corpus_sha256",
                "project_context_project_id",
                "project_context_source_authenticity",
                "project_context_owner_reviewed",
            )
        payload["pipeline"]["model_input_binding"] = {
            key: model_input_binding.get(key)
            for key in model_input_fields
        }
    schema = ROUTING_RECEIPT_SCHEMA_V2
    if repository_identity is not None:
        valid, reason = validate_repository_identity(repository_identity)
        if not valid:
            raise ValueError(f"Routing receipt repository identity is invalid: {reason}")
        payload["repository"] = repository_identity
        schema = ROUTING_RECEIPT_SCHEMA_V4
    signed_content = {"schema": schema, "payload": payload}
    return {**signed_content, "receipt_id": _canonical_hash(signed_content)}


def validate_routing_receipt(receipt: dict[str, Any]) -> tuple[bool, str | None]:
    """Validate the self-binding fields before checking persisted provenance."""
    schema = receipt.get("schema")
    if schema not in ROUTING_RECEIPT_SCHEMAS:
        return False, "receipt_schema_mismatch"
    payload = receipt.get("payload")
    receipt_id = receipt.get("receipt_id")
    if not isinstance(payload, dict) or not isinstance(receipt_id, str):
        return False, "receipt_incomplete"
    expected = _canonical_hash({"schema": schema, "payload": payload})
    if receipt_id != expected:
        return False, "receipt_digest_mismatch"

    route = payload.get("route", {})
    pipeline = payload.get("pipeline", {})
    governance = payload.get("governance", {})
    session = payload.get("session", {})
    if (
        route.get("mode") != CONFIG.mode
        or route.get("provider") != CONFIG.provider
        or route.get("requires_api_key") is not False
        or pipeline.get("model_provider") != CONFIG.provider
    ):
        return False, "route_boundary_mismatch"
    if tuple(pipeline.get("trace", [])) != COMPLETE_TRACE:
        return False, "pipeline_trace_incomplete"
    if pipeline.get("reflection_approved") is not True:
        return False, "reflection_not_approved"
    if governance.get("authorized") is not True:
        return False, "governance_not_authorized"
    if not isinstance(session.get("turn"), int) or int(session["turn"]) < 1:
        return False, "session_turn_invalid"
    if schema == ROUTING_RECEIPT_SCHEMA_V1:
        if not session.get("memory_record_id"):
            return False, "memory_binding_missing"
    else:
        profile = route.get("execution_profile", {})
        memory_binding = session.get("memory_binding", {})
        if profile.get("name") not in {"light", "standard", "strict"}:
            return False, "execution_profile_invalid"
        binding_status = memory_binding.get("status")
        if binding_status == "skipped_by_light_profile":
            if profile.get("name") != "light" or memory_binding.get("record_id") is not None:
                return False, "light_memory_binding_invalid"
        elif binding_status == "persisted_and_policy_filtered":
            if not memory_binding.get("record_id"):
                return False, "memory_binding_missing"
        else:
            return False, "memory_binding_status_invalid"
        model_input_binding = pipeline.get("model_input_binding")
        if model_input_binding is not None:
            if not isinstance(model_input_binding, dict):
                return False, "model_input_binding_invalid"
            model_input_schema = model_input_binding.get("schema")
            if model_input_schema not in {
                "spst-model-input-binding-v1",
                "spst-model-input-binding-v2",
                "spst-model-input-binding-v3",
            }:
                return False, "model_input_binding_schema_mismatch"
            if any(
                not _is_sha256(model_input_binding.get(field))
                for field in (
                    "model_input_sha256",
                    "prompt_sha256",
                    "instructions_sha256",
                    "context_projection_sha256",
                    "canonical_input_sha256",
                )
            ):
                return False, "model_input_binding_digest_invalid"
            if model_input_binding.get("prompt_sha256") != route.get("prompt_sha256"):
                return False, "model_input_prompt_mismatch"
            delivered_items = model_input_binding.get("delivered_context_items")
            if (
                not isinstance(delivered_items, int)
                or isinstance(delivered_items, bool)
                or delivered_items < 0
            ):
                return False, "model_input_item_count_invalid"
            packet_binding = model_input_binding.get("context_packet_sha256")
            if packet_binding is not None and not _is_sha256(packet_binding):
                return False, "model_input_context_digest_invalid"
            if model_input_binding.get("context_status") not in {
                "ready",
                "empty",
                "blocked",
                "unavailable",
                "skipped",
                "not_provided",
                "unbound_context_rejected",
            }:
                return False, "model_input_context_status_invalid"
            if not isinstance(model_input_binding.get("unbound_context_rejected"), bool):
                return False, "model_input_rejection_flag_invalid"
            if model_input_binding.get("delivery_status") not in {
                "recorded_not_executed",
                "not_submitted_no_api_key",
                "submitted_to_provider",
            }:
                return False, "model_input_delivery_status_invalid"
            if model_input_schema in {
                "spst-model-input-binding-v2",
                "spst-model-input-binding-v3",
            }:
                artifacts = model_input_binding.get("delivered_artifact_sha256s")
                reviews = model_input_binding.get("delivered_semantic_review_sha256s")
                if (
                    not isinstance(artifacts, list)
                    or artifacts != sorted(set(artifacts))
                    or any(not _is_sha256(value) for value in artifacts)
                    or len(artifacts) > delivered_items
                ):
                    return False, "model_input_artifact_bindings_invalid"
                if (
                    not isinstance(reviews, list)
                    or reviews != sorted(set(reviews))
                    or any(not _is_sha256(value) for value in reviews)
                    or len(reviews) != len(artifacts)
                ):
                    return False, "model_input_review_bindings_invalid"
                if model_input_binding.get(
                    "delivered_artifact_set_sha256"
                ) != _canonical_hash(artifacts):
                    return False, "model_input_artifact_set_digest_mismatch"
                if model_input_binding.get(
                    "delivered_semantic_review_set_sha256"
                ) != _canonical_hash(reviews):
                    return False, "model_input_review_set_digest_mismatch"
            if model_input_schema == "spst-model-input-binding-v3":
                if not _is_sha256(
                    model_input_binding.get("project_context_corpus_sha256")
                ):
                    return False, "model_input_project_corpus_digest_invalid"
                project_id = model_input_binding.get("project_context_project_id")
                if not isinstance(project_id, str) or len(project_id) != 32:
                    return False, "model_input_project_id_invalid"
                try:
                    int(project_id, 16)
                except ValueError:
                    return False, "model_input_project_id_invalid"
                if model_input_binding.get("project_context_source_authenticity") != (
                    "owner_supplied_not_independently_verified"
                ):
                    return False, "model_input_project_source_authenticity_invalid"
                if model_input_binding.get("project_context_owner_reviewed") is not True:
                    return False, "model_input_project_review_invalid"
        packet_sha256 = memory_binding.get("context_packet_sha256")
        task_sha256 = memory_binding.get("context_task_sha256")
        origin_index_sha256 = memory_binding.get("context_origin_index_sha256")
        for value, reason in (
            (packet_sha256, "context_packet_binding_invalid"),
            (task_sha256, "context_task_binding_invalid"),
            (origin_index_sha256, "context_origin_index_binding_invalid"),
        ):
            if value is not None and not _is_sha256(value):
                return False, reason
        if model_input_binding is not None:
            model_packet_sha256 = model_input_binding.get("context_packet_sha256")
            if packet_sha256 is not None and model_packet_sha256 != packet_sha256:
                return False, "model_input_context_mismatch"
        if task_sha256 is not None and task_sha256 != route.get("prompt_sha256"):
            return False, "context_task_route_mismatch"
        if binding_status == "persisted_and_policy_filtered":
            record_text_sha256 = memory_binding.get("record_text_sha256")
            record_sha256 = memory_binding.get("record_sha256")
            record_source = memory_binding.get("record_source")
            record_kind = memory_binding.get("record_kind")
            record_created_turn = memory_binding.get("record_created_turn")
            if record_text_sha256 is not None and not _is_sha256(record_text_sha256):
                return False, "memory_record_text_binding_invalid"
            if record_sha256 is not None and not _is_sha256(record_sha256):
                return False, "memory_record_binding_invalid"
            if record_source is not None and (
                not isinstance(record_source, str) or not record_source.strip()
            ):
                return False, "memory_record_source_binding_invalid"
            if record_kind is not None and (
                not isinstance(record_kind, str) or not record_kind.strip()
            ):
                return False, "memory_record_kind_binding_invalid"
            if record_created_turn is not None and (
                not isinstance(record_created_turn, int)
                or isinstance(record_created_turn, bool)
                or record_created_turn < 0
            ):
                return False, "memory_record_turn_binding_invalid"
            if schema == ROUTING_RECEIPT_SCHEMA_V4:
                if not _is_sha256(record_text_sha256):
                    return False, "memory_record_text_binding_missing"
                if not _is_sha256(record_sha256):
                    return False, "memory_record_binding_missing"
                if not isinstance(record_source, str) or not record_source.strip():
                    return False, "memory_record_source_binding_missing"
                if not isinstance(record_kind, str) or not record_kind.strip():
                    return False, "memory_record_kind_binding_missing"
                if not isinstance(record_created_turn, int) or isinstance(
                    record_created_turn, bool
                ):
                    return False, "memory_record_turn_binding_missing"
        if schema == ROUTING_RECEIPT_SCHEMA_V4 and profile.get("name") != "light":
            if not _is_sha256(packet_sha256):
                return False, "context_packet_binding_missing"
            if not _is_sha256(task_sha256):
                return False, "context_task_binding_missing"
            if not _is_sha256(origin_index_sha256):
                return False, "context_origin_index_binding_missing"
        if schema in {ROUTING_RECEIPT_SCHEMA_V3, ROUTING_RECEIPT_SCHEMA_V4}:
            valid, repository_reason = validate_repository_identity(payload.get("repository"))
            if not valid:
                return False, repository_reason
    state_hash = session.get("state_record_hash")
    if not isinstance(state_hash, str) or len(state_hash) != 64:
        return False, "session_state_binding_missing"
    return True, None


def _seal_origin_index(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "index_sha256": _canonical_hash(payload)}


def empty_memory_origin_index(
    reason: str = "session_store_missing",
    *,
    current_repository_identity_sha256: str | None = None,
) -> dict[str, Any]:
    """Represent the absence of receipt evidence without inventing verification."""

    return _seal_origin_index(
        {
            "schema": CONTEXT_ORIGIN_INDEX_SCHEMA,
            "status": "empty",
            "reason": reason,
            "total_receipts": 0,
            "verified_receipts": 0,
            "failed_receipts": 0,
            "legacy_unbound_receipts": 0,
            "bound_record_count": 0,
            "provenance_entries": 0,
            "provenance_latest_hash": None,
            "current_repository_identity_sha256": current_repository_identity_sha256,
            "bindings": {},
        }
    )


class RoutingReceiptLedger:
    """Persist and independently verify immutable per-turn routing receipts."""

    def __init__(self, path: str, *, read_only: bool = False):
        self.repository = SQLiteRepository(path, read_only=read_only)

    def persist(self, receipt: dict[str, Any]) -> None:
        valid, reason = validate_routing_receipt(receipt)
        if not valid:
            raise ValueError(f"Cannot persist invalid routing receipt: {reason}")
        key = _receipt_key(str(receipt["receipt_id"]), str(receipt["schema"]))
        existing = asyncio.run(self.repository.load(key))
        if existing is not None:
            raise ValueError("Routing receipt already exists and cannot be rewritten.")
        asyncio.run(self.repository.save(key, receipt))

    def verify(
        self, receipt_id: str, *, repository_root: str | Path | None = None
    ) -> dict[str, Any]:
        receipt = None
        key = ""
        for schema in ROUTING_RECEIPT_SCHEMAS:
            candidate_key = _receipt_key(receipt_id, schema)
            candidate = asyncio.run(self.repository.load(candidate_key))
            if candidate is not None:
                key = candidate_key
                receipt = candidate
                break
        if receipt is None:
            return {"verified": False, "receipt_id": receipt_id, "reason": "receipt_missing"}

        provenance = self.repository.verify_provenance()
        entries = self.repository.provenance_entries() if provenance.get("valid") else []
        return self._verify_loaded(
            receipt_id,
            receipt,
            key,
            provenance,
            entries,
            repository_root=repository_root,
        )

    def _verify_loaded(
        self,
        receipt_id: str,
        receipt: dict[str, Any],
        key: str,
        provenance: dict[str, Any],
        entries: list[dict[str, Any]],
        *,
        repository_root: str | Path | None = None,
    ) -> dict[str, Any]:
        valid, reason = validate_routing_receipt(receipt)
        if not valid:
            return {"verified": False, "receipt_id": receipt_id, "reason": reason}

        if not provenance.get("valid"):
            return {
                "verified": False,
                "receipt_id": receipt_id,
                "reason": provenance.get("reason", "provenance_invalid"),
                "provenance": provenance,
            }

        receipt_entries = [entry for entry in entries if entry["record_key"] == key]
        if len(receipt_entries) != 1:
            return {
                "verified": False,
                "receipt_id": receipt_id,
                "reason": "receipt_record_rewritten" if receipt_entries else "receipt_provenance_missing",
                "provenance": provenance,
            }
        receipt_entry = receipt_entries[0]
        if receipt_entry["record_hash"] != SQLiteRepository.record_hash(receipt):
            return {
                "verified": False,
                "receipt_id": receipt_id,
                "reason": "receipt_record_hash_mismatch",
                "provenance": provenance,
            }

        session_hash = receipt["payload"]["session"]["state_record_hash"]
        session_bindings = [
            entry
            for entry in entries
            if entry["record_key"] == SESSION_STATE_KEY
            and entry["record_hash"] == session_hash
            and entry["sequence"] < receipt_entry["sequence"]
        ]
        if not session_bindings:
            return {
                "verified": False,
                "receipt_id": receipt_id,
                "reason": "session_state_binding_missing",
                "provenance": provenance,
            }

        if receipt["schema"] in {ROUTING_RECEIPT_SCHEMA_V3, ROUTING_RECEIPT_SCHEMA_V4}:
            repository = compare_repository_identity(
                receipt["payload"]["repository"], repository_root
            )
        else:
            repository = {
                "bound": False,
                "binding_verified": False,
                "current_match": None,
                "reason": "legacy_receipt_repository_unbound",
            }

        return {
            "verified": True,
            "receipt_id": receipt_id,
            "schema": receipt["schema"],
            "route": receipt["payload"]["route"],
            "pipeline": receipt["payload"]["pipeline"],
            "governance": receipt["payload"]["governance"],
            "session": receipt["payload"]["session"],
            "repository": repository,
            "provenance": provenance,
            "binding": {
                "receipt_sequence": receipt_entry["sequence"],
                "receipt_record_hash": receipt_entry["record_hash"],
                "receipt_chain_hash": receipt_entry["chain_hash"],
                "session_sequence": session_bindings[-1]["sequence"],
            },
        }

    def memory_origin_index(
        self, current_repository_identity: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Index memory records by independently verified producer receipts.

        The result is safe to pass to context mediation: it contains no prompt
        text or filesystem paths, and its digest covers every selected binding.
        A single failed receipt invalidates the complete index rather than
        permitting a caller to silently choose a favorable subset.
        """

        current_identity_sha256: str | None = None
        if current_repository_identity is not None:
            valid, reason = validate_repository_identity(current_repository_identity)
            if not valid:
                return _seal_origin_index(
                    {
                        "schema": CONTEXT_ORIGIN_INDEX_SCHEMA,
                        "status": "invalid",
                        "reason": reason or "current_repository_identity_invalid",
                        "total_receipts": 0,
                        "verified_receipts": 0,
                        "failed_receipts": 0,
                        "legacy_unbound_receipts": 0,
                        "bound_record_count": 0,
                        "provenance_entries": 0,
                        "provenance_latest_hash": None,
                        "current_repository_identity_sha256": None,
                        "bindings": {},
                    }
                )
            current_identity_sha256 = str(current_repository_identity["identity_sha256"])

        records = asyncio.run(self.repository.load_prefix(ROUTING_RECEIPT_PREFIX))
        receipt_items = sorted(
            records.items(),
            key=lambda item: (
                int(item[1].get("payload", {}).get("session", {}).get("turn", 0)),
                str(item[1].get("receipt_id", "")),
            ),
        )
        receipts = [receipt for _, receipt in receipt_items]
        provenance = self.repository.verify_provenance()
        base: dict[str, Any] = {
            "schema": CONTEXT_ORIGIN_INDEX_SCHEMA,
            "total_receipts": len(receipts),
            "verified_receipts": 0,
            "failed_receipts": 0,
            "legacy_unbound_receipts": 0,
            "bound_record_count": 0,
            "provenance_entries": int(provenance.get("entries", 0) or 0),
            "provenance_latest_hash": provenance.get("latest_hash"),
            "current_repository_identity_sha256": current_identity_sha256,
            "bindings": {},
        }
        if provenance.get("valid") is not True:
            return _seal_origin_index(
                {
                    **base,
                    "status": "invalid",
                    "reason": provenance.get("reason", "session_provenance_invalid"),
                    "failed_receipts": len(receipts),
                }
            )

        entries = self.repository.provenance_entries()
        verification = [
            self._verify_loaded(
                str(receipt.get("receipt_id", "")),
                receipt,
                key,
                provenance,
                entries,
            )
            for key, receipt in receipt_items
        ]
        failed = [result for result in verification if result.get("verified") is not True]
        if failed:
            return _seal_origin_index(
                {
                    **base,
                    "status": "invalid",
                    "reason": "routing_receipt_verification_failed",
                    "verified_receipts": len(verification) - len(failed),
                    "failed_receipts": len(failed),
                }
            )

        bindings: dict[str, list[dict[str, Any]]] = {}
        legacy_unbound = 0
        for result in verification:
            session = result.get("session", {})
            memory_binding = session.get("memory_binding", {})
            if not isinstance(memory_binding, dict):
                legacy_unbound += 1
                continue
            if memory_binding.get("status") != "persisted_and_policy_filtered":
                continue
            record_id = memory_binding.get("record_id")
            policy_version = memory_binding.get("policy_version")
            if not isinstance(record_id, str) or not record_id:
                legacy_unbound += 1
                continue
            if not isinstance(policy_version, str) or not policy_version:
                legacy_unbound += 1
                continue

            repository = dict(result.get("repository", {}))
            repository["current_identity_sha256"] = current_identity_sha256
            if current_identity_sha256 is not None:
                repository["current_match"] = (
                    repository.get("bound") is True
                    and repository.get("binding_verified") is True
                    and repository.get("identity_sha256") == current_identity_sha256
                )
                repository["reason"] = (
                    None
                    if repository["current_match"]
                    else "repository_identity_mismatch"
                    if repository.get("bound") is True
                    else "origin_repository_unbound"
                )

            origin = {
                "receipt_verified": True,
                "receipt_id": result["receipt_id"],
                "receipt_schema": result["schema"],
                "session_turn": session.get("turn"),
                "record_id": record_id,
                "policy_version": policy_version,
                "record_text_sha256": memory_binding.get("record_text_sha256"),
                "record_sha256": memory_binding.get("record_sha256"),
                "record_source": memory_binding.get("record_source"),
                "record_kind": memory_binding.get("record_kind"),
                "record_created_turn": memory_binding.get("record_created_turn"),
                "repository": repository,
                "provenance_binding": result.get("binding", {}),
            }
            bindings.setdefault(record_id, []).append(origin)

        for record_bindings in bindings.values():
            record_bindings.sort(
                key=lambda item: (int(item.get("session_turn", 0)), str(item.get("receipt_id", "")))
            )
        payload = {
            **base,
            "status": "verified",
            "reason": None,
            "verified_receipts": len(verification),
            "failed_receipts": 0,
            "legacy_unbound_receipts": legacy_unbound,
            "bound_record_count": len(bindings),
            "bindings": bindings,
        }
        return _seal_origin_index(payload)

    def summarize(
        self, session_turn_count: int, *, repository_root: str | Path | None = None
    ) -> dict[str, Any]:
        records = asyncio.run(self.repository.load_prefix(ROUTING_RECEIPT_PREFIX))
        receipt_items = sorted(
            records.items(),
            key=lambda item: int(
                item[1].get("payload", {}).get("session", {}).get("turn", 0)
            ),
        )
        receipts = [receipt for _, receipt in receipt_items]
        provenance = self.repository.verify_provenance()
        entries = self.repository.provenance_entries() if provenance.get("valid") else []
        verification = [
            self._verify_loaded(
                str(receipt.get("receipt_id", "")),
                receipt,
                key,
                provenance,
                entries,
            )
            for key, receipt in receipt_items
        ]
        verified = sum(1 for result in verification if result.get("verified"))
        repository_bound = sum(
            1
            for result in verification
            if result.get("verified") and result.get("repository", {}).get("bound") is True
        )
        verified_turns = {
            int(result["session"]["turn"])
            for result in verification
            if result.get("verified") and isinstance(result.get("session", {}).get("turn"), int)
        }
        profile_counts: dict[str, int] = {}
        memory_action_counts: dict[str, int] = {}
        receipt_schema_counts: dict[str, int] = {}
        context_bound_receipts = 0
        repository_context_bound_receipts = 0
        for result in verification:
            if not result.get("verified"):
                continue
            schema = str(result.get("schema", "unknown"))
            receipt_schema_counts[schema] = receipt_schema_counts.get(schema, 0) + 1
            profile_name = (
                result.get("route", {}).get("execution_profile", {}).get("name")
                or "legacy_unprofiled"
            )
            memory_action = (
                result.get("session", {}).get("memory_binding", {}).get("status")
                or "legacy_persisted"
            )
            profile_counts[profile_name] = profile_counts.get(profile_name, 0) + 1
            memory_action_counts[memory_action] = memory_action_counts.get(memory_action, 0) + 1
            memory_binding = result.get("session", {}).get("memory_binding", {})
            has_context_binding = isinstance(memory_binding, dict) and all(
                _is_sha256(memory_binding.get(field))
                for field in (
                    "context_packet_sha256",
                    "context_task_sha256",
                    "context_origin_index_sha256",
                )
            )
            if has_context_binding:
                context_bound_receipts += 1
                if result.get("repository", {}).get("bound") is True:
                    repository_context_bound_receipts += 1
        turns = [
            int(receipt["payload"]["session"]["turn"])
            for receipt in receipts
            if isinstance(receipt.get("payload", {}).get("session", {}).get("turn"), int)
        ]
        start_turn = min(turns) if turns else None
        eligible_turns = max(session_turn_count - start_turn + 1, 0) if start_turn else 0
        latest_pair = receipt_items[-1] if receipt_items else None
        latest = latest_pair[1] if latest_pair else None
        latest_id = str(latest.get("receipt_id")) if latest else None
        latest_result = (
            self._verify_loaded(
                latest_id,
                latest_pair[1],
                latest_pair[0],
                provenance,
                entries,
                repository_root=repository_root,
            )
            if latest_id and latest_pair
            else None
        )
        return {
            "receipt_schema": ROUTING_RECEIPT_SCHEMA,
            "total_receipts": len(receipts),
            "verified_receipts": verified,
            "repository_bound_receipts": repository_bound,
            "verified_turns": len(verified_turns),
            "profile_counts": profile_counts,
            "memory_action_counts": memory_action_counts,
            "receipt_schema_counts": receipt_schema_counts,
            "context_bound_receipts": context_bound_receipts,
            "repository_context_bound_receipts": repository_context_bound_receipts,
            "context_unbound_receipts": verified - context_bound_receipts,
            "failed_receipts": len(receipts) - verified,
            "receipt_epoch_start_turn": start_turn,
            "eligible_turns": eligible_turns,
            "legacy_unreceipted_turns": (start_turn - 1) if start_turn else session_turn_count,
            "receipt_coverage": (len(verified_turns) / eligible_turns) if eligible_turns else None,
            "latest_receipt_id": latest_id,
            "latest_receipt_verified": (
                bool(latest_result and latest_result.get("verified")) if latest else None
            ),
            "latest_repository_current_match": (
                latest_result.get("repository", {}).get("current_match")
                if latest_result
                else None
            ),
            "global_codex_task_coverage": None,
            "global_coverage_reason": "codex_task_denominator_unavailable",
            "task_quality_delta": None,
            "task_quality_reason": "paired_outcome_measurement_unavailable",
        }
