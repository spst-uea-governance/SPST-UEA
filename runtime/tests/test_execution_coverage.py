import asyncio
import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from spst_runtime.action_manifest import ActionManifestLedger
from spst_runtime.chat_bridge import run_chat_turn, verify_chat_receipt
from spst_runtime.execution_coverage import (
    EXECUTION_PLAN_PREFIX,
    EXECUTION_PLAN_REQUEST_SCHEMA,
    GovernedExecutionCoverageLedger,
    validate_execution_plan,
)
from spst_runtime.execution_coverage_bridge import main as coverage_bridge_main


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
    (repository / "runtime" / "spst_runtime").mkdir(parents=True)
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "SPST Test")
    _git(repository, "config", "user.email", "spst@example.invalid")
    _git(repository, "config", "core.autocrlf", "false")
    _git(repository, "config", "core.filemode", "false")
    (repository / ".gitignore").write_text("*.db\n.coverage\n", encoding="utf-8")
    (repository / "tracked.txt").write_text("initial\n", encoding="utf-8")
    (repository / "runtime" / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n",
        encoding="utf-8",
    )
    (repository / "runtime" / "marker.txt").write_text("runtime\n", encoding="utf-8")
    (repository / "runtime" / "spst_runtime" / "__init__.py").write_text(
        "",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "initial")
    return repository


def _routed_turn(tmp_path: Path) -> tuple[Path, Path, dict]:
    repository = _init_repository(tmp_path)
    session_path = tmp_path / "session.db"
    turn = run_chat_turn(
        "verify a declared governed action denominator",
        steps=1,
        event="execution_coverage_test",
        profile="standard",
        session_path=str(session_path),
        memory_path=str(tmp_path / "memory.db"),
        repository_root=str(repository),
    )
    return repository, session_path, turn


def _receipt_id(turn: dict) -> str:
    return str(turn["routing_receipt"]["receipt_id"])


def _fixed(slot_id: str, profile: str) -> dict:
    return {"slot_id": slot_id, "kind": "fixed_profile", "profile": profile}


def _external(
    slot_id: str,
    *,
    operation: str = "read",
    tool_name: str = "codex.shell_command",
    arguments_sha256: str = "a" * 64,
) -> dict:
    return {
        "slot_id": slot_id,
        "kind": "external_tool",
        "operation": operation,
        "tool_name": tool_name,
        "arguments_sha256": arguments_sha256,
    }


def _file_manifest(path: Path) -> dict[str, tuple[int, str]]:
    return {
        candidate.name: (
            candidate.stat().st_size,
            hashlib.sha256(candidate.read_bytes()).hexdigest(),
        )
        for candidate in path.parent.glob(f"{path.name}*")
        if candidate.is_file()
    }


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


def test_declared_fixed_profiles_reach_runtime_verified_complete(tmp_path: Path):
    repository, session_path, turn = _routed_turn(tmp_path)
    receipt_id = _receipt_id(turn)
    coverage = GovernedExecutionCoverageLedger(str(session_path))
    plan = coverage.register_plan(
        receipt_id,
        [_fixed("head", "git_head"), _fixed("status", "git_status")],
        repository_root=str(repository),
    )
    actions = ActionManifestLedger(str(session_path))
    actions.run_fixed_profile(receipt_id, "git_head", repository_root=str(repository))
    actions.run_fixed_profile(receipt_id, "git_status", repository_root=str(repository))

    result = coverage.summarize(plan["plan_id"])

    assert result["status"] == "runtime_verified_complete"
    assert result["coverage_complete"] is True
    assert result["governance_binding_complete"] is True
    assert result["declared_actions"] == 2
    assert result["manifest_bound_actions"] == 2
    assert result["runtime_execution_verified_actions"] == 2
    assert result["successful_runtime_actions"] == 2
    assert result["repository_transition_verified_actions"] == 2
    assert result["coverage"]["runtime_verified_execution"] == {
        "numerator": 2,
        "denominator": 2,
        "value": 1.0,
    }
    assert result["declaration_completeness_verified"] is False
    assert result["global_codex_tool_coverage"] is None
    assert result["honesty"]["coverage_proves_task_quality_uplift"] is False
    assert [slot["status"] for slot in result["slots"]] == [
        "runtime_verified_success",
        "runtime_verified_success",
    ]

    receipt = verify_chat_receipt(
        receipt_id,
        str(session_path),
        repository_root=str(repository),
    )
    assert receipt["verified"] is True
    assert receipt["execution_coverage"]["plan_id"] == plan["plan_id"]
    assert receipt["execution_coverage"]["coverage_complete"] is True


def test_missing_declared_action_is_visible_in_denominator(tmp_path: Path):
    repository, session_path, turn = _routed_turn(tmp_path)
    coverage = GovernedExecutionCoverageLedger(str(session_path))
    plan = coverage.register_plan(
        _receipt_id(turn),
        [_fixed("head", "git_head"), _fixed("status", "git_status")],
        repository_root=str(repository),
    )
    ActionManifestLedger(str(session_path)).run_fixed_profile(
        _receipt_id(turn), "git_head", repository_root=str(repository)
    )

    result = coverage.summarize(plan["plan_id"])

    assert result["status"] == "partial"
    assert result["coverage_complete"] is False
    assert result["manifest_bound_actions"] == 1
    assert result["coverage"]["action_manifest"]["value"] == 0.5
    assert result["slots"][1]["status"] == "missing_action_manifest"


def test_external_attestation_never_becomes_verified_execution(tmp_path: Path):
    repository, session_path, turn = _routed_turn(tmp_path)
    receipt_id = _receipt_id(turn)
    coverage = GovernedExecutionCoverageLedger(str(session_path))
    plan = coverage.register_plan(
        receipt_id,
        [_external("inspect")],
        repository_root=str(repository),
    )
    actions = ActionManifestLedger(str(session_path))
    manifest = actions.prepare_external_action(
        receipt_id,
        operation="read",
        tool_name="codex.shell_command",
        arguments_sha256="a" * 64,
        workspace_root=str(repository),
    )
    actions.record_external_attestation(
        manifest["action_id"],
        result_status="completed",
        returncode=0,
        result_evidence_sha256="b" * 64,
        workspace_root=str(repository),
    )

    result = coverage.summarize(plan["plan_id"])

    assert result["status"] == "declared_governance_complete_external_execution_unverified"
    assert result["governance_binding_complete"] is True
    assert result["result_evidence_complete"] is True
    assert result["coverage_complete"] is False
    assert result["external_attested_unverified_actions"] == 1
    assert result["runtime_execution_verified_actions"] == 0
    assert result["coverage"]["runtime_verified_execution"]["value"] == 0.0
    assert result["slots"][0]["status"] == "external_attested_completed_unverified"
    assert result["honesty"]["external_attestation_is_execution_verification"] is False


def test_r2_external_action_requires_approval_in_coverage(tmp_path: Path):
    repository, session_path, turn = _routed_turn(tmp_path)
    receipt_id = _receipt_id(turn)
    coverage = GovernedExecutionCoverageLedger(str(session_path))
    plan = coverage.register_plan(
        receipt_id,
        [_external("deploy", operation="deploy")],
        repository_root=str(repository),
    )
    actions = ActionManifestLedger(str(session_path))
    manifest = actions.prepare_external_action(
        receipt_id,
        operation="deploy",
        tool_name="codex.shell_command",
        arguments_sha256="a" * 64,
        workspace_root=str(repository),
    )

    pending = coverage.summarize(plan["plan_id"])

    assert pending["authorized_actions"] == 0
    assert pending["slots"][0]["status"] == "pending_human_approval"
    assert pending["governance_binding_complete"] is False

    actions.record_approval(manifest["action_id"], approved=True, actor="local-human")
    actions.record_external_attestation(
        manifest["action_id"],
        result_status="completed",
        returncode=0,
        result_evidence_sha256="b" * 64,
        workspace_root=str(repository),
    )
    approved = coverage.summarize(plan["plan_id"])

    assert approved["authorized_actions"] == 1
    assert approved["governance_binding_complete"] is True
    assert approved["coverage_complete"] is False


def test_plan_registration_rejects_post_hoc_denominator(tmp_path: Path):
    repository, session_path, turn = _routed_turn(tmp_path)
    receipt_id = _receipt_id(turn)
    ActionManifestLedger(str(session_path)).prepare_fixed_profile(
        receipt_id,
        "git_head",
        repository_root=str(repository),
    )

    with pytest.raises(ValueError, match="execution_plan_registration_after_action"):
        GovernedExecutionCoverageLedger(str(session_path)).register_plan(
            receipt_id,
            [_fixed("head", "git_head")],
            repository_root=str(repository),
        )


def test_plan_registration_rejects_stale_repository_and_duplicate_slots(tmp_path: Path):
    repository, session_path, turn = _routed_turn(tmp_path)
    coverage = GovernedExecutionCoverageLedger(str(session_path))
    receipt_id = _receipt_id(turn)
    (repository / "untracked.txt").write_text("changed after receipt\n", encoding="utf-8")

    with pytest.raises(ValueError, match="parent_repository_identity_mismatch"):
        coverage.register_plan(
            receipt_id,
            [_fixed("head", "git_head")],
            repository_root=str(repository),
        )

    (repository / "untracked.txt").unlink()
    with pytest.raises(ValueError, match="declared_action_slot_id_duplicate"):
        coverage.register_plan(
            receipt_id,
            [_fixed("same", "git_head"), _fixed("same", "git_status")],
            repository_root=str(repository),
        )


def test_out_of_plan_duplicate_and_order_drift_block_completion(tmp_path: Path):
    repository, session_path, turn = _routed_turn(tmp_path)
    receipt_id = _receipt_id(turn)
    coverage = GovernedExecutionCoverageLedger(str(session_path))
    plan = coverage.register_plan(
        receipt_id,
        [_fixed("status", "git_status"), _fixed("head", "git_head")],
        repository_root=str(repository),
    )
    actions = ActionManifestLedger(str(session_path))
    actions.run_fixed_profile(receipt_id, "git_head", repository_root=str(repository))
    actions.run_fixed_profile(receipt_id, "git_status", repository_root=str(repository))
    actions.run_fixed_profile(receipt_id, "git_head", repository_root=str(repository))

    result = coverage.summarize(plan["plan_id"])

    assert result["manifest_bound_actions"] == 2
    assert result["out_of_plan_actions"] == 1
    assert result["declared_order_verified"] is False
    assert result["coverage_complete"] is False
    assert result["out_of_plan"][0]["reason"] == "action_not_declared_or_duplicate"


def test_identical_clone_cannot_satisfy_plan_workspace_binding(tmp_path: Path):
    repository, session_path, turn = _routed_turn(tmp_path)
    receipt_id = _receipt_id(turn)
    coverage = GovernedExecutionCoverageLedger(str(session_path))
    plan = coverage.register_plan(
        receipt_id,
        [_fixed("head", "git_head")],
        repository_root=str(repository),
    )
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(repository), str(clone)],
        check=True,
        capture_output=True,
        text=True,
    )
    ActionManifestLedger(str(session_path)).run_fixed_profile(
        receipt_id,
        "git_head",
        repository_root=str(clone),
    )

    result = coverage.summarize(plan["plan_id"])

    assert result["manifest_bound_actions"] == 0
    assert result["out_of_plan_actions"] == 1
    assert result["out_of_plan"][0]["reason"] == "action_workspace_mismatch"
    assert result["coverage_complete"] is False


def test_unknown_external_operation_is_predeclared_as_r2(tmp_path: Path):
    repository, session_path, turn = _routed_turn(tmp_path)
    plan = GovernedExecutionCoverageLedger(str(session_path)).register_plan(
        _receipt_id(turn),
        [_external("unknown", operation="future_operation")],
        repository_root=str(repository),
    )

    slot = plan["payload"]["declaration"]["slots"][0]
    assert slot["risk"] == {"level": "R2", "requires_human_approval": True}
    assert slot["expected_observation"] == "external_execution_unobserved"


def test_git_diff_check_plan_risk_matches_read_only_action_contract(tmp_path: Path):
    repository, session_path, turn = _routed_turn(tmp_path)
    receipt_id = _receipt_id(turn)
    coverage = GovernedExecutionCoverageLedger(str(session_path))
    plan = coverage.register_plan(
        receipt_id,
        [_fixed("diff", "git_diff_check")],
        repository_root=str(repository),
    )
    ActionManifestLedger(str(session_path)).run_fixed_profile(
        receipt_id,
        "git_diff_check",
        repository_root=str(repository),
    )

    result = coverage.summarize(plan["plan_id"])

    assert plan["payload"]["declaration"]["slots"][0]["risk"] == {
        "level": "R0",
        "requires_human_approval": False,
    }
    assert result["slots"][0]["risk_level"] == "R0"
    assert result["coverage_complete"] is True


@pytest.mark.parametrize(
    "profile",
    [
        "git_head",
        "git_status",
        "git_diff_check",
        "pytest",
        "pytest_coverage",
        "coverage_report",
        "ruff",
        "mypy",
    ],
)
def test_every_action_profile_contract_matches_its_declared_slot(
    tmp_path: Path,
    profile: str,
):
    repository, session_path, turn = _routed_turn(tmp_path)
    if profile == "coverage_report":
        (repository / "runtime" / ".coverage").write_bytes(b"ignored marker")
    receipt_id = _receipt_id(turn)
    coverage = GovernedExecutionCoverageLedger(str(session_path))
    plan = coverage.register_plan(
        receipt_id,
        [_fixed("profile", profile)],
        repository_root=str(repository),
    )
    action = ActionManifestLedger(str(session_path)).prepare_fixed_profile(
        receipt_id,
        profile,
        repository_root=str(repository),
    )

    result = coverage.summarize(plan["plan_id"])

    assert result["manifest_bound_actions"] == 1
    assert result["out_of_plan_actions"] == 0
    assert result["slots"][0]["action_id"] == action["action_id"]
    assert result["slots"][0]["risk_level"] == action["payload"]["risk"]["level"]


def test_tampered_or_rewritten_plan_fails_closed(tmp_path: Path):
    repository, session_path, turn = _routed_turn(tmp_path)
    ledger = GovernedExecutionCoverageLedger(str(session_path))
    plan = ledger.register_plan(
        _receipt_id(turn),
        [_fixed("head", "git_head")],
        repository_root=str(repository),
    )
    key = f"{EXECUTION_PLAN_PREFIX}{plan['plan_id']}"
    _tamper_record(
        session_path,
        key,
        lambda record: record["payload"]["declaration"].update({"slot_count": 9}),
    )

    tampered = ledger.summarize(plan["plan_id"])

    assert tampered["status"] == "invalid"
    assert tampered["coverage_complete"] is False
    assert tampered["plan"]["reason"] in {
        "execution_plan_digest_mismatch",
        "state_hash_mismatch",
    }

    repository_two, session_two, turn_two = _routed_turn(tmp_path / "rewrite")
    writer = GovernedExecutionCoverageLedger(str(session_two))
    plan_two = writer.register_plan(
        _receipt_id(turn_two),
        [_fixed("head", "git_head")],
        repository_root=str(repository_two),
    )
    asyncio.run(
        writer.repository.save(
            f"{EXECUTION_PLAN_PREFIX}{plan_two['plan_id']}",
            plan_two,
        )
    )
    rewritten = writer.verify(plan_two["plan_id"])
    assert rewritten["verified"] is False
    assert rewritten["reason"] == "execution_plan_rewritten"


def test_plan_validator_rejects_risk_and_digest_relabeling(tmp_path: Path):
    repository, session_path, turn = _routed_turn(tmp_path)
    plan = GovernedExecutionCoverageLedger(str(session_path)).register_plan(
        _receipt_id(turn),
        [_external("deploy", operation="deploy")],
        repository_root=str(repository),
    )
    relabeled = json.loads(json.dumps(plan))
    relabeled["payload"]["declaration"]["slots"][0]["risk"]["level"] = "R0"
    signed = {"schema": relabeled["schema"], "payload": relabeled["payload"]}
    relabeled["plan_id"] = hashlib.sha256(
        json.dumps(
            signed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()

    valid, reason = validate_execution_plan(relabeled)

    assert valid is False
    assert reason == "declared_action_binding_mismatch"


def test_read_only_status_preserves_database_and_missing_path(tmp_path: Path):
    repository, session_path, turn = _routed_turn(tmp_path)
    ledger = GovernedExecutionCoverageLedger(str(session_path))
    ledger.register_plan(
        _receipt_id(turn),
        [_fixed("head", "git_head")],
        repository_root=str(repository),
    )
    before = _file_manifest(session_path)

    status = GovernedExecutionCoverageLedger(
        str(session_path), read_only=True
    ).summarize_receipt(_receipt_id(turn))
    after = _file_manifest(session_path)

    assert status["status"] == "partial"
    assert before == after

    missing = tmp_path / "missing.db"
    empty = GovernedExecutionCoverageLedger(
        str(missing), read_only=True
    ).summarize_receipt("0" * 64)
    assert empty["status"] == "not_declared"
    assert not missing.exists()
    assert list(tmp_path.glob("missing.db*")) == []


def test_cli_register_and_read_only_status(tmp_path: Path, capsys):
    repository, session_path, turn = _routed_turn(tmp_path)
    request_path = tmp_path / "plan.json"
    request_path.write_text(
        json.dumps(
            {
                "schema": EXECUTION_PLAN_REQUEST_SCHEMA,
                "actions": [_fixed("head", "git_head")],
            }
        ),
        encoding="utf-8",
    )

    exit_code = coverage_bridge_main(
        [
            "--session-db",
            str(session_path),
            "register",
            "--receipt-id",
            _receipt_id(turn),
            "--plan-file",
            str(request_path),
            "--repository-root",
            str(repository),
        ]
    )
    registered = json.loads(capsys.readouterr().out)
    before = _file_manifest(session_path)
    status_exit = coverage_bridge_main(
        [
            "--session-db",
            str(session_path),
            "status",
            "--receipt-id",
            _receipt_id(turn),
        ]
    )
    status = json.loads(capsys.readouterr().out)
    after = _file_manifest(session_path)

    assert exit_code == 0
    assert registered["schema"] == "spst-governed-execution-plan-v1"
    assert status_exit == 0
    assert status["status"] == "partial"
    assert status["global_codex_tool_coverage"] is None
    assert before == after


def test_cli_rejects_malformed_plan_request(tmp_path: Path, capsys):
    repository, session_path, turn = _routed_turn(tmp_path)
    request_path = tmp_path / "plan.json"
    request_path.write_text(json.dumps({"schema": "wrong", "actions": []}), encoding="utf-8")

    exit_code = coverage_bridge_main(
        [
            "--session-db",
            str(session_path),
            "register",
            "--receipt-id",
            _receipt_id(turn),
            "--plan-file",
            str(request_path),
            "--repository-root",
            str(repository),
        ]
    )
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert result["status"] == "rejected"
    assert result["reason"] == "execution_plan_request_schema_mismatch"
