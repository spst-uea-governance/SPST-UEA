import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from spst_runtime.action_manifest import ActionManifestLedger
from spst_runtime.chat_bridge import preview_context, run_chat_turn
from spst_runtime.context_review import (
    SEMANTIC_REVIEW_SCOPE,
    ContextSemanticReviewLedger,
)
from spst_runtime.evidence_context import (
    EVIDENCE_CONTEXT_METADATA_KEY,
    EvidenceContextCompiler,
    EvidenceContextError,
)


NOW = datetime(2026, 7, 22, tzinfo=timezone.utc)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path, *, runtime_profile: bool = False) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "SPST Test")
    _git(repository, "config", "user.email", "spst@example.invalid")
    _git(repository, "config", "core.autocrlf", "false")
    (repository / "architecture.md").write_text(
        "Context compiler decisions remain source bound.\n",
        encoding="utf-8",
    )
    (repository / "unrelated.txt").write_text("stable\n", encoding="utf-8")
    if runtime_profile:
        runtime = repository / "runtime"
        package = runtime / "spst_runtime"
        package.mkdir(parents=True)
        (runtime / "pyproject.toml").write_text(
            "[tool.ruff]\nline-length = 100\n",
            encoding="utf-8",
        )
        (package / "__init__.py").write_text("", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "initial evidence")
    return repository


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "session.db", tmp_path / "memory.db"


def _route(repository: Path, session: Path, memory: Path, prompt: str) -> str:
    result = run_chat_turn(
        prompt,
        steps=1,
        profile="strict",
        session_path=str(session),
        memory_path=str(memory),
        repository_root=str(repository),
    )
    assert result["routing_receipt"]["verification"]["verified"] is True
    return str(result["routing_receipt"]["receipt_id"])


def _db_manifest(path: Path) -> dict[str, str]:
    return {
        candidate.name: hashlib.sha256(candidate.read_bytes()).hexdigest()
        for candidate in path.parent.glob(f"{path.name}*")
        if candidate.is_file()
    }


def _support(
    compiler: EvidenceContextCompiler,
    compiled: dict,
    *,
    decision: str = "supported",
) -> dict:
    record_id = compiled["memory_record"]["id"]
    record = next(record for record in compiler.memory.list_all() if record.id == record_id)
    structural = compiler.verifier.verify_record(record, now=NOW)
    artifact = compiled["artifact"]
    return ContextSemanticReviewLedger(compiler.memory.repository.path).review(
        record,
        structural,
        {
            "reviewer_id": "human-reviewer-local",
            "reviewer_kind": "human",
            "review_scope": SEMANTIC_REVIEW_SCOPE,
            "decision": decision,
            "artifact_sha256": artifact["artifact_sha256"],
            "source_sha256": artifact["source"]["source_sha256"],
            "review_note": "The bound source supports the bounded artifact statement.",
        },
        now=NOW,
    )


def test_file_artifact_survives_unrelated_change_but_rejects_source_change(
    tmp_path: Path,
):
    repository = _repository(tmp_path)
    session, memory = _paths(tmp_path)
    receipt_id = _route(
        repository,
        session,
        memory,
        "Compile the source-bound context architecture decision.",
    )
    compiler = EvidenceContextCompiler(
        repository_root=repository,
        session_path=session,
        memory_path=memory,
    )
    compiled = compiler.compile_repository_file(
        producer_receipt_id=receipt_id,
        artifact_kind="architecture_decision",
        title="Source-scoped context validity",
        statement=(
            "Repository-file context remains eligible across unrelated changes and "
            "fails closed when its source changes."
        ),
        relative_path="architecture.md",
        tags=["arch-02", "context compiler"],
        now=NOW,
    )
    assert compiled["verification"]["verified"] is True
    unreviewed = preview_context(
        "source-scoped context validity unrelated changes",
        session_path=str(session),
        memory_path=str(memory),
        repository_root=str(repository),
    )
    assert not any(
        item["source"] == "evidence_context_compiler" for item in unreviewed["items"]
    )
    assert unreviewed["selection"]["rejection_reasons"][
        "artifact_semantic_review_missing"
    ] == 1
    _support(compiler, compiled)
    session_before = _db_manifest(session)
    memory_before = _db_manifest(memory)

    packet = preview_context(
        "source-scoped context validity unrelated changes",
        session_path=str(session),
        memory_path=str(memory),
        repository_root=str(repository),
    )
    assert packet["status"] == "ready"
    artifact_item = next(
        item for item in packet["items"] if item["source"] == "evidence_context_compiler"
    )
    assert artifact_item["kind"] == "architecture_decision"
    assert artifact_item["source_trust"] == (
        "human_supported_source_verified_untrusted"
    )
    assert artifact_item["origin"]["binding_type"] == "evidence_context_artifact"
    assert artifact_item["origin"]["source_binding"]["current_match"] is True
    assert artifact_item["origin"]["semantic_review"]["decision"] == "supported"
    assert artifact_item["origin"]["semantic_review"][
        "human_identity_cryptographically_verified"
    ] is False
    assert _db_manifest(session) == session_before
    assert _db_manifest(memory) == memory_before

    (repository / "unrelated-new.txt").write_text("unrelated edit\n", encoding="utf-8")
    unrelated_packet = preview_context(
        "source-scoped context validity unrelated changes",
        session_path=str(session),
        memory_path=str(memory),
        repository_root=str(repository),
    )
    assert unrelated_packet["status"] == "ready"
    unrelated_artifact = next(
        item
        for item in unrelated_packet["items"]
        if item["source"] == "evidence_context_compiler"
    )
    assert unrelated_artifact["origin"]["repository"]["current_match"] is False
    assert unrelated_artifact["origin"]["source_binding"]["current_match"] is True

    (repository / "architecture.md").write_text("changed source\n", encoding="utf-8")
    stale_packet = preview_context(
        "source-scoped context validity unrelated changes",
        session_path=str(session),
        memory_path=str(memory),
        repository_root=str(repository),
    )
    assert not any(
        item["source"] == "evidence_context_compiler" for item in stale_packet["items"]
    )
    assert stale_packet["selection"]["rejection_reasons"][
        "artifact_source_file_digest_mismatch"
    ] == 1
    assert _db_manifest(session) == session_before
    assert _db_manifest(memory) == memory_before


def test_compiler_rejects_missing_receipt_unsupported_kind_and_unsafe_path(
    tmp_path: Path,
):
    repository = _repository(tmp_path)
    session, memory = _paths(tmp_path)
    receipt_id = _route(repository, session, memory, "Compile a bounded code fact.")
    compiler = EvidenceContextCompiler(
        repository_root=repository,
        session_path=session,
        memory_path=memory,
    )
    common = {
        "title": "Bounded code fact",
        "statement": "The source path and file bytes are bound.",
        "relative_path": "architecture.md",
        "now": NOW,
    }

    with pytest.raises(EvidenceContextError, match="artifact_producer_receipt_unverified"):
        compiler.compile_repository_file(
            producer_receipt_id="0" * 64,
            artifact_kind="code_fact",
            **common,
        )
    with pytest.raises(EvidenceContextError, match="artifact_kind_unsupported"):
        compiler.compile_repository_file(
            producer_receipt_id=receipt_id,
            artifact_kind="free_form_memory",
            **common,
        )
    with pytest.raises(EvidenceContextError, match="artifact_source_path_invalid"):
        compiler.compile_repository_file(
            producer_receipt_id=receipt_id,
            artifact_kind="code_fact",
            title="Unsafe path",
            statement="Traversal must not be compiled.",
            relative_path="../outside.txt",
            now=NOW,
        )


def test_artifact_tampering_and_unreceipted_records_fail_closed(tmp_path: Path):
    repository = _repository(tmp_path)
    session, memory = _paths(tmp_path)
    receipt_id = _route(repository, session, memory, "Compile tamper-resistant context.")
    compiler = EvidenceContextCompiler(
        repository_root=repository,
        session_path=session,
        memory_path=memory,
    )
    compiled = compiler.compile_repository_file(
        producer_receipt_id=receipt_id,
        artifact_kind="code_fact",
        title="Tamper-resistant projection",
        statement="Artifact fields are covered by a canonical digest.",
        relative_path="architecture.md",
        now=NOW,
    )
    record = json.loads(json.dumps(compiled["memory_record"]))
    record["metadata"][EVIDENCE_CONTEXT_METADATA_KEY]["statement"] = "forged"
    verification = compiler.verifier.verify_record(record, now=NOW)
    assert verification == {"verified": False, "reason": "artifact_digest_mismatch"}

    unreceipted = compiler.memory.remember(
        '{"statement":"unreceipted context"}',
        kind="code_fact",
        source="evidence_context_compiler",
        confidence=0.95,
        salience=0.9,
        created_turn=99,
        metadata={},
    )
    direct = compiler.verifier.verify_record(unreceipted, now=NOW)
    assert direct == {"verified": False, "reason": "artifact_metadata_missing"}


def test_action_evidence_requires_verified_success_and_exact_repository_state(
    tmp_path: Path,
):
    repository = _repository(tmp_path, runtime_profile=True)
    session, memory = _paths(tmp_path)
    receipt_id = _route(repository, session, memory, "Verify the runtime lint evidence.")
    action = ActionManifestLedger(str(session)).run_fixed_profile(
        receipt_id,
        "ruff",
        repository_root=str(repository),
    )
    verification = action["verification"]
    assert verification["execution_verified"] is True
    assert verification["successful"] is True

    compiler = EvidenceContextCompiler(
        repository_root=repository,
        session_path=session,
        memory_path=memory,
    )
    compiled = compiler.compile_action_result(
        producer_receipt_id=receipt_id,
        artifact_kind="test_evidence",
        title="Ruff verification passed",
        statement="The fixed Ruff profile completed successfully for this repository state.",
        action_id=action["manifest"]["action_id"],
        now=NOW,
    )
    _support(compiler, compiled)
    packet = preview_context(
        "Ruff verification passed repository state",
        session_path=str(session),
        memory_path=str(memory),
        repository_root=str(repository),
    )
    assert packet["status"] == "ready"
    assert any(item["kind"] == "test_evidence" for item in packet["items"])

    (repository / "after-test.txt").write_text("changed\n", encoding="utf-8")
    stale = preview_context(
        "Ruff verification passed repository state",
        session_path=str(session),
        memory_path=str(memory),
        repository_root=str(repository),
    )
    assert not any(item["kind"] == "test_evidence" for item in stale["items"])
    assert stale["selection"]["rejection_reasons"][
        "artifact_source_action_repository_stale"
    ] == 1


def test_ancestor_commit_artifact_and_explicit_supersession(tmp_path: Path):
    repository = _repository(tmp_path)
    session, memory = _paths(tmp_path)
    receipt_id = _route(repository, session, memory, "Record an architecture commit decision.")
    compiler = EvidenceContextCompiler(
        repository_root=repository,
        session_path=session,
        memory_path=memory,
    )
    commit_artifact = compiler.compile_git_commit(
        producer_receipt_id=receipt_id,
        artifact_kind="architecture_decision",
        title="Historical architecture commit",
        statement="The initial evidence commit records the source-bound architecture decision.",
        revision="HEAD",
        now=NOW,
    )
    _support(compiler, commit_artifact)

    (repository / "unrelated.txt").write_text("descendant\n", encoding="utf-8")
    _git(repository, "add", "unrelated.txt")
    _git(repository, "commit", "-qm", "unrelated descendant")
    packet = preview_context(
        "historical architecture commit source-bound decision",
        session_path=str(session),
        memory_path=str(memory),
        repository_root=str(repository),
    )
    assert any(
        item["origin"].get("artifact", {}).get("artifact_sha256")
        == commit_artifact["artifact"]["artifact_sha256"]
        for item in packet["items"]
    )

    current_receipt = _route(
        repository,
        session,
        memory,
        "Supersede the architecture context statement.",
    )
    first = compiler.memory.list_all()
    old_record = next(
        record
        for record in first
        if record.metadata.get(EVIDENCE_CONTEXT_METADATA_KEY, {}).get("artifact_sha256")
        == commit_artifact["artifact"]["artifact_sha256"]
    )
    refreshed_compiler = EvidenceContextCompiler(
        repository_root=repository,
        session_path=session,
        memory_path=memory,
    )
    replacement = refreshed_compiler.compile_git_commit(
        producer_receipt_id=current_receipt,
        artifact_kind="architecture_decision",
        title="Current architecture commit",
        statement="A newer source-bound architecture statement explicitly supersedes the old one.",
        revision="HEAD",
        supersedes_artifact_sha256=commit_artifact["artifact"]["artifact_sha256"],
        now=NOW,
    )
    records = refreshed_compiler.memory.list_all()
    retired = next(record for record in records if record.id == old_record.id)
    assert retired.status == "retired"
    assert retired.superseded_by == replacement["memory_record"]["id"]


def test_evidence_context_cli_compiles_and_verifies_record(tmp_path: Path):
    repository = _repository(tmp_path)
    session, memory = _paths(tmp_path)
    receipt_id = _route(repository, session, memory, "Compile through the context CLI.")
    compile_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "spst_runtime.evidence_context_bridge",
            "compile-file",
            "--repository-root",
            str(repository),
            "--session-db",
            str(session),
            "--memory-db",
            str(memory),
            "--producer-receipt",
            receipt_id,
            "--kind",
            "code_fact",
            "--title",
            "CLI-bound context",
            "--statement",
            "The evidence-context CLI persists a source-bound record.",
            "--source-file",
            "architecture.md",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert compile_result.returncode == 0, compile_result.stderr
    compiled = json.loads(compile_result.stdout)
    record_id = compiled["memory_record"]["id"]

    verify_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "spst_runtime.evidence_context_bridge",
            "verify",
            "--repository-root",
            str(repository),
            "--session-db",
            str(session),
            "--memory-db",
            str(memory),
            "--record-id",
            record_id,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert verify_result.returncode == 0, verify_result.stderr
    unreviewed = json.loads(verify_result.stdout)
    assert unreviewed["verified"] is True
    assert unreviewed["semantic_support"]["verified"] is False
    assert unreviewed["semantic_support"]["reason"] == (
        "artifact_semantic_review_missing"
    )

    review_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "spst_runtime.evidence_context_bridge",
            "review",
            "--repository-root",
            str(repository),
            "--session-db",
            str(session),
            "--memory-db",
            str(memory),
            "--record-id",
            record_id,
            "--reviewer-id",
            "human-reviewer-local",
            "--decision",
            "supported",
            "--artifact-sha256",
            compiled["artifact"]["artifact_sha256"],
            "--source-sha256",
            compiled["artifact"]["source_sha256"],
            "--review-note",
            "The exact bound source supports this bounded CLI statement.",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert review_result.returncode == 0, review_result.stderr
    reviewed = json.loads(review_result.stdout)
    assert reviewed["semantic_support"]["verified"] is True
    assert reviewed["semantic_support"]["semantic_review"][
        "human_identity_cryptographically_verified"
    ] is False

    intervention_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "spst_runtime.evidence_context_bridge",
            "intervention",
            "--repository-root",
            str(repository),
            "--session-db",
            str(session),
            "--memory-db",
            str(memory),
            "--artifact-sha256",
            compiled["artifact"]["artifact_sha256"],
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert intervention_result.returncode == 0, intervention_result.stderr
    intervention = json.loads(intervention_result.stdout)
    assert intervention["binding"]["semantic_support"] == (
        "human_self_attested_supported"
    )
