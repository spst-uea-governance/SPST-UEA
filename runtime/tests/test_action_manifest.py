import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from spst_runtime.action_manifest import (
    ACTION_APPROVAL_PREFIX,
    ACTION_EVIDENCE_PREFIX,
    ACTION_MANIFEST_PREFIX,
    ActionManifestLedger,
)
from spst_runtime.chat_bridge import run_chat_turn, verify_chat_receipt


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _file_manifest(path: Path) -> dict[str, tuple[int, str]]:
    return {
        candidate.name: (
            candidate.stat().st_size,
            hashlib.sha256(candidate.read_bytes()).hexdigest(),
        )
        for candidate in path.parent.glob(f"{path.name}*")
        if candidate.is_file()
    }


def _routed_turn(tmp_path: Path) -> tuple[dict, Path]:
    session_path = tmp_path / "chat-state.db"
    result = run_chat_turn(
        "bind subsequent governed actions",
        steps=1,
        event="action_manifest_test",
        profile="standard",
        session_path=str(session_path),
        memory_path=str(tmp_path / "memory.db"),
    )
    return result, session_path


def _tamper_record(path: Path, key: str, mutate) -> None:
    connection = sqlite3.connect(path)
    try:
        serialized = connection.execute(
            "SELECT value FROM state_store WHERE key = ?", (key,)
        ).fetchone()[0]
        record = json.loads(serialized)
        mutate(record)
        connection.execute(
            "UPDATE state_store SET value = ? WHERE key = ?",
            (json.dumps(record, sort_keys=True), key),
        )
        connection.commit()
    finally:
        connection.close()


def test_fixed_profile_execution_is_bound_to_verified_parent_receipt(tmp_path: Path):
    turn, session_path = _routed_turn(tmp_path)
    receipt_id = turn["routing_receipt"]["receipt_id"]
    ledger = ActionManifestLedger(str(session_path))

    result = ledger.run_fixed_profile(
        receipt_id,
        "git_status",
        workspace_root=str(REPOSITORY_ROOT),
    )

    manifest = result["manifest"]
    verification = result["verification"]
    assert manifest["schema"] == "spst-action-manifest-v1"
    assert manifest["payload"]["parent_receipt"]["receipt_id"] == receipt_id
    assert manifest["payload"]["action"]["profile"] == "git_status"
    assert manifest["payload"]["risk"] == {
        "level": "R0",
        "reasons": ["fixed_read_only_profile"],
        "requires_human_approval": False,
    }
    assert manifest["payload"]["status"] == "authorized"
    assert verification["verified"] is True
    assert verification["manifest_verified"] is True
    assert verification["execution_verified"] is True
    assert verification["successful"] is True
    assert verification["execution"]["status"] == "completed"
    assert verification["provenance"]["valid"] is True
    assert "stdout" not in json.dumps(verification)
    assert "stderr" not in json.dumps(verification)

    parent = verify_chat_receipt(receipt_id, str(session_path))
    assert parent["verified"] is True
    assert parent["actions"]["bound_actions"] == 1
    assert parent["actions"]["execution_verified_actions"] == 1
    assert parent["actions"]["actions"][0]["action_id"] == manifest["action_id"]
    assert parent["actions"]["actions"][0]["profile"] == "git_status"
    assert isinstance(parent["actions"]["actions"][0]["manifest_sequence"], int)
    assert parent["actions"]["failed_actions"] == 0
    assert parent["actions"]["global_codex_tool_coverage"] is None
    assert (
        parent["actions"]["global_coverage_reason"]
        == "codex_tool_call_denominator_unavailable"
    )


def test_profile_risk_is_derived_instead_of_caller_labelled(tmp_path: Path):
    turn, session_path = _routed_turn(tmp_path)
    ledger = ActionManifestLedger(str(session_path))

    manifest = ledger.prepare_fixed_profile(
        turn["routing_receipt"]["receipt_id"],
        "pytest",
        workspace_root=str(REPOSITORY_ROOT),
    )

    assert manifest["payload"]["risk"]["level"] == "R1"
    assert manifest["payload"]["risk"]["reasons"] == ["fixed_local_quality_profile"]
    assert manifest["payload"]["governance"]["authorized"] is True
    pending_execution = ledger.verify(manifest["action_id"])
    assert pending_execution["manifest_verified"] is True
    assert pending_execution["execution_verified"] is False
    assert pending_execution["reason"] == "execution_evidence_missing"


def test_failed_fixed_profile_is_execution_verified_but_not_successful(tmp_path: Path):
    turn, session_path = _routed_turn(tmp_path)
    ledger = ActionManifestLedger(str(session_path))

    result = ledger.run_fixed_profile(
        turn["routing_receipt"]["receipt_id"],
        "git_status",
        workspace_root=str(tmp_path),
    )["verification"]

    assert result["verified"] is True
    assert result["execution_verified"] is True
    assert result["successful"] is False
    assert result["execution"]["status"] == "failed"


def test_missing_or_tampered_parent_receipt_cannot_bind_action(tmp_path: Path):
    turn, session_path = _routed_turn(tmp_path)
    ledger = ActionManifestLedger(str(session_path))

    with pytest.raises(ValueError, match="parent_receipt_unverified:receipt_missing"):
        ledger.prepare_fixed_profile(
            "0" * 64,
            "git_status",
            workspace_root=str(REPOSITORY_ROOT),
        )

    receipt_id = turn["routing_receipt"]["receipt_id"]
    _tamper_record(
        session_path,
        f"routing_receipt:v2:{receipt_id}",
        lambda record: record["payload"]["route"].__setitem__("event", "tampered"),
    )
    with pytest.raises(ValueError, match="parent_receipt_unverified"):
        ledger.prepare_fixed_profile(
            receipt_id,
            "git_status",
            workspace_root=str(REPOSITORY_ROOT),
        )


@pytest.mark.parametrize("record_type", ["manifest", "evidence"])
def test_tampered_action_records_are_never_verified(tmp_path: Path, record_type: str):
    turn, session_path = _routed_turn(tmp_path)
    ledger = ActionManifestLedger(str(session_path))
    result = ledger.run_fixed_profile(
        turn["routing_receipt"]["receipt_id"],
        "git_status",
        workspace_root=str(REPOSITORY_ROOT),
    )
    action_id = result["manifest"]["action_id"]
    prefix = ACTION_MANIFEST_PREFIX if record_type == "manifest" else ACTION_EVIDENCE_PREFIX
    key = f"{prefix}{action_id}"
    _tamper_record(
        session_path,
        key,
        lambda record: record["payload"].__setitem__("status", "forged"),
    )

    verification = ledger.verify(action_id)

    assert verification["verified"] is False
    assert verification["execution_verified"] is False
    assert verification["reason"] in {
        "state_hash_mismatch",
        "action_manifest_digest_mismatch",
        "action_evidence_digest_mismatch",
    }


def test_r2_external_action_is_held_for_hitl_and_never_claimed_as_executed(tmp_path: Path):
    turn, session_path = _routed_turn(tmp_path)
    ledger = ActionManifestLedger(str(session_path))
    arguments_digest = hashlib.sha256(b"deploy production").hexdigest()

    manifest = ledger.prepare_external_action(
        turn["routing_receipt"]["receipt_id"],
        operation="deploy",
        tool_name="codex.shell_command",
        arguments_sha256=arguments_digest,
        workspace_root=str(REPOSITORY_ROOT),
    )

    assert manifest["payload"]["risk"]["level"] == "R2"
    assert manifest["payload"]["status"] == "pending_human_approval"
    assert manifest["payload"]["governance"]["authorized"] is False
    assert manifest["payload"]["governance"]["requires_human_approval"] is True
    pending = ledger.verify(manifest["action_id"])
    assert pending["reason"] == "pending_human_approval"
    assert pending["execution_verified"] is False

    approval = ledger.record_approval(
        manifest["action_id"],
        approved=True,
        actor="test-human",
    )
    assert approval["payload"]["decision"] == "approved"
    approved = ledger.verify(manifest["action_id"])
    assert approved["approval"]["decision"] == "approved"
    assert approved["verified"] is False
    assert approved["execution_verified"] is False
    assert approved["reason"] == "external_execution_unobserved"

    execution = ledger.execute(manifest["action_id"], workspace_root=str(REPOSITORY_ROOT))
    assert execution["verified"] is False
    assert execution["reason"] == "external_execution_unobserved"
    assert not any(
        key.startswith(ACTION_EVIDENCE_PREFIX)
        for key in ledger.records().keys()
    )


def test_tampered_human_approval_cannot_authorize_external_action(tmp_path: Path):
    turn, session_path = _routed_turn(tmp_path)
    ledger = ActionManifestLedger(str(session_path))
    manifest = ledger.prepare_external_action(
        turn["routing_receipt"]["receipt_id"],
        operation="deploy",
        tool_name="codex.shell_command",
        arguments_sha256="b" * 64,
        workspace_root=str(REPOSITORY_ROOT),
    )
    action_id = manifest["action_id"]
    ledger.record_approval(action_id, approved=True, actor="test-human")
    _tamper_record(
        session_path,
        f"{ACTION_APPROVAL_PREFIX}{action_id}",
        lambda record: record["payload"].__setitem__("decision", "rejected"),
    )

    verification = ledger.verify(action_id)

    assert verification["verified"] is False
    assert verification["execution_verified"] is False
    assert verification["reason"] in {
        "state_hash_mismatch",
        "action_approval_digest_mismatch",
    }


def test_unknown_external_operation_fails_closed_as_unresolved_r2(tmp_path: Path):
    turn, session_path = _routed_turn(tmp_path)
    ledger = ActionManifestLedger(str(session_path))

    manifest = ledger.prepare_external_action(
        turn["routing_receipt"]["receipt_id"],
        operation="novel_unclassified_operation",
        tool_name="codex.unknown",
        arguments_sha256="a" * 64,
        workspace_root=str(REPOSITORY_ROOT),
    )

    assert manifest["payload"]["risk"]["level"] == "R2"
    assert manifest["payload"]["risk"]["reasons"] == ["risk_unresolved"]
    assert manifest["payload"]["status"] == "pending_human_approval"


def test_workspace_mismatch_and_duplicate_execution_are_rejected(tmp_path: Path):
    turn, session_path = _routed_turn(tmp_path)
    ledger = ActionManifestLedger(str(session_path))
    manifest = ledger.prepare_fixed_profile(
        turn["routing_receipt"]["receipt_id"],
        "git_status",
        workspace_root=str(REPOSITORY_ROOT),
    )

    mismatch = ledger.execute(manifest["action_id"], workspace_root=str(tmp_path))
    assert mismatch["verified"] is False
    assert mismatch["reason"] == "workspace_binding_mismatch"

    completed = ledger.execute(
        manifest["action_id"], workspace_root=str(REPOSITORY_ROOT)
    )
    assert completed["verified"] is True
    with pytest.raises(ValueError, match="action_already_executed"):
        ledger.execute(manifest["action_id"], workspace_root=str(REPOSITORY_ROOT))


def test_action_status_read_path_does_not_change_database(tmp_path: Path):
    turn, session_path = _routed_turn(tmp_path)
    receipt_id = turn["routing_receipt"]["receipt_id"]
    writer = ActionManifestLedger(str(session_path))
    writer.run_fixed_profile(
        receipt_id,
        "git_status",
        workspace_root=str(REPOSITORY_ROOT),
    )
    before = _file_manifest(session_path)

    summary = ActionManifestLedger(str(session_path), read_only=True).summarize_receipt(
        receipt_id
    )

    assert _file_manifest(session_path) == before
    assert summary["bound_actions"] == 1
    assert summary["execution_verified_actions"] == 1
    assert summary["global_codex_tool_coverage"] is None


def test_read_only_status_for_missing_database_creates_nothing(tmp_path: Path):
    session_path = tmp_path / "missing" / "chat-state.db"

    summary = ActionManifestLedger(
        str(session_path), read_only=True
    ).summarize_receipt("0" * 64)

    assert summary["bound_actions"] == 0
    assert summary["global_codex_tool_coverage"] is None
    assert list(tmp_path.rglob("*")) == []
