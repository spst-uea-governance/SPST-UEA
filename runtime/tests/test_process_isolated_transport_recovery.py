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
    build_recovery_supervisor_authority,
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
    adapter = ProcessIsolatedTransportAdapter(provider_database)
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
    time.sleep(0.9)
    blocked = subprocess.run(
        _supervisor_arguments(
            database=database,
            provider_database=provider_database,
            provider_key_file=provider_key_file,
            program_id=registered["id"],
            intervention_file=intervention_file,
            recovery_authority_file=recovery_authority_file,
            owner_id="supervisor-two",
            ttl_ms=1000,
        ),
        cwd=RUNTIME_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert blocked.returncode == 2
    assert json.loads(blocked.stdout)["reason"] == "process_recovery_lease_active"

    first_supervisor.kill()
    first_supervisor.communicate(timeout=10)
    assert first_supervisor.returncode != 0

    lease = marker["lease"]
    time.sleep(0.9)
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
