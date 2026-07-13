import asyncio
import hashlib
import json
from typing import Any

from spst_runtime.always_on import CONFIG
from spst_runtime.persistence.sqlite_repository import SQLiteRepository


ROUTING_RECEIPT_SCHEMA_V1 = "spst-routing-receipt-v1"
ROUTING_RECEIPT_SCHEMA_V2 = "spst-routing-receipt-v2"
ROUTING_RECEIPT_SCHEMA = ROUTING_RECEIPT_SCHEMA_V2
ROUTING_RECEIPT_PREFIX = "routing_receipt:v"
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


def _canonical_hash(value: dict[str, Any]) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _receipt_key(receipt_id: str, schema: str) -> str:
    version = "v2" if schema == ROUTING_RECEIPT_SCHEMA_V2 else "v1"
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
) -> dict[str, Any]:
    """Bind adaptive execution and memory policy without invalidating v1 receipts."""
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
    signed_content = {"schema": ROUTING_RECEIPT_SCHEMA_V2, "payload": payload}
    return {**signed_content, "receipt_id": _canonical_hash(signed_content)}


def validate_routing_receipt(receipt: dict[str, Any]) -> tuple[bool, str | None]:
    """Validate the self-binding fields before checking persisted provenance."""
    schema = receipt.get("schema")
    if schema not in {ROUTING_RECEIPT_SCHEMA_V1, ROUTING_RECEIPT_SCHEMA_V2}:
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
    state_hash = session.get("state_record_hash")
    if not isinstance(state_hash, str) or len(state_hash) != 64:
        return False, "session_state_binding_missing"
    return True, None


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

    def verify(self, receipt_id: str) -> dict[str, Any]:
        receipt = None
        key = ""
        for schema in (ROUTING_RECEIPT_SCHEMA_V2, ROUTING_RECEIPT_SCHEMA_V1):
            candidate_key = _receipt_key(receipt_id, schema)
            candidate = asyncio.run(self.repository.load(candidate_key))
            if candidate is not None:
                key = candidate_key
                receipt = candidate
                break
        if receipt is None:
            return {"verified": False, "receipt_id": receipt_id, "reason": "receipt_missing"}

        valid, reason = validate_routing_receipt(receipt)
        if not valid:
            return {"verified": False, "receipt_id": receipt_id, "reason": reason}

        provenance = self.repository.verify_provenance()
        if not provenance.get("valid"):
            return {
                "verified": False,
                "receipt_id": receipt_id,
                "reason": provenance.get("reason", "provenance_invalid"),
                "provenance": provenance,
            }

        entries = self.repository.provenance_entries()
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

        return {
            "verified": True,
            "receipt_id": receipt_id,
            "schema": receipt["schema"],
            "route": receipt["payload"]["route"],
            "session": receipt["payload"]["session"],
            "provenance": provenance,
            "binding": {
                "receipt_sequence": receipt_entry["sequence"],
                "receipt_chain_hash": receipt_entry["chain_hash"],
                "session_sequence": session_bindings[-1]["sequence"],
            },
        }

    def summarize(self, session_turn_count: int) -> dict[str, Any]:
        records = asyncio.run(self.repository.load_prefix(ROUTING_RECEIPT_PREFIX))
        receipts = list(records.values())
        receipts.sort(key=lambda item: int(item.get("payload", {}).get("session", {}).get("turn", 0)))
        verification = [self.verify(str(receipt.get("receipt_id", ""))) for receipt in receipts]
        verified = sum(1 for result in verification if result.get("verified"))
        verified_turns = {
            int(result["session"]["turn"])
            for result in verification
            if result.get("verified") and isinstance(result.get("session", {}).get("turn"), int)
        }
        profile_counts: dict[str, int] = {}
        memory_action_counts: dict[str, int] = {}
        for result in verification:
            if not result.get("verified"):
                continue
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
        turns = [
            int(receipt["payload"]["session"]["turn"])
            for receipt in receipts
            if isinstance(receipt.get("payload", {}).get("session", {}).get("turn"), int)
        ]
        start_turn = min(turns) if turns else None
        eligible_turns = max(session_turn_count - start_turn + 1, 0) if start_turn else 0
        latest = receipts[-1] if receipts else None
        latest_id = str(latest.get("receipt_id")) if latest else None
        latest_result = self.verify(latest_id) if latest_id else None
        return {
            "receipt_schema": ROUTING_RECEIPT_SCHEMA,
            "total_receipts": len(receipts),
            "verified_receipts": verified,
            "verified_turns": len(verified_turns),
            "profile_counts": profile_counts,
            "memory_action_counts": memory_action_counts,
            "failed_receipts": len(receipts) - verified,
            "receipt_epoch_start_turn": start_turn,
            "eligible_turns": eligible_turns,
            "legacy_unreceipted_turns": (start_turn - 1) if start_turn else session_turn_count,
            "receipt_coverage": (len(verified_turns) / eligible_turns) if eligible_turns else None,
            "latest_receipt_id": latest_id,
            "latest_receipt_verified": (
                bool(latest_result and latest_result.get("verified")) if latest else None
            ),
            "global_codex_task_coverage": None,
            "global_coverage_reason": "codex_task_denominator_unavailable",
            "task_quality_delta": None,
            "task_quality_reason": "paired_outcome_measurement_unavailable",
        }
