import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Any

import pytest

from spst_runtime.context_review import (
    CONTEXT_AUTHORITY,
    CONTEXT_INSTRUCTION_BOUNDARY,
    CONTEXT_INTERVENTION_BINDING_SCHEMA,
    CONTEXT_INTERVENTION_SCHEMA,
    SEMANTIC_SUPPORT_STATUS,
)
from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.live_pairing import canonical_live_pair_hash
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.process_transport import (
    PROCESS_PROVIDER_OBSERVATION_SOURCE,
    DurableProcessProviderStore,
    ProcessIsolatedTransportAdapter,
    ProcessTransportError,
    build_recovery_lease_resource_id,
    build_recovery_supervisor_authority,
)
from spst_runtime.recovery_authority import (
    RecoveryAuthorityError,
    SupervisorAttestationLedger,
    generate_recovery_authority_pki,
    generate_supervisor_attestation_key,
    issue_recovery_authority_grant,
    verify_recovery_authority_grant,
)
from spst_runtime.recovery_authority_state import (
    build_recovery_key_custody_evidence,
    issue_recovery_authority_state,
    issue_recovery_rollback_anchor,
)
from spst_runtime.provider_observation import build_provider_request_binding
from spst_runtime.provider_transport import (
    TRANSPORT_QUERY_PREFIX,
    build_provider_transport_request,
    build_recovery_authority,
)
from spst_runtime.real_paired_outcome import (
    PROGRAM_RECORD_PREFIX,
    PROVIDER_EXECUTION_AUTHORITY_SCHEMA,
    RealPairedOutcomeProgram,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[1]


def _intervention() -> dict[str, Any]:
    artifact_sha256 = "1" * 64
    source_sha256 = "2" * 64
    receipt_sha256 = "3" * 64
    policy_version = "spst-uea-covenant-v1"
    projection = {
        "artifact_sha256": artifact_sha256,
        "source": {"source_sha256": source_sha256},
        "producer_receipt_id": receipt_sha256,
        "policy_version": policy_version,
        "authority": CONTEXT_AUTHORITY,
        "statement": "Bound process-isolated recovery fixture.",
    }
    text = json.dumps(projection, sort_keys=True, separators=(",", ":"))
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    binding_unsigned = {
        "schema": CONTEXT_INTERVENTION_BINDING_SCHEMA,
        "artifact_sha256": artifact_sha256,
        "artifact_kind": "architecture_decision",
        "source_sha256": source_sha256,
        "producer_receipt_id": receipt_sha256,
        "memory_record_id": "process-recovery-memory-record",
        "memory_record_sha256": "4" * 64,
        "projection_sha256": text_sha256,
        "policy_version": policy_version,
        "semantic_review_id": "process-recovery-semantic-review",
        "semantic_review_sha256": "5" * 64,
        "semantic_support": SEMANTIC_SUPPORT_STATUS,
        "authority": CONTEXT_AUTHORITY,
    }
    binding = {
        **binding_unsigned,
        "binding_sha256": canonical_live_pair_hash(binding_unsigned),
    }
    unsigned = {
        "schema": CONTEXT_INTERVENTION_SCHEMA,
        "binding": binding,
        "context_item": {
            "authority": CONTEXT_AUTHORITY,
            "instruction_boundary": CONTEXT_INSTRUCTION_BOUNDARY,
            "text": text,
            "text_sha256": text_sha256,
        },
    }
    return {**unsigned, "intervention_sha256": canonical_live_pair_hash(unsigned)}


def _task(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "prompt": f"Return exact JSON for {task_id}.",
        "domain": "process-isolated-recovery",
        "split": "holdout",
        "required_markers": [],
        "expected_json_keys": ["answer"],
        "quality_rubric": {
            "schema": "task-specific-json-rubric-v1",
            "criteria": [
                {
                    "id": "answer",
                    "json_pointer": "/answer",
                    "expected": 1,
                    "weight": 1.0,
                }
            ],
        },
        "consent": {
            "granted": True,
            "scope": "local_operational_evaluation",
            "retention_until": "2099-12-31",
        },
    }


def _fixture(
    tmp_path: Path,
    *,
    recovery_authority_trust_anchor: dict[str, Any] | None = None,
) -> tuple[
    SQLiteRepository,
    RealPairedOutcomeProgram,
    dict[str, Any],
    dict[str, Any],
    Path,
    Path,
]:
    database = tmp_path / "process-recovery.db"
    provider_database = tmp_path / "process-provider.db"
    repository = SQLiteRepository(str(database))
    corpus = OperationalEvaluationCorpus(repository)
    task_ids = [f"process-recovery-{index:02d}" for index in range(8)]
    for task_id in task_ids:
        assert corpus.register(_task(task_id))["status"] == "registered"
    intervention = _intervention()
    adapter = ProcessIsolatedTransportAdapter(
        provider_database,
        recovery_authority_trust_anchor=recovery_authority_trust_anchor,
    )
    program = RealPairedOutcomeProgram(
        repository,
        corpus,
        adapter,
        plan_nonce_factory=lambda: "a" * 64,
        scoring_nonce_factory=lambda: "b" * 64,
        attempt_nonce_factory=lambda: "c" * 64,
    )
    registered = program.register(
        {
            "baseline_candidate_id": "normal-control",
            "candidate_id": "spst-treatment",
            "context_intervention": intervention,
            "execution_authority": {
                "schema": PROVIDER_EXECUTION_AUTHORITY_SCHEMA,
                "external_provider_calls_authorized": False,
                "paid_provider_calls_authorized": False,
                "maximum_adapter_invocations": 32,
            },
            "study": {"workload_class": "test_fixture"},
            "task_ids": task_ids,
        }
    )
    assert registered["status"] == "registered"
    assert registered["provider"]["execution_environment"] == "local_process"
    assert registered["provider"]["process_isolated_recovery_supported"] is True
    assert len(registered["provider"]["transport_instance_sha256"]) == 64
    return (
        repository,
        program,
        intervention,
        registered,
        database,
        provider_database,
    )


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _bridge_arguments(
    command: str,
    *,
    database: Path,
    provider_database: Path,
    program_id: str,
    intervention_file: Path,
    authority_file: Path | None = None,
    commit_marker: Path | None = None,
    response_delay_ms: int = 0,
    failure_mode: str | None = None,
    authority_trust_file: Path | None = None,
) -> list[str]:
    arguments = [
        sys.executable,
        "-m",
        "spst_runtime.process_recovery_bridge",
        command,
        "--database",
        str(database),
        "--provider-db",
        str(provider_database),
        "--program-id",
        program_id,
        "--intervention-file",
        str(intervention_file),
    ]
    if authority_file is not None:
        arguments.extend(["--authority-file", str(authority_file)])
    if authority_trust_file is not None:
        arguments.extend(["--authority-trust-file", str(authority_trust_file)])
    if commit_marker is not None:
        arguments.extend(["--commit-marker", str(commit_marker)])
    if response_delay_ms:
        arguments.extend(["--response-delay-ms", str(response_delay_ms)])
    if failure_mode is not None:
        arguments.extend(["--failure-mode", failure_mode])
    return arguments


def _run_bridge(arguments: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        arguments,
        cwd=RUNTIME_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    value = json.loads(completed.stdout)
    assert isinstance(value, dict)
    return value


def _wait_for_marker(process: subprocess.Popen[str], marker: Path) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if marker.is_file():
            value = json.loads(marker.read_text(encoding="utf-8"))
            assert isinstance(value, dict)
            return value
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            raise AssertionError(stderr or stdout or "client exited before marker")
        time.sleep(0.05)
    process.kill()
    raise AssertionError("provider commit marker timeout")


def _crash_after_provider_commit(
    *,
    database: Path,
    provider_database: Path,
    program_id: str,
    intervention_file: Path,
    marker: Path,
    authority_trust_file: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    process = subprocess.Popen(
        _bridge_arguments(
            "execute",
            database=database,
            provider_database=provider_database,
            program_id=program_id,
            intervention_file=intervention_file,
            commit_marker=marker,
            response_delay_ms=3000,
            authority_trust_file=authority_trust_file,
        ),
        cwd=RUNTIME_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    committed = _wait_for_marker(process, marker)
    assert committed["state"] == "provider_committed"
    process.kill()
    process.communicate(timeout=10)
    assert process.returncode != 0
    return process.pid, committed


def _authority_file(
    tmp_path: Path,
    program: RealPairedOutcomeProgram,
    program_id: str,
) -> Path:
    status = program.get(program_id)
    assert status is not None
    assert status["status"] == "recovery_required"
    attempt_id = status["operational_hardening"]["attempt"]["attempt_id"]
    authority = build_recovery_authority(
        program_id=program_id,
        attempt_id=attempt_id,
        operator_id="human-process-recovery-reviewer",
    )
    path = tmp_path / "recovery-authority.json"
    _write_json(path, authority)
    return path


def test_durable_provider_deduplicates_concurrent_process_requests(tmp_path: Path):
    provider_database = tmp_path / "dedupe-provider.db"
    first = ProcessIsolatedTransportAdapter(provider_database)
    second = ProcessIsolatedTransportAdapter(provider_database)
    prompt = "Return exact JSON."
    base_context = {
        "instructions": "Bound process transport concurrency fixture.",
        "evaluation": {"mode": "process_dedupe", "case_id": "dedupe-01"},
    }
    logical = build_provider_request_binding(prompt, base_context)
    request = build_provider_transport_request(
        {
            "id": "RPOP-process-dedupe",
            "program_sha256": "1" * 64,
            "execution_plan": {"manifest_sha256": "2" * 64},
        },
        {
            "attempt_id": "RATT-process-dedupe",
            "attempt_sha256": "3" * 64,
            "state": "running",
        },
        call_ordinal=1,
        logical_request_binding=logical,
    )
    context = {**base_context, "provider_transport": request}

    def invoke(adapter: ProcessIsolatedTransportAdapter) -> dict[str, Any]:
        return asyncio.run(adapter.infer(prompt, context))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, (first, second)))
    assert results[0] == results[1]
    assert results[0]["provider_observation"]["observation_source"] == (
        PROCESS_PROVIDER_OBSERVATION_SOURCE
    )
    status = DurableProcessProviderStore(
        provider_database,
        read_only=True,
    ).status()
    assert status["record_count"] == 1
    assert status["total_execution_count"] == 1
    assert status["total_infer_request_count"] == 2
    assert status["integrity_verified"] is True


def test_fresh_client_recovers_committed_result_without_duplicate_inference(
    tmp_path: Path,
):
    repository, program, intervention, registered, database, provider_database = (
        _fixture(tmp_path)
    )
    intervention_file = tmp_path / "intervention.json"
    _write_json(intervention_file, intervention)
    first_client_pid, marker = _crash_after_provider_commit(
        database=database,
        provider_database=provider_database,
        program_id=registered["id"],
        intervention_file=intervention_file,
        marker=tmp_path / "provider-committed.json",
    )
    before = DurableProcessProviderStore(
        provider_database,
        read_only=True,
    ).status()
    assert before["record_count"] == 1
    assert before["total_execution_count"] == 1
    assert before["total_infer_request_count"] == 1
    authority_file = _authority_file(tmp_path, program, registered["id"])
    recovery = _run_bridge(
        _bridge_arguments(
            "recover",
            database=database,
            provider_database=provider_database,
            program_id=registered["id"],
            intervention_file=intervention_file,
            authority_file=authority_file,
        )
    )
    recovery_client_pid = int(recovery["client_pid"])
    assert recovery_client_pid != first_client_pid
    assert int(marker["worker_pid"]) != first_client_pid
    result = recovery["result"]
    assert result["status"] == "pending_human_review"
    assert result["operational_hardening"]["assurance"] == (
        "attempt_and_transport_bound"
    )
    assert result["operational_hardening"][
        "process_isolated_recovery_observed"
    ] is True
    assert result["outcome"][
        "process_isolated_recovery_mechanism_observed"
    ] is True
    after = DurableProcessProviderStore(
        provider_database,
        read_only=True,
    ).status()
    assert after["record_count"] == 32
    assert after["total_execution_count"] == 32
    assert after["total_infer_request_count"] == 32
    assert after["reconciliation_query_count"] == 1
    assert after["reconciliation_found_count"] == 1
    assert marker["worker_pid"] in after["creator_pids"]
    assert marker["worker_pid"] not in after["reconciliation_pids"]
    assert recovery_client_pid not in after["reconciliation_pids"]
    assert after["integrity_verified"] is True
    assert repository.verify_provenance()["valid"] is True


def test_process_exit_before_provider_commit_never_resumes_new_inference(
    tmp_path: Path,
):
    repository, program, intervention, registered, database, provider_database = (
        _fixture(tmp_path)
    )
    intervention_file = tmp_path / "intervention.json"
    _write_json(intervention_file, intervention)
    execution = _run_bridge(
        _bridge_arguments(
            "execute",
            database=database,
            provider_database=provider_database,
            program_id=registered["id"],
            intervention_file=intervention_file,
            commit_marker=tmp_path / "before-commit.json",
            failure_mode="before_commit_exit",
        )
    )
    assert execution["result"]["status"] == "recovery_required"
    before = DurableProcessProviderStore(
        provider_database,
        read_only=True,
    ).status()
    assert before["record_count"] == 0
    assert before["total_execution_count"] == 0
    authority_file = _authority_file(tmp_path, program, registered["id"])
    recovery = _run_bridge(
        _bridge_arguments(
            "recover",
            database=database,
            provider_database=provider_database,
            program_id=registered["id"],
            intervention_file=intervention_file,
            authority_file=authority_file,
        )
    )["result"]
    assert recovery["status"] == "recovery_required"
    assert recovery["recovery"]["reason"] == (
        "provider_transport_reconciliation_unresolved"
    )
    assert recovery["recovery"]["new_adapter_invocations"] == 0
    after = DurableProcessProviderStore(
        provider_database,
        read_only=True,
    ).status()
    assert after["record_count"] == 0
    assert after["total_execution_count"] == 0
    assert after["reconciliation_query_count"] == 1
    assert after["reconciliation_found_count"] == 0
    assert repository.verify_provenance()["valid"] is True


def test_recovery_rejects_substituted_provider_instance_before_query(
    tmp_path: Path,
):
    _, program, intervention, registered, database, provider_database = _fixture(
        tmp_path
    )
    intervention_file = tmp_path / "intervention.json"
    _write_json(intervention_file, intervention)
    _crash_after_provider_commit(
        database=database,
        provider_database=provider_database,
        program_id=registered["id"],
        intervention_file=intervention_file,
        marker=tmp_path / "provider-committed.json",
    )
    authority_file = _authority_file(tmp_path, program, registered["id"])
    substituted_database = tmp_path / "substituted-provider.db"
    recovery = _run_bridge(
        _bridge_arguments(
            "recover",
            database=database,
            provider_database=substituted_database,
            program_id=registered["id"],
            intervention_file=intervention_file,
            authority_file=authority_file,
        )
    )["result"]
    assert recovery["status"] == "blocked"
    assert recovery["reason"] == "real_paired_outcome_provider_contract_mismatch"
    original = DurableProcessProviderStore(
        provider_database,
        read_only=True,
    ).status()
    substituted = DurableProcessProviderStore(
        substituted_database,
        read_only=True,
    ).status()
    assert original["reconciliation_query_count"] == 0
    assert substituted["record_count"] == 0
    assert substituted["reconciliation_query_count"] == 0


def test_tampered_durable_provider_result_fails_closed_without_new_inference(
    tmp_path: Path,
):
    repository, program, intervention, registered, database, provider_database = (
        _fixture(tmp_path)
    )
    intervention_file = tmp_path / "intervention.json"
    _write_json(intervention_file, intervention)
    _crash_after_provider_commit(
        database=database,
        provider_database=provider_database,
        program_id=registered["id"],
        intervention_file=intervention_file,
        marker=tmp_path / "provider-committed.json",
    )
    with sqlite3.connect(provider_database) as connection:
        connection.execute("UPDATE provider_results SET result_json = '{}' ")
        connection.commit()
    authority_file = _authority_file(tmp_path, program, registered["id"])
    recovery = _run_bridge(
        _bridge_arguments(
            "recover",
            database=database,
            provider_database=provider_database,
            program_id=registered["id"],
            intervention_file=intervention_file,
            authority_file=authority_file,
        )
    )["result"]
    assert recovery["status"] == "blocked"
    assert recovery["reason"] == "real_paired_outcome_provider_unavailable"
    provider_status = DurableProcessProviderStore(
        provider_database,
        read_only=True,
    ).status()
    assert provider_status["record_count"] == 1
    assert provider_status["total_execution_count"] == 1
    assert provider_status["total_infer_request_count"] == 1
    assert provider_status["integrity_verified"] is False
    queries = asyncio.run(repository.load_prefix(TRANSPORT_QUERY_PREFIX))
    assert queries == {}
    assert repository.verify_provenance()["valid"] is True


def _authenticated_provider_call(
    provider_database: Path,
    *,
    provider_key_file: Path | None = None,
) -> tuple[ProcessIsolatedTransportAdapter, dict[str, Any]]:
    adapter = ProcessIsolatedTransportAdapter(
        provider_database,
        provider_authentication_key_file=provider_key_file,
    )
    prompt = "Return exact authenticated JSON."
    base_context = {
        "instructions": "Bound authenticated provider-store fixture.",
        "evaluation": {"mode": "authenticated_store", "case_id": "auth-01"},
    }
    logical = build_provider_request_binding(prompt, base_context)
    request = build_provider_transport_request(
        {
            "id": "RPOP-authenticated-store",
            "program_sha256": "6" * 64,
            "execution_plan": {"manifest_sha256": "7" * 64},
        },
        {
            "attempt_id": "RATT-authenticated-store",
            "attempt_sha256": "8" * 64,
            "state": "running",
        },
        call_ordinal=1,
        logical_request_binding=logical,
    )
    result = asyncio.run(
        adapter.infer(prompt, {**base_context, "provider_transport": request})
    )
    return adapter, result


def test_authenticated_store_rejects_metadata_rewrite_without_key(
    tmp_path: Path,
):
    provider_database = tmp_path / "authenticated-provider.db"
    adapter, _ = _authenticated_provider_call(provider_database)
    before = DurableProcessProviderStore(
        provider_database,
        authentication_key_file=adapter.provider_authentication_key_file,
        read_only=True,
    ).status()
    assert before["provider_store_authenticity_verified"] is True
    assert before["provider_identity_authenticated"] is False
    missing_key = tmp_path / "missing-provider.key"
    with pytest.raises(ProcessTransportError, match="key_unavailable"):
        DurableProcessProviderStore(
            provider_database,
            authentication_key_file=missing_key,
        )
    assert missing_key.exists() is False
    with sqlite3.connect(provider_database) as connection:
        connection.execute("UPDATE provider_results SET request_count = 99")
        connection.commit()
    after = DurableProcessProviderStore(
        provider_database,
        authentication_key_file=adapter.provider_authentication_key_file,
        read_only=True,
    ).status()
    assert after["integrity_verified"] is False
    assert after["reason"] == "process_provider_result_authentication_failed"


def test_provider_key_rotation_reauthenticates_rows_and_rejects_old_key(
    tmp_path: Path,
):
    provider_database = tmp_path / "rotation-provider.db"
    adapter, _ = _authenticated_provider_call(provider_database)
    old_key_file = adapter.provider_authentication_key_file
    store = DurableProcessProviderStore(
        provider_database,
        authentication_key_file=old_key_file,
    )
    previous_key_id = store.authentication_key_id
    new_key_file = tmp_path / "rotated-provider.key"
    rotation = store.rotate_authentication_key(new_key_file)
    assert rotation["status"] == "rotated"
    assert rotation["previous_key_id"] == previous_key_id
    assert rotation["authentication_key_id"] != previous_key_id
    assert rotation["provider_store_authenticity_verified"] is True
    old_status = DurableProcessProviderStore(
        provider_database,
        authentication_key_file=old_key_file,
        read_only=True,
    ).status()
    assert old_status["integrity_verified"] is False
    assert old_status["reason"] == "process_provider_result_authentication_failed"
    new_status = DurableProcessProviderStore(
        provider_database,
        authentication_key_file=new_key_file,
        read_only=True,
    ).status()
    assert new_status["integrity_verified"] is True
    assert new_status["authentication_key_id"] == rotation["authentication_key_id"]
    with pytest.raises(ProcessTransportError, match="instance_authentication_invalid"):
        ProcessIsolatedTransportAdapter(
            provider_database,
            provider_authentication_key_file=old_key_file,
        )


def test_recovery_lease_requires_expiry_authority_and_fences_stale_owner(
    tmp_path: Path,
):
    provider_database = tmp_path / "lease-provider.db"
    store = DurableProcessProviderStore(provider_database)
    resource_id = hashlib.sha256(b"lease-resource").hexdigest()
    first = store.acquire_recovery_lease(
        resource_id,
        "supervisor-one",
        ttl_ms=150,
    )
    with pytest.raises(ProcessTransportError, match="lease_active"):
        store.acquire_recovery_lease(
            resource_id,
            "supervisor-two",
            ttl_ms=500,
        )
    time.sleep(0.2)
    with pytest.raises(ProcessTransportError, match="authority_required"):
        store.acquire_recovery_lease(
            resource_id,
            "supervisor-two",
            ttl_ms=500,
        )
    authority = build_recovery_supervisor_authority(
        resource_id=resource_id,
        provider_instance_sha256=store.identity(),
        previous_owner_id="supervisor-one",
        expired_generation=int(first["generation"]),
        operator_id="human-recovery-operator",
    )
    second = store.acquire_recovery_lease(
        resource_id,
        "supervisor-two",
        ttl_ms=500,
        adoption_authority=authority,
    )
    assert second["adopted_orphan"] is True
    assert second["generation"] == 2
    with pytest.raises(ProcessTransportError, match="lease_fencing_failed"):
        store.release_recovery_lease(
            resource_id,
            "supervisor-one",
            generation=int(first["generation"]),
            lease_token=str(first["lease_token"]),
        )
    released = store.release_recovery_lease(
        resource_id,
        "supervisor-two",
        generation=int(second["generation"]),
        lease_token=str(second["lease_token"]),
    )
    assert released["status"] == "released"
    assert store.status()["orphan_adoption_count"] == 1


def test_recovery_lease_rewrite_is_not_adoptable(tmp_path: Path):
    provider_database = tmp_path / "tampered-lease-provider.db"
    store = DurableProcessProviderStore(provider_database)
    resource_id = hashlib.sha256(b"tampered-lease").hexdigest()
    first = store.acquire_recovery_lease(
        resource_id,
        "supervisor-one",
        ttl_ms=10_000,
    )
    with sqlite3.connect(provider_database) as connection:
        connection.execute(
            "UPDATE provider_recovery_leases SET expires_at_ms = 0"
        )
        connection.commit()
    authority = build_recovery_supervisor_authority(
        resource_id=resource_id,
        provider_instance_sha256=store.identity(),
        previous_owner_id="supervisor-one",
        expired_generation=int(first["generation"]),
        operator_id="human-recovery-operator",
    )
    with pytest.raises(ProcessTransportError, match="lease_authentication_failed"):
        store.acquire_recovery_lease(
            resource_id,
            "supervisor-two",
            ttl_ms=500,
            adoption_authority=authority,
        )
    assert store.status()["integrity_verified"] is False


def test_supervisor_status_is_read_only_and_missing_paths_remain_absent(
    tmp_path: Path,
):
    provider_database = tmp_path / "status-provider.db"
    store = DurableProcessProviderStore(provider_database)
    resource_id = hashlib.sha256(b"status-lease").hexdigest()
    store.acquire_recovery_lease(resource_id, "status-owner", ttl_ms=5000)
    key_file = store.authentication_key_file
    before_database = hashlib.sha256(provider_database.read_bytes()).hexdigest()
    before_key = hashlib.sha256(key_file.read_bytes()).hexdigest()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "spst_runtime.process_recovery_supervisor",
            "status",
            "--provider-db",
            str(provider_database),
            "--provider-key-file",
            str(key_file),
            "--resource-id",
            resource_id,
        ],
        cwd=RUNTIME_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    status = json.loads(completed.stdout)
    assert status["command"] == "status"
    assert status["lease"]["owner_id"] == "status-owner"
    assert hashlib.sha256(provider_database.read_bytes()).hexdigest() == (
        before_database
    )
    assert hashlib.sha256(key_file.read_bytes()).hexdigest() == before_key

    missing_database = tmp_path / "missing-status-provider.db"
    missing_key = tmp_path / "missing-status-provider.key"
    rejected = subprocess.run(
        [
            sys.executable,
            "-m",
            "spst_runtime.process_recovery_supervisor",
            "status",
            "--provider-db",
            str(missing_database),
            "--provider-key-file",
            str(missing_key),
            "--resource-id",
            resource_id,
        ],
        cwd=RUNTIME_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert rejected.returncode == 2
    assert missing_database.exists() is False
    assert missing_key.exists() is False


def test_arch09_program_remains_readable_as_legacy_unauthenticated_record(
    tmp_path: Path,
):
    repository, program, _, registered, _, _ = _fixture(tmp_path)
    stored = asyncio.run(
        repository.load(f"{PROGRAM_RECORD_PREFIX}{registered['id']}")
    )
    assert isinstance(stored, dict)
    legacy = json.loads(json.dumps(stored))
    authentication_fields = (
        "provider_store_authenticated",
        "provider_store_authentication_schema",
        "provider_store_authentication_key_id",
        "leased_recovery_supervisor_supported",
        "recovery_lease_schema",
        "recovery_supervisor_authority_schema",
        "signed_recovery_authority_supported",
        "recovery_authority_grant_schema",
        "recovery_authority_trust_schema",
        "recovery_authority_trust_anchor",
        "recovery_authority_trust_anchor_sha256",
        "supervisor_attestation_schema",
    )
    for field in authentication_fields:
        legacy["provider"].pop(field, None)
        legacy["operational_hardening"].pop(field, None)
    legacy["provider"]["process_transport_protocol"] = (
        "subprocess-stdio-sqlite-v1"
    )
    legacy["operational_hardening"]["process_transport_protocol"] = (
        "subprocess-stdio-sqlite-v1"
    )
    unsigned = {
        key: value
        for key, value in legacy.items()
        if key not in {"id", "program_sha256"}
    }
    legacy_digest = hashlib.sha256(
        json.dumps(
            unsigned,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    legacy["program_sha256"] = legacy_digest
    legacy["id"] = f"RPOP-{legacy_digest[:16]}"
    assert program._registration_reason(legacy, legacy["id"]) is None


def _supervisor_arguments(
    *,
    database: Path,
    provider_database: Path,
    provider_key_file: Path,
    program_id: str,
    intervention_file: Path,
    recovery_authority_file: Path,
    owner_id: str,
    ttl_ms: int,
    lease_marker: Path | None = None,
    post_lease_delay_ms: int = 0,
    adoption_authority_file: Path | None = None,
    authority_trust_file: Path | None = None,
    authority_grant_file: Path | None = None,
    supervisor_signing_key_file: Path | None = None,
    authority_state_file: Path | None = None,
    rollback_anchor_file: Path | None = None,
) -> list[str]:
    arguments = [
        sys.executable,
        "-m",
        "spst_runtime.process_recovery_supervisor",
        "recover",
        "--database",
        str(database),
        "--provider-db",
        str(provider_database),
        "--provider-key-file",
        str(provider_key_file),
        "--program-id",
        program_id,
        "--intervention-file",
        str(intervention_file),
        "--recovery-authority-file",
        str(recovery_authority_file),
        "--lease-owner",
        owner_id,
        "--lease-ttl-ms",
        str(ttl_ms),
    ]
    if lease_marker is not None:
        arguments.extend(["--lease-marker", str(lease_marker)])
    if post_lease_delay_ms:
        arguments.extend(["--post-lease-delay-ms", str(post_lease_delay_ms)])
    if adoption_authority_file is not None:
        arguments.extend(
            ["--adoption-authority-file", str(adoption_authority_file)]
        )
    if authority_trust_file is not None:
        arguments.extend(["--authority-trust-file", str(authority_trust_file)])
    if authority_grant_file is not None:
        arguments.extend(["--authority-grant-file", str(authority_grant_file)])
    if supervisor_signing_key_file is not None:
        arguments.extend(
            ["--supervisor-signing-key-file", str(supervisor_signing_key_file)]
        )
    if authority_state_file is not None:
        arguments.extend(["--authority-state-file", str(authority_state_file)])
    if rollback_anchor_file is not None:
        arguments.extend(["--rollback-anchor-file", str(rollback_anchor_file)])
    return arguments


def test_killed_supervisor_requires_expired_lease_adoption_without_duplicate_call(
    tmp_path: Path,
):
    repository, program, intervention, registered, database, provider_database = (
        _fixture(tmp_path)
    )
    provider_key_file = Path(f"{provider_database}.provider_key")
    intervention_file = tmp_path / "intervention.json"
    _write_json(intervention_file, intervention)
    _crash_after_provider_commit(
        database=database,
        provider_database=provider_database,
        program_id=registered["id"],
        intervention_file=intervention_file,
        marker=tmp_path / "provider-committed.json",
    )
    recovery_authority_file = _authority_file(
        tmp_path,
        program,
        registered["id"],
    )
    lease_marker = tmp_path / "supervisor-lease.json"
    first_supervisor = subprocess.Popen(
        _supervisor_arguments(
            database=database,
            provider_database=provider_database,
            provider_key_file=provider_key_file,
            program_id=registered["id"],
            intervention_file=intervention_file,
            recovery_authority_file=recovery_authority_file,
            owner_id="supervisor-one",
            ttl_ms=800,
            lease_marker=lease_marker,
            post_lease_delay_ms=5000,
        ),
        cwd=RUNTIME_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    marker = _wait_for_marker(first_supervisor, lease_marker)
    lease = marker["lease"]
    provider_store = DurableProcessProviderStore(
        provider_database,
        authentication_key_file=provider_key_file,
    )
    with pytest.raises(ProcessTransportError, match="process_recovery_lease_active"):
        provider_store.acquire_recovery_lease(
            str(lease["resource_id"]),
            "supervisor-two",
            ttl_ms=1000,
        )

    first_supervisor.kill()
    first_supervisor.communicate(timeout=10)
    assert first_supervisor.returncode != 0

    remaining_ms = int(lease["expires_at_ms"]) - (time.time_ns() // 1_000_000)
    if remaining_ms >= 0:
        time.sleep((remaining_ms + 50) / 1000)
    adoption_authority = build_recovery_supervisor_authority(
        resource_id=str(lease["resource_id"]),
        provider_instance_sha256=str(
            registered["provider"]["transport_instance_sha256"]
        ),
        previous_owner_id="supervisor-one",
        expired_generation=int(lease["generation"]),
        operator_id="human-recovery-operator",
    )
    adoption_authority_file = tmp_path / "adoption-authority.json"
    _write_json(adoption_authority_file, adoption_authority)
    recovered = _run_bridge(
        _supervisor_arguments(
            database=database,
            provider_database=provider_database,
            provider_key_file=provider_key_file,
            program_id=registered["id"],
            intervention_file=intervention_file,
            recovery_authority_file=recovery_authority_file,
            owner_id="supervisor-two",
            ttl_ms=5000,
            adoption_authority_file=adoption_authority_file,
        )
    )
    assert recovered["lease"]["adopted_orphan"] is True
    assert recovered["lease_release"]["status"] == "released"
    assert recovered["result"]["status"] == "pending_human_review"
    provider_status = DurableProcessProviderStore(
        provider_database,
        authentication_key_file=provider_key_file,
        read_only=True,
    ).status()
    assert provider_status["record_count"] == 32
    assert provider_status["total_execution_count"] == 32
    assert provider_status["total_infer_request_count"] == 32
    assert provider_status["orphan_adoption_count"] == 1
    assert provider_status["provider_store_authenticity_verified"] is True
    assert repository.verify_provenance()["valid"] is True


def _pki_material(
    tmp_path: Path,
    *,
    authority_state_required: bool = False,
) -> dict[str, Any]:
    now_ms = time.time_ns() // 1_000_000
    root_private = tmp_path / "authority-root.private.pem"
    operator_private = tmp_path / "recovery-operator.private.pem"
    trust, certificate = generate_recovery_authority_pki(
        root_private,
        operator_private,
        operator_id="local-recovery-operator",
        valid_from_ms=now_ms - 1000,
        valid_until_ms=now_ms + 600_000,
        authority_state_required=authority_state_required,
    )
    trust_file = tmp_path / "authority-trust.json"
    certificate_file = tmp_path / "operator-certificate.json"
    _write_json(trust_file, trust)
    _write_json(certificate_file, certificate)
    material = {
        "root_private": root_private,
        "operator_private": operator_private,
        "trust": trust,
        "trust_file": trust_file,
        "certificate": certificate,
        "certificate_file": certificate_file,
    }
    if authority_state_required:
        custody = build_recovery_key_custody_evidence(trust["root_key_id"])
        state = issue_recovery_authority_state(
            trust,
            root_private,
            custody,
            generation=1,
            issued_at_ms=now_ms - 100,
            valid_until_ms=now_ms + 600_000,
            minimum_accepted_time_ms=now_ms - 100,
        )
        anchor = issue_recovery_rollback_anchor(trust, root_private, state)
        state_file = tmp_path / "authority-state.json"
        anchor_file = tmp_path / "rollback-anchor.json"
        _write_json(state_file, state)
        _write_json(anchor_file, anchor)
        material.update(
            {
                "custody": custody,
                "state": state,
                "state_file": state_file,
                "anchor": anchor,
                "anchor_file": anchor_file,
            }
        )
    return material


def _signed_recovery_material(
    tmp_path: Path,
    *,
    program: RealPairedOutcomeProgram,
    registered: dict[str, Any],
    intervention: dict[str, Any],
    pki: dict[str, Any],
    owner_id: str,
    suffix: str,
    previous_owner_id: str | None = None,
    expired_generation: int | None = None,
) -> dict[str, Any]:
    status = program.get(str(registered["id"]))
    assert status is not None
    assert status["status"] == "recovery_required"
    attempt_id = status["operational_hardening"]["attempt"]["attempt_id"]
    recovery_authority = build_recovery_authority(
        program_id=str(registered["id"]),
        attempt_id=str(attempt_id),
        operator_id="local-recovery-operator",
    )
    recovery_authority_file = tmp_path / f"transport-authority-{suffix}.json"
    _write_json(recovery_authority_file, recovery_authority)
    supervisor_private = tmp_path / f"supervisor-{suffix}.private.pem"
    supervisor_public = generate_supervisor_attestation_key(supervisor_private)
    resource_id = build_recovery_lease_resource_id(
        program_id=str(registered["id"]),
        attempt_id=str(attempt_id),
        provider_instance_sha256=str(
            registered["provider"]["transport_instance_sha256"]
        ),
    )
    grant = issue_recovery_authority_grant(
        pki["trust"],
        pki["certificate"],
        pki["operator_private"],
        supervisor_public,
        program_id=str(registered["id"]),
        program_sha256=str(registered["program_sha256"]),
        attempt_id=str(attempt_id),
        provider_instance_sha256=str(
            registered["provider"]["transport_instance_sha256"]
        ),
        lease_resource_id=resource_id,
        lease_owner_id=owner_id,
        transport_recovery_authority_sha256=str(
            recovery_authority["authority_sha256"]
        ),
        context_intervention_sha256=str(intervention["intervention_sha256"]),
        previous_owner_id=previous_owner_id,
        expired_generation=expired_generation,
        authority_state=pki.get("state"),
        rollback_anchor=pki.get("anchor"),
    )
    grant_file = tmp_path / f"recovery-grant-{suffix}.json"
    _write_json(grant_file, grant)
    return {
        "attempt_id": attempt_id,
        "resource_id": resource_id,
        "recovery_authority": recovery_authority,
        "recovery_authority_file": recovery_authority_file,
        "supervisor_private": supervisor_private,
        "supervisor_public": supervisor_public,
        "grant": grant,
        "grant_file": grant_file,
    }


def test_recovery_authority_pki_rejects_tamper_expiry_and_wrong_root(
    tmp_path: Path,
):
    pki = _pki_material(tmp_path / "primary")
    supervisor_private = tmp_path / "primary" / "supervisor.private.pem"
    supervisor_public = generate_supervisor_attestation_key(supervisor_private)
    now_ms = time.time_ns() // 1_000_000
    grant = issue_recovery_authority_grant(
        pki["trust"],
        pki["certificate"],
        pki["operator_private"],
        supervisor_public,
        program_id="RPOP-pki",
        program_sha256="1" * 64,
        attempt_id="RATT-pki",
        provider_instance_sha256="2" * 64,
        lease_resource_id="3" * 64,
        lease_owner_id="supervisor-pki",
        transport_recovery_authority_sha256="4" * 64,
        context_intervention_sha256="5" * 64,
        issued_at_ms=now_ms,
        expires_at_ms=now_ms + 10_000,
        nonce="pki-grant-nonce",
    )
    projection, reason = verify_recovery_authority_grant(
        grant,
        pki["trust"],
        expected={"lease_owner_id": "supervisor-pki"},
        verification_time_ms=now_ms + 1,
    )
    assert reason is None
    assert projection is not None and projection["verified"] is True

    tampered = json.loads(json.dumps(grant))
    tampered["claims"]["lease_owner_id"] = "attacker"
    tampered["claims_sha256"] = _canonical_digest(tampered["claims"])
    tampered_unsigned = {
        key: value for key, value in tampered.items() if key != "grant_sha256"
    }
    tampered["grant_sha256"] = _canonical_digest(tampered_unsigned)
    assert verify_recovery_authority_grant(
        tampered,
        pki["trust"],
        verification_time_ms=now_ms + 1,
    )[1] == "signed_recovery_authority_signature_invalid"
    assert verify_recovery_authority_grant(
        grant,
        pki["trust"],
        verification_time_ms=now_ms + 10_001,
    )[1] == "signed_recovery_authority_binding_invalid"

    other = _pki_material(tmp_path / "other")
    assert verify_recovery_authority_grant(
        grant,
        other["trust"],
        verification_time_ms=now_ms + 1,
    )[1] == "recovery_operator_certificate_signature_invalid"


def test_signed_program_rejects_unsigned_recovery_and_store_trust_downgrade(
    tmp_path: Path,
):
    pki = _pki_material(tmp_path)
    repository, program, intervention, registered, database, provider_database = (
        _fixture(
            tmp_path,
            recovery_authority_trust_anchor=pki["trust"],
        )
    )
    intervention_file = tmp_path / "intervention.json"
    _write_json(intervention_file, intervention)
    _crash_after_provider_commit(
        database=database,
        provider_database=provider_database,
        program_id=registered["id"],
        intervention_file=intervention_file,
        marker=tmp_path / "provider-committed.json",
        authority_trust_file=pki["trust_file"],
    )
    material = _signed_recovery_material(
        tmp_path,
        program=program,
        registered=registered,
        intervention=intervention,
        pki=pki,
        owner_id="signed-supervisor",
        suffix="unsigned-reject",
    )
    rejected = program.recover(
        registered["id"],
        context_intervention=intervention,
        recovery_authority=material["recovery_authority"],
    )
    assert rejected["status"] == "blocked"
    assert rejected["reason"] == "signed_recovery_authority_required"
    provider_key_file = Path(f"{provider_database}.provider_key")
    downgraded = DurableProcessProviderStore(
        provider_database,
        authentication_key_file=provider_key_file,
    )
    with pytest.raises(ProcessTransportError, match="trust_unavailable"):
        downgraded.acquire_recovery_lease(
            str(material["resource_id"]),
            "signed-supervisor",
            ttl_ms=1000,
            adoption_authority=build_recovery_supervisor_authority(
                resource_id=str(material["resource_id"]),
                provider_instance_sha256=str(
                    registered["provider"]["transport_instance_sha256"]
                ),
                previous_owner_id="previous",
                expired_generation=1,
                operator_id="legacy-operator",
            ),
        )
    assert b"BEGIN PRIVATE KEY" not in database.read_bytes()
    assert b"BEGIN PRIVATE KEY" not in provider_database.read_bytes()
    assert repository.verify_provenance()["valid"] is True


def test_signed_supervisor_lifecycle_is_program_bound_and_replay_fails(
    tmp_path: Path,
):
    pki = _pki_material(tmp_path)
    repository, program, intervention, registered, database, provider_database = (
        _fixture(
            tmp_path,
            recovery_authority_trust_anchor=pki["trust"],
        )
    )
    intervention_file = tmp_path / "intervention.json"
    _write_json(intervention_file, intervention)
    _crash_after_provider_commit(
        database=database,
        provider_database=provider_database,
        program_id=registered["id"],
        intervention_file=intervention_file,
        marker=tmp_path / "provider-committed.json",
        authority_trust_file=pki["trust_file"],
    )
    material = _signed_recovery_material(
        tmp_path,
        program=program,
        registered=registered,
        intervention=intervention,
        pki=pki,
        owner_id="signed-supervisor",
        suffix="complete",
    )
    recovered = _run_bridge(
        _supervisor_arguments(
            database=database,
            provider_database=provider_database,
            provider_key_file=Path(f"{provider_database}.provider_key"),
            program_id=registered["id"],
            intervention_file=intervention_file,
            recovery_authority_file=material["recovery_authority_file"],
            owner_id="signed-supervisor",
            ttl_ms=5000,
            authority_trust_file=pki["trust_file"],
            authority_grant_file=material["grant_file"],
            supervisor_signing_key_file=material["supervisor_private"],
        )
    )
    attestation = recovered["supervisor_attestation"]
    assert attestation["status"] == "complete"
    assert attestation["integrity_verified"] is True
    assert attestation["lifecycle_complete"] is True
    assert attestation["program_bound"] is True
    assert attestation["operator_certificate_verified"] is True
    assert attestation["operator_identity_external_verified"] is False
    assert attestation["supervisor_attestation_key_verified"] is True
    assert attestation["events"][:3] == [
        "authority_accepted",
        "lease_acquired",
        "lease_renewed",
    ]
    assert attestation["events"][-3:] == [
        "heartbeat_stopped",
        "recovery_returned",
        "lease_released",
    ]
    with pytest.raises(RecoveryAuthorityError, match="grant_replay"):
        program.attest_supervisor(
            registered["id"],
            material["grant"],
            str(material["supervisor_private"]),
            event="authority_accepted",
            details={"replayed": True},
        )
    provider_status = DurableProcessProviderStore(
        provider_database,
        authentication_key_file=Path(f"{provider_database}.provider_key"),
        recovery_authority_trust_anchor=pki["trust"],
        read_only=True,
    ).status()
    assert provider_status["total_execution_count"] == 32
    assert provider_status["total_infer_request_count"] == 32
    before_program = hashlib.sha256(database.read_bytes()).hexdigest()
    before_provider = hashlib.sha256(provider_database.read_bytes()).hexdigest()
    read_only_status = subprocess.run(
        [
            sys.executable,
            "-m",
            "spst_runtime.process_recovery_supervisor",
            "status",
            "--database",
            str(database),
            "--program-id",
            str(registered["id"]),
            "--provider-db",
            str(provider_database),
            "--provider-key-file",
            str(Path(f"{provider_database}.provider_key")),
            "--authority-trust-file",
            str(pki["trust_file"]),
            "--resource-id",
            str(material["resource_id"]),
        ],
        cwd=RUNTIME_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert read_only_status.returncode == 0, read_only_status.stderr
    status_value = json.loads(read_only_status.stdout)
    assert status_value["program"]["operational_hardening"][
        "recovery_supervisor"
    ]["attestation"]["lifecycle_complete"] is True
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_program
    assert hashlib.sha256(provider_database.read_bytes()).hexdigest() == before_provider
    assert repository.verify_provenance()["valid"] is True


def test_arch12_supervisor_binds_live_revocation_state_and_rollback_anchor(
    tmp_path: Path,
):
    pki = _pki_material(tmp_path, authority_state_required=True)
    repository, program, intervention, registered, database, provider_database = (
        _fixture(
            tmp_path,
            recovery_authority_trust_anchor=pki["trust"],
        )
    )
    assert registered["provider"]["process_transport_protocol"] == (
        "subprocess-stdio-sqlite-hmac-lease-pki-state-v4"
    )
    intervention_file = tmp_path / "intervention-arch12.json"
    _write_json(intervention_file, intervention)
    _crash_after_provider_commit(
        database=database,
        provider_database=provider_database,
        program_id=registered["id"],
        intervention_file=intervention_file,
        marker=tmp_path / "provider-committed-arch12.json",
        authority_trust_file=pki["trust_file"],
    )
    material = _signed_recovery_material(
        tmp_path,
        program=program,
        registered=registered,
        intervention=intervention,
        pki=pki,
        owner_id="arch12-supervisor",
        suffix="arch12",
    )

    recovered = _run_bridge(
        _supervisor_arguments(
            database=database,
            provider_database=provider_database,
            provider_key_file=Path(f"{provider_database}.provider_key"),
            program_id=registered["id"],
            intervention_file=intervention_file,
            recovery_authority_file=material["recovery_authority_file"],
            owner_id="arch12-supervisor",
            ttl_ms=5000,
            authority_trust_file=pki["trust_file"],
            authority_grant_file=material["grant_file"],
            supervisor_signing_key_file=material["supervisor_private"],
            authority_state_file=pki["state_file"],
            rollback_anchor_file=pki["anchor_file"],
        )
    )

    attestation = recovered["supervisor_attestation"]
    assert attestation["status"] == "complete"
    assert attestation["authority_state_sha256"] == pki["state"]["state_sha256"]
    assert attestation["authority_state_generation"] == 1
    assert attestation["rollback_anchor_sha256"] == pki["anchor"][
        "anchor_sha256"
    ]
    assert attestation["revocation_checked"] is True
    assert attestation["trusted_time_source_verified"] is False
    assert attestation["hardware_key_custody_verified"] is False
    assert attestation["full_rollback_resistance_verified"] is False
    provider_status = DurableProcessProviderStore(
        provider_database,
        authentication_key_file=Path(f"{provider_database}.provider_key"),
        recovery_authority_trust_anchor=pki["trust"],
        recovery_authority_state=pki["state"],
        recovery_authority_rollback_anchor=pki["anchor"],
        read_only=True,
    ).status()
    assert provider_status["recovery_authority_state_sha256"] == pki["state"][
        "state_sha256"
    ]
    assert provider_status["total_execution_count"] == 32
    assert provider_status["total_infer_request_count"] == 32
    assert repository.verify_provenance()["valid"] is True


def test_arch12_attestation_ledger_rejects_authority_state_generation_downgrade(
    tmp_path: Path,
):
    pki = _pki_material(tmp_path, authority_state_required=True)
    repository, program, _, registered, _, _ = _fixture(
        tmp_path,
        recovery_authority_trust_anchor=pki["trust"],
    )
    now_ms = time.time_ns() // 1_000_000
    state_two = issue_recovery_authority_state(
        pki["trust"],
        pki["root_private"],
        pki["custody"],
        generation=2,
        previous_state_sha256=pki["state"]["state_sha256"],
        issued_at_ms=now_ms - 10,
        valid_until_ms=now_ms + 600_000,
        minimum_accepted_time_ms=now_ms - 10,
    )
    anchor_two = issue_recovery_rollback_anchor(
        pki["trust"],
        pki["root_private"],
        state_two,
        previous_anchor_sha256=pki["anchor"]["anchor_sha256"],
    )

    def grant_for(
        suffix: str,
        state: dict[str, Any],
        anchor: dict[str, Any],
    ) -> tuple[dict[str, Any], Path]:
        private_key = tmp_path / f"downgrade-{suffix}.private.pem"
        public_key = generate_supervisor_attestation_key(private_key)
        grant = issue_recovery_authority_grant(
            pki["trust"],
            pki["certificate"],
            pki["operator_private"],
            public_key,
            program_id=registered["id"],
            program_sha256=registered["program_sha256"],
            attempt_id=f"attempt-{suffix}",
            provider_instance_sha256=registered["provider"][
                "transport_instance_sha256"
            ],
            lease_resource_id=hashlib.sha256(suffix.encode("utf-8")).hexdigest(),
            lease_owner_id=f"owner-{suffix}",
            transport_recovery_authority_sha256="a" * 64,
            context_intervention_sha256="b" * 64,
            issued_at_ms=now_ms,
            expires_at_ms=now_ms + 300_000,
            nonce=f"nonce-{suffix}",
            authority_state=state,
            rollback_anchor=anchor,
        )
        return grant, private_key

    newer_grant, newer_private = grant_for("generation-two", state_two, anchor_two)
    program.attest_supervisor(
        registered["id"],
        newer_grant,
        str(newer_private),
        event="authority_accepted",
        details={"generation": 2},
        authority_state=state_two,
        rollback_anchor=anchor_two,
    )
    older_grant, older_private = grant_for(
        "generation-one",
        pki["state"],
        pki["anchor"],
    )
    with pytest.raises(
        RecoveryAuthorityError,
        match="supervisor_authority_state_generation_rollback",
    ):
        program.attest_supervisor(
            registered["id"],
            older_grant,
            str(older_private),
            event="authority_accepted",
            details={"generation": 1},
            authority_state=pki["state"],
            rollback_anchor=pki["anchor"],
        )
    assert repository.verify_provenance()["valid"] is True


def test_signed_attestation_detects_rewrite_accepted_by_local_provenance(
    tmp_path: Path,
):
    pki = _pki_material(tmp_path)
    repository, program, intervention, registered, database, provider_database = (
        _fixture(
            tmp_path,
            recovery_authority_trust_anchor=pki["trust"],
        )
    )
    intervention_file = tmp_path / "intervention.json"
    _write_json(intervention_file, intervention)
    _crash_after_provider_commit(
        database=database,
        provider_database=provider_database,
        program_id=registered["id"],
        intervention_file=intervention_file,
        marker=tmp_path / "provider-committed.json",
        authority_trust_file=pki["trust_file"],
    )
    material = _signed_recovery_material(
        tmp_path,
        program=program,
        registered=registered,
        intervention=intervention,
        pki=pki,
        owner_id="tamper-supervisor",
        suffix="tamper",
    )
    _run_bridge(
        _supervisor_arguments(
            database=database,
            provider_database=provider_database,
            provider_key_file=Path(f"{provider_database}.provider_key"),
            program_id=registered["id"],
            intervention_file=intervention_file,
            recovery_authority_file=material["recovery_authority_file"],
            owner_id="tamper-supervisor",
            ttl_ms=5000,
            authority_trust_file=pki["trust_file"],
            authority_grant_file=material["grant_file"],
            supervisor_signing_key_file=material["supervisor_private"],
        )
    )
    ledger = SupervisorAttestationLedger(
        repository,
        asyncio.run(repository.load(f"{PROGRAM_RECORD_PREFIX}{registered['id']}"))
        or {},
    )
    records = asyncio.run(
        repository.load_prefix(
            f"runtime:real_paired_outcome:supervisor_attestation:{registered['id']}:"
        )
    )
    first_key = sorted(records)[0]
    tampered = json.loads(json.dumps(records[first_key]))
    tampered["details"]["authority_grant_sha256"] = "0" * 64
    tampered["details_sha256"] = _canonical_digest(tampered["details"])
    unsigned = {
        key: value
        for key, value in tampered.items()
        if key not in {"attestation_sha256", "supervisor_signature"}
    }
    tampered["attestation_sha256"] = _canonical_digest(unsigned)
    asyncio.run(repository.save(first_key, tampered))
    assert repository.verify_provenance()["valid"] is True
    summary = ledger.summary()
    assert summary["integrity_verified"] is False
    assert summary["reason"] == "supervisor_attestation_signature_invalid"
    blocked = program.get(registered["id"])
    assert blocked is not None
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "supervisor_attestation_signature_invalid"


def test_killed_signed_supervisor_requires_exact_pki_adoption_without_duplicate(
    tmp_path: Path,
):
    pki = _pki_material(tmp_path)
    repository, program, intervention, registered, database, provider_database = (
        _fixture(
            tmp_path,
            recovery_authority_trust_anchor=pki["trust"],
        )
    )
    provider_key_file = Path(f"{provider_database}.provider_key")
    intervention_file = tmp_path / "intervention.json"
    _write_json(intervention_file, intervention)
    _crash_after_provider_commit(
        database=database,
        provider_database=provider_database,
        program_id=registered["id"],
        intervention_file=intervention_file,
        marker=tmp_path / "provider-committed.json",
        authority_trust_file=pki["trust_file"],
    )
    first = _signed_recovery_material(
        tmp_path,
        program=program,
        registered=registered,
        intervention=intervention,
        pki=pki,
        owner_id="signed-supervisor-one",
        suffix="killed",
    )
    lease_marker = tmp_path / "signed-supervisor-lease.json"
    first_supervisor = subprocess.Popen(
        _supervisor_arguments(
            database=database,
            provider_database=provider_database,
            provider_key_file=provider_key_file,
            program_id=registered["id"],
            intervention_file=intervention_file,
            recovery_authority_file=first["recovery_authority_file"],
            owner_id="signed-supervisor-one",
            ttl_ms=800,
            lease_marker=lease_marker,
            post_lease_delay_ms=5000,
            authority_trust_file=pki["trust_file"],
            authority_grant_file=first["grant_file"],
            supervisor_signing_key_file=first["supervisor_private"],
        ),
        cwd=RUNTIME_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    marker = _wait_for_marker(first_supervisor, lease_marker)
    first_supervisor.kill()
    first_supervisor.communicate(timeout=10)
    assert first_supervisor.returncode != 0
    time.sleep(0.9)
    lease = marker["lease"]
    wrong = _signed_recovery_material(
        tmp_path,
        program=program,
        registered=registered,
        intervention=intervention,
        pki=pki,
        owner_id="signed-supervisor-two",
        suffix="wrong-adoption",
        previous_owner_id="wrong-previous-owner",
        expired_generation=int(lease["generation"]),
    )
    store = DurableProcessProviderStore(
        provider_database,
        authentication_key_file=provider_key_file,
        recovery_authority_trust_anchor=pki["trust"],
    )
    with pytest.raises(ProcessTransportError, match="previous_owner_id_mismatch"):
        store.acquire_recovery_lease(
            str(first["resource_id"]),
            "signed-supervisor-two",
            ttl_ms=5000,
            adoption_authority=wrong["grant"],
        )
    second = _signed_recovery_material(
        tmp_path,
        program=program,
        registered=registered,
        intervention=intervention,
        pki=pki,
        owner_id="signed-supervisor-two",
        suffix="adopted",
        previous_owner_id="signed-supervisor-one",
        expired_generation=int(lease["generation"]),
    )
    recovered = _run_bridge(
        _supervisor_arguments(
            database=database,
            provider_database=provider_database,
            provider_key_file=provider_key_file,
            program_id=registered["id"],
            intervention_file=intervention_file,
            recovery_authority_file=second["recovery_authority_file"],
            owner_id="signed-supervisor-two",
            ttl_ms=5000,
            authority_trust_file=pki["trust_file"],
            authority_grant_file=second["grant_file"],
            supervisor_signing_key_file=second["supervisor_private"],
        )
    )
    assert recovered["lease"]["adopted_orphan"] is True
    attestation = recovered["supervisor_attestation"]
    assert attestation["status"] == "complete"
    assert attestation["event_count"] > attestation["session_event_count"]
    assert attestation["lifecycle_complete"] is True
    provider_status = DurableProcessProviderStore(
        provider_database,
        authentication_key_file=provider_key_file,
        recovery_authority_trust_anchor=pki["trust"],
        read_only=True,
    ).status()
    assert provider_status["total_execution_count"] == 32
    assert provider_status["total_infer_request_count"] == 32
    assert provider_status["orphan_adoption_count"] == 1
    assert repository.verify_provenance()["valid"] is True
