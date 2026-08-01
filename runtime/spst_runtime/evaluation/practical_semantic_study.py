from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import re
import secrets
from typing import Any, Callable

from spst_runtime.evaluation.quality_evidence import paired_hoeffding_interval
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.provider_observation import (
    CODEX_CLI_OBSERVATION_SOURCE,
    validate_provider_observation_binding,
)
from spst_runtime.routing_receipt import RoutingReceiptLedger


STUDY_SCHEMA = "spst-practical-semantic-paired-study-v1"
TASK_PACK_SCHEMA = "spst-novel-practical-task-pack-v1"
BLIND_ARTIFACT_SCHEMA = "spst-practical-semantic-blind-artifact-v1"
REVIEW_SCHEMA = "spst-practical-semantic-review-v1"
CORRECTION_SCHEMA = "spst-practical-semantic-review-correction-v1"
STATUS_SCHEMA = "spst-practical-semantic-study-status-v1"
INTERVENTION_SCHEMA = "spst-practical-semantic-intervention-v1"
EXECUTION_AUTHORITY_SCHEMA = "spst-practical-semantic-execution-authority-v1"
INDEX_KEY = "runtime:practical_semantic_study:index:v1"
RECORD_PREFIX = "runtime:practical_semantic_study:record:"
MINIMUM_PAIR_COUNT = 30
MAXIMUM_PAIR_COUNT = 50
CONFIDENCE_LEVEL = 0.95
CRITERION_WEIGHTS = {
    "technical_correctness": 0.40,
    "constraint_and_edge_coverage": 0.25,
    "minimality_and_safety": 0.15,
    "verification_quality": 0.20,
}
CORRECTION_ALLOWED_REASONS = frozenset({"clerical_data_entry_error"})
MAXIMUM_REVIEW_CORRECTIONS = 1
CORRECTION_POLICY = {
    "schema": "spst-practical-semantic-correction-policy-v1",
    "allowed_reasons": sorted(CORRECTION_ALLOWED_REASONS),
    "maximum_corrections": MAXIMUM_REVIEW_CORRECTIONS,
    "before_unblinding_required": True,
    "same_reviewer_required": True,
    "preserve_superseded_review": True,
    "provider_rerun_permitted": False,
}
REVIEW_POLICY = {
    "schema": "spst-practical-semantic-review-policy-v1",
    "reviewer_kind": "human",
    "reviewer_must_differ_from_generator": True,
    "arm_blinded": True,
    "arm_mapping_not_accessed_required": True,
    "reviewer_independence_attestation_required": True,
    "all_blind_slots_must_be_scored": True,
    "score_minimum": 0,
    "score_maximum": 4,
    "criterion_ids": list(CRITERION_WEIGHTS),
}

_identifier = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_sha256 = re.compile(r"^[0-9a-f]{64}$")


class PracticalSemanticStudyLedger:
    """Append-only, receipt-bound semantic paired-study evidence."""

    def __init__(
        self,
        repository: SQLiteRepository,
        *,
        repository_root: str | None = None,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self.repository = repository
        self.repository_root = repository_root
        self.nonce_factory = nonce_factory or (lambda: secrets.token_hex(32))

    def preregister(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.repository.read_only:
            return self._blocked("practical_semantic_store_read_only")
        normalized, reason = self._normalize_preregistration(payload)
        if reason is not None:
            return self._blocked(reason)
        assert normalized is not None
        unsigned = {
            "schema": STUDY_SCHEMA,
            "kind": "preregistration",
            **normalized,
            "claims": self._claim_boundary(),
        }
        study_sha256 = self._digest(unsigned)
        record = {
            **unsigned,
            "id": f"PSPS-{study_sha256[:16]}",
            "study_sha256": study_sha256,
        }
        if not self._append_event(record["id"], record):
            return self._blocked("practical_semantic_study_exists")
        return self.get(record["id"]) or self._blocked(
            "practical_semantic_preregistration_unreadable"
        )

    def validate_task_pack(self, task_pack: dict[str, Any]) -> dict[str, Any]:
        """Validate a candidate task pack without reading or writing persistent state."""
        normalized, reason = self._normalize_task_pack(task_pack)
        if reason is not None or normalized is None:
            return self._blocked(reason or "practical_semantic_task_pack_invalid")
        return {
            "schema": TASK_PACK_SCHEMA,
            "status": "valid",
            "task_count": normalized["task_count"],
            "task_pack_sha256": normalized["task_pack_sha256"],
            "minimum_pair_count": MINIMUM_PAIR_COUNT,
            "maximum_pair_count": MAXIMUM_PAIR_COUNT,
            "provider_calls_executed": 0,
            "state_changed": False,
        }

    def authorize_execution(self, study_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append exact study-bound external execution authority before any call."""
        if self.repository.read_only:
            return self._blocked("practical_semantic_store_read_only")
        state = self._state(study_id)
        if state is None:
            return self._blocked("practical_semantic_study_not_found")
        if state.get("integrity_reason") is not None:
            return self._blocked(str(state["integrity_reason"]))
        if state["authorities"] or state["pairs"] or state["reviews"]:
            return self._blocked("practical_semantic_execution_authority_not_pending")
        preregistration = state["preregistration"]
        source = payload if isinstance(payload, dict) else {}
        if self._execution_authority_reason(preregistration, source) is not None:
            return self._blocked("practical_semantic_execution_authority_invalid")
        event = {
            "schema": EXECUTION_AUTHORITY_SCHEMA,
            "kind": "execution_authority",
            **deepcopy(source),
        }
        event["authority_sha256"] = self._digest(event)
        if not self._append_event(study_id, event):
            return self._blocked("practical_semantic_execution_authority_write_conflict")
        return self.get(study_id, revalidate_receipts=False) or self._blocked(
            "practical_semantic_execution_authority_unreadable"
        )

    def validate_execution_authority(
        self,
        study_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate execution authority without changing the study or calling a provider."""
        state = self._state(study_id)
        if state is None:
            return self._blocked("practical_semantic_study_not_found")
        reason = self._execution_authority_reason(state["preregistration"], payload)
        if reason is not None:
            return self._blocked(reason)
        return {
            "schema": EXECUTION_AUTHORITY_SCHEMA,
            "status": "valid",
            "study_id": study_id,
            "authority_sha256": self._digest(payload),
            "maximum_adapter_invocations": payload["maximum_adapter_invocations"],
            "provider_calls_executed": 0,
            "state_changed": False,
        }

    def execution_schedule(self, study_id: str) -> dict[str, Any]:
        """Return the operator-only condition schedule, never the blind review surface."""
        state = self._state(study_id)
        if state is None:
            return self._blocked("practical_semantic_study_not_found")
        if state.get("integrity_reason") is not None:
            return self._blocked(str(state["integrity_reason"]))
        return deepcopy(state["preregistration"]["execution_plan"])

    def record_pair(self, study_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.repository.read_only:
            return self._blocked("practical_semantic_store_read_only")
        state = self._state(study_id)
        if state is None:
            return self._blocked("practical_semantic_study_not_found")
        if state.get("integrity_reason") is not None:
            return self._blocked(str(state["integrity_reason"]))
        if len(state["authorities"]) != 1:
            return self._blocked("practical_semantic_execution_authority_required")
        if state["phase"] != "collecting_pairs":
            return self._blocked("practical_semantic_pair_collection_closed")
        normalized, reason = self._normalize_pair(state, payload)
        if reason is not None:
            return self._blocked(reason)
        assert normalized is not None
        event = {
            "schema": STUDY_SCHEMA,
            "kind": "pair",
            "study_id": study_id,
            **normalized,
        }
        event["event_sha256"] = self._digest(event)
        if not self._append_event(study_id, event):
            return self._blocked("practical_semantic_pair_write_conflict")
        return self.get(study_id, revalidate_receipts=False) or self._blocked(
            "practical_semantic_pair_unreadable"
        )

    def blind_artifact(self, study_id: str) -> dict[str, Any]:
        state = self._state(study_id)
        if state is None:
            return self._blocked("practical_semantic_study_not_found")
        if state.get("integrity_reason") is not None:
            return self._blocked(str(state["integrity_reason"]))
        receipt_reason = self._receipt_integrity_reason(state)
        if receipt_reason is not None:
            return self._blocked(receipt_reason)
        target = int(state["preregistration"]["stop_rule"]["target_pair_count"])
        if len(state["pairs"]) != target:
            return self._blocked("practical_semantic_pair_set_incomplete")
        tasks = {
            task["task_id"]: task
            for task in state["preregistration"]["task_pack"]["tasks"]
        }
        slots = sorted(
            [
                {
                    "slot_id": arm["slot_id"],
                    "task_id": pair["task_id"],
                    "domain": tasks[pair["task_id"]]["domain"],
                    "prompt": tasks[pair["task_id"]]["prompt"],
                    "artifact_text": arm["artifact_text"],
                    "artifact_sha256": arm["artifact_sha256"],
                }
                for pair in state["pairs"]
                for arm in (pair["baseline"], pair["treatment"])
            ],
            key=lambda item: item["slot_id"],
        )
        unsigned = {
            "schema": BLIND_ARTIFACT_SCHEMA,
            "study_id": study_id,
            "slot_count": len(slots),
            "slots": slots,
        }
        return {**unsigned, "blind_artifact_sha256": self._digest(unsigned)}

    def submit_review(self, study_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.repository.read_only:
            return self._blocked("practical_semantic_store_read_only")
        state = self._state(study_id)
        if state is None:
            return self._blocked("practical_semantic_study_not_found")
        if state.get("integrity_reason") is not None:
            return self._blocked(str(state["integrity_reason"]))
        if state["phase"] != "collecting_pairs" or state["reviews"]:
            return self._blocked("practical_semantic_review_not_pending")
        artifact = self.blind_artifact(study_id)
        if artifact.get("schema") != BLIND_ARTIFACT_SCHEMA:
            return artifact
        normalized, reason = self._normalize_review(state, payload, artifact)
        if reason is not None:
            return self._blocked(reason)
        assert normalized is not None
        event = {
            "schema": REVIEW_SCHEMA,
            "kind": "review",
            "study_id": study_id,
            "revision": 1,
            **normalized,
        }
        event["review_sha256"] = self._digest(event)
        if not self._append_event(study_id, event):
            return self._blocked("practical_semantic_review_write_conflict")
        return self.get(study_id) or self._blocked("practical_semantic_review_unreadable")

    def correct_review(self, study_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.repository.read_only:
            return self._blocked("practical_semantic_store_read_only")
        state = self._state(study_id)
        if state is None:
            return self._blocked("practical_semantic_study_not_found")
        if state.get("integrity_reason") is not None:
            return self._blocked(str(state["integrity_reason"]))
        receipt_reason = self._receipt_integrity_reason(state)
        if receipt_reason is not None:
            return self._blocked(receipt_reason)
        if state["phase"] == "finalized":
            return self._blocked("practical_semantic_correction_after_unblinding_forbidden")
        if len(state["reviews"]) != 1:
            return self._blocked("practical_semantic_review_correction_not_pending")
        source = payload if isinstance(payload, dict) else {}
        expected_fields = {
            "schema",
            "reviewer_id",
            "reason",
            "prior_review_sha256",
            "blind_artifact_sha256",
            "arm_mapping_not_accessed_since_prior_review",
            "scores",
        }
        if set(source) != expected_fields or source.get("schema") != CORRECTION_SCHEMA:
            return self._blocked("practical_semantic_correction_shape_invalid")
        previous = state["reviews"][0]
        if source.get("reason") not in CORRECTION_ALLOWED_REASONS:
            return self._blocked("practical_semantic_correction_reason_not_preregistered")
        if source.get("reviewer_id") != previous.get("reviewer_id"):
            return self._blocked("practical_semantic_correction_reviewer_mismatch")
        if source.get("prior_review_sha256") != previous.get("review_sha256"):
            return self._blocked("practical_semantic_correction_prior_review_mismatch")
        if source.get("arm_mapping_not_accessed_since_prior_review") is not True:
            return self._blocked("practical_semantic_correction_blinding_not_attested")
        artifact = self.blind_artifact(study_id)
        review_payload = {
            "schema": REVIEW_SCHEMA,
            "reviewer_id": previous["reviewer_id"],
            "reviewer_kind": "human",
            "decision": "scored",
            "blind_artifact_sha256": source.get("blind_artifact_sha256"),
            "arm_mapping_not_accessed": True,
            "reviewer_independence_attested": previous[
                "reviewer_independence_attested"
            ],
            "scores": source.get("scores"),
        }
        normalized, reason = self._normalize_review(state, review_payload, artifact)
        if reason is not None:
            return self._blocked(reason)
        assert normalized is not None
        event = {
            "schema": CORRECTION_SCHEMA,
            "kind": "review_correction",
            "study_id": study_id,
            "revision": 2,
            "reason": source["reason"],
            "supersedes_review_sha256": previous["review_sha256"],
            "arm_mapping_not_accessed_since_prior_review": True,
            **normalized,
        }
        event["review_sha256"] = self._digest(event)
        if not self._append_event(study_id, event):
            return self._blocked("practical_semantic_correction_write_conflict")
        return self.get(study_id) or self._blocked(
            "practical_semantic_correction_unreadable"
        )

    def finalize(self, study_id: str) -> dict[str, Any]:
        if self.repository.read_only:
            return self._blocked("practical_semantic_store_read_only")
        state = self._state(study_id)
        if state is None:
            return self._blocked("practical_semantic_study_not_found")
        if state.get("integrity_reason") is not None:
            return self._blocked(str(state["integrity_reason"]))
        receipt_reason = self._receipt_integrity_reason(state)
        if receipt_reason is not None:
            return self._blocked(receipt_reason)
        if state["phase"] == "finalized":
            return self._blocked("practical_semantic_study_already_finalized")
        if not state["reviews"]:
            return self._blocked("practical_semantic_human_review_required")
        review = state["reviews"][-1]
        measurement = self._measurement(state["pairs"], review)
        event = {
            "schema": STUDY_SCHEMA,
            "kind": "finalization",
            "study_id": study_id,
            "review_sha256": review["review_sha256"],
            "correction_count": max(0, len(state["reviews"]) - 1),
            "measurement": measurement,
            "claims": self._claim_boundary(),
        }
        event["finalization_sha256"] = self._digest(event)
        if not self._append_event(study_id, event):
            return self._blocked("practical_semantic_finalization_write_conflict")
        return self.get(study_id) or self._blocked(
            "practical_semantic_finalization_unreadable"
        )

    def get(
        self,
        study_id: str,
        *,
        revalidate_receipts: bool = True,
    ) -> dict[str, Any] | None:
        state = self._state(study_id)
        if state is None:
            return None
        preregistration = state["preregistration"]
        pair_count = len(state["pairs"])
        target = int(preregistration["stop_rule"]["target_pair_count"])
        receipt_count = sum(pair["routing"]["verified"] is True for pair in state["pairs"])
        finalization = state["finalization"]
        measurement = (
            deepcopy(finalization["measurement"])
            if finalization is not None
            else self._unavailable_measurement(pair_count)
        )
        latest_review = state["reviews"][-1] if state["reviews"] else None
        if finalization is not None:
            status = "reviewed_semantic_evidence"
            reason = None
        elif latest_review is not None:
            status = "pending_unblinding"
            reason = "practical_semantic_finalization_pending"
        elif pair_count == target:
            status = "pending_human_review"
            reason = "practical_semantic_human_review_pending"
        else:
            status = "collecting_pairs"
            reason = "practical_semantic_pair_set_incomplete"
        provenance = self.repository.verify_provenance()
        integrity_reason = state.get("integrity_reason")
        if integrity_reason is None and revalidate_receipts:
            integrity_reason = self._receipt_integrity_reason(state)
        if integrity_reason is not None:
            status = "blocked"
            reason = integrity_reason
            measurement = self._unavailable_measurement(pair_count)
            finalization = None
        elif provenance.get("valid") is not True:
            status = "blocked"
            reason = "provenance_invalid"
        return {
            "schema": STUDY_SCHEMA,
            "id": study_id,
            "status": status,
            "reason": reason,
            "study_sha256": preregistration["study_sha256"],
            "task_pack": deepcopy(preregistration["task_pack"]),
            "generator": deepcopy(preregistration["generator"]),
            "intervention": deepcopy(preregistration["intervention"]),
            "review_policy": deepcopy(preregistration["review_policy"]),
            "correction_policy": deepcopy(preregistration["correction_policy"]),
            "stop_rule": deepcopy(preregistration["stop_rule"]),
            "repository_identity": deepcopy(preregistration["repository_identity"]),
            "execution_plan": {
                "schema": preregistration["execution_plan"]["schema"],
                "condition_order": preregistration["execution_plan"]["condition_order"],
                "condition_order_balance": deepcopy(
                    preregistration["execution_plan"]["condition_order_balance"]
                ),
                "schedule_sha256": preregistration["execution_plan"]["schedule_sha256"],
                "per_task_order_disclosed_to_reviewer": False,
            },
            "execution_authority": {
                "status": "authorized" if state["authorities"] else "pending",
                "authority_sha256": (
                    state["authorities"][0].get("authority_sha256")
                    if state["authorities"]
                    else None
                ),
                "maximum_adapter_invocations": (
                    state["authorities"][0].get("maximum_adapter_invocations")
                    if state["authorities"]
                    else 0
                ),
                "paid_provider_calls_authorized": False,
                "automatic_retry_permitted": False,
            },
            "pair_collection": {
                "pair_count": pair_count,
                "target_pair_count": target,
                "complete": pair_count == target,
            },
            "routing_receipts": {
                "verified_count": receipt_count,
                "required_count": target,
                "coverage": round(receipt_count / target, 6),
                "complete": receipt_count == target,
                "receipt_ids": [pair["routing"]["receipt_id"] for pair in state["pairs"]],
            },
            "human_review": {
                "status": "recorded" if latest_review is not None else "pending",
                "reviewer_id": latest_review.get("reviewer_id") if latest_review else None,
                "reviewer_kind": latest_review.get("reviewer_kind") if latest_review else None,
                "reviewer_role_separated": bool(
                    latest_review
                    and latest_review.get("reviewer_id")
                    != preregistration["generator"]["generator_id"]
                ),
                "reviewer_independence_attested": (
                    latest_review.get("reviewer_independence_attested")
                    if latest_review
                    else None
                ),
                "reviewer_independence_cryptographically_verified": False,
                "arm_mapping_not_accessed_attested": (
                    latest_review.get("arm_mapping_not_accessed")
                    if latest_review
                    else None
                ),
                "correction_count": max(0, len(state["reviews"]) - 1),
                "latest_review_sha256": (
                    latest_review.get("review_sha256") if latest_review else None
                ),
            },
            "measurement": measurement,
            "task_quality": {
                "available": finalization is not None,
                "paired_delta": measurement.get("paired_delta"),
                "pair_count": measurement.get("sample_count", 0),
                "uncertainty": measurement.get("uncertainty"),
                "direction": measurement.get("direction"),
                "claim_eligible": bool(
                    finalization is not None
                    and measurement.get("direction") == "beneficial"
                ),
                "generalization_beyond_registered_corpus": False,
                "automatic_promotion": False,
            },
            "claims": self._claim_boundary(),
            "state_provenance": {
                key: provenance.get(key)
                for key in ("valid", "entries", "latest_hash", "key_source")
                if key in provenance
            },
        }

    def history(self) -> list[dict[str, Any]]:
        return [
            study
            for study_id in self._study_ids()
            if (study := self.get(study_id)) is not None
        ]

    def status(self) -> dict[str, Any]:
        records = self.history()
        latest = records[-1] if records else None
        available = [record for record in records if record["task_quality"]["available"]]
        selected = available[-1] if available else latest
        task_quality = (
            deepcopy(selected["task_quality"])
            if selected is not None
            else {
                "available": False,
                "paired_delta": None,
                "pair_count": 0,
                "uncertainty": None,
                "direction": None,
                "claim_eligible": False,
                "generalization_beyond_registered_corpus": False,
                "automatic_promotion": False,
            }
        )
        if selected is None:
            reason = "practical_semantic_study_unavailable"
        elif task_quality["available"]:
            reason = None
        else:
            reason = selected["reason"]
        return {
            "schema": STATUS_SCHEMA,
            "study_count": len(records),
            "completed_study_count": len(available),
            "latest_study_id": latest["id"] if latest else None,
            "selected_study_id": selected["id"] if selected else None,
            "status": "ready" if task_quality["available"] else "unavailable",
            "reason": reason,
            "task_quality": task_quality,
            "latest": deepcopy(latest) if latest is not None else {},
            "read_only_projection": True,
        }

    def _normalize_preregistration(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        source = payload if isinstance(payload, dict) else {}
        expected_fields = {
            "task_pack",
            "generator",
            "intervention",
            "review_policy",
            "correction_policy",
            "stop_rule",
            "repository_identity",
        }
        if set(source) != expected_fields:
            return None, "practical_semantic_preregistration_shape_invalid"
        task_pack, reason = self._normalize_task_pack(source.get("task_pack"))
        if reason is not None:
            return None, reason
        assert task_pack is not None
        generator = source.get("generator")
        if (
            not isinstance(generator, dict)
            or set(generator) != {"generator_id", "generator_kind"}
            or not self._safe_identifier(generator.get("generator_id"))
            or generator.get("generator_kind") != "model_adapter"
        ):
            return None, "practical_semantic_generator_invalid"
        if source.get("review_policy") != REVIEW_POLICY:
            return None, "practical_semantic_review_policy_invalid"
        if source.get("correction_policy") != CORRECTION_POLICY:
            return None, "practical_semantic_correction_policy_invalid"
        intervention = source.get("intervention")
        if (
            not isinstance(intervention, dict)
            or set(intervention)
            != {
                "schema",
                "context_sha256",
                "model_instructions_sha256",
                "context_kind",
                "task_specific_answers_absent_attested",
            }
            or intervention.get("schema") != INTERVENTION_SCHEMA
            or intervention.get("context_kind") != "spst_process_guidance"
            or not self._valid_sha256(intervention.get("context_sha256"))
            or not self._valid_sha256(intervention.get("model_instructions_sha256"))
            or intervention.get("task_specific_answers_absent_attested") is not True
        ):
            return None, "practical_semantic_intervention_invalid"
        target = len(task_pack["tasks"])
        expected_stop_rule = {
            "target_pair_count": target,
            "minimum_pair_count": MINIMUM_PAIR_COUNT,
            "maximum_pair_count": MAXIMUM_PAIR_COUNT,
            "optional_stopping_permitted": False,
            "automatic_retry_permitted": False,
            "partial_execution_claim_eligible": False,
        }
        if source.get("stop_rule") != expected_stop_rule:
            return None, "practical_semantic_stop_rule_invalid"
        identity = source.get("repository_identity")
        if (
            not isinstance(identity, dict)
            or not self._valid_sha256(identity.get("identity_sha256"))
            or not isinstance(identity.get("head_revision"), str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", identity["head_revision"])
        ):
            return None, "practical_semantic_repository_identity_invalid"
        nonce = self.nonce_factory()
        if not isinstance(nonce, str) or not nonce:
            return None, "practical_semantic_blinding_nonce_invalid"
        ranked_task_ids = sorted(
            (item["task_id"] for item in task_pack["tasks"]),
            key=lambda task_id: self._digest([nonce, task_id, "condition-order"]),
        )
        control_first_count = (len(ranked_task_ids) + 1) // 2
        schedule_items = [
            {
                "task_id": task_id,
                "first_condition": (
                    "baseline" if index < control_first_count else "treatment"
                ),
            }
            for index, task_id in enumerate(ranked_task_ids)
        ]
        execution_plan = {
            "schema": "spst-practical-semantic-condition-schedule-v1",
            "condition_order": "sha256_rank_balanced_v1",
            "condition_order_balance": {
                "baseline_first": control_first_count,
                "treatment_first": len(ranked_task_ids) - control_first_count,
            },
            "items": schedule_items,
        }
        execution_plan["schedule_sha256"] = self._digest(execution_plan)
        return {
            "task_pack": task_pack,
            "generator": deepcopy(generator),
            "intervention": deepcopy(intervention),
            "review_policy": deepcopy(REVIEW_POLICY),
            "correction_policy": deepcopy(CORRECTION_POLICY),
            "stop_rule": expected_stop_rule,
            "repository_identity": deepcopy(identity),
            "execution_plan": execution_plan,
            "blinding": {
                "nonce_sha256": hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
                "nonce": nonce,
                "mapping_disclosed": False,
            },
        }, None

    def _normalize_task_pack(
        self, value: Any
    ) -> tuple[dict[str, Any] | None, str | None]:
        if not isinstance(value, dict) or set(value) != {
            "schema",
            "workload_class",
            "generalization_target",
            "tasks",
        }:
            return None, "practical_semantic_task_pack_shape_invalid"
        tasks = value.get("tasks")
        if (
            value.get("schema") != TASK_PACK_SCHEMA
            or value.get("workload_class") != "novel_practical_repository_task"
            or not isinstance(tasks, list)
            or not MINIMUM_PAIR_COUNT <= len(tasks) <= MAXIMUM_PAIR_COUNT
        ):
            return None, "practical_semantic_task_count_invalid"
        normalized: list[dict[str, Any]] = []
        task_ids: set[str] = set()
        prompt_digests: set[str] = set()
        for item in tasks:
            if not isinstance(item, dict) or set(item) != {"task_id", "domain", "prompt"}:
                return None, "practical_semantic_task_shape_invalid"
            task_id = self._safe_identifier(item.get("task_id"))
            domain = item.get("domain")
            prompt = item.get("prompt")
            if (
                not task_id
                or task_id in task_ids
                or not isinstance(domain, str)
                or not domain.strip()
                or len(domain) > 64
                or not isinstance(prompt, str)
                or not prompt.strip()
                or len(prompt) > 12000
            ):
                return None, "practical_semantic_task_invalid"
            prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            if prompt_sha256 in prompt_digests:
                return None, "practical_semantic_duplicate_task_semantics"
            task_ids.add(task_id)
            prompt_digests.add(prompt_sha256)
            normalized.append(
                {
                    "task_id": task_id,
                    "domain": domain.strip().lower(),
                    "prompt": prompt,
                    "prompt_sha256": prompt_sha256,
                    "task_contract_sha256": self._digest(
                        {"task_id": task_id, "domain": domain.strip().lower(), "prompt": prompt}
                    ),
                }
            )
        pack = {
            "schema": TASK_PACK_SCHEMA,
            "workload_class": "novel_practical_repository_task",
            "generalization_target": str(value.get("generalization_target") or ""),
            "task_count": len(normalized),
            "tasks": normalized,
        }
        pack["task_pack_sha256"] = self._digest(pack)
        return pack, None

    def _normalize_pair(
        self, state: dict[str, Any], payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        source = payload if isinstance(payload, dict) else {}
        if set(source) != {"task_id", "task_contract_sha256", "routing_receipt_id", "baseline", "treatment"}:
            return None, "practical_semantic_pair_shape_invalid"
        task = next(
            (
                item
                for item in state["preregistration"]["task_pack"]["tasks"]
                if item["task_id"] == source.get("task_id")
            ),
            None,
        )
        if task is None:
            return None, "practical_semantic_task_not_registered"
        if any(pair["task_id"] == task["task_id"] for pair in state["pairs"]):
            return None, "practical_semantic_duplicate_pair"
        if source.get("task_contract_sha256") != task["task_contract_sha256"]:
            return None, "practical_semantic_task_contract_mismatch"
        receipt_id = self._safe_identifier(source.get("routing_receipt_id"))
        if not receipt_id:
            return None, "practical_semantic_routing_receipt_invalid"
        if any(pair["routing"]["receipt_id"] == receipt_id for pair in state["pairs"]):
            return None, "practical_semantic_routing_receipt_reused"
        verification = RoutingReceiptLedger(self.repository.path, read_only=True).verify(
            receipt_id,
            repository_root=self.repository_root,
        )
        if verification.get("verified") is not True:
            return None, "practical_semantic_routing_receipt_unverified"
        if verification.get("route", {}).get("prompt_sha256") != task["prompt_sha256"]:
            return None, "practical_semantic_routing_receipt_task_mismatch"
        repository = verification.get("repository", {})
        if repository.get("bound") is not True or repository.get("current_match") is not True:
            return None, "practical_semantic_routing_receipt_repository_mismatch"
        arms: dict[str, dict[str, Any]] = {}
        for arm_name in ("baseline", "treatment"):
            arm = source.get(arm_name)
            if (
                not isinstance(arm, dict)
                or set(arm)
                != {
                    "artifact_text",
                    "producer_response_id",
                    "provider_observation",
                }
                or not isinstance(arm.get("artifact_text"), str)
                or not arm.get("artifact_text")
                or not self._safe_identifier(arm.get("producer_response_id"))
            ):
                return None, f"practical_semantic_{arm_name}_artifact_invalid"
            artifact_text = str(arm["artifact_text"])
            artifact_sha256 = hashlib.sha256(artifact_text.encode("utf-8")).hexdigest()
            observation = arm.get("provider_observation")
            expected_instructions_sha256 = (
                state["preregistration"]["intervention"]["model_instructions_sha256"]
                if arm_name == "treatment"
                else None
            )
            observation_valid, _ = validate_provider_observation_binding(
                observation,
                expected_output_sha256=artifact_sha256,
                expected_context_intervention_sha256=None,
            )
            if (
                not observation_valid
                or not isinstance(observation, dict)
                or observation.get("observation_source") != CODEX_CLI_OBSERVATION_SOURCE
                or observation.get("request", {}).get("prompt_sha256")
                != task["prompt_sha256"]
                or observation.get("request", {}).get("instructions_sha256")
                != (expected_instructions_sha256 or hashlib.sha256(b"").hexdigest())
                or observation.get("response", {}).get("response_id_sha256")
                != arm.get("producer_response_id")
            ):
                return None, f"practical_semantic_{arm_name}_provider_observation_invalid"
            guard = observation.get("execution_guard")
            if not isinstance(guard, dict):
                return None, f"practical_semantic_{arm_name}_execution_guard_invalid"
            nonce = state["preregistration"]["blinding"]["nonce"]
            slot_id = f"slot-{self._digest([nonce, task['task_id'], arm_name, artifact_sha256])[:16]}"
            arms[arm_name] = {
                "artifact_text": artifact_text,
                "artifact_sha256": artifact_sha256,
                "producer_response_id": str(arm["producer_response_id"]),
                "provider_observation": deepcopy(observation),
                "provider_observation_sha256": str(observation["observation_sha256"]),
                "execution_guard_sha256": self._digest(guard),
                "slot_id": slot_id,
            }
        if arms["baseline"]["producer_response_id"] == arms["treatment"]["producer_response_id"]:
            return None, "practical_semantic_distinct_provider_responses_required"
        return {
            "task_id": task["task_id"],
            "task_contract_sha256": task["task_contract_sha256"],
            "routing": {
                "receipt_id": receipt_id,
                "receipt_schema": verification.get("schema"),
                "verified": True,
                "repository_current_match_at_recording": True,
                "receipt_record_hash": verification.get("binding", {}).get(
                    "receipt_record_hash"
                ),
            },
            **arms,
        }, None

    def _normalize_review(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
        artifact: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        source = payload if isinstance(payload, dict) else {}
        expected_fields = {
            "schema",
            "reviewer_id",
            "reviewer_kind",
            "decision",
            "blind_artifact_sha256",
            "arm_mapping_not_accessed",
            "reviewer_independence_attested",
            "scores",
        }
        if set(source) != expected_fields or source.get("schema") != REVIEW_SCHEMA:
            return None, "practical_semantic_review_shape_invalid"
        reviewer_id = self._safe_identifier(source.get("reviewer_id"))
        generator_id = state["preregistration"]["generator"]["generator_id"]
        if not reviewer_id or reviewer_id == generator_id:
            return None, "practical_semantic_reviewer_generator_role_conflict"
        if source.get("reviewer_kind") != "human" or source.get("decision") != "scored":
            return None, "practical_semantic_human_scored_review_required"
        if source.get("blind_artifact_sha256") != artifact["blind_artifact_sha256"]:
            return None, "practical_semantic_blind_artifact_mismatch"
        if source.get("arm_mapping_not_accessed") is not True:
            return None, "practical_semantic_arm_mapping_non_access_required"
        if source.get("reviewer_independence_attested") is not True:
            return None, "practical_semantic_reviewer_independence_attestation_required"
        scores = source.get("scores")
        expected_slots = {item["slot_id"] for item in artifact["slots"]}
        if not isinstance(scores, list) or len(scores) != len(expected_slots):
            return None, "practical_semantic_review_slot_coverage_invalid"
        normalized_scores: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in scores:
            if not isinstance(item, dict) or set(item) != {"slot_id", "criteria", "comment"}:
                return None, "practical_semantic_review_score_shape_invalid"
            slot_id = item.get("slot_id")
            criteria = item.get("criteria")
            if (
                slot_id not in expected_slots
                or slot_id in seen
                or not isinstance(criteria, dict)
                or set(criteria) != set(CRITERION_WEIGHTS)
                or any(
                    not isinstance(score, int)
                    or isinstance(score, bool)
                    or not 0 <= score <= 4
                    for score in criteria.values()
                )
                or not isinstance(item.get("comment"), str)
            ):
                return None, "practical_semantic_review_score_invalid"
            seen.add(str(slot_id))
            normalized_scores.append(deepcopy(item))
        if seen != expected_slots:
            return None, "practical_semantic_review_slot_coverage_invalid"
        return {
            "reviewer_id": reviewer_id,
            "reviewer_kind": "human",
            "decision": "scored",
            "blind_artifact_sha256": artifact["blind_artifact_sha256"],
            "arm_mapping_not_accessed": True,
            "reviewer_independence_attested": True,
            "reviewer_identity_cryptographically_verified": False,
            "reviewer_independence_cryptographically_verified": False,
            "scores": sorted(normalized_scores, key=lambda item: item["slot_id"]),
        }, None

    def _measurement(
        self, pairs: list[dict[str, Any]], review: dict[str, Any]
    ) -> dict[str, Any]:
        scores = {
            item["slot_id"]: self._normalized_slot_score(item["criteria"])
            for item in review["scores"]
        }
        paired_scores = [
            {
                "task_id": pair["task_id"],
                "baseline_score": scores[pair["baseline"]["slot_id"]],
                "treatment_score": scores[pair["treatment"]["slot_id"]],
                "delta": round(
                    scores[pair["treatment"]["slot_id"]]
                    - scores[pair["baseline"]["slot_id"]],
                    6,
                ),
                "routing_receipt_id": pair["routing"]["receipt_id"],
            }
            for pair in sorted(pairs, key=lambda item: item["task_id"])
        ]
        deltas = [float(item["delta"]) for item in paired_scores]
        baseline = [float(item["baseline_score"]) for item in paired_scores]
        treatment = [float(item["treatment_score"]) for item in paired_scores]
        uncertainty = paired_hoeffding_interval(
            deltas,
            confidence_level=CONFIDENCE_LEVEL,
        )
        lower = uncertainty.get("lower_bound")
        upper = uncertainty.get("upper_bound")
        direction = (
            "beneficial"
            if isinstance(lower, float) and lower > 0.0
            else "harmful"
            if isinstance(upper, float) and upper < 0.0
            else "inconclusive"
        )
        return {
            "available": True,
            "metric_scope": "human_reviewed_semantic_quality_on_preregistered_novel_practical_tasks",
            "sample_count": len(paired_scores),
            "minimum_pair_count": MINIMUM_PAIR_COUNT,
            "baseline_mean": self._mean(baseline),
            "treatment_mean": self._mean(treatment),
            "paired_delta": self._mean(deltas),
            "paired_scores": paired_scores,
            "uncertainty": uncertainty,
            "direction": direction,
        }

    def _state(self, study_id: str) -> dict[str, Any] | None:
        if not self._safe_identifier(study_id):
            return None
        stored = asyncio.run(self.repository.load(f"{RECORD_PREFIX}{study_id}"))
        if not isinstance(stored, dict) or not isinstance(stored.get("events"), list):
            return None
        events = [event for event in stored["events"] if isinstance(event, dict)]
        if not events or events[0].get("kind") != "preregistration":
            return None
        reviews = [
            event
            for event in events
            if event.get("kind") in {"review", "review_correction"}
        ]
        finalizations = [event for event in events if event.get("kind") == "finalization"]
        return {
            "preregistration": events[0],
            "pairs": [event for event in events if event.get("kind") == "pair"],
            "authorities": [
                event for event in events if event.get("kind") == "execution_authority"
            ],
            "reviews": reviews,
            "finalization": finalizations[-1] if finalizations else None,
            "phase": (
                "finalized"
                if finalizations
                else "pending_unblinding"
                if reviews
                else "collecting_pairs"
            ),
            "integrity_reason": self._event_integrity_reason(study_id, events),
        }

    def _event_integrity_reason(
        self,
        study_id: str,
        events: list[dict[str, Any]],
    ) -> str | None:
        preregistration = events[0]
        if (
            preregistration.get("schema") != STUDY_SCHEMA
            or preregistration.get("kind") != "preregistration"
            or preregistration.get("id") != study_id
        ):
            return "practical_semantic_preregistration_invalid"
        study_sha256 = preregistration.get("study_sha256")
        unsigned = {
            key: value
            for key, value in preregistration.items()
            if key not in {"id", "study_sha256"}
        }
        if not self._valid_sha256(study_sha256) or self._digest(unsigned) != study_sha256:
            return "practical_semantic_preregistration_digest_mismatch"

        phase = "pairs"
        task_ids: set[str] = set()
        receipt_ids: set[str] = set()
        review_count = 0
        finalization_count = 0
        authority_count = 0
        for event in events[1:]:
            kind = event.get("kind")
            if kind == "execution_authority":
                if phase != "pairs" or task_ids or authority_count:
                    return "practical_semantic_event_order_invalid"
                digest = event.get("authority_sha256")
                if not self._valid_sha256(digest) or self._digest(
                    {key: value for key, value in event.items() if key != "authority_sha256"}
                ) != digest:
                    return "practical_semantic_execution_authority_digest_mismatch"
                authority_reason = self._execution_authority_reason(
                    preregistration,
                    {key: value for key, value in event.items() if key not in {"kind", "authority_sha256"}},
                )
                if authority_reason is not None:
                    return authority_reason
                authority_count += 1
            elif kind == "pair":
                if phase != "pairs" or authority_count != 1:
                    return "practical_semantic_event_order_invalid"
                digest = event.get("event_sha256")
                if not self._valid_sha256(digest) or self._digest(
                    {key: value for key, value in event.items() if key != "event_sha256"}
                ) != digest:
                    return "practical_semantic_pair_digest_mismatch"
                task_id = str(event.get("task_id") or "")
                receipt_id = str(event.get("routing", {}).get("receipt_id") or "")
                if task_id in task_ids or receipt_id in receipt_ids:
                    return "practical_semantic_pair_identity_reused"
                task_ids.add(task_id)
                receipt_ids.add(receipt_id)
            elif kind in {"review", "review_correction"}:
                if finalization_count or (kind == "review" and review_count != 0):
                    return "practical_semantic_event_order_invalid"
                if kind == "review_correction" and review_count != 1:
                    return "practical_semantic_event_order_invalid"
                digest = event.get("review_sha256")
                if not self._valid_sha256(digest) or self._digest(
                    {key: value for key, value in event.items() if key != "review_sha256"}
                ) != digest:
                    return "practical_semantic_review_digest_mismatch"
                review_count += 1
                if review_count > MAXIMUM_REVIEW_CORRECTIONS + 1:
                    return "practical_semantic_correction_limit_exceeded"
                phase = "reviews"
            elif kind == "finalization":
                if phase != "reviews" or finalization_count:
                    return "practical_semantic_event_order_invalid"
                digest = event.get("finalization_sha256")
                if not self._valid_sha256(digest) or self._digest(
                    {key: value for key, value in event.items() if key != "finalization_sha256"}
                ) != digest:
                    return "practical_semantic_finalization_digest_mismatch"
                finalization_count += 1
                phase = "finalized"
            else:
                return "practical_semantic_event_kind_invalid"

        return None

    def _execution_authority_reason(
        self,
        preregistration: dict[str, Any],
        payload: dict[str, Any],
    ) -> str | None:
        source = payload if isinstance(payload, dict) else {}
        expected = {
            "schema": EXECUTION_AUTHORITY_SCHEMA,
            "decision": "authorized",
            "authority_label": source.get("authority_label"),
            "study_id": preregistration["id"],
            "study_sha256": preregistration["study_sha256"],
            "repository_identity_sha256": preregistration["repository_identity"][
                "identity_sha256"
            ],
            "task_pack_sha256": preregistration["task_pack"]["task_pack_sha256"],
            "intervention_sha256": preregistration["intervention"]["context_sha256"],
            "external_provider_calls_authorized": True,
            "chatgpt_plan_usage_authorized": True,
            "paid_provider_calls_authorized": False,
            "maximum_adapter_invocations": 2
            * int(preregistration["stop_rule"]["target_pair_count"]),
            "per_call_no_paid_guard_required": True,
            "optional_stopping_permitted": False,
            "automatic_retry_permitted": False,
        }
        if (
            set(source) != set(expected)
            or not self._safe_identifier(source.get("authority_label"))
            or source != expected
        ):
            return "practical_semantic_execution_authority_invalid"
        return None

    def _receipt_integrity_reason(self, state: dict[str, Any]) -> str | None:
        preregistration = state["preregistration"]
        tasks = preregistration.get("task_pack", {}).get("tasks", [])
        contracts = {
            item.get("task_id"): item
            for item in tasks
            if isinstance(item, dict)
        }
        recorded_pairs = state["pairs"]
        expected_repository_identity = preregistration.get("repository_identity", {}).get(
            "identity_sha256"
        )
        for pair in recorded_pairs:
            task = contracts.get(pair.get("task_id"))
            if (
                task is None
                or pair.get("task_contract_sha256") != task.get("task_contract_sha256")
            ):
                return "practical_semantic_task_contract_mismatch"
            verification = RoutingReceiptLedger(
                self.repository.path,
                read_only=True,
            ).verify(
                str(pair.get("routing", {}).get("receipt_id") or ""),
                repository_root=None,
            )
            if verification.get("verified") is not True:
                return "practical_semantic_routing_receipt_unverified"
            if verification.get("route", {}).get("prompt_sha256") != task.get(
                "prompt_sha256"
            ):
                return "practical_semantic_routing_receipt_task_mismatch"
            if verification.get("repository", {}).get(
                "identity_sha256"
            ) != expected_repository_identity:
                return "practical_semantic_routing_receipt_repository_mismatch"
            if verification.get("binding", {}).get("receipt_record_hash") != pair.get(
                "routing", {}
            ).get("receipt_record_hash"):
                return "practical_semantic_routing_receipt_binding_mismatch"
            for arm_name in ("baseline", "treatment"):
                arm = pair.get(arm_name)
                if not isinstance(arm, dict):
                    return f"practical_semantic_{arm_name}_artifact_invalid"
                artifact_text = arm.get("artifact_text")
                artifact_sha256 = arm.get("artifact_sha256")
                observation = arm.get("provider_observation")
                guard = observation.get("execution_guard") if isinstance(observation, dict) else None
                expected_instructions_sha256 = (
                    preregistration["intervention"]["model_instructions_sha256"]
                    if arm_name == "treatment"
                    else hashlib.sha256(b"").hexdigest()
                )
                observation_valid, _ = validate_provider_observation_binding(
                    observation,
                    expected_output_sha256=str(artifact_sha256 or ""),
                    expected_context_intervention_sha256=None,
                )
                if (
                    not isinstance(artifact_text, str)
                    or hashlib.sha256(artifact_text.encode("utf-8")).hexdigest()
                    != artifact_sha256
                    or not observation_valid
                    or not isinstance(observation, dict)
                    or observation.get("observation_source") != CODEX_CLI_OBSERVATION_SOURCE
                    or observation.get("observation_sha256")
                    != arm.get("provider_observation_sha256")
                    or observation.get("request", {}).get("prompt_sha256")
                    != task.get("prompt_sha256")
                    or observation.get("request", {}).get("instructions_sha256")
                    != expected_instructions_sha256
                    or observation.get("response", {}).get("response_id_sha256")
                    != arm.get("producer_response_id")
                    or not isinstance(guard, dict)
                    or self._digest(guard) != arm.get("execution_guard_sha256")
                ):
                    return f"practical_semantic_{arm_name}_provider_observation_invalid"
        return None

    def _append_event(self, study_id: str, event: dict[str, Any]) -> bool:
        key = f"{RECORD_PREFIX}{study_id}"
        with self.repository.locked():
            existing = asyncio.run(self.repository.load(key))
            if existing is None:
                if event.get("kind") != "preregistration":
                    return False
                inserted = asyncio.run(
                    self.repository.save_if_absent(
                        key,
                        {"schema": STUDY_SCHEMA, "events": [event]},
                    )
                )
                if not inserted:
                    return False
                index = asyncio.run(self.repository.load(INDEX_KEY))
                study_ids = self._index_ids(index)
                asyncio.run(
                    self.repository.save(
                        INDEX_KEY,
                        {"schema": STUDY_SCHEMA, "study_ids": [*study_ids, study_id]},
                    )
                )
                return True
            events = existing.get("events") if isinstance(existing, dict) else None
            if not isinstance(events, list):
                return False
            expected_hash = SQLiteRepository.record_hash(existing)
            updated = {"schema": STUDY_SCHEMA, "events": [*events, event]}
            return asyncio.run(
                self.repository.replace_if_record_hash(key, expected_hash, updated)
            )

    def _study_ids(self) -> list[str]:
        return self._index_ids(asyncio.run(self.repository.load(INDEX_KEY)))

    @staticmethod
    def _index_ids(value: Any) -> list[str]:
        if not isinstance(value, dict) or value.get("schema") != STUDY_SCHEMA:
            return []
        study_ids = value.get("study_ids")
        return [str(item) for item in study_ids] if isinstance(study_ids, list) else []

    @staticmethod
    def _normalized_slot_score(criteria: dict[str, int]) -> float:
        return round(
            sum(criteria[key] * weight for key, weight in CRITERION_WEIGHTS.items()) / 4.0,
            6,
        )

    @staticmethod
    def _mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 6) if values else None

    @staticmethod
    def _unavailable_measurement(sample_count: int) -> dict[str, Any]:
        return {
            "available": False,
            "metric_scope": "human_reviewed_semantic_quality_on_preregistered_novel_practical_tasks",
            "sample_count": sample_count,
            "minimum_pair_count": MINIMUM_PAIR_COUNT,
            "baseline_mean": None,
            "treatment_mean": None,
            "paired_delta": None,
            "paired_scores": [],
            "uncertainty": None,
            "direction": None,
        }

    @staticmethod
    def _claim_boundary() -> dict[str, bool]:
        return {
            "model_weight_change_claimed": False,
            "general_model_quality_claimed": False,
            "causal_spst_advantage_claimed": False,
            "reviewer_identity_cryptographically_verified": False,
            "reviewer_independence_cryptographically_verified": False,
            "automatic_promotion": False,
        }

    @staticmethod
    def _blocked(reason: str) -> dict[str, Any]:
        return {
            "schema": STUDY_SCHEMA,
            "status": "blocked",
            "reason": reason,
            "task_quality": {
                "available": False,
                "paired_delta": None,
                "claim_eligible": False,
            },
            "claims": PracticalSemanticStudyLedger._claim_boundary(),
        }

    @staticmethod
    def _safe_identifier(value: Any) -> str:
        candidate = str(value or "")
        return candidate if _identifier.fullmatch(candidate) else ""

    @staticmethod
    def _valid_sha256(value: Any) -> bool:
        return isinstance(value, str) and bool(_sha256.fullmatch(value))

    @staticmethod
    def _digest(value: Any) -> str:
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
