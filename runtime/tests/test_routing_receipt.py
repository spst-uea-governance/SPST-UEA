import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from spst_runtime.chat_bridge import get_chat_status, run_chat_turn, verify_chat_receipt
from spst_runtime.chat_session import SESSION_KEY
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.repository_identity import (
    RepositoryIdentityError,
    capture_repository_identity,
)
from spst_runtime.routing_receipt import RoutingReceiptLedger, build_routing_receipt


def _file_manifest(path: Path) -> dict[str, tuple[int, str]]:
    return {
        candidate.name: (candidate.stat().st_size, hashlib.sha256(candidate.read_bytes()).hexdigest())
        for candidate in path.parent.glob(f"{path.name}*")
        if candidate.is_file()
    }


def _run_isolated_turn(
    tmp_path: Path,
    prompt: str = "route this task",
    *,
    repository_root: Path | None = None,
) -> tuple[dict, Path]:
    session_path = tmp_path / "chat-state.db"
    result = run_chat_turn(
        prompt,
        steps=1,
        event="receipt_test",
        profile="standard",
        session_path=str(session_path),
        memory_path=str(tmp_path / "memory.db"),
        repository_root=str(repository_root) if repository_root else None,
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
    repository.mkdir(parents=True)
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "SPST Test")
    _git(repository, "config", "user.email", "spst@example.invalid")
    _git(repository, "config", "core.autocrlf", "false")
    _git(repository, "config", "core.filemode", "false")
    (repository / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repository / "tracked.txt").write_text("initial evidence\n", encoding="utf-8")
    _git(repository, "add", ".gitignore", "tracked.txt")
    _git(repository, "commit", "-qm", "initial")
    return repository


def _tree_file_manifest(root: Path) -> dict[str, str]:
    return {
        candidate.relative_to(root).as_posix(): hashlib.sha256(candidate.read_bytes()).hexdigest()
        for candidate in root.rglob("*")
        if candidate.is_file()
    }


def test_routed_turn_emits_persisted_verifiable_receipt(tmp_path: Path):
    prompt = "sensitive task body is represented by a digest"
    result, session_path = _run_isolated_turn(tmp_path, prompt)
    receipt = result["routing_receipt"]

    assert receipt["schema"] == "spst-routing-receipt-v2"
    assert receipt["payload"]["route"]["prompt_sha256"] == hashlib.sha256(
        prompt.encode("utf-8")
    ).hexdigest()
    assert prompt not in json.dumps(receipt, sort_keys=True)
    assert receipt["payload"]["pipeline"]["trace"] == [
        "observe",
        "retrieve",
        "infer",
        "reflect",
        "govern",
        "commit",
        "act",
    ]
    assert receipt["payload"]["governance"]["authorized"] is True
    assert receipt["payload"]["session"]["turn"] == 1
    assert receipt["payload"]["session"]["memory_binding"]["record_id"]
    assert receipt["verification"]["verified"] is True

    independent = verify_chat_receipt(receipt["receipt_id"], str(session_path))
    assert independent["verified"] is True
    assert independent["receipt_id"] == receipt["receipt_id"]
    assert independent["provenance"]["valid"] is True


def test_repository_bound_receipt_binds_head_and_worktree_without_path_leakage(
    tmp_path: Path,
):
    repository = _init_repository(tmp_path)
    result, session_path = _run_isolated_turn(
        tmp_path / "state",
        "bind the repository state",
        repository_root=repository,
    )
    receipt = result["routing_receipt"]
    identity = receipt["payload"]["repository"]

    assert receipt["schema"] == "spst-routing-receipt-v3"
    assert identity["schema"] == "spst-repository-identity-v1"
    assert identity["head_revision"] == _git(repository, "rev-parse", "HEAD")
    assert len(identity["worktree_sha256"]) == 64
    assert len(identity["identity_sha256"]) == 64
    assert identity["dirty"] is False
    assert receipt["verification"]["repository"] == {
        "bound": True,
        "binding_verified": True,
        "identity_sha256": identity["identity_sha256"],
        "head_revision": identity["head_revision"],
        "worktree_sha256": identity["worktree_sha256"],
        "dirty": False,
        "current_match": None,
        "reason": "repository_root_not_provided",
    }
    serialized = json.dumps(receipt, sort_keys=True)
    assert str(repository) not in serialized
    assert "tracked.txt" not in serialized
    assert "initial evidence" not in serialized

    database_before = _file_manifest(session_path)
    repository_before = _tree_file_manifest(repository)
    independent = verify_chat_receipt(
        receipt["receipt_id"],
        str(session_path),
        str(repository),
    )
    assert _file_manifest(session_path) == database_before
    assert _tree_file_manifest(repository) == repository_before
    assert independent["verified"] is True
    assert independent["repository"]["binding_verified"] is True
    assert independent["repository"]["current_match"] is True
    assert independent["repository"]["head_match"] is True
    assert independent["repository"]["worktree_match"] is True
    status = get_chat_status(str(session_path), str(repository))
    assert status["routing"]["repository_bound_receipts"] == 1
    assert status["routing"]["latest_repository_current_match"] is True
    assert _file_manifest(session_path) == database_before
    assert _tree_file_manifest(repository) == repository_before


def test_repository_bound_receipt_preserves_historical_truth_after_worktree_changes(
    tmp_path: Path,
):
    repository = _init_repository(tmp_path)
    result, session_path = _run_isolated_turn(
        tmp_path / "state",
        repository_root=repository,
    )
    receipt_id = result["routing_receipt"]["receipt_id"]

    (repository / "tracked.txt").write_text("changed evidence\n", encoding="utf-8")
    verification = verify_chat_receipt(receipt_id, str(session_path), str(repository))

    assert verification["verified"] is True
    assert verification["repository"]["binding_verified"] is True
    assert verification["repository"]["current_match"] is False
    assert verification["repository"]["head_match"] is True
    assert verification["repository"]["worktree_match"] is False
    assert verification["repository"]["reason"] == "repository_identity_mismatch"


def test_repository_identity_detects_head_index_tracked_and_untracked_changes(tmp_path: Path):
    repository = _init_repository(tmp_path)
    initial = capture_repository_identity(repository)

    _git(repository, "commit", "--allow-empty", "-qm", "head only")
    head_changed = capture_repository_identity(repository)
    assert head_changed["head_revision"] != initial["head_revision"]
    assert head_changed["worktree_sha256"] == initial["worktree_sha256"]
    assert head_changed["identity_sha256"] != initial["identity_sha256"]

    (repository / "tracked.txt").write_text("staged evidence\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    staged = capture_repository_identity(repository)
    assert staged["index_sha256"] != head_changed["index_sha256"]
    assert staged["worktree_sha256"] != head_changed["worktree_sha256"]
    assert staged["dirty"] is True

    (repository / "untracked.txt").write_text("new evidence\n", encoding="utf-8")
    untracked = capture_repository_identity(repository)
    assert untracked["worktree_sha256"] != staged["worktree_sha256"]
    assert untracked["untracked_entry_count"] == 1

    (repository / "untracked.txt").write_text("different evidence\n", encoding="utf-8")
    untracked_changed = capture_repository_identity(repository)
    assert untracked_changed["worktree_sha256"] != untracked["worktree_sha256"]


def test_repository_identity_ignores_mtime_and_git_ignored_content(tmp_path: Path):
    repository = _init_repository(tmp_path)
    tracked = repository / "tracked.txt"
    initial = capture_repository_identity(repository)

    original_stat = tracked.stat()
    os.utime(
        tracked,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 2_000_000_000),
    )
    after_mtime = capture_repository_identity(repository)
    assert after_mtime == initial

    (repository / "ignored.txt").write_text("ignored metadata\n", encoding="utf-8")
    after_ignored = capture_repository_identity(repository)
    assert after_ignored == initial


def test_repository_binding_rejects_non_git_and_subdirectory_before_state_write(tmp_path: Path):
    non_git = tmp_path / "not-a-repository"
    non_git.mkdir()
    state_root = tmp_path / "state"
    with pytest.raises(RepositoryIdentityError, match="repository_not_git"):
        _run_isolated_turn(state_root, repository_root=non_git)
    assert not state_root.exists()

    repository = _init_repository(tmp_path / "valid")
    nested = repository / "nested"
    nested.mkdir()
    with pytest.raises(RepositoryIdentityError, match="repository_root_not_toplevel"):
        capture_repository_identity(nested)


def test_tampered_repository_identity_invalidates_v3_receipt(tmp_path: Path):
    repository = _init_repository(tmp_path)
    result, session_path = _run_isolated_turn(
        tmp_path / "state",
        repository_root=repository,
    )
    receipt_id = result["routing_receipt"]["receipt_id"]
    receipt_key = f"routing_receipt:v3:{receipt_id}"
    conn = sqlite3.connect(session_path)
    try:
        serialized = conn.execute(
            "SELECT value FROM state_store WHERE key = ?", (receipt_key,)
        ).fetchone()[0]
        receipt = json.loads(serialized)
        receipt["payload"]["repository"]["worktree_sha256"] = "0" * 64
        conn.execute(
            "UPDATE state_store SET value = ? WHERE key = ?",
            (json.dumps(receipt, sort_keys=True), receipt_key),
        )
        conn.commit()
    finally:
        conn.close()

    verification = verify_chat_receipt(receipt_id, str(session_path), str(repository))
    assert verification["verified"] is False
    assert verification["reason"] in {"receipt_digest_mismatch", "state_hash_mismatch"}


def test_status_reads_existing_session_without_changing_any_source_file(tmp_path: Path):
    result, session_path = _run_isolated_turn(tmp_path)
    before = _file_manifest(session_path)

    status = get_chat_status(str(session_path))

    after = _file_manifest(session_path)
    assert after == before
    assert status["persistence"]["access"] == "read_only_immutable"
    assert status["persistence"]["source_unchanged"] is True
    assert status["persistence"]["state"] == "present"
    assert status["persistence"]["provenance"]["valid"] is True
    assert status["routing"]["verified_receipts"] == 1
    assert status["routing"]["verified_turns"] == 1
    assert status["routing"]["eligible_turns"] == 1
    assert status["routing"]["receipt_coverage"] == 1.0
    assert status["routing"]["latest_receipt_id"] == result["routing_receipt"]["receipt_id"]
    assert status["routing"]["global_codex_task_coverage"] is None
    assert status["routing"]["global_coverage_reason"] == "codex_task_denominator_unavailable"
    assert status["routing"]["task_quality_delta"] is None
    assert status["routing"]["task_quality_reason"] == "paired_outcome_measurement_unavailable"


def test_multiple_receipts_for_one_turn_do_not_inflate_coverage(tmp_path: Path):
    result, session_path = _run_isolated_turn(tmp_path)
    original = result["routing_receipt"]
    alternate = build_routing_receipt(
        prompt="alternate claim for the same persisted turn",
        event="receipt_test",
        steps=1,
        pipeline=result["runtime"]["pipeline"],
        session_turn=1,
        session_state_hash=original["payload"]["session"]["state_record_hash"],
        memory_record_id=original["payload"]["session"]["memory_binding"]["record_id"],
    )
    RoutingReceiptLedger(str(session_path)).persist(alternate)

    status = get_chat_status(str(session_path))

    assert status["routing"]["verified_receipts"] == 2
    assert status["routing"]["verified_turns"] == 1
    assert status["routing"]["eligible_turns"] == 1
    assert status["routing"]["receipt_coverage"] == 1.0


def test_status_for_missing_database_is_side_effect_free(tmp_path: Path):
    session_path = tmp_path / "missing" / "chat-state.db"

    status = get_chat_status(str(session_path))

    assert status["turn_count"] == 0
    assert status["persistence"]["state"] == "missing"
    assert not session_path.exists()
    assert not Path(f"{session_path}.provenance_key").exists()
    assert list(tmp_path.rglob("*")) == []


def test_missing_or_tampered_receipt_is_not_verified(tmp_path: Path):
    result, session_path = _run_isolated_turn(tmp_path)
    receipt_id = result["routing_receipt"]["receipt_id"]

    missing = verify_chat_receipt("0" * 64, str(session_path))
    assert missing == {
        "verified": False,
        "receipt_id": "0" * 64,
        "reason": "receipt_missing",
    }

    receipt_key = f"routing_receipt:v2:{receipt_id}"
    conn = sqlite3.connect(session_path)
    try:
        serialized = conn.execute(
            "SELECT value FROM state_store WHERE key = ?", (receipt_key,)
        ).fetchone()[0]
        receipt = json.loads(serialized)
        receipt["payload"]["route"]["event"] = "tampered"
        conn.execute(
            "UPDATE state_store SET value = ? WHERE key = ?",
            (json.dumps(receipt, sort_keys=True), receipt_key),
        )
        conn.commit()
    finally:
        conn.close()

    tampered = verify_chat_receipt(receipt_id, str(session_path))
    assert tampered["verified"] is False
    assert tampered["reason"] in {"receipt_digest_mismatch", "state_hash_mismatch"}


def test_incomplete_pipeline_cannot_claim_a_routing_receipt():
    with pytest.raises(ValueError, match="complete SPST trace"):
        build_routing_receipt(
            prompt="not fully routed",
            event="chat_turn",
            steps=1,
            pipeline={"last_trace": ["observe", "infer"], "version": 1},
            session_turn=1,
            session_state_hash="a" * 64,
            memory_record_id="memory-1",
        )


def test_read_only_repository_rejects_writes(tmp_path: Path):
    path = tmp_path / "state.db"
    writer = SQLiteRepository(str(path))
    asyncio.run(writer.save(SESSION_KEY, {"turn_count": 0}))

    reader = SQLiteRepository(str(path), read_only=True)
    with pytest.raises(PermissionError, match="read-only"):
        asyncio.run(reader.save(SESSION_KEY, {"turn_count": 99}))


def test_legacy_v1_receipt_remains_verifiable_after_profiled_v2_upgrade(tmp_path: Path):
    result, session_path = _run_isolated_turn(tmp_path)
    receipt = result["routing_receipt"]

    assert receipt["schema"] == "spst-routing-receipt-v2"

    legacy = build_routing_receipt(
        prompt="legacy receipt",
        event="receipt_test",
        steps=1,
        pipeline=result["runtime"]["pipeline"],
        session_turn=1,
        session_state_hash=receipt["payload"]["session"]["state_record_hash"],
        memory_record_id=receipt["payload"]["session"]["memory_binding"]["record_id"],
    )
    RoutingReceiptLedger(str(session_path)).persist(legacy)

    verification = RoutingReceiptLedger(str(session_path), read_only=True).verify(
        legacy["receipt_id"]
    )
    assert legacy["schema"] == "spst-routing-receipt-v1"
    assert verification["verified"] is True
