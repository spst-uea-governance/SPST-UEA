import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from spst_runtime.action_manifest import (
    ACTION_APPROVAL_PREFIX,
    ACTION_EVIDENCE_PREFIX,
    ACTION_EXTERNAL_ATTESTATION_PREFIX,
    ACTION_MANIFEST_PREFIX,
    ActionManifestLedger,
    validate_action_manifest,
)
from spst_runtime.action_bridge import main as action_bridge_main
from spst_runtime.chat_bridge import run_chat_turn, verify_chat_receipt
from spst_runtime.repository_identity import (
    RepositoryIdentityError,
    capture_repository_identity,
)
from spst_runtime.verification_profiles import profile_contract_for


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


def _routed_turn(
    tmp_path: Path,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> tuple[dict, Path]:
    session_path = tmp_path / "chat-state.db"
    result = run_chat_turn(
        "bind subsequent governed actions",
        steps=1,
        event="action_manifest_test",
        profile="standard",
        session_path=str(session_path),
        memory_path=str(tmp_path / "memory.db"),
        repository_root=str(repository_root),
    )
    return result, session_path


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    (repository / "runtime").mkdir(parents=True)
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "SPST Test")
    _git(repository, "config", "user.email", "spst@example.invalid")
    _git(repository, "config", "core.autocrlf", "false")
    _git(repository, "config", "core.filemode", "false")
    (repository / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n*.db\n",
        encoding="utf-8",
    )
    (repository / "tracked.txt").write_text("before\n", encoding="utf-8")
    (repository / "runtime" / "marker.txt").write_text("runtime\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "initial")
    return repository


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


def _reidentify_manifest(manifest: dict) -> dict:
    signed = {"schema": manifest["schema"], "payload": manifest["payload"]}
    serialized = json.dumps(
        signed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    manifest["action_id"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return manifest


@pytest.mark.parametrize(
    ("profile", "execution_root"),
    [
        ("git_head", "."),
        ("git_status", "."),
        ("git_diff_check", "."),
        ("pytest", "runtime"),
        ("ruff", "runtime"),
        ("mypy", "runtime"),
    ],
)
def test_every_fixed_profile_owns_an_execution_root(
    profile: str, execution_root: str
):
    contract = profile_contract_for(profile)

    assert contract is not None
    assert contract["version"] == 2
    assert contract["execution_root"] == execution_root


def test_fixed_profile_execution_is_bound_to_verified_parent_receipt(tmp_path: Path):
    turn, session_path = _routed_turn(tmp_path)
    receipt_id = turn["routing_receipt"]["receipt_id"]
    ledger = ActionManifestLedger(str(session_path))

    result = ledger.run_fixed_profile(
        receipt_id,
        "git_status",
        repository_root=str(REPOSITORY_ROOT),
    )

    manifest = result["manifest"]
    verification = result["verification"]
    assert manifest["schema"] == "spst-action-manifest-v1"
    assert manifest["payload"]["parent_receipt"]["receipt_id"] == receipt_id
    assert manifest["payload"]["action"]["profile"] == "git_status"
    assert manifest["payload"]["action"]["profile_contract_version"] == 2
    assert manifest["payload"]["action"]["execution_root"] == "."
    assert manifest["payload"]["action"]["repository_sha256"]
    assert manifest["payload"]["action"]["execution_root_sha256"]
    transition = manifest["payload"]["repository_transition"]
    assert transition["contract_version"] == 1
    assert transition["parent_repository_identity_sha256"] == turn[
        "routing_receipt"
    ]["payload"]["repository"]["identity_sha256"]
    assert transition["before_repository_identity"] == turn["routing_receipt"][
        "payload"
    ]["repository"]
    assert transition["after_repository_identity_source"] == "action_evidence"
    assert transition["expected_effect"] == "preserve"
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
    assert verification["repository_transition"]["status"] == "preserved"
    assert verification["repository_transition"]["transition_verified"] is True
    assert verification["repository_transition"]["state_changed"] is False
    assert verification["repository_transition"]["expected_effect_met"] is True
    assert verification["repository_transition"]["before_repository_identity"] == (
        verification["repository_transition"]["after_repository_identity"]
    )
    assert verification["provenance"]["valid"] is True
    assert "stdout" not in json.dumps(verification)
    assert "stderr" not in json.dumps(verification)

    parent = verify_chat_receipt(receipt_id, str(session_path))
    assert parent["verified"] is True
    assert parent["actions"]["bound_actions"] == 1
    assert parent["actions"]["execution_verified_actions"] == 1
    assert parent["actions"]["actions"][0]["action_id"] == manifest["action_id"]
    assert parent["actions"]["actions"][0]["profile"] == "git_status"
    assert parent["actions"]["actions"][0]["execution_root"] == "."
    assert isinstance(parent["actions"]["actions"][0]["manifest_sequence"], int)
    assert parent["actions"]["failed_actions"] == 0
    assert parent["actions"]["repository_transition_bound_actions"] == 1
    assert parent["actions"]["repository_transition_verified_actions"] == 1
    assert parent["actions"]["repository_transition_preserved_actions"] == 1
    assert parent["actions"]["repository_transition_changed_actions"] == 0
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
        repository_root=str(REPOSITORY_ROOT),
    )

    assert manifest["payload"]["risk"]["level"] == "R1"
    assert manifest["payload"]["risk"]["reasons"] == ["fixed_local_quality_profile"]
    assert manifest["payload"]["action"]["execution_root"] == "runtime"
    assert manifest["payload"]["governance"]["authorized"] is True
    pending_execution = ledger.verify(manifest["action_id"])
    assert pending_execution["manifest_verified"] is True
    assert pending_execution["execution_verified"] is False
    assert pending_execution["reason"] == "execution_evidence_missing"


def test_profile_definition_controls_execution_root(monkeypatch, tmp_path: Path):
    turn, session_path = _routed_turn(tmp_path)
    ledger = ActionManifestLedger(str(session_path))
    captured: dict[str, object] = {}

    def fake_profile(self, profile: str, *, governance_authorized: bool) -> dict:
        captured["cwd"] = self._workspace_root
        captured["profile"] = profile
        assert governance_authorized is True
        return {
            "profile": profile,
            "status": "completed",
            "returncode": 0,
            "duration_ms": 1,
            "output_digest": "a" * 64,
        }

    monkeypatch.setattr(
        "spst_runtime.action_manifest.ToolProvider.execute_verification_profile",
        fake_profile,
    )

    result = ledger.run_fixed_profile(
        turn["routing_receipt"]["receipt_id"],
        "pytest",
        repository_root=str(REPOSITORY_ROOT),
    )

    assert result["verification"]["successful"] is True
    assert captured == {
        "cwd": (REPOSITORY_ROOT / "runtime").resolve(),
        "profile": "pytest",
    }
    assert result["manifest"]["payload"]["action"]["execution_root"] == "runtime"


def test_caller_cannot_substitute_runtime_directory_for_repository_root(
    tmp_path: Path,
):
    turn, session_path = _routed_turn(tmp_path)
    ledger = ActionManifestLedger(str(session_path))

    with pytest.raises(ValueError, match="repository_root_invalid"):
        ledger.prepare_fixed_profile(
            turn["routing_receipt"]["receipt_id"],
            "pytest",
            repository_root=str(REPOSITORY_ROOT / "runtime"),
        )

    assert not any(
        key.startswith(ACTION_MANIFEST_PREFIX) for key in ledger.records()
    )


def test_action_bridge_returns_structured_rejection_for_wrong_repository_root(
    capsys, tmp_path: Path
):
    exit_code = action_bridge_main(
        [
            "--session-db",
            str(tmp_path / "unused.db"),
            "execute-profile",
            "--receipt-id",
            "0" * 64,
            "--profile",
            "pytest",
            "--repository-root",
            str(REPOSITORY_ROOT / "runtime"),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert output == {
        "schema": "spst-action-bridge-error-v1",
        "status": "rejected",
        "reason": "repository_root_invalid",
    }
    assert not (tmp_path / "unused.db").exists()


def test_profile_root_contract_rejects_reidentified_surface_tampering(
    tmp_path: Path,
):
    turn, session_path = _routed_turn(tmp_path)
    ledger = ActionManifestLedger(str(session_path))
    manifest = ledger.prepare_fixed_profile(
        turn["routing_receipt"]["receipt_id"],
        "pytest",
        repository_root=str(REPOSITORY_ROOT),
    )
    forged = json.loads(json.dumps(manifest))
    forged["payload"]["action"]["execution_root"] = "."
    _reidentify_manifest(forged)

    valid, reason = validate_action_manifest(forged)

    assert valid is False
    assert reason == "fixed_profile_contract_mismatch"


def test_prepare_rejects_repository_state_that_no_longer_matches_parent_receipt(
    tmp_path: Path,
):
    repository = _init_repository(tmp_path)
    turn, session_path = _routed_turn(
        tmp_path / "state",
        repository_root=repository,
    )
    (repository / "tracked.txt").write_text("changed before prepare\n", encoding="utf-8")
    ledger = ActionManifestLedger(str(session_path))

    with pytest.raises(ValueError, match="parent_repository_identity_mismatch"):
        ledger.prepare_fixed_profile(
            turn["routing_receipt"]["receipt_id"],
            "git_status",
            repository_root=str(repository),
        )

    assert not any(key.startswith(ACTION_MANIFEST_PREFIX) for key in ledger.records())


def test_execute_rejects_repository_change_between_manifest_and_action(
    monkeypatch, tmp_path: Path
):
    repository = _init_repository(tmp_path)
    turn, session_path = _routed_turn(
        tmp_path / "state",
        repository_root=repository,
    )
    ledger = ActionManifestLedger(str(session_path))
    manifest = ledger.prepare_fixed_profile(
        turn["routing_receipt"]["receipt_id"],
        "git_status",
        repository_root=str(repository),
    )
    (repository / "tracked.txt").write_text("changed before action\n", encoding="utf-8")
    called = False

    def must_not_execute(self, profile: str, *, governance_authorized: bool) -> dict:
        nonlocal called
        called = True
        raise AssertionError("verification profile must not execute")

    monkeypatch.setattr(
        "spst_runtime.action_manifest.ToolProvider.execute_verification_profile",
        must_not_execute,
    )

    result = ledger.execute(manifest["action_id"], repository_root=str(repository))

    assert called is False
    assert result["manifest_verified"] is True
    assert result["execution_verified"] is False
    assert result["reason"] == "before_repository_identity_mismatch"
    assert not any(key.startswith(ACTION_EVIDENCE_PREFIX) for key in ledger.records())


def test_after_repository_identity_records_unexpected_action_mutation(
    monkeypatch, tmp_path: Path
):
    repository = _init_repository(tmp_path)
    turn, session_path = _routed_turn(
        tmp_path / "state",
        repository_root=repository,
    )
    ledger = ActionManifestLedger(str(session_path))

    def mutating_profile(self, profile: str, *, governance_authorized: bool) -> dict:
        assert governance_authorized is True
        (self._workspace_root / "tracked.txt").write_text(
            "mutated by action\n",
            encoding="utf-8",
        )
        return {
            "profile": profile,
            "status": "completed",
            "returncode": 0,
            "duration_ms": 1,
            "output_digest": "c" * 64,
        }

    monkeypatch.setattr(
        "spst_runtime.action_manifest.ToolProvider.execute_verification_profile",
        mutating_profile,
    )

    result = ledger.run_fixed_profile(
        turn["routing_receipt"]["receipt_id"],
        "git_status",
        repository_root=str(repository),
    )["verification"]

    transition = result["repository_transition"]
    assert result["verified"] is True
    assert result["execution_verified"] is True
    assert result["successful"] is False
    assert result["reason"] == "unexpected_repository_mutation"
    assert transition["status"] == "unexpected_change"
    assert transition["transition_verified"] is True
    assert transition["state_changed"] is True
    assert transition["expected_effect_met"] is False
    assert transition["before_repository_identity"]["identity_sha256"] != transition[
        "after_repository_identity"
    ]["identity_sha256"]
    summary = ledger.summarize_receipt(turn["routing_receipt"]["receipt_id"])
    assert summary["repository_transition_changed_actions"] == 1
    assert summary["failed_actions"] == 1


def test_after_repository_identity_capture_failure_is_recorded_not_hidden(
    monkeypatch, tmp_path: Path
):
    repository = _init_repository(tmp_path)
    turn, session_path = _routed_turn(
        tmp_path / "state",
        repository_root=repository,
    )
    ledger = ActionManifestLedger(str(session_path))
    manifest = ledger.prepare_fixed_profile(
        turn["routing_receipt"]["receipt_id"],
        "git_status",
        repository_root=str(repository),
    )
    calls = 0

    def flaky_capture(root: str | Path) -> dict:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RepositoryIdentityError("synthetic_after_capture_failure")
        return capture_repository_identity(root)

    monkeypatch.setattr(
        "spst_runtime.action_manifest.capture_repository_identity",
        flaky_capture,
    )
    monkeypatch.setattr(
        "spst_runtime.action_manifest.ToolProvider.execute_verification_profile",
        lambda self, profile, governance_authorized: {
            "profile": profile,
            "status": "completed",
            "returncode": 0,
            "duration_ms": 1,
            "output_digest": "d" * 64,
        },
    )

    result = ledger.execute(manifest["action_id"], repository_root=str(repository))

    transition = result["repository_transition"]
    assert result["verified"] is True
    assert result["execution_verified"] is True
    assert result["successful"] is False
    assert result["reason"] == "after_repository_identity_unresolved"
    assert transition["status"] == "after_capture_failed"
    assert transition["transition_verified"] is False
    assert transition["after_repository_identity"] is None
    assert transition["after_capture_reason"] == "synthetic_after_capture_failure"


def test_new_action_rejects_legacy_receipt_without_repository_identity(tmp_path: Path):
    session_path = tmp_path / "chat-state.db"
    turn = run_chat_turn(
        "legacy unbound action",
        steps=1,
        event="action_manifest_legacy_test",
        profile="standard",
        session_path=str(session_path),
        memory_path=str(tmp_path / "memory.db"),
    )
    assert turn["routing_receipt"]["schema"] == "spst-routing-receipt-v2"
    ledger = ActionManifestLedger(str(session_path))

    with pytest.raises(ValueError, match="parent_repository_identity_unbound"):
        ledger.prepare_fixed_profile(
            turn["routing_receipt"]["receipt_id"],
            "git_status",
            repository_root=str(REPOSITORY_ROOT),
        )

    assert verify_chat_receipt(
        turn["routing_receipt"]["receipt_id"], str(session_path)
    )["verified"] is True


def test_legacy_fixed_profile_manifest_shape_remains_valid(tmp_path: Path):
    turn, session_path = _routed_turn(tmp_path)
    ledger = ActionManifestLedger(str(session_path))
    manifest = ledger.prepare_fixed_profile(
        turn["routing_receipt"]["receipt_id"],
        "git_status",
        repository_root=str(REPOSITORY_ROOT),
    )
    legacy = json.loads(json.dumps(manifest))
    legacy["payload"].pop("repository_transition")
    action = legacy["payload"]["action"]
    for key in (
        "profile_contract_version",
        "profile_contract_sha256",
        "repository_sha256",
        "execution_root",
        "execution_root_sha256",
    ):
        action.pop(key)
    action["arguments_sha256"] = hashlib.sha256(
        b'{"profile":"git_status"}'
    ).hexdigest()
    _reidentify_manifest(legacy)

    assert validate_action_manifest(legacy) == (True, None)


def test_failed_fixed_profile_is_execution_verified_but_not_successful(
    monkeypatch, tmp_path: Path
):
    turn, session_path = _routed_turn(tmp_path)
    ledger = ActionManifestLedger(str(session_path))

    monkeypatch.setattr(
        "spst_runtime.action_manifest.ToolProvider.execute_verification_profile",
        lambda self, profile, governance_authorized: {
            "profile": profile,
            "status": "failed",
            "returncode": 1,
            "duration_ms": 1,
            "output_digest": "f" * 64,
        },
    )

    result = ledger.run_fixed_profile(
        turn["routing_receipt"]["receipt_id"],
        "git_status",
        repository_root=str(REPOSITORY_ROOT),
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
            repository_root=str(REPOSITORY_ROOT),
        )

    receipt_id = turn["routing_receipt"]["receipt_id"]
    _tamper_record(
        session_path,
        f"routing_receipt:v4:{receipt_id}",
        lambda record: record["payload"]["route"].__setitem__("event", "tampered"),
    )
    with pytest.raises(ValueError, match="parent_receipt_unverified"):
        ledger.prepare_fixed_profile(
            receipt_id,
            "git_status",
            repository_root=str(REPOSITORY_ROOT),
        )


@pytest.mark.parametrize("record_type", ["manifest", "evidence"])
def test_tampered_action_records_are_never_verified(tmp_path: Path, record_type: str):
    turn, session_path = _routed_turn(tmp_path)
    ledger = ActionManifestLedger(str(session_path))
    result = ledger.run_fixed_profile(
        turn["routing_receipt"]["receipt_id"],
        "git_status",
        repository_root=str(REPOSITORY_ROOT),
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


def test_tampered_after_repository_identity_is_never_verified(tmp_path: Path):
    repository = _init_repository(tmp_path)
    turn, session_path = _routed_turn(
        tmp_path / "state",
        repository_root=repository,
    )
    ledger = ActionManifestLedger(str(session_path))
    result = ledger.run_fixed_profile(
        turn["routing_receipt"]["receipt_id"],
        "git_status",
        repository_root=str(repository),
    )
    action_id = result["manifest"]["action_id"]
    _tamper_record(
        session_path,
        f"{ACTION_EVIDENCE_PREFIX}{action_id}",
        lambda record: record["payload"]["repository_transition"][
            "after_repository_identity"
        ].__setitem__("worktree_sha256", "0" * 64),
    )

    verification = ledger.verify(action_id)

    assert verification["verified"] is False
    assert verification["execution_verified"] is False
    assert verification["reason"] in {
        "state_hash_mismatch",
        "action_evidence_digest_mismatch",
        "repository_identity_digest_mismatch",
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
    transition = manifest["payload"]["repository_transition"]
    assert transition["before_repository_identity"]["identity_sha256"] == turn[
        "routing_receipt"
    ]["payload"]["repository"]["identity_sha256"]
    assert (
        transition["after_repository_identity_source"]
        == "external_execution_unobserved"
    )
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
    assert (
        approved["repository_transition"]["status"]
        == "external_execution_unobserved"
    )
    assert approved["repository_transition"]["after_repository_identity"] is None

    execution = ledger.execute(manifest["action_id"])
    assert execution["verified"] is False
    assert execution["reason"] == "external_execution_unobserved"
    assert not any(
        key.startswith(ACTION_EVIDENCE_PREFIX)
        for key in ledger.records().keys()
    )


def test_approved_r2_external_result_attestation_binds_after_state_without_claiming_execution(
    tmp_path: Path,
):
    repository = _init_repository(tmp_path)
    turn, session_path = _routed_turn(tmp_path, repository_root=repository)
    ledger = ActionManifestLedger(str(session_path))
    manifest = ledger.prepare_external_action(
        turn["routing_receipt"]["receipt_id"],
        operation="commit",
        tool_name="codex.shell_command",
        arguments_sha256=hashlib.sha256(b"git commit audited changes").hexdigest(),
        workspace_root=str(repository),
    )
    action_id = manifest["action_id"]
    ledger.record_approval(action_id, approved=True, actor="test-human")

    (repository / "tracked.txt").write_text("after\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-qm", "external result")
    result_digest = hashlib.sha256(b"commit-object-and-output-bundle").hexdigest()

    verification = ledger.record_external_attestation(
        action_id,
        result_status="completed",
        returncode=0,
        result_evidence_sha256=result_digest,
        workspace_root=str(repository),
    )

    assert verification["verified"] is False
    assert verification["execution_verified"] is False
    assert verification["successful"] is False
    assert verification["reason"] == "external_execution_attested_unverified"
    assert verification["external_attestation"]["integrity_verified"] is True
    assert verification["external_attestation"]["source_authenticated"] is False
    assert verification["external_attestation"]["result_status"] == "completed"
    assert verification["external_attestation"]["result_evidence_sha256"] == result_digest
    transition = verification["repository_transition"]
    assert transition["status"] == "external_after_state_attested_changed"
    assert transition["transition_verified"] is False
    assert transition["causal_link_verified"] is False
    assert transition["state_changed"] is True
    assert transition["after_repository_identity"]["head_revision"] == _git(
        repository, "rev-parse", "HEAD"
    )

    summary = ledger.summarize_receipt(turn["routing_receipt"]["receipt_id"])
    assert summary["external_attested_actions"] == 1
    assert summary["external_unobserved_actions"] == 0
    assert summary["execution_verified_actions"] == 0
    assert summary["successful_actions"] == 0
    assert summary["repository_transition_attested_actions"] == 1
    assert summary["repository_transition_unresolved_actions"] == 1
    assert summary["global_codex_tool_coverage"] is None


def test_external_attestation_rejects_missing_hitl_workspace_mismatch_and_duplicate(
    tmp_path: Path,
):
    repository = _init_repository(tmp_path)
    turn, session_path = _routed_turn(tmp_path, repository_root=repository)
    ledger = ActionManifestLedger(str(session_path))
    manifest = ledger.prepare_external_action(
        turn["routing_receipt"]["receipt_id"],
        operation="commit",
        tool_name="codex.shell_command",
        arguments_sha256="a" * 64,
        workspace_root=str(repository),
    )
    action_id = manifest["action_id"]

    with pytest.raises(ValueError, match="external_attestation_requires_approval"):
        ledger.record_external_attestation(
            action_id,
            result_status="completed",
            returncode=0,
            result_evidence_sha256="b" * 64,
            workspace_root=str(repository),
        )
    assert not any(
        key.startswith(ACTION_EXTERNAL_ATTESTATION_PREFIX)
        for key in ledger.records()
    )

    ledger.record_approval(action_id, approved=True, actor="test-human")
    alternate = _init_repository(tmp_path / "alternate")
    with pytest.raises(ValueError, match="external_attestation_workspace_mismatch"):
        ledger.record_external_attestation(
            action_id,
            result_status="completed",
            returncode=0,
            result_evidence_sha256="b" * 64,
            workspace_root=str(alternate),
        )

    ledger.record_external_attestation(
        action_id,
        result_status="completed",
        returncode=0,
        result_evidence_sha256="b" * 64,
        workspace_root=str(repository),
    )
    with pytest.raises(ValueError, match="external_attestation_already_recorded"):
        ledger.record_external_attestation(
            action_id,
            result_status="completed",
            returncode=0,
            result_evidence_sha256="b" * 64,
            workspace_root=str(repository),
        )


def test_external_attestation_rejects_fixed_action_invalid_result_and_tampering(
    tmp_path: Path,
):
    repository = _init_repository(tmp_path)
    turn, session_path = _routed_turn(tmp_path, repository_root=repository)
    ledger = ActionManifestLedger(str(session_path))
    fixed = ledger.prepare_fixed_profile(
        turn["routing_receipt"]["receipt_id"],
        "git_status",
        repository_root=str(repository),
    )
    with pytest.raises(ValueError, match="external_attestation_requires_external_action"):
        ledger.record_external_attestation(
            fixed["action_id"],
            result_status="completed",
            returncode=0,
            result_evidence_sha256="c" * 64,
            workspace_root=str(repository),
        )

    external = ledger.prepare_external_action(
        turn["routing_receipt"]["receipt_id"],
        operation="workspace_edit",
        tool_name="codex.apply_patch",
        arguments_sha256="d" * 64,
        workspace_root=str(repository),
    )
    action_id = external["action_id"]
    with pytest.raises(ValueError, match="external_attestation_result_inconsistent"):
        ledger.record_external_attestation(
            action_id,
            result_status="completed",
            returncode=1,
            result_evidence_sha256="e" * 64,
            workspace_root=str(repository),
        )

    ledger.record_external_attestation(
        action_id,
        result_status="failed",
        returncode=1,
        result_evidence_sha256="e" * 64,
        workspace_root=str(repository),
    )
    _tamper_record(
        session_path,
        f"{ACTION_EXTERNAL_ATTESTATION_PREFIX}{action_id}",
        lambda record: record["payload"].__setitem__(
            "result_evidence_sha256", "f" * 64
        ),
    )

    verification = ledger.verify(action_id)

    assert verification["verified"] is False
    assert verification["execution_verified"] is False
    assert verification["reason"] in {
        "state_hash_mismatch",
        "external_attestation_digest_mismatch",
    }


def test_action_bridge_records_external_attestation_without_execution_claim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    repository = _init_repository(tmp_path)
    turn, session_path = _routed_turn(tmp_path, repository_root=repository)
    ledger = ActionManifestLedger(str(session_path))
    manifest = ledger.prepare_external_action(
        turn["routing_receipt"]["receipt_id"],
        operation="workspace_edit",
        tool_name="codex.apply_patch",
        arguments_sha256="1" * 64,
        workspace_root=str(repository),
    )

    returncode = action_bridge_main(
        [
            "--session-db",
            str(session_path),
            "attest-external",
            "--action-id",
            manifest["action_id"],
            "--result-status",
            "completed",
            "--returncode",
            "0",
            "--result-evidence-sha256",
            "2" * 64,
            "--workspace-root",
            str(repository),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert returncode == 0
    assert payload["execution_verified"] is False
    assert payload["reason"] == "external_execution_attested_unverified"
    assert payload["external_attestation"]["integrity_verified"] is True
    assert (
        payload["repository_transition"]["status"]
        == "external_after_state_attested_preserved"
    )

    before = _file_manifest(session_path)
    read_only = ActionManifestLedger(str(session_path), read_only=True).verify(
        manifest["action_id"]
    )
    assert _file_manifest(session_path) == before
    assert read_only["external_attestation"]["integrity_verified"] is True


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
        repository_root=str(REPOSITORY_ROOT),
    )

    alternate_repository = tmp_path / "alternate-repository"
    (alternate_repository / ".git").mkdir(parents=True)
    mismatch = ledger.execute(
        manifest["action_id"], repository_root=str(alternate_repository)
    )
    assert mismatch["verified"] is False
    assert mismatch["reason"] == "repository_binding_mismatch"

    completed = ledger.execute(
        manifest["action_id"], repository_root=str(REPOSITORY_ROOT)
    )
    assert completed["verified"] is True
    with pytest.raises(ValueError, match="action_already_executed"):
        ledger.execute(manifest["action_id"], repository_root=str(REPOSITORY_ROOT))


def test_action_status_read_path_does_not_change_database(tmp_path: Path):
    turn, session_path = _routed_turn(tmp_path)
    receipt_id = turn["routing_receipt"]["receipt_id"]
    writer = ActionManifestLedger(str(session_path))
    writer.run_fixed_profile(
        receipt_id,
        "git_status",
        repository_root=str(REPOSITORY_ROOT),
    )
    before = _file_manifest(session_path)

    summary = ActionManifestLedger(str(session_path), read_only=True).summarize_receipt(
        receipt_id
    )

    assert _file_manifest(session_path) == before
    assert summary["bound_actions"] == 1
    assert summary["execution_verified_actions"] == 1
    assert summary["repository_transition_bound_actions"] == 1
    assert summary["repository_transition_verified_actions"] == 1
    assert summary["repository_transition_preserved_actions"] == 1
    assert summary["global_codex_tool_coverage"] is None


def test_read_only_status_for_missing_database_creates_nothing(tmp_path: Path):
    session_path = tmp_path / "missing" / "chat-state.db"

    summary = ActionManifestLedger(
        str(session_path), read_only=True
    ).summarize_receipt("0" * 64)

    assert summary["bound_actions"] == 0
    assert summary["global_codex_tool_coverage"] is None
    assert list(tmp_path.rglob("*")) == []
