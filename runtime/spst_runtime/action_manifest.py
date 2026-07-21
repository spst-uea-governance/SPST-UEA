import asyncio
import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any

from spst_runtime.engines.governance_engine import GovernanceEngine
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.providers.tool_provider import ToolProvider
from spst_runtime.repository_identity import (
    RepositoryIdentityError,
    capture_repository_identity,
    validate_repository_identity,
)
from spst_runtime.routing_receipt import RoutingReceiptLedger
from spst_runtime.verification_profiles import (
    PROFILE_CONTRACT_VERSION,
    QUALITY_PROFILE_NAMES,
    SNAPSHOT_PROFILE_NAMES,
    SUPPORTED_PROFILE_CONTRACT_VERSIONS,
    command_for,
    profile_contract_for,
    resolve_execution_root,
)


ACTION_MANIFEST_SCHEMA = "spst-action-manifest-v1"
ACTION_EVIDENCE_SCHEMA = "spst-action-evidence-v1"
ACTION_APPROVAL_SCHEMA = "spst-action-approval-v1"
ACTION_EXTERNAL_ATTESTATION_SCHEMA = "spst-external-action-attestation-v1"
ACTION_MANIFEST_PREFIX = "action_manifest:v1:"
ACTION_EVIDENCE_PREFIX = "action_evidence:v1:"
ACTION_APPROVAL_PREFIX = "action_approval:v1:"
ACTION_EXTERNAL_ATTESTATION_PREFIX = "action_external_attestation:v1:"
FIXED_EXECUTOR = "spst-fixed-profile-v1"
EXTERNAL_EXECUTOR = "external-codex-tool-unobserved"
EXTERNAL_ATTESTATION_SOURCE = "caller-supplied-external-result"
REPOSITORY_TRANSITION_SCHEMA = "spst-action-repository-transition-v1"
REPOSITORY_TRANSITION_CONTRACT_VERSION = 1
FIXED_PROFILE_CONTRACT_FIELDS = (
    "profile_contract_version",
    "profile_contract_sha256",
    "repository_sha256",
    "execution_root",
    "execution_root_sha256",
)

R0_EXTERNAL_OPERATIONS = frozenset({"read", "inspect", "analyze", "status"})
R1_EXTERNAL_OPERATIONS = frozenset(
    {"workspace_edit", "local_test", "build", "format"}
)
R2_EXTERNAL_OPERATIONS = frozenset(
    {
        "commit",
        "push",
        "pull_request",
        "deploy",
        "publish",
        "delete",
        "data_migration",
        "billing",
        "credential",
        "architecture_change",
        "breaking_change",
        "external_network",
    }
)


def _canonical_hash(value: dict[str, Any]) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value.lower())


def _workspace_digest(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve())).replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _command_digest(profile: str, contract_version: int | None = None) -> str:
    command = command_for(profile, contract_version=contract_version)
    if command is None:
        raise ValueError("verification_profile_not_permitted")
    return _canonical_hash({"command": list(command)})


def _profile_contract(
    profile: str, contract_version: int | None = None
) -> dict[str, Any]:
    contract = profile_contract_for(profile, contract_version=contract_version)
    if contract is None:
        raise ValueError("verification_profile_not_permitted")
    return contract


def _profile_contract_digest(profile: str, contract_version: int | None = None) -> str:
    return _canonical_hash(_profile_contract(profile, contract_version))


def _profile_arguments_digest(profile: str, contract_version: int | None = None) -> str:
    return _canonical_hash(
        {
            "profile": profile,
            "profile_contract_sha256": _profile_contract_digest(
                profile, contract_version
            ),
        }
    )


def _repository_transition_digest(
    action_id: str, transition: dict[str, Any]
) -> str:
    return _canonical_hash({"action_id": action_id, "transition": transition})


def _validate_manifest_repository_transition(
    transition: object,
    *,
    action_kind: object,
) -> tuple[bool, str | None]:
    if not isinstance(transition, dict):
        return False, "repository_transition_contract_invalid"
    if (
        transition.get("schema") != REPOSITORY_TRANSITION_SCHEMA
        or transition.get("contract_version")
        != REPOSITORY_TRANSITION_CONTRACT_VERSION
    ):
        return False, "repository_transition_contract_invalid"
    before = transition.get("before_repository_identity")
    if not isinstance(before, dict):
        return False, "before_repository_identity_invalid"
    valid, reason = validate_repository_identity(before)
    if not valid:
        return False, reason or "before_repository_identity_invalid"
    if transition.get("parent_repository_identity_sha256") != before.get(
        "identity_sha256"
    ):
        return False, "parent_before_repository_identity_mismatch"
    if action_kind == "fixed_profile":
        if (
            transition.get("after_repository_identity_source") != "action_evidence"
            or transition.get("expected_effect") != "preserve"
        ):
            return False, "repository_transition_contract_invalid"
    elif action_kind == "external_tool":
        if (
            transition.get("after_repository_identity_source")
            != "external_execution_unobserved"
            or transition.get("expected_effect") != "unobserved"
        ):
            return False, "repository_transition_contract_invalid"
    else:
        return False, "action_kind_invalid"
    return True, None


def _validate_evidence_repository_transition(
    action_id: str,
    transition: object,
) -> tuple[bool, str | None]:
    if not isinstance(transition, dict):
        return False, "repository_transition_evidence_missing"
    if (
        transition.get("schema") != REPOSITORY_TRANSITION_SCHEMA
        or transition.get("contract_version")
        != REPOSITORY_TRANSITION_CONTRACT_VERSION
        or not _is_sha256(transition.get("parent_repository_identity_sha256"))
        or not _is_sha256(transition.get("before_repository_identity_sha256"))
        or transition.get("expected_effect") != "preserve"
    ):
        return False, "repository_transition_evidence_invalid"
    capture_status = transition.get("after_capture_status")
    after = transition.get("after_repository_identity")
    if capture_status == "captured":
        valid, reason = validate_repository_identity(after)
        if not valid:
            return False, reason or "after_repository_identity_invalid"
        if transition.get("after_capture_reason") is not None:
            return False, "repository_transition_evidence_invalid"
    elif capture_status == "failed":
        if after is not None or not str(transition.get("after_capture_reason") or ""):
            return False, "repository_transition_evidence_invalid"
    else:
        return False, "repository_transition_evidence_invalid"
    transition_sha256 = transition.get("transition_sha256")
    if not _is_sha256(transition_sha256):
        return False, "repository_transition_digest_invalid"
    content = {key: value for key, value in transition.items() if key != "transition_sha256"}
    if transition_sha256 != _repository_transition_digest(action_id, content):
        return False, "repository_transition_digest_mismatch"
    return True, None


def _validate_external_attestation_repository_transition(
    action_id: str,
    transition: object,
) -> tuple[bool, str | None]:
    if not isinstance(transition, dict):
        return False, "external_attestation_repository_state_missing"
    if (
        transition.get("schema") != REPOSITORY_TRANSITION_SCHEMA
        or transition.get("contract_version")
        != REPOSITORY_TRANSITION_CONTRACT_VERSION
        or not _is_sha256(transition.get("parent_repository_identity_sha256"))
        or not _is_sha256(transition.get("before_repository_identity_sha256"))
        or transition.get("after_capture_status") != "captured"
        or transition.get("after_capture_reason") is not None
        or transition.get("expected_effect") != "unobserved"
        or transition.get("observation_scope") != "attestation_time_only"
        or transition.get("causal_link_verified") is not False
        or transition.get("execution_window_verified") is not False
    ):
        return False, "external_attestation_repository_state_invalid"
    valid, reason = validate_repository_identity(
        transition.get("after_repository_identity")
    )
    if not valid:
        return False, reason or "after_repository_identity_invalid"
    transition_sha256 = transition.get("transition_sha256")
    if not _is_sha256(transition_sha256):
        return False, "external_attestation_transition_digest_invalid"
    content = {
        key: value for key, value in transition.items() if key != "transition_sha256"
    }
    if transition_sha256 != _repository_transition_digest(action_id, content):
        return False, "external_attestation_transition_digest_mismatch"
    return True, None


def _risk_for_profile(profile: str) -> dict[str, Any]:
    if profile in SNAPSHOT_PROFILE_NAMES or profile == "git_diff_check":
        return {
            "level": "R0",
            "reasons": ["fixed_read_only_profile"],
            "requires_human_approval": False,
        }
    if profile in QUALITY_PROFILE_NAMES:
        return {
            "level": "R1",
            "reasons": ["fixed_local_quality_profile"],
            "requires_human_approval": False,
        }
    raise ValueError("verification_profile_not_permitted")


def _risk_for_external(operation: str) -> dict[str, Any]:
    if operation in R0_EXTERNAL_OPERATIONS:
        return {
            "level": "R0",
            "reasons": ["external_read_only_attestation"],
            "requires_human_approval": False,
        }
    if operation in R1_EXTERNAL_OPERATIONS:
        return {
            "level": "R1",
            "reasons": ["external_local_reversible_attestation"],
            "requires_human_approval": False,
        }
    if operation in R2_EXTERNAL_OPERATIONS:
        return {
            "level": "R2",
            "reasons": [f"high_impact_operation:{operation}"],
            "requires_human_approval": True,
        }
    return {
        "level": "R2",
        "reasons": ["risk_unresolved"],
        "requires_human_approval": True,
    }


def _manifest_key(action_id: str) -> str:
    return f"{ACTION_MANIFEST_PREFIX}{action_id}"


def _evidence_key(action_id: str) -> str:
    return f"{ACTION_EVIDENCE_PREFIX}{action_id}"


def _approval_key(action_id: str) -> str:
    return f"{ACTION_APPROVAL_PREFIX}{action_id}"


def _external_attestation_key(action_id: str) -> str:
    return f"{ACTION_EXTERNAL_ATTESTATION_PREFIX}{action_id}"


def validate_action_manifest(manifest: dict[str, Any]) -> tuple[bool, str | None]:
    if manifest.get("schema") != ACTION_MANIFEST_SCHEMA:
        return False, "action_manifest_schema_mismatch"
    payload = manifest.get("payload")
    action_id = manifest.get("action_id")
    if not isinstance(payload, dict) or not isinstance(action_id, str):
        return False, "action_manifest_incomplete"
    if action_id != _canonical_hash({"schema": ACTION_MANIFEST_SCHEMA, "payload": payload}):
        return False, "action_manifest_digest_mismatch"

    nonce = payload.get("nonce")
    parent = payload.get("parent_receipt", {})
    action = payload.get("action", {})
    risk = payload.get("risk", {})
    governance = payload.get("governance", {})
    status = payload.get("status")
    if not isinstance(nonce, str) or len(nonce) != 32:
        return False, "action_nonce_invalid"
    if (
        not _is_sha256(parent.get("receipt_id"))
        or not _is_sha256(parent.get("receipt_record_hash"))
        or not _is_sha256(parent.get("receipt_chain_hash"))
        or not isinstance(parent.get("receipt_sequence"), int)
    ):
        return False, "parent_receipt_binding_incomplete"
    if not _is_sha256(action.get("arguments_sha256")) or not _is_sha256(
        action.get("workspace_sha256")
    ):
        return False, "action_digest_binding_incomplete"

    kind = action.get("kind")
    if kind == "fixed_profile":
        profile = str(action.get("profile") or "")
        contract_version = action.get("profile_contract_version")
        if (
            contract_version is not None
            and contract_version not in SUPPORTED_PROFILE_CONTRACT_VERSIONS
        ):
            return False, "fixed_profile_contract_mismatch"
        try:
            expected_risk = _risk_for_profile(profile)
            expected_command = _command_digest(profile, contract_version)
            expected_contract = _profile_contract(profile, contract_version)
            expected_contract_digest = _profile_contract_digest(
                profile, contract_version
            )
        except ValueError:
            return False, "verification_profile_not_permitted"
        if (
            action.get("executor") != FIXED_EXECUTOR
            or action.get("tool_name") != "verify.local"
            or action.get("command_sha256") != expected_command
            or risk != expected_risk
        ):
            return False, "fixed_profile_binding_mismatch"
        contract_fields_present = any(
            key in action
            for key in FIXED_PROFILE_CONTRACT_FIELDS
            if key != "profile_contract_version"
        )
        if contract_version is None and contract_fields_present:
            return False, "fixed_profile_contract_mismatch"
        if contract_version is not None and (
            action.get("profile_contract_sha256") != expected_contract_digest
            or action.get("arguments_sha256")
            != _profile_arguments_digest(profile, contract_version)
            or action.get("execution_root")
            != expected_contract["execution_root"]
            or not _is_sha256(action.get("repository_sha256"))
            or not _is_sha256(action.get("execution_root_sha256"))
            or action.get("execution_root_sha256")
            != action.get("workspace_sha256")
        ):
            return False, "fixed_profile_contract_mismatch"
    elif kind == "external_tool":
        operation = str(action.get("operation") or "")
        if (
            action.get("executor") != EXTERNAL_EXECUTOR
            or not str(action.get("tool_name") or "").strip()
            or risk != _risk_for_external(operation)
        ):
            return False, "external_action_binding_mismatch"
    else:
        return False, "action_kind_invalid"

    transition = payload.get("repository_transition")
    if transition is not None:
        transition_valid, transition_reason = _validate_manifest_repository_transition(
            transition,
            action_kind=kind,
        )
        if not transition_valid:
            return False, transition_reason

    if risk.get("level") not in {"R0", "R1", "R2"}:
        return False, "action_risk_invalid"
    authorized = governance.get("authorized") is True
    requires_approval = governance.get("requires_human_approval") is True
    if status == "authorized" and (not authorized or requires_approval):
        return False, "action_governance_status_mismatch"
    if status == "pending_human_approval" and (authorized or not requires_approval):
        return False, "action_governance_status_mismatch"
    if status not in {"authorized", "pending_human_approval", "denied"}:
        return False, "action_status_invalid"
    if risk.get("level") == "R2" and status == "authorized":
        return False, "r2_action_cannot_be_pre_authorized"
    return True, None


def _validate_approval(approval: dict[str, Any]) -> tuple[bool, str | None]:
    if approval.get("schema") != ACTION_APPROVAL_SCHEMA:
        return False, "action_approval_schema_mismatch"
    payload = approval.get("payload")
    approval_record_id = approval.get("approval_record_id")
    if not isinstance(payload, dict) or not isinstance(approval_record_id, str):
        return False, "action_approval_incomplete"
    expected = _canonical_hash({"schema": ACTION_APPROVAL_SCHEMA, "payload": payload})
    if approval_record_id != expected:
        return False, "action_approval_digest_mismatch"
    if payload.get("decision") not in {"approved", "rejected"}:
        return False, "action_approval_decision_invalid"
    if not _is_sha256(payload.get("manifest_record_hash")) or not _is_sha256(
        payload.get("actor_sha256")
    ):
        return False, "action_approval_binding_incomplete"
    return True, None


def _validate_evidence(evidence: dict[str, Any]) -> tuple[bool, str | None]:
    if evidence.get("schema") != ACTION_EVIDENCE_SCHEMA:
        return False, "action_evidence_schema_mismatch"
    payload = evidence.get("payload")
    evidence_id = evidence.get("evidence_id")
    if not isinstance(payload, dict) or not isinstance(evidence_id, str):
        return False, "action_evidence_incomplete"
    expected = _canonical_hash({"schema": ACTION_EVIDENCE_SCHEMA, "payload": payload})
    if evidence_id != expected:
        return False, "action_evidence_digest_mismatch"
    if payload.get("executor") != FIXED_EXECUTOR or payload.get("execution_verified") is not True:
        return False, "action_evidence_executor_unverified"
    if payload.get("status") not in {
        "completed",
        "failed",
        "timed_out",
        "unavailable",
        "denied",
    }:
        return False, "action_evidence_status_invalid"
    if (
        not _is_sha256(payload.get("manifest_record_hash"))
        or not _is_sha256(payload.get("output_digest"))
        or not _is_sha256(payload.get("command_sha256"))
        or not _is_sha256(payload.get("workspace_sha256"))
    ):
        return False, "action_evidence_binding_incomplete"
    contract_version = payload.get("profile_contract_version")
    contract_fields_present = any(
        key in payload
        for key in FIXED_PROFILE_CONTRACT_FIELDS
        if key != "profile_contract_version"
    )
    if contract_version is None and contract_fields_present:
        return False, "action_evidence_profile_contract_incomplete"
    if contract_version is not None and (
        contract_version not in SUPPORTED_PROFILE_CONTRACT_VERSIONS
        or not _is_sha256(payload.get("profile_contract_sha256"))
        or not _is_sha256(payload.get("repository_sha256"))
        or not _is_sha256(payload.get("execution_root_sha256"))
        or payload.get("execution_root_sha256")
        != payload.get("workspace_sha256")
        or not isinstance(payload.get("execution_root"), str)
    ):
        return False, "action_evidence_profile_contract_incomplete"
    transition = payload.get("repository_transition")
    if transition is not None:
        transition_valid, transition_reason = _validate_evidence_repository_transition(
            str(payload.get("action_id") or ""),
            transition,
        )
        if not transition_valid:
            return False, transition_reason
    return True, None


def _validate_external_attestation(
    attestation: dict[str, Any],
) -> tuple[bool, str | None]:
    if attestation.get("schema") != ACTION_EXTERNAL_ATTESTATION_SCHEMA:
        return False, "external_attestation_schema_mismatch"
    payload = attestation.get("payload")
    attestation_id = attestation.get("attestation_id")
    if not isinstance(payload, dict) or not isinstance(attestation_id, str):
        return False, "external_attestation_incomplete"
    expected = _canonical_hash(
        {"schema": ACTION_EXTERNAL_ATTESTATION_SCHEMA, "payload": payload}
    )
    if attestation_id != expected:
        return False, "external_attestation_digest_mismatch"
    if (
        payload.get("executor") != EXTERNAL_EXECUTOR
        or payload.get("attestation_source") != EXTERNAL_ATTESTATION_SOURCE
        or payload.get("source_authenticated") is not False
        or payload.get("execution_verified") is not False
        or payload.get("result_status") not in {"completed", "failed"}
        or not isinstance(payload.get("returncode"), int)
        or isinstance(payload.get("returncode"), bool)
    ):
        return False, "external_attestation_claim_invalid"
    if not _is_sha256(payload.get("action_id")) or not _is_sha256(
        payload.get("parent_receipt_id")
    ):
        return False, "external_attestation_binding_incomplete"
    if (
        payload["result_status"] == "completed" and payload["returncode"] != 0
    ) or (
        payload["result_status"] == "failed" and payload["returncode"] == 0
    ):
        return False, "external_attestation_result_inconsistent"
    if any(
        not _is_sha256(payload.get(key))
        for key in (
            "manifest_record_hash",
            "manifest_chain_hash",
            "result_evidence_sha256",
            "arguments_sha256",
            "workspace_sha256",
        )
    ) or not isinstance(payload.get("manifest_sequence"), int):
        return False, "external_attestation_binding_incomplete"
    approval_values = (
        payload.get("approval_record_id"),
        payload.get("approval_record_hash"),
        payload.get("approval_chain_hash"),
        payload.get("approval_sequence"),
    )
    approval_present = tuple(value is not None for value in approval_values)
    if any(approval_present) and not all(approval_present):
        return False, "external_attestation_approval_binding_incomplete"
    if all(approval_present) and (
        not all(_is_sha256(value) for value in approval_values[:3])
        or not isinstance(approval_values[3], int)
    ):
        return False, "external_attestation_approval_binding_incomplete"
    transition_valid, transition_reason = (
        _validate_external_attestation_repository_transition(
            str(payload.get("action_id") or ""),
            payload.get("repository_transition"),
        )
    )
    if not transition_valid:
        return False, transition_reason
    return True, None


class ActionManifestLedger:
    """Bind governed local actions and compact evidence to a verified route."""

    def __init__(
        self,
        path: str,
        *,
        read_only: bool = False,
        governance_engine: GovernanceEngine | None = None,
    ):
        self.path = path
        self.read_only = read_only
        self.repository = SQLiteRepository(path, read_only=read_only)
        self.routing = RoutingReceiptLedger(path, read_only=read_only)
        self.governance_engine = governance_engine or GovernanceEngine()

    def prepare_fixed_profile(
        self,
        receipt_id: str,
        profile: str,
        *,
        repository_root: str,
    ) -> dict[str, Any]:
        repository, execution_root = resolve_execution_root(
            profile,
            repository_root,
            contract_version=PROFILE_CONTRACT_VERSION,
        )
        before_repository_identity = capture_repository_identity(repository)
        contract = _profile_contract(profile, PROFILE_CONTRACT_VERSION)
        contract_digest = _profile_contract_digest(profile, PROFILE_CONTRACT_VERSION)
        risk = _risk_for_profile(profile)
        action = {
            "kind": "fixed_profile",
            "operation": "inspect" if risk["level"] == "R0" else "local_test",
            "tool_name": "verify.local",
            "profile": profile,
            "executor": FIXED_EXECUTOR,
            "profile_contract_version": PROFILE_CONTRACT_VERSION,
            "profile_contract_sha256": contract_digest,
            "arguments_sha256": _profile_arguments_digest(
                profile, PROFILE_CONTRACT_VERSION
            ),
            "command_sha256": _command_digest(profile, PROFILE_CONTRACT_VERSION),
            "repository_sha256": _workspace_digest(repository),
            "execution_root": contract["execution_root"],
            "execution_root_sha256": _workspace_digest(execution_root),
            "workspace_sha256": _workspace_digest(execution_root),
        }
        return self._prepare(
            receipt_id,
            action,
            risk,
            before_repository_identity=before_repository_identity,
            after_repository_identity_source="action_evidence",
            expected_effect="preserve",
        )

    def prepare_external_action(
        self,
        receipt_id: str,
        *,
        operation: str,
        tool_name: str,
        arguments_sha256: str,
        workspace_root: str,
    ) -> dict[str, Any]:
        workspace = self._workspace(workspace_root)
        before_repository_identity = capture_repository_identity(workspace)
        normalized_operation = operation.strip().lower()
        normalized_tool = tool_name.strip()
        if not normalized_tool:
            raise ValueError("external_tool_name_required")
        if not _is_sha256(arguments_sha256):
            raise ValueError("external_arguments_digest_invalid")
        risk = _risk_for_external(normalized_operation)
        action = {
            "kind": "external_tool",
            "operation": normalized_operation,
            "tool_name": normalized_tool,
            "profile": None,
            "executor": EXTERNAL_EXECUTOR,
            "arguments_sha256": arguments_sha256.lower(),
            "command_sha256": None,
            "workspace_sha256": _workspace_digest(workspace),
        }
        return self._prepare(
            receipt_id,
            action,
            risk,
            before_repository_identity=before_repository_identity,
            after_repository_identity_source="external_execution_unobserved",
            expected_effect="unobserved",
        )

    def run_fixed_profile(
        self,
        receipt_id: str,
        profile: str,
        *,
        repository_root: str,
    ) -> dict[str, Any]:
        manifest = self.prepare_fixed_profile(
            receipt_id,
            profile,
            repository_root=repository_root,
        )
        verification = self.execute(
            manifest["action_id"], repository_root=repository_root
        )
        return {"manifest": manifest, "verification": verification}

    def execute(
        self,
        action_id: str,
        *,
        repository_root: str | None = None,
    ) -> dict[str, Any]:
        self._require_writable()
        current = self.verify(action_id)
        if current.get("execution_verified"):
            raise ValueError("action_already_executed")
        if not current.get("manifest_verified"):
            return current
        if current.get("reason") in {
            "pending_human_approval",
            "rejected_by_human",
            "governance_denied",
        }:
            return current

        manifest = self._load(_manifest_key(action_id))
        if manifest is None:
            return self._failure(action_id, "action_manifest_missing")
        action = manifest["payload"]["action"]
        if action["kind"] != "fixed_profile":
            return {**current, "reason": "external_execution_unobserved"}

        if repository_root is None:
            return {**current, "reason": "repository_root_required"}
        contract_version = action.get("profile_contract_version")
        try:
            repository, execution_root = resolve_execution_root(
                str(action["profile"]),
                repository_root,
                contract_version=contract_version,
            )
        except ValueError as error:
            return {**current, "reason": str(error)}

        execution_root_digest = _workspace_digest(execution_root)
        if action.get("profile_contract_version") is None:
            if execution_root_digest != action["workspace_sha256"]:
                return {**current, "reason": "legacy_execution_root_mismatch"}
        elif _workspace_digest(repository) != action.get("repository_sha256"):
            return {**current, "reason": "repository_binding_mismatch"}
        elif (
            execution_root_digest != action.get("execution_root_sha256")
            or execution_root_digest != action.get("workspace_sha256")
        ):
            return {**current, "reason": "execution_root_binding_mismatch"}
        manifest_transition = manifest["payload"].get("repository_transition")
        if manifest_transition is not None:
            try:
                current_before = capture_repository_identity(repository)
            except RepositoryIdentityError as error:
                return {
                    **current,
                    "reason": f"before_repository_identity_capture_failed:{error}",
                }
            expected_before = manifest_transition["before_repository_identity"]
            if (
                current_before.get("identity_sha256")
                != expected_before.get("identity_sha256")
            ):
                return {**current, "reason": "before_repository_identity_mismatch"}
        if self._load(_evidence_key(action_id)) is not None:
            raise ValueError("action_already_executed")

        raw = ToolProvider(
            workspace_root=str(execution_root)
        ).execute_verification_profile(
            str(action["profile"]),
            governance_authorized=True,
            profile_contract_version=contract_version,
        )
        output_digest = raw.get("output_digest")
        if not _is_sha256(output_digest):
            output_digest = hashlib.sha256(b"").hexdigest()
        transition_evidence: dict[str, Any] | None = None
        if manifest_transition is not None:
            after_repository_identity: dict[str, Any] | None
            after_capture_status = "captured"
            after_capture_reason: str | None = None
            try:
                after_repository_identity = capture_repository_identity(repository)
            except RepositoryIdentityError as error:
                after_repository_identity = None
                after_capture_status = "failed"
                after_capture_reason = str(error)
            transition_content: dict[str, Any] = {
                "schema": REPOSITORY_TRANSITION_SCHEMA,
                "contract_version": REPOSITORY_TRANSITION_CONTRACT_VERSION,
                "parent_repository_identity_sha256": manifest_transition[
                    "parent_repository_identity_sha256"
                ],
                "before_repository_identity_sha256": manifest_transition[
                    "before_repository_identity"
                ]["identity_sha256"],
                "after_repository_identity": after_repository_identity,
                "after_capture_status": after_capture_status,
                "after_capture_reason": after_capture_reason,
                "expected_effect": manifest_transition["expected_effect"],
            }
            transition_evidence = {
                **transition_content,
                "transition_sha256": _repository_transition_digest(
                    action_id,
                    transition_content,
                ),
            }
        manifest_binding = self._record_binding(_manifest_key(action_id), manifest)
        payload = {
            "action_id": action_id,
            "parent_receipt_id": manifest["payload"]["parent_receipt"]["receipt_id"],
            "manifest_record_hash": manifest_binding["record_hash"],
            "manifest_sequence": manifest_binding["sequence"],
            "manifest_chain_hash": manifest_binding["chain_hash"],
            "executor": FIXED_EXECUTOR,
            "profile": action["profile"],
            "command_sha256": action["command_sha256"],
            "workspace_sha256": action["workspace_sha256"],
            "status": raw.get("status", "unavailable"),
            "returncode": raw.get("returncode"),
            "duration_ms": raw.get("duration_ms"),
            "output_digest": output_digest,
            "execution_verified": True,
        }
        if transition_evidence is not None:
            payload["repository_transition"] = transition_evidence
        for key in FIXED_PROFILE_CONTRACT_FIELDS:
            if key in action:
                payload[key] = action[key]
        signed: dict[str, Any] = {"schema": ACTION_EVIDENCE_SCHEMA, "payload": payload}
        evidence_id = _canonical_hash(signed)
        evidence: dict[str, Any] = {**signed, "evidence_id": evidence_id}
        asyncio.run(self.repository.save(_evidence_key(action_id), evidence))
        return self.verify(action_id)

    def record_approval(
        self,
        action_id: str,
        *,
        approved: bool,
        actor: str,
    ) -> dict[str, Any]:
        self._require_writable()
        current = self.verify(action_id)
        if not current.get("manifest_verified"):
            raise ValueError(f"action_manifest_unverified:{current.get('reason')}")
        if current.get("reason") != "pending_human_approval":
            raise ValueError("action_not_pending_human_approval")
        if self._load(_approval_key(action_id)) is not None:
            raise ValueError("action_approval_already_recorded")
        if not actor.strip():
            raise ValueError("human_actor_required")

        manifest = self._load(_manifest_key(action_id))
        if manifest is None:
            raise ValueError("action_manifest_missing")
        binding = self._record_binding(_manifest_key(action_id), manifest)
        payload = {
            "action_id": action_id,
            "parent_receipt_id": manifest["payload"]["parent_receipt"]["receipt_id"],
            "governance_approval_id": manifest["payload"]["governance"]["approval_id"],
            "manifest_record_hash": binding["record_hash"],
            "manifest_sequence": binding["sequence"],
            "decision": "approved" if approved else "rejected",
            "actor_sha256": hashlib.sha256(actor.strip().encode("utf-8")).hexdigest(),
            "actor_identity_verified": False,
        }
        signed: dict[str, Any] = {"schema": ACTION_APPROVAL_SCHEMA, "payload": payload}
        approval_record_id = _canonical_hash(signed)
        approval: dict[str, Any] = {
            **signed,
            "approval_record_id": approval_record_id,
        }
        asyncio.run(self.repository.save(_approval_key(action_id), approval))
        return approval

    def record_external_attestation(
        self,
        action_id: str,
        *,
        result_status: str,
        returncode: int,
        result_evidence_sha256: str,
        workspace_root: str,
    ) -> dict[str, Any]:
        """Record tamper-evident post-hoc evidence without verifying execution."""

        self._require_writable()
        if result_status not in {"completed", "failed"}:
            raise ValueError("external_attestation_status_invalid")
        if not isinstance(returncode, int) or isinstance(returncode, bool):
            raise ValueError("external_attestation_returncode_invalid")
        if (result_status == "completed" and returncode != 0) or (
            result_status == "failed" and returncode == 0
        ):
            raise ValueError("external_attestation_result_inconsistent")
        if not _is_sha256(result_evidence_sha256):
            raise ValueError("external_attestation_result_digest_invalid")

        current = self.verify(action_id)
        if not current.get("manifest_verified"):
            raise ValueError(f"action_manifest_unverified:{current.get('reason')}")
        manifest = self._load(_manifest_key(action_id))
        if manifest is None:
            raise ValueError("action_manifest_missing")
        action = manifest["payload"]["action"]
        if action.get("kind") != "external_tool":
            raise ValueError("external_attestation_requires_external_action")
        if current.get("reason") == "pending_human_approval":
            raise ValueError("external_attestation_requires_approval")
        if current.get("reason") == "rejected_by_human":
            raise ValueError("external_attestation_rejected_by_human")
        if current.get("reason") == "governance_denied":
            raise ValueError("external_attestation_governance_denied")
        if manifest["payload"]["risk"].get("requires_human_approval") is True and (
            current.get("approval", {}).get("decision") != "approved"
        ):
            raise ValueError("external_attestation_requires_approval")
        key = _external_attestation_key(action_id)
        if self._load(key) is not None:
            raise ValueError("external_attestation_already_recorded")

        workspace = self._workspace(workspace_root)
        if _workspace_digest(workspace) != action.get("workspace_sha256"):
            raise ValueError("external_attestation_workspace_mismatch")
        try:
            after_repository_identity = capture_repository_identity(workspace)
        except RepositoryIdentityError as error:
            raise ValueError(
                f"external_attestation_after_identity_capture_failed:{error}"
            ) from error

        manifest_transition = manifest["payload"].get("repository_transition")
        if not isinstance(manifest_transition, dict):
            raise ValueError("external_attestation_repository_transition_unbound")
        manifest_binding = self._record_binding(_manifest_key(action_id), manifest)
        approval_binding = current.get("approval", {}).get("binding")
        approval = self._load(_approval_key(action_id))
        transition_content: dict[str, Any] = {
            "schema": REPOSITORY_TRANSITION_SCHEMA,
            "contract_version": REPOSITORY_TRANSITION_CONTRACT_VERSION,
            "parent_repository_identity_sha256": manifest_transition[
                "parent_repository_identity_sha256"
            ],
            "before_repository_identity_sha256": manifest_transition[
                "before_repository_identity"
            ]["identity_sha256"],
            "after_repository_identity": after_repository_identity,
            "after_capture_status": "captured",
            "after_capture_reason": None,
            "expected_effect": "unobserved",
            "observation_scope": "attestation_time_only",
            "causal_link_verified": False,
            "execution_window_verified": False,
        }
        transition = {
            **transition_content,
            "transition_sha256": _repository_transition_digest(
                action_id,
                transition_content,
            ),
        }
        payload = {
            "action_id": action_id,
            "parent_receipt_id": manifest["payload"]["parent_receipt"]["receipt_id"],
            "manifest_record_hash": manifest_binding["record_hash"],
            "manifest_sequence": manifest_binding["sequence"],
            "manifest_chain_hash": manifest_binding["chain_hash"],
            "approval_record_hash": (
                approval_binding.get("record_hash")
                if isinstance(approval_binding, dict)
                else None
            ),
            "approval_record_id": (
                approval.get("approval_record_id")
                if isinstance(approval, dict)
                else None
            ),
            "approval_chain_hash": (
                approval_binding.get("chain_hash")
                if isinstance(approval_binding, dict)
                else None
            ),
            "approval_sequence": (
                approval_binding.get("sequence")
                if isinstance(approval_binding, dict)
                else None
            ),
            "executor": EXTERNAL_EXECUTOR,
            "attestation_source": EXTERNAL_ATTESTATION_SOURCE,
            "source_authenticated": False,
            "execution_verified": False,
            "result_status": result_status,
            "returncode": returncode,
            "result_evidence_sha256": result_evidence_sha256.lower(),
            "arguments_sha256": action["arguments_sha256"],
            "workspace_sha256": action["workspace_sha256"],
            "repository_transition": transition,
        }
        signed: dict[str, Any] = {
            "schema": ACTION_EXTERNAL_ATTESTATION_SCHEMA,
            "payload": payload,
        }
        attestation_id = _canonical_hash(signed)
        attestation: dict[str, Any] = {
            **signed,
            "attestation_id": attestation_id,
        }
        asyncio.run(self.repository.save(key, attestation))
        return self.verify(action_id)

    def verify(self, action_id: str) -> dict[str, Any]:
        if self.read_only and not Path(self.path).expanduser().is_file():
            return self._failure(action_id, "action_manifest_missing")
        manifest = self._load(_manifest_key(action_id))
        if manifest is None:
            return self._failure(action_id, "action_manifest_missing")
        valid, reason = validate_action_manifest(manifest)
        if not valid:
            return self._failure(action_id, reason or "action_manifest_invalid")

        provenance = self.repository.verify_provenance()
        if not provenance.get("valid"):
            return self._failure(
                action_id,
                str(provenance.get("reason") or "provenance_invalid"),
                provenance=provenance,
            )
        try:
            manifest_binding = self._record_binding(_manifest_key(action_id), manifest)
        except ValueError as error:
            return self._failure(action_id, str(error), provenance=provenance)

        parent_claim = manifest["payload"]["parent_receipt"]
        parent = self.routing.verify(str(parent_claim["receipt_id"]))
        if not parent.get("verified"):
            return self._failure(
                action_id,
                f"parent_receipt_unverified:{parent.get('reason')}",
                provenance=provenance,
            )
        parent_binding = parent.get("binding", {})
        if any(
            parent_claim.get(key) != parent_binding.get(binding_key)
            for key, binding_key in (
                ("receipt_sequence", "receipt_sequence"),
                ("receipt_chain_hash", "receipt_chain_hash"),
                ("receipt_record_hash", "receipt_record_hash"),
            )
        ):
            return self._failure(
                action_id,
                "parent_receipt_binding_mismatch",
                provenance=provenance,
            )
        if manifest_binding["sequence"] <= int(parent_binding["receipt_sequence"]):
            return self._failure(
                action_id,
                "action_precedes_parent_receipt",
                provenance=provenance,
            )

        payload = manifest["payload"]
        manifest_transition = payload.get("repository_transition")
        if manifest_transition is None:
            repository_transition: dict[str, Any] = {
                "contract_version": None,
                "status": "legacy_unbound",
                "transition_verified": False,
                "before_repository_identity": None,
                "after_repository_identity": None,
                "reason": "repository_transition_unbound",
            }
        else:
            parent_repository = parent.get("repository", {})
            if (
                parent_repository.get("bound") is not True
                or parent_repository.get("binding_verified") is not True
                or parent_repository.get("identity_sha256")
                != manifest_transition.get("parent_repository_identity_sha256")
            ):
                return self._failure(
                    action_id,
                    "parent_repository_identity_binding_mismatch",
                    provenance=provenance,
                )
            repository_transition = {
                "contract_version": manifest_transition["contract_version"],
                "status": (
                    "external_execution_unobserved"
                    if payload["action"]["kind"] == "external_tool"
                    else "awaiting_execution_evidence"
                ),
                "transition_verified": False,
                "parent_repository_identity_sha256": manifest_transition[
                    "parent_repository_identity_sha256"
                ],
                "before_repository_identity": manifest_transition[
                    "before_repository_identity"
                ],
                "after_repository_identity": None,
                "expected_effect": manifest_transition["expected_effect"],
                "reason": (
                    "external_execution_unobserved"
                    if payload["action"]["kind"] == "external_tool"
                    else "execution_evidence_missing"
                ),
            }
        result: dict[str, Any] = {
            "verified": False,
            "manifest_verified": True,
            "execution_verified": False,
            "successful": False,
            "action_id": action_id,
            "parent_receipt_id": parent_claim["receipt_id"],
            "action": payload["action"],
            "risk": payload["risk"],
            "manifest_status": payload["status"],
            "governance": payload["governance"],
            "repository_transition": repository_transition,
            "approval": {"status": "not_required"},
            "provenance": provenance,
            "binding": {"manifest": manifest_binding, "parent_receipt": parent_binding},
        }

        approval = self._load(_approval_key(action_id))
        if approval is not None:
            approval_valid, approval_reason = _validate_approval(approval)
            if not approval_valid:
                return {**result, "reason": approval_reason}
            try:
                approval_binding = self._record_binding(_approval_key(action_id), approval)
            except ValueError as error:
                return {**result, "reason": str(error)}
            approval_payload = approval["payload"]
            if (
                approval_payload.get("action_id") != action_id
                or approval_payload.get("parent_receipt_id") != parent_claim["receipt_id"]
                or approval_payload.get("manifest_record_hash") != manifest_binding["record_hash"]
                or int(approval_payload.get("manifest_sequence", -1))
                != manifest_binding["sequence"]
                or approval_binding["sequence"] <= manifest_binding["sequence"]
            ):
                return {**result, "reason": "action_approval_binding_mismatch"}
            result["approval"] = {
                "status": approval_payload["decision"],
                "decision": approval_payload["decision"],
                "actor_sha256": approval_payload["actor_sha256"],
                "actor_identity_verified": False,
                "binding": approval_binding,
            }

        if payload["status"] == "denied":
            return {**result, "reason": "governance_denied"}
        if payload["status"] == "pending_human_approval":
            decision = result["approval"].get("decision")
            if decision is None:
                result["approval"] = {"status": "pending"}
                return {**result, "reason": "pending_human_approval"}
            if decision == "rejected":
                return {**result, "reason": "rejected_by_human"}

        if payload["action"]["kind"] == "external_tool":
            attestation = self._load(_external_attestation_key(action_id))
            if attestation is None:
                return {**result, "reason": "external_execution_unobserved"}
            if not isinstance(manifest_transition, dict):
                return {
                    **result,
                    "reason": "external_attestation_repository_transition_unbound",
                }
            attestation_valid, attestation_reason = _validate_external_attestation(
                attestation
            )
            if not attestation_valid:
                return {**result, "reason": attestation_reason}
            try:
                attestation_binding = self._record_binding(
                    _external_attestation_key(action_id),
                    attestation,
                )
            except ValueError as error:
                return {**result, "reason": str(error)}
            attestation_payload = attestation["payload"]
            action = payload["action"]
            transition = attestation_payload["repository_transition"]
            approval_binding = result.get("approval", {}).get("binding")
            approval_record_hash = (
                approval_binding.get("record_hash")
                if isinstance(approval_binding, dict)
                else None
            )
            approval_record_id = (
                approval.get("approval_record_id")
                if isinstance(approval, dict)
                else None
            )
            approval_chain_hash = (
                approval_binding.get("chain_hash")
                if isinstance(approval_binding, dict)
                else None
            )
            approval_sequence = (
                approval_binding.get("sequence")
                if isinstance(approval_binding, dict)
                else None
            )
            if (
                attestation_payload.get("action_id") != action_id
                or attestation_payload.get("parent_receipt_id")
                != parent_claim["receipt_id"]
                or attestation_payload.get("manifest_record_hash")
                != manifest_binding["record_hash"]
                or attestation_payload.get("manifest_sequence")
                != manifest_binding["sequence"]
                or attestation_payload.get("manifest_chain_hash")
                != manifest_binding["chain_hash"]
                or attestation_payload.get("approval_record_hash")
                != approval_record_hash
                or attestation_payload.get("approval_record_id")
                != approval_record_id
                or attestation_payload.get("approval_chain_hash")
                != approval_chain_hash
                or attestation_payload.get("approval_sequence") != approval_sequence
                or attestation_payload.get("arguments_sha256")
                != action.get("arguments_sha256")
                or attestation_payload.get("workspace_sha256")
                != action.get("workspace_sha256")
                or transition.get("parent_repository_identity_sha256")
                != manifest_transition.get("parent_repository_identity_sha256")
                or transition.get("before_repository_identity_sha256")
                != manifest_transition.get("before_repository_identity", {}).get(
                    "identity_sha256"
                )
                or attestation_binding["sequence"] <= manifest_binding["sequence"]
                or (
                    approval_sequence is not None
                    and attestation_binding["sequence"] <= approval_sequence
                )
            ):
                return {**result, "reason": "external_attestation_binding_mismatch"}
            after = transition["after_repository_identity"]
            before_identity_sha256 = manifest_transition[
                "before_repository_identity"
            ]["identity_sha256"]
            state_changed = after["identity_sha256"] != before_identity_sha256
            transition_status = (
                "external_after_state_attested_changed"
                if state_changed
                else "external_after_state_attested_preserved"
            )
            reason = "external_execution_attested_unverified"
            return {
                **result,
                "reason": reason,
                "external_attestation": {
                    "attestation_id": attestation["attestation_id"],
                    "integrity_verified": True,
                    "source_authenticated": False,
                    "execution_verified": False,
                    "result_status": attestation_payload["result_status"],
                    "returncode": attestation_payload["returncode"],
                    "result_evidence_sha256": attestation_payload[
                        "result_evidence_sha256"
                    ],
                    "executor": attestation_payload["executor"],
                    "binding": attestation_binding,
                },
                "repository_transition": {
                    **repository_transition,
                    "status": transition_status,
                    "transition_verified": False,
                    "attestation_integrity_verified": True,
                    "after_repository_identity": after,
                    "after_capture_status": transition["after_capture_status"],
                    "after_capture_reason": transition["after_capture_reason"],
                    "transition_sha256": transition["transition_sha256"],
                    "state_changed": state_changed,
                    "expected_effect_met": None,
                    "observation_scope": transition["observation_scope"],
                    "causal_link_verified": False,
                    "execution_window_verified": False,
                    "reason": reason,
                },
            }

        evidence = self._load(_evidence_key(action_id))
        if evidence is None:
            return {**result, "reason": "execution_evidence_missing"}
        evidence_valid, evidence_reason = _validate_evidence(evidence)
        if not evidence_valid:
            return {**result, "reason": evidence_reason}
        try:
            evidence_binding = self._record_binding(_evidence_key(action_id), evidence)
        except ValueError as error:
            return {**result, "reason": str(error)}
        evidence_payload = evidence["payload"]
        action = payload["action"]
        evidence_transition = evidence_payload.get("repository_transition")
        if (
            evidence_payload.get("action_id") != action_id
            or evidence_payload.get("parent_receipt_id") != parent_claim["receipt_id"]
            or evidence_payload.get("manifest_record_hash") != manifest_binding["record_hash"]
            or int(evidence_payload.get("manifest_sequence", -1))
            != manifest_binding["sequence"]
            or evidence_payload.get("profile") != action.get("profile")
            or evidence_payload.get("command_sha256") != action.get("command_sha256")
            or evidence_payload.get("workspace_sha256") != action.get("workspace_sha256")
            or any(
                key in action and evidence_payload.get(key) != action.get(key)
                for key in FIXED_PROFILE_CONTRACT_FIELDS
            )
            or (
                manifest_transition is None
                and evidence_transition is not None
            )
            or (
                manifest_transition is not None
                and (
                    not isinstance(evidence_transition, dict)
                    or evidence_transition.get("parent_repository_identity_sha256")
                    != manifest_transition.get("parent_repository_identity_sha256")
                    or evidence_transition.get("before_repository_identity_sha256")
                    != manifest_transition.get("before_repository_identity", {}).get(
                        "identity_sha256"
                    )
                    or evidence_transition.get("expected_effect")
                    != manifest_transition.get("expected_effect")
                )
            )
            or evidence_binding["sequence"] <= manifest_binding["sequence"]
        ):
            return {**result, "reason": "action_evidence_binding_mismatch"}

        execution = {
            "evidence_id": evidence["evidence_id"],
            "status": evidence_payload["status"],
            "returncode": evidence_payload["returncode"],
            "duration_ms": evidence_payload["duration_ms"],
            "output_digest": evidence_payload["output_digest"],
            "executor": evidence_payload["executor"],
            "binding": evidence_binding,
        }
        if action.get("profile_contract_version") is not None:
            execution["profile_contract"] = {
                key: action[key]
                for key in FIXED_PROFILE_CONTRACT_FIELDS
            }
        transition_reason: str | None = None
        transition_expected_effect_met = True
        if manifest_transition is not None and isinstance(evidence_transition, dict):
            after = evidence_transition.get("after_repository_identity")
            after_captured = evidence_transition.get("after_capture_status") == "captured"
            before_identity_sha256 = manifest_transition[
                "before_repository_identity"
            ]["identity_sha256"]
            after_identity_sha256 = (
                after.get("identity_sha256") if isinstance(after, dict) else None
            )
            state_changed = (
                after_captured and after_identity_sha256 != before_identity_sha256
            )
            if not after_captured:
                transition_status = "after_capture_failed"
                transition_reason = "after_repository_identity_unresolved"
                transition_expected_effect_met = False
            elif state_changed:
                transition_status = "unexpected_change"
                transition_reason = "unexpected_repository_mutation"
                transition_expected_effect_met = False
            else:
                transition_status = "preserved"
            repository_transition = {
                **repository_transition,
                "status": transition_status,
                "transition_verified": after_captured,
                "after_repository_identity": after,
                "after_capture_status": evidence_transition["after_capture_status"],
                "after_capture_reason": evidence_transition["after_capture_reason"],
                "transition_sha256": evidence_transition["transition_sha256"],
                "state_changed": state_changed if after_captured else None,
                "expected_effect_met": transition_expected_effect_met,
                "reason": transition_reason,
            }
        return {
            **result,
            "verified": True,
            "execution_verified": True,
            "successful": (
                evidence_payload["status"] == "completed"
                and transition_expected_effect_met
            ),
            "reason": transition_reason,
            "execution": execution,
            "repository_transition": repository_transition,
        }

    def summarize_receipt(self, receipt_id: str) -> dict[str, Any]:
        if self.read_only and not Path(self.path).expanduser().is_file():
            return self._empty_summary(receipt_id)
        manifests = asyncio.run(self.repository.load_prefix(ACTION_MANIFEST_PREFIX))
        matching = [
            record
            for record in manifests.values()
            if record.get("payload", {}).get("parent_receipt", {}).get("receipt_id")
            == receipt_id
        ]
        verifications = [self.verify(str(record.get("action_id", ""))) for record in matching]
        valid = [item for item in verifications if item.get("manifest_verified")]
        valid.sort(
            key=lambda item: int(
                item.get("binding", {}).get("manifest", {}).get("sequence", 0)
            )
        )
        actions = [
            {
                "action_id": item.get("action_id"),
                "manifest_sequence": item.get("binding", {})
                .get("manifest", {})
                .get("sequence"),
                "operation": item.get("action", {}).get("operation"),
                "tool_name": item.get("action", {}).get("tool_name"),
                "profile": item.get("action", {}).get("profile"),
                "profile_contract_version": item.get("action", {}).get(
                    "profile_contract_version"
                ),
                "execution_root": item.get("action", {}).get("execution_root"),
                "risk_level": item.get("risk", {}).get("level"),
                "manifest_status": item.get("manifest_status"),
                "approval_status": item.get("approval", {}).get("status"),
                "execution_status": item.get("execution", {}).get("status"),
                "execution_verified": bool(item.get("execution_verified")),
                "successful": bool(item.get("successful")),
                "external_attestation_integrity_verified": bool(
                    item.get("external_attestation", {}).get("integrity_verified")
                ),
                "external_attestation_status": item.get(
                    "external_attestation", {}
                ).get("result_status"),
                "repository_transition_status": item.get(
                    "repository_transition", {}
                ).get("status"),
                "repository_transition_verified": bool(
                    item.get("repository_transition", {}).get("transition_verified")
                ),
                "before_repository_identity_sha256": (
                    item.get("repository_transition", {}).get(
                        "before_repository_identity"
                    )
                    or {}
                ).get("identity_sha256"),
                "after_repository_identity_sha256": (
                    item.get("repository_transition", {}).get(
                        "after_repository_identity"
                    )
                    or {}
                ).get("identity_sha256"),
                "reason": item.get("reason"),
            }
            for item in valid
        ]
        return {
            "receipt_id": receipt_id,
            "stored_manifests": len(matching),
            "bound_actions": len(valid),
            "failed_manifest_bindings": len(matching) - len(valid),
            "execution_verified_actions": sum(
                1 for item in valid if item.get("execution_verified")
            ),
            "successful_actions": sum(1 for item in valid if item.get("successful")),
            "failed_actions": sum(
                1
                for item in valid
                if item.get("execution_verified") and not item.get("successful")
            ),
            "pending_human_approval": sum(
                1 for item in valid if item.get("reason") == "pending_human_approval"
            ),
            "external_unobserved_actions": sum(
                1 for item in valid if item.get("reason") == "external_execution_unobserved"
            ),
            "external_attested_actions": sum(
                1
                for item in valid
                if item.get("external_attestation", {}).get("integrity_verified")
                is True
            ),
            "repository_transition_bound_actions": sum(
                1
                for item in valid
                if item.get("repository_transition", {}).get("contract_version")
                == REPOSITORY_TRANSITION_CONTRACT_VERSION
            ),
            "repository_transition_verified_actions": sum(
                1
                for item in valid
                if item.get("repository_transition", {}).get("transition_verified")
                is True
            ),
            "repository_transition_preserved_actions": sum(
                1
                for item in valid
                if item.get("repository_transition", {}).get("status") == "preserved"
            ),
            "repository_transition_changed_actions": sum(
                1
                for item in valid
                if item.get("repository_transition", {}).get("status")
                == "unexpected_change"
            ),
            "repository_transition_attested_actions": sum(
                1
                for item in valid
                if item.get("repository_transition", {}).get(
                    "attestation_integrity_verified"
                )
                is True
            ),
            "repository_transition_unresolved_actions": sum(
                1
                for item in valid
                if item.get("repository_transition", {}).get("status")
                in {
                    "legacy_unbound",
                    "awaiting_execution_evidence",
                    "after_capture_failed",
                    "external_execution_unobserved",
                    "external_after_state_attested_changed",
                    "external_after_state_attested_preserved",
                }
            ),
            "actions": actions,
            "global_codex_tool_coverage": None,
            "global_coverage_reason": "codex_tool_call_denominator_unavailable",
        }

    def records(self) -> dict[str, dict[str, Any]]:
        if self.read_only and not Path(self.path).expanduser().is_file():
            return {}
        return asyncio.run(self.repository.load_prefix("action_"))

    def _prepare(
        self,
        receipt_id: str,
        action: dict[str, Any],
        risk: dict[str, Any],
        *,
        before_repository_identity: dict[str, Any],
        after_repository_identity_source: str,
        expected_effect: str,
    ) -> dict[str, Any]:
        self._require_writable()
        parent = self.routing.verify(receipt_id)
        if not parent.get("verified"):
            raise ValueError(f"parent_receipt_unverified:{parent.get('reason')}")
        valid, reason = validate_repository_identity(before_repository_identity)
        if not valid:
            raise ValueError(f"before_repository_identity_invalid:{reason}")
        parent_repository = parent.get("repository", {})
        if (
            parent_repository.get("bound") is not True
            or parent_repository.get("binding_verified") is not True
        ):
            raise ValueError("parent_repository_identity_unbound")
        parent_identity_sha256 = parent_repository.get("identity_sha256")
        if parent_identity_sha256 != before_repository_identity["identity_sha256"]:
            raise ValueError("parent_repository_identity_mismatch")
        nonce = secrets.token_hex(16)
        governance_payload = self._governance_payload(action, risk, nonce)
        decision = self.governance_engine.decide(
            {
                "type": "bound_tool_action",
                "event_type": "action_manifest_prepare",
                "source": "action_manifest_ledger",
                "provenance_scope": "sqlite",
                "provenance_verified": True,
                "change_risk": {
                    "requires_human_approval": risk["requires_human_approval"]
                },
                "payload": governance_payload,
                "security_assessment": {"blocked": False, "findings": []},
            }
        )
        status = (
            "authorized"
            if decision.get("authorized")
            else "pending_human_approval"
            if decision.get("requires_human_approval")
            else "denied"
        )
        binding = parent["binding"]
        payload = {
            "nonce": nonce,
            "parent_receipt": {
                "receipt_id": receipt_id,
                "receipt_schema": parent["schema"],
                "receipt_sequence": binding["receipt_sequence"],
                "receipt_record_hash": binding["receipt_record_hash"],
                "receipt_chain_hash": binding["receipt_chain_hash"],
            },
            "action": action,
            "repository_transition": {
                "schema": REPOSITORY_TRANSITION_SCHEMA,
                "contract_version": REPOSITORY_TRANSITION_CONTRACT_VERSION,
                "parent_repository_identity_sha256": parent_identity_sha256,
                "before_repository_identity": before_repository_identity,
                "after_repository_identity_source": after_repository_identity_source,
                "expected_effect": expected_effect,
            },
            "risk": risk,
            "governance": {
                "authorized": bool(decision.get("authorized")),
                "trust_level": decision.get("trust_level"),
                "requires_human_approval": bool(
                    decision.get("requires_human_approval")
                ),
                "approval_id": decision.get("approval_id"),
                "reasons": decision.get("reasons", []),
                "policy_version": decision.get("covenant_policy", {}).get(
                    "policy_version"
                ),
            },
            "status": status,
        }
        signed: dict[str, Any] = {"schema": ACTION_MANIFEST_SCHEMA, "payload": payload}
        action_id = _canonical_hash(signed)
        manifest: dict[str, Any] = {**signed, "action_id": action_id}
        valid, reason = validate_action_manifest(manifest)
        if not valid:
            raise ValueError(f"action_manifest_invalid:{reason}")
        key = _manifest_key(manifest["action_id"])
        if self._load(key) is not None:
            raise ValueError("action_manifest_already_exists")
        asyncio.run(self.repository.save(key, manifest))
        return manifest

    def _governance_payload(
        self,
        action: dict[str, Any],
        risk: dict[str, Any],
        nonce: str,
    ) -> dict[str, Any]:
        operation = str(action.get("operation") or "")
        payload: dict[str, Any] = {
            "manifest_schema": ACTION_MANIFEST_SCHEMA,
            "action_nonce": nonce,
            "parent_receipt_verified": True,
            "risk_level": risk["level"],
            "read_only": risk["level"] == "R0",
            "analysis_only": risk["level"] == "R0",
            "local_only": True,
            "reversible": risk["level"] in {"R0", "R1"},
            "human_approved": False,
        }
        flag_by_operation = {
            "architecture_change": "architecture_change",
            "breaking_change": "breaking_change",
            "delete": "delete_data",
            "deploy": "external_transaction",
            "publish": "external_transaction",
            "push": "external_transaction",
            "billing": "external_transaction",
            "external_network": "external_network",
        }
        flag = flag_by_operation.get(operation)
        if flag:
            payload[flag] = True
        return payload

    def _record_binding(self, key: str, record: dict[str, Any]) -> dict[str, Any]:
        entries = [
            entry
            for entry in self.repository.provenance_entries()
            if entry["record_key"] == key
        ]
        if len(entries) != 1:
            raise ValueError(
                "action_record_rewritten" if entries else "action_provenance_missing"
            )
        entry = entries[0]
        if entry["record_hash"] != SQLiteRepository.record_hash(record):
            raise ValueError("action_record_hash_mismatch")
        return {
            "sequence": entry["sequence"],
            "record_hash": entry["record_hash"],
            "chain_hash": entry["chain_hash"],
        }

    def _load(self, key: str) -> dict[str, Any] | None:
        return asyncio.run(self.repository.load(key))

    def _workspace(self, workspace_root: str) -> Path:
        path = Path(workspace_root).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("workspace_root_invalid")
        return path

    def _require_writable(self) -> None:
        if self.read_only:
            raise PermissionError("Cannot mutate ActionManifestLedger through read-only path.")

    def _failure(
        self,
        action_id: str,
        reason: str,
        *,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "verified": False,
            "manifest_verified": False,
            "execution_verified": False,
            "successful": False,
            "action_id": action_id,
            "reason": reason,
            **({"provenance": provenance} if provenance is not None else {}),
        }

    def _empty_summary(self, receipt_id: str) -> dict[str, Any]:
        return {
            "receipt_id": receipt_id,
            "stored_manifests": 0,
            "bound_actions": 0,
            "failed_manifest_bindings": 0,
            "execution_verified_actions": 0,
            "successful_actions": 0,
            "failed_actions": 0,
            "pending_human_approval": 0,
            "external_unobserved_actions": 0,
            "external_attested_actions": 0,
            "repository_transition_bound_actions": 0,
            "repository_transition_verified_actions": 0,
            "repository_transition_preserved_actions": 0,
            "repository_transition_changed_actions": 0,
            "repository_transition_attested_actions": 0,
            "repository_transition_unresolved_actions": 0,
            "actions": [],
            "global_codex_tool_coverage": None,
            "global_coverage_reason": "codex_tool_call_denominator_unavailable",
        }
