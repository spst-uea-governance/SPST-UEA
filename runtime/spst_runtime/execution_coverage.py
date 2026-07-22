import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from spst_runtime.action_manifest import (
    ACTION_MANIFEST_PREFIX,
    R0_EXTERNAL_OPERATIONS,
    R1_EXTERNAL_OPERATIONS,
    R2_EXTERNAL_OPERATIONS,
    ActionManifestLedger,
)
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.repository_identity import capture_repository_identity
from spst_runtime.routing_receipt import RoutingReceiptLedger
from spst_runtime.verification_profiles import (
    ACTION_QUALITY_PROFILE_NAMES,
    PROFILE_CONTRACT_VERSION,
    SNAPSHOT_PROFILE_NAMES,
    command_for,
    profile_contract_for,
)


EXECUTION_PLAN_SCHEMA = "spst-governed-execution-plan-v1"
EXECUTION_PLAN_REQUEST_SCHEMA = "spst-governed-execution-plan-request-v1"
EXECUTION_PLAN_PREFIX = "governed_execution_plan:v1:"
EXECUTION_COVERAGE_SCHEMA = "spst-governed-execution-coverage-v1"
MAX_DECLARED_ACTIONS = 128
GLOBAL_COVERAGE_REASON = "codex_tool_call_denominator_unavailable"


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


def _workspace_digest(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve())).replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _risk_for_contract(contract: dict[str, Any]) -> dict[str, Any]:
    if contract["kind"] == "fixed_profile":
        profile = contract["profile"]
        if profile in SNAPSHOT_PROFILE_NAMES or profile == "git_diff_check":
            return {
                "level": "R0",
                "requires_human_approval": False,
            }
        if profile in ACTION_QUALITY_PROFILE_NAMES:
            return {
                "level": "R1",
                "requires_human_approval": False,
            }
        raise ValueError("verification_profile_not_permitted")

    operation = contract["operation"]
    if operation in R0_EXTERNAL_OPERATIONS:
        level = "R0"
    elif operation in R1_EXTERNAL_OPERATIONS:
        level = "R1"
    elif operation in R2_EXTERNAL_OPERATIONS:
        level = "R2"
    else:
        level = "R2"
    return {
        "level": level,
        "requires_human_approval": level == "R2",
    }


def _fixed_contract(profile: str) -> dict[str, Any]:
    normalized_profile = profile.strip()
    definition = profile_contract_for(
        normalized_profile,
        contract_version=PROFILE_CONTRACT_VERSION,
    )
    command = command_for(
        normalized_profile,
        contract_version=PROFILE_CONTRACT_VERSION,
    )
    if definition is None or command is None:
        raise ValueError("verification_profile_not_permitted")
    profile_contract_sha256 = _canonical_hash(definition)
    return {
        "kind": "fixed_profile",
        "operation": (
            "inspect"
            if normalized_profile in SNAPSHOT_PROFILE_NAMES
            or normalized_profile == "git_diff_check"
            else "local_test"
        ),
        "tool_name": "verify.local",
        "profile": normalized_profile,
        "profile_contract_version": PROFILE_CONTRACT_VERSION,
        "profile_contract_sha256": profile_contract_sha256,
        "arguments_sha256": _canonical_hash(
            {
                "profile": normalized_profile,
                "profile_contract_sha256": profile_contract_sha256,
            }
        ),
        "command_sha256": _canonical_hash({"command": list(command)}),
    }


def _external_contract(
    *,
    operation: str,
    tool_name: str,
    arguments_sha256: str,
) -> dict[str, Any]:
    normalized_operation = operation.strip().lower()
    normalized_tool = tool_name.strip()
    if not normalized_operation:
        raise ValueError("external_operation_required")
    if not normalized_tool:
        raise ValueError("external_tool_name_required")
    if not _is_sha256(arguments_sha256):
        raise ValueError("external_arguments_digest_invalid")
    return {
        "kind": "external_tool",
        "operation": normalized_operation,
        "tool_name": normalized_tool,
        "profile": None,
        "profile_contract_version": None,
        "profile_contract_sha256": None,
        "arguments_sha256": arguments_sha256.lower(),
        "command_sha256": None,
    }


def _contract_from_request(request: dict[str, Any]) -> dict[str, Any]:
    kind = request.get("kind")
    if kind == "fixed_profile":
        profile = request.get("profile")
        if not isinstance(profile, str):
            raise ValueError("fixed_profile_required")
        unexpected = set(request) - {"slot_id", "kind", "profile"}
        if unexpected:
            raise ValueError("declared_action_fields_invalid")
        return _fixed_contract(profile)
    if kind == "external_tool":
        unexpected = set(request) - {
            "slot_id",
            "kind",
            "operation",
            "tool_name",
            "arguments_sha256",
        }
        if unexpected:
            raise ValueError("declared_action_fields_invalid")
        operation = request.get("operation")
        tool_name = request.get("tool_name")
        arguments_sha256 = request.get("arguments_sha256")
        if (
            not isinstance(operation, str)
            or not isinstance(tool_name, str)
            or not isinstance(arguments_sha256, str)
        ):
            raise ValueError("external_action_contract_incomplete")
        return _external_contract(
            operation=operation,
            tool_name=tool_name,
            arguments_sha256=arguments_sha256,
        )
    raise ValueError("declared_action_kind_invalid")


def _contract_from_action(action: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "kind",
        "operation",
        "tool_name",
        "profile",
        "profile_contract_version",
        "profile_contract_sha256",
        "arguments_sha256",
        "command_sha256",
    )
    return {field: action.get(field) for field in fields}


def _valid_slot_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and all(
            character
            in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
            for character in value
        )
    )


def _normalize_slot(request: dict[str, Any], ordinal: int) -> dict[str, Any]:
    slot_id = request.get("slot_id")
    if not _valid_slot_id(slot_id):
        raise ValueError("declared_action_slot_id_invalid")
    assert isinstance(slot_id, str)
    contract = _contract_from_request(request)
    return {
        "ordinal": ordinal,
        "slot_id": slot_id,
        "contract": contract,
        "contract_sha256": _canonical_hash(contract),
        "risk": _risk_for_contract(contract),
        "expected_observation": (
            "runtime_execution_verified"
            if contract["kind"] == "fixed_profile"
            else "external_execution_unobserved"
        ),
    }


def _validate_contract(contract: object) -> tuple[bool, str | None]:
    if not isinstance(contract, dict):
        return False, "declared_action_contract_invalid"
    kind = contract.get("kind")
    try:
        if kind == "fixed_profile":
            expected = _fixed_contract(str(contract.get("profile") or ""))
        elif kind == "external_tool":
            expected = _external_contract(
                operation=str(contract.get("operation") or ""),
                tool_name=str(contract.get("tool_name") or ""),
                arguments_sha256=str(contract.get("arguments_sha256") or ""),
            )
        else:
            return False, "declared_action_kind_invalid"
    except ValueError as error:
        return False, str(error)
    if contract != expected:
        return False, "declared_action_contract_mismatch"
    return True, None


def validate_execution_plan(plan: dict[str, Any]) -> tuple[bool, str | None]:
    if plan.get("schema") != EXECUTION_PLAN_SCHEMA:
        return False, "execution_plan_schema_mismatch"
    payload = plan.get("payload")
    plan_id = plan.get("plan_id")
    if not isinstance(payload, dict) or not isinstance(plan_id, str):
        return False, "execution_plan_incomplete"
    if plan_id != _canonical_hash({"schema": EXECUTION_PLAN_SCHEMA, "payload": payload}):
        return False, "execution_plan_digest_mismatch"

    parent = payload.get("parent_receipt")
    repository = payload.get("repository")
    declaration = payload.get("declaration")
    if not isinstance(parent, dict) or not isinstance(repository, dict):
        return False, "execution_plan_parent_binding_incomplete"
    if (
        not _is_sha256(parent.get("receipt_id"))
        or not _is_sha256(parent.get("receipt_record_hash"))
        or not _is_sha256(parent.get("receipt_chain_hash"))
        or not isinstance(parent.get("receipt_sequence"), int)
        or isinstance(parent.get("receipt_sequence"), bool)
        or not _is_sha256(parent.get("task_contract_sha256"))
    ):
        return False, "execution_plan_parent_binding_incomplete"
    if (
        not _is_sha256(repository.get("identity_sha256"))
        or not _is_sha256(repository.get("worktree_sha256"))
        or not _is_sha256(repository.get("workspace_sha256"))
        or repository.get("identity_sha256") != parent.get("repository_identity_sha256")
    ):
        return False, "execution_plan_repository_binding_invalid"
    if not isinstance(declaration, dict):
        return False, "execution_plan_declaration_invalid"
    slots = declaration.get("slots")
    if (
        declaration.get("scope") != "declared_task_actions_only"
        or declaration.get("declaration_completeness_verified") is not False
        or declaration.get("global_codex_tool_coverage") is not None
        or declaration.get("global_coverage_reason") != GLOBAL_COVERAGE_REASON
        or not isinstance(slots, list)
        or not 1 <= len(slots) <= MAX_DECLARED_ACTIONS
        or declaration.get("slot_count") != len(slots)
    ):
        return False, "execution_plan_declaration_invalid"

    slot_ids: set[str] = set()
    for ordinal, slot in enumerate(slots, start=1):
        if not isinstance(slot, dict) or slot.get("ordinal") != ordinal:
            return False, "declared_action_order_invalid"
        slot_id = slot.get("slot_id")
        if not _valid_slot_id(slot_id) or slot_id in slot_ids:
            return False, "declared_action_slot_id_invalid"
        assert isinstance(slot_id, str)
        slot_ids.add(slot_id)
        contract = slot.get("contract")
        valid, reason = _validate_contract(contract)
        if not valid:
            return False, reason
        assert isinstance(contract, dict)
        if (
            slot.get("contract_sha256") != _canonical_hash(contract)
            or slot.get("risk") != _risk_for_contract(contract)
            or slot.get("expected_observation")
            != (
                "runtime_execution_verified"
                if contract["kind"] == "fixed_profile"
                else "external_execution_unobserved"
            )
        ):
            return False, "declared_action_binding_mismatch"
    return True, None


def _ratio(numerator: int, denominator: int) -> dict[str, int | float]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator,
    }


class GovernedExecutionCoverageLedger:
    """Bind an immutable declared-action denominator to later Action Manifests."""

    def __init__(self, path: str, *, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        self.repository = SQLiteRepository(path, read_only=read_only)
        self.routing = RoutingReceiptLedger(path, read_only=read_only)

    def register_plan(
        self,
        receipt_id: str,
        actions: list[dict[str, Any]],
        *,
        repository_root: str,
    ) -> dict[str, Any]:
        self._require_writable()
        root = Path(repository_root).expanduser().resolve()
        if not root.is_dir() or not (root / ".git").exists():
            raise ValueError("repository_root_invalid")
        parent = self.routing.verify(receipt_id, repository_root=root)
        if not parent.get("verified"):
            raise ValueError(f"parent_receipt_unverified:{parent.get('reason')}")
        parent_repository = parent.get("repository", {})
        if (
            parent_repository.get("bound") is not True
            or parent_repository.get("binding_verified") is not True
            or parent_repository.get("current_match") is not True
        ):
            raise ValueError("parent_repository_identity_mismatch")
        if (
            not 1 <= len(actions) <= MAX_DECLARED_ACTIONS
            or any(not isinstance(action, dict) for action in actions)
        ):
            raise ValueError("declared_action_count_invalid")
        if self._plans_for_receipt(receipt_id):
            raise ValueError("execution_plan_already_registered")
        if self._action_manifests_for_receipt(receipt_id):
            raise ValueError("execution_plan_registration_after_action")

        slots = [_normalize_slot(action, ordinal) for ordinal, action in enumerate(actions, 1)]
        slot_ids = [slot["slot_id"] for slot in slots]
        if len(set(slot_ids)) != len(slot_ids):
            raise ValueError("declared_action_slot_id_duplicate")
        captured = capture_repository_identity(root)
        if captured["identity_sha256"] != parent_repository.get("identity_sha256"):
            raise ValueError("parent_repository_identity_mismatch")

        binding = parent["binding"]
        payload = {
            "parent_receipt": {
                "receipt_id": receipt_id,
                "receipt_schema": parent["schema"],
                "receipt_sequence": binding["receipt_sequence"],
                "receipt_record_hash": binding["receipt_record_hash"],
                "receipt_chain_hash": binding["receipt_chain_hash"],
                "task_contract_sha256": parent["route"]["prompt_sha256"],
                "repository_identity_sha256": parent_repository["identity_sha256"],
            },
            "repository": {
                "head_revision": captured["head_revision"],
                "identity_sha256": captured["identity_sha256"],
                "worktree_sha256": captured["worktree_sha256"],
                "workspace_sha256": _workspace_digest(root),
            },
            "declaration": {
                "scope": "declared_task_actions_only",
                "declaration_completeness_verified": False,
                "global_codex_tool_coverage": None,
                "global_coverage_reason": GLOBAL_COVERAGE_REASON,
                "slot_count": len(slots),
                "slots": slots,
            },
        }
        signed = {"schema": EXECUTION_PLAN_SCHEMA, "payload": payload}
        plan = {**signed, "plan_id": _canonical_hash(signed)}
        valid, reason = validate_execution_plan(plan)
        if not valid:
            raise ValueError(f"execution_plan_invalid:{reason}")
        asyncio.run(self.repository.save(self._plan_key(str(plan["plan_id"])), plan))
        return plan

    def verify(self, plan_id: str) -> dict[str, Any]:
        if self.read_only and not Path(self.path).expanduser().is_file():
            return self._failure(plan_id, "execution_plan_missing")
        plan = self._load(self._plan_key(plan_id))
        if plan is None:
            return self._failure(plan_id, "execution_plan_missing")
        valid, reason = validate_execution_plan(plan)
        if not valid:
            return self._failure(plan_id, reason or "execution_plan_invalid")
        provenance = self.repository.verify_provenance()
        if provenance.get("valid") is not True:
            return self._failure(
                plan_id,
                str(provenance.get("reason") or "provenance_invalid"),
                provenance=provenance,
            )
        key = self._plan_key(plan_id)
        entries = [
            entry for entry in self.repository.provenance_entries() if entry["record_key"] == key
        ]
        if len(entries) != 1:
            return self._failure(
                plan_id,
                "execution_plan_rewritten" if entries else "execution_plan_provenance_missing",
                provenance=provenance,
            )
        entry = entries[0]
        if entry["record_hash"] != SQLiteRepository.record_hash(plan):
            return self._failure(
                plan_id,
                "execution_plan_record_hash_mismatch",
                provenance=provenance,
            )

        claim = plan["payload"]["parent_receipt"]
        parent = self.routing.verify(str(claim["receipt_id"]))
        if not parent.get("verified"):
            return self._failure(
                plan_id,
                f"parent_receipt_unverified:{parent.get('reason')}",
                provenance=provenance,
            )
        parent_binding = parent["binding"]
        if any(
            claim.get(claim_key) != parent_binding.get(binding_key)
            for claim_key, binding_key in (
                ("receipt_sequence", "receipt_sequence"),
                ("receipt_record_hash", "receipt_record_hash"),
                ("receipt_chain_hash", "receipt_chain_hash"),
            )
        ):
            return self._failure(plan_id, "parent_receipt_binding_mismatch", provenance=provenance)
        if entry["sequence"] <= parent_binding["receipt_sequence"]:
            return self._failure(plan_id, "execution_plan_precedes_parent", provenance=provenance)
        parent_repository = parent.get("repository", {})
        if (
            parent_repository.get("bound") is not True
            or parent_repository.get("binding_verified") is not True
            or parent_repository.get("identity_sha256")
            != claim.get("repository_identity_sha256")
            or claim.get("repository_identity_sha256")
            != plan["payload"]["repository"]["identity_sha256"]
            or claim.get("task_contract_sha256") != parent["route"]["prompt_sha256"]
        ):
            return self._failure(
                plan_id,
                "execution_plan_parent_binding_mismatch",
                provenance=provenance,
            )
        return {
            "verified": True,
            "plan_id": plan_id,
            "schema": EXECUTION_PLAN_SCHEMA,
            "parent_receipt_id": claim["receipt_id"],
            "declared_actions": plan["payload"]["declaration"]["slot_count"],
            "declaration": plan["payload"]["declaration"],
            "repository": plan["payload"]["repository"],
            "binding": {
                "plan_sequence": entry["sequence"],
                "plan_record_hash": entry["record_hash"],
                "plan_chain_hash": entry["chain_hash"],
                "parent_receipt": parent_binding,
            },
            "provenance": provenance,
        }

    def summarize(self, plan_id: str) -> dict[str, Any]:
        plan_verification = self.verify(plan_id)
        if not plan_verification.get("verified"):
            return {
                "schema": EXECUTION_COVERAGE_SCHEMA,
                "status": "invalid",
                "coverage_complete": False,
                "plan": plan_verification,
                "global_codex_tool_coverage": None,
                "global_coverage_reason": GLOBAL_COVERAGE_REASON,
            }

        plan = self._load(self._plan_key(plan_id))
        assert plan is not None
        receipt_id = str(plan_verification["parent_receipt_id"])
        plan_sequence = int(plan_verification["binding"]["plan_sequence"])
        raw_manifests = self._action_manifests_for_receipt(receipt_id)
        action_ledger = ActionManifestLedger(self.path, read_only=True)
        verifications = [
            action_ledger.verify(str(manifest.get("action_id", "")))
            for manifest in raw_manifests
        ]
        valid_actions = [item for item in verifications if item.get("manifest_verified")]
        valid_actions.sort(
            key=lambda item: int(item.get("binding", {}).get("manifest", {}).get("sequence", 0))
        )
        preplan_actions = [
            item
            for item in valid_actions
            if int(item["binding"]["manifest"]["sequence"]) <= plan_sequence
        ]
        eligible_actions = [item for item in valid_actions if item not in preplan_actions]

        plan_workspace_sha256 = plan["payload"]["repository"]["workspace_sha256"]
        queues: dict[str, list[dict[str, Any]]] = {}
        workspace_mismatched_action_ids: set[str] = set()
        for action in eligible_actions:
            action_contract = action["action"]
            action_workspace_sha256 = (
                action_contract.get("repository_sha256")
                if action_contract.get("kind") == "fixed_profile"
                else action_contract.get("workspace_sha256")
            )
            if action_workspace_sha256 != plan_workspace_sha256:
                workspace_mismatched_action_ids.add(str(action["action_id"]))
                continue
            contract_sha256 = _canonical_hash(_contract_from_action(action["action"]))
            queues.setdefault(contract_sha256, []).append(action)

        slots: list[dict[str, Any]] = []
        used_action_ids: set[str] = set()
        matched_sequences: list[int] = []
        for declaration in plan["payload"]["declaration"]["slots"]:
            candidates = queues.get(declaration["contract_sha256"], [])
            matched_action = candidates.pop(0) if candidates else None
            if matched_action is None:
                slots.append(
                    {
                        "ordinal": declaration["ordinal"],
                        "slot_id": declaration["slot_id"],
                        "contract_sha256": declaration["contract_sha256"],
                        "risk_level": declaration["risk"]["level"],
                        "expected_observation": declaration["expected_observation"],
                        "status": "missing_action_manifest",
                        "manifest_bound": False,
                        "action_authorized": False,
                        "execution_verified": False,
                        "successful": False,
                        "external_attestation_integrity_verified": False,
                        "repository_transition_verified": False,
                    }
                )
                continue
            action_id = str(matched_action["action_id"])
            used_action_ids.add(action_id)
            manifest_sequence = int(
                matched_action["binding"]["manifest"]["sequence"]
            )
            matched_sequences.append(manifest_sequence)
            approval_decision = matched_action.get("approval", {}).get("decision")
            action_authorized = (
                matched_action.get("manifest_status") == "authorized"
                or approval_decision == "approved"
            )
            external_attested = (
                matched_action.get("external_attestation", {}).get("integrity_verified")
                is True
            )
            execution_is_verified = matched_action.get("execution_verified") is True
            action_is_successful = matched_action.get("successful") is True
            external_status = matched_action.get("external_attestation", {}).get(
                "result_status"
            )
            if execution_is_verified:
                slot_status = (
                    "runtime_verified_success"
                    if action_is_successful
                    else "runtime_verified_failure"
                )
            elif external_attested:
                slot_status = (
                    "external_attested_completed_unverified"
                    if external_status == "completed"
                    else "external_attested_failed_unverified"
                )
            else:
                slot_status = str(
                    matched_action.get("reason") or "execution_evidence_missing"
                )
            slots.append(
                {
                    "ordinal": declaration["ordinal"],
                    "slot_id": declaration["slot_id"],
                    "contract_sha256": declaration["contract_sha256"],
                    "risk_level": declaration["risk"]["level"],
                    "expected_observation": declaration["expected_observation"],
                    "status": slot_status,
                    "action_id": action_id,
                    "manifest_sequence": manifest_sequence,
                    "manifest_bound": True,
                    "action_authorized": action_authorized,
                    "approval_status": matched_action.get("approval", {}).get("status"),
                    "execution_verified": execution_is_verified,
                    "successful": action_is_successful,
                    "external_attestation_integrity_verified": external_attested,
                    "external_result_status": external_status,
                    "repository_transition_verified": (
                        matched_action.get("repository_transition", {}).get(
                            "transition_verified"
                        )
                        is True
                    ),
                    "reason": matched_action.get("reason"),
                }
            )

        out_of_plan = [
            {
                "action_id": item["action_id"],
                "manifest_sequence": item["binding"]["manifest"]["sequence"],
                "contract_sha256": _canonical_hash(_contract_from_action(item["action"])),
                "reason": (
                    "action_precedes_execution_plan"
                    if item in preplan_actions
                    else "action_workspace_mismatch"
                    if item["action_id"] in workspace_mismatched_action_ids
                    else "action_not_declared_or_duplicate"
                ),
            }
            for item in valid_actions
            if item["action_id"] not in used_action_ids
        ]
        declared = len(slots)
        manifest_bound = sum(1 for slot in slots if slot["manifest_bound"])
        authorized = sum(1 for slot in slots if slot["action_authorized"])
        execution_verified_count = sum(
            1 for slot in slots if slot["execution_verified"]
        )
        successful_count = sum(1 for slot in slots if slot["successful"])
        external_attested_count = sum(
            1 for slot in slots if slot["external_attestation_integrity_verified"]
        )
        completed_result_evidence = sum(
            1
            for slot in slots
            if slot["successful"]
            or (
                slot["external_attestation_integrity_verified"]
                and slot.get("external_result_status") == "completed"
            )
        )
        transitions_verified = sum(
            1 for slot in slots if slot["repository_transition_verified"]
        )
        order_verified = matched_sequences == sorted(matched_sequences)
        no_out_of_plan = not out_of_plan and len(verifications) == len(valid_actions)
        governance_binding_complete = (
            manifest_bound == declared
            and authorized == declared
            and order_verified
            and no_out_of_plan
        )
        runtime_coverage_complete = (
            governance_binding_complete
            and execution_verified_count == declared
            and successful_count == declared
            and transitions_verified == declared
        )
        result_evidence_complete = (
            governance_binding_complete and completed_result_evidence == declared
        )
        if runtime_coverage_complete:
            status = "runtime_verified_complete"
        elif governance_binding_complete and result_evidence_complete:
            status = "declared_governance_complete_external_execution_unverified"
        else:
            status = "partial"
        return {
            "schema": EXECUTION_COVERAGE_SCHEMA,
            "status": status,
            "plan_id": plan_id,
            "parent_receipt_id": receipt_id,
            "claim_scope": "declared_task_actions_only",
            "declaration_completeness_verified": False,
            "declared_actions": declared,
            "manifest_bound_actions": manifest_bound,
            "authorized_actions": authorized,
            "runtime_execution_verified_actions": execution_verified_count,
            "successful_runtime_actions": successful_count,
            "external_attested_unverified_actions": external_attested_count,
            "completed_result_evidence_actions": completed_result_evidence,
            "repository_transition_verified_actions": transitions_verified,
            "failed_manifest_bindings": len(verifications) - len(valid_actions),
            "preplan_actions": len(preplan_actions),
            "out_of_plan_actions": len(out_of_plan),
            "declared_order_verified": order_verified,
            "governance_binding_complete": governance_binding_complete,
            "result_evidence_complete": result_evidence_complete,
            "coverage_complete": runtime_coverage_complete,
            "coverage": {
                "action_manifest": _ratio(manifest_bound, declared),
                "action_authorization": _ratio(authorized, declared),
                "runtime_verified_execution": _ratio(
                    execution_verified_count, declared
                ),
                "successful_runtime_execution": _ratio(successful_count, declared),
                "completed_result_evidence": _ratio(completed_result_evidence, declared),
                "repository_transition_verified": _ratio(transitions_verified, declared),
            },
            "slots": slots,
            "out_of_plan": out_of_plan,
            "global_codex_tool_coverage": None,
            "global_coverage_reason": GLOBAL_COVERAGE_REASON,
            "honesty": {
                "external_attestation_is_execution_verification": False,
                "declaration_is_complete_codex_tool_denominator": False,
                "coverage_proves_task_quality_uplift": False,
            },
            "plan": plan_verification,
        }

    def summarize_receipt(self, receipt_id: str) -> dict[str, Any]:
        if self.read_only and not Path(self.path).expanduser().is_file():
            return self._empty_receipt_summary(receipt_id)
        plans = self._plans_for_receipt(receipt_id)
        if not plans:
            return self._empty_receipt_summary(receipt_id)
        if len(plans) != 1:
            return {
                "schema": EXECUTION_COVERAGE_SCHEMA,
                "status": "invalid",
                "reason": "multiple_execution_plans_for_receipt",
                "parent_receipt_id": receipt_id,
                "coverage_complete": False,
                "global_codex_tool_coverage": None,
                "global_coverage_reason": GLOBAL_COVERAGE_REASON,
            }
        return self.summarize(str(plans[0]["plan_id"]))

    def _plans_for_receipt(self, receipt_id: str) -> list[dict[str, Any]]:
        records = asyncio.run(self.repository.load_prefix(EXECUTION_PLAN_PREFIX))
        return [
            record
            for record in records.values()
            if record.get("payload", {}).get("parent_receipt", {}).get("receipt_id")
            == receipt_id
        ]

    def _action_manifests_for_receipt(self, receipt_id: str) -> list[dict[str, Any]]:
        records = asyncio.run(self.repository.load_prefix(ACTION_MANIFEST_PREFIX))
        return [
            record
            for record in records.values()
            if record.get("payload", {}).get("parent_receipt", {}).get("receipt_id")
            == receipt_id
        ]

    def _load(self, key: str) -> dict[str, Any] | None:
        return asyncio.run(self.repository.load(key))

    @staticmethod
    def _plan_key(plan_id: str) -> str:
        return f"{EXECUTION_PLAN_PREFIX}{plan_id}"

    def _require_writable(self) -> None:
        if self.read_only:
            raise PermissionError("Cannot mutate execution coverage through read-only path.")

    @staticmethod
    def _failure(
        plan_id: str,
        reason: str,
        *,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "verified": False,
            "plan_id": plan_id,
            "reason": reason,
            **({"provenance": provenance} if provenance is not None else {}),
        }

    @staticmethod
    def _empty_receipt_summary(receipt_id: str) -> dict[str, Any]:
        return {
            "schema": EXECUTION_COVERAGE_SCHEMA,
            "status": "not_declared",
            "parent_receipt_id": receipt_id,
            "claim_scope": "declared_task_actions_only",
            "declaration_completeness_verified": False,
            "coverage_complete": False,
            "global_codex_tool_coverage": None,
            "global_coverage_reason": GLOBAL_COVERAGE_REASON,
        }
