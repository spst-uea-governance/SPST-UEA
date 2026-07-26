import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from threading import Event
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
from spst_runtime.interfaces.model_adapter import ModelAdapter
from spst_runtime.live_pairing import canonical_live_pair_hash
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.provider_observation import (
    build_provider_observation,
    build_provider_request_binding,
)
from spst_runtime.provider_transport import (
    RECOVERY_RECORD_PREFIX,
    TRANSPORT_QUERY_PREFIX,
    TRANSPORT_RECORD_PREFIX,
    build_provider_transport_receipt,
    build_recovery_authority,
)
from spst_runtime.real_paired_outcome import (
    PROGRAM_EXECUTION_PREFIX,
    PROVIDER_EXECUTION_AUTHORITY_SCHEMA,
    RealPairedOutcomeProgram,
)
from spst_runtime.real_paired_outcome_attempt import PROGRAM_ATTEMPT_PREFIX


class HardenedAdapter(ModelAdapter):
    def __init__(self, *, failure: str | None = None):
        self.calls = 0
        self.failure = failure
        self.started = Event()
        self.release = Event()
        self.model_version = "operational-hardening-v1"

    async def infer(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        self.started.set()
        if self.failure == "wait" and self.calls == 1:
            if not self.release.wait(10):
                raise RuntimeError("test release timeout")
        if self.failure == "error":
            raise RuntimeError("sensitive provider detail must not persist")
        if self.failure == "interrupt":
            raise KeyboardInterrupt("simulated process interruption")
        source = context or {}
        text = json.dumps(
            {
                "answer": (
                    1
                    if isinstance(source.get("evidence_context_intervention"), dict)
                    else 0
                )
            }
        )
        request = build_provider_request_binding(prompt, source)
        observation = build_provider_observation(
            request,
            provider_name="hardened-in-process-provider",
            model_version=self.model_version,
            response_id=f"hardened-{self.calls:04d}",
            response_status="completed",
            output_text=text,
            observation_source="in_process_provider_echo",
            acknowledged_request_binding_sha256=request["request_binding_sha256"],
        )
        return {
            "provider": "hardened-in-process-provider",
            "model_version": self.model_version,
            "available": True,
            "text": text,
            "provider_observation": observation,
        }

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": "hardened-in-process-provider",
            "model_version": self.model_version,
            "requires_api_key": False,
        }

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "interface": "ModelAdapter",
            "supports_structured_evaluation": True,
            "supports_provider_observation": True,
            "provider_observation_source": "in_process_provider_echo",
            "execution_environment": "in_process",
            "billing_class": "no_charge",
        }


class RecoverableTransportAdapter(HardenedAdapter):
    def __init__(
        self,
        *,
        loss_mode: str | None = None,
        reconciliation_attack: str | None = None,
        receipt_attack: str | None = None,
    ):
        super().__init__()
        self.loss_mode = loss_mode
        self.reconciliation_attack = reconciliation_attack
        self.receipt_attack = receipt_attack
        self.reconcile_calls = 0
        self.transport_results: dict[str, dict[str, Any]] = {}
        self.model_version = "provider-transport-v1"

    async def infer(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        source = context or {}
        transport_request = source.get("provider_transport")
        assert isinstance(transport_request, dict)
        idempotency_key = str(transport_request["idempotency_key"])
        if idempotency_key in self.transport_results:
            return deepcopy(self.transport_results[idempotency_key])
        self.calls += 1
        has_context = isinstance(source.get("evidence_context_intervention"), dict)
        text = json.dumps({"answer": 1 if has_context else 0})
        request = build_provider_request_binding(prompt, source)
        response_id = f"transport-{idempotency_key[-16:]}"
        observation = build_provider_observation(
            request,
            provider_name="hardened-in-process-provider",
            model_version=self.model_version,
            response_id=response_id,
            response_status="completed",
            output_text=text,
            observation_source="in_process_provider_echo",
            acknowledged_request_binding_sha256=request["request_binding_sha256"],
        )
        receipt = build_provider_transport_receipt(
            transport_request,
            request,
            provider_name="hardened-in-process-provider",
            model_version=self.model_version,
            response_id=response_id,
            response_status="completed",
            output_text=text,
            acknowledged_idempotency_key=(
                None if self.receipt_attack == "missing_ack" else idempotency_key
            ),
        )
        result = {
            "provider": "hardened-in-process-provider",
            "model_version": self.model_version,
            "available": True,
            "text": text,
            "provider_observation": observation,
            "provider_transport_receipt": receipt,
            "provider_request_binding": request,
        }
        if self.calls == 1 and self.loss_mode == "after_provider_completion":
            self.transport_results[idempotency_key] = deepcopy(result)
            raise KeyboardInterrupt("response lost after provider completion")
        if self.calls == 1 and self.loss_mode == "before_provider_completion":
            raise KeyboardInterrupt("provider outcome unresolved")
        self.transport_results[idempotency_key] = deepcopy(result)
        return result

    def reconcile_transport(
        self,
        idempotency_key: str,
        provider_request_binding_sha256: str,
    ) -> dict[str, Any] | None:
        self.reconcile_calls += 1
        result = self.transport_results.get(idempotency_key)
        if result is None:
            return None
        resolved = deepcopy(result)
        if self.reconciliation_attack == "altered_output":
            resolved["text"] = json.dumps({"answer": 999})
        elif self.reconciliation_attack == "wrong_request":
            binding = deepcopy(
                resolved.get("provider_request_binding")
                or {"request_binding_sha256": provider_request_binding_sha256}
            )
            binding["request_binding_sha256"] = "f" * 64
            resolved["provider_request_binding"] = binding
        return resolved

    def get_capabilities(self) -> dict[str, Any]:
        return {
            **super().get_capabilities(),
            "supports_transport_idempotency": True,
            "supports_transport_reconciliation": True,
        }


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
        "statement": "Bound operational hardening fixture.",
    }
    text = json.dumps(projection, sort_keys=True, separators=(",", ":"))
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    binding_unsigned = {
        "schema": CONTEXT_INTERVENTION_BINDING_SCHEMA,
        "artifact_sha256": artifact_sha256,
        "artifact_kind": "architecture_decision",
        "source_sha256": source_sha256,
        "producer_receipt_id": receipt_sha256,
        "memory_record_id": "hardening-memory-record",
        "memory_record_sha256": "4" * 64,
        "projection_sha256": text_sha256,
        "policy_version": policy_version,
        "semantic_review_id": "hardening-semantic-review",
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
        "domain": "operational-hardening",
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
    adapter: HardenedAdapter,
) -> tuple[SQLiteRepository, RealPairedOutcomeProgram, dict[str, Any], dict[str, Any]]:
    repository = SQLiteRepository(str(tmp_path / "hardening.db"))
    corpus = OperationalEvaluationCorpus(repository)
    task_ids = [f"hardening-{index:02d}" for index in range(8)]
    for task_id in task_ids:
        assert corpus.register(_task(task_id))["status"] == "registered"
    intervention = _intervention()
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
    return repository, program, intervention, registered


def test_atomic_repository_compare_and_set_preserves_provenance(tmp_path: Path):
    repository = SQLiteRepository(str(tmp_path / "cas.db"))
    assert asyncio.run(repository.save_if_absent("claim", {"value": 1})) is True
    assert asyncio.run(repository.save_if_absent("claim", {"value": 2})) is False
    first = asyncio.run(repository.load("claim"))
    assert first == {"value": 1}
    assert asyncio.run(
        repository.replace_if_record_hash(
            "claim", repository.record_hash(first), {"value": 3}
        )
    ) is True
    assert asyncio.run(
        repository.replace_if_record_hash(
            "claim", repository.record_hash(first), {"value": 4}
        )
    ) is False
    assert asyncio.run(repository.load("claim")) == {"value": 3}
    assert repository.verify_provenance()["valid"] is True


def test_concurrent_execute_has_one_claim_and_no_duplicate_calls(tmp_path: Path):
    adapter = HardenedAdapter(failure="wait")
    repository, program, intervention, registered = _fixture(tmp_path, adapter)
    second_program = RealPairedOutcomeProgram(
        repository,
        OperationalEvaluationCorpus(repository),
        adapter,
        attempt_nonce_factory=lambda: "d" * 64,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(
            program.execute,
            registered["id"],
            context_intervention=intervention,
        )
        assert adapter.started.wait(5)
        second = second_program.execute(
            registered["id"], context_intervention=intervention
        )
        assert second["status"] == "recovery_required"
        assert second["reason"] == "real_paired_outcome_execution_recovery_required"
        assert adapter.calls == 1
        adapter.release.set()
        first = first_future.result(timeout=30)
    assert first["status"] == "pending_human_review"
    assert first["operational_hardening"]["assurance"] == "attempt_bound"
    assert first["operational_hardening"]["transition_provenance"]["verified"] is True
    assert adapter.calls == 32
    assert repository.verify_provenance()["valid"] is True


def test_adapter_exception_is_terminal_blocked_and_retry_never_calls_again(
    tmp_path: Path,
):
    adapter = HardenedAdapter(failure="error")
    repository, program, intervention, registered = _fixture(tmp_path, adapter)
    failed = program.execute(registered["id"], context_intervention=intervention)
    assert failed["status"] == "blocked"
    assert failed["reason"] == "real_paired_outcome_provider_observation_source_mismatch"
    assert failed["operational_hardening"]["assurance"] == "attempt_bound"
    assert failed["operational_hardening"]["attempt"]["terminal_outcome"] == "blocked"
    assert failed["operational_hardening"]["attempt"]["failure_type"] is None
    assert "sensitive provider detail" not in json.dumps(failed)
    stored = asyncio.run(repository.load_prefix("runtime:"))
    assert "sensitive provider detail" not in json.dumps(stored)
    calls = adapter.calls
    retried = program.execute(registered["id"], context_intervention=intervention)
    assert retried["status"] == "blocked"
    assert adapter.calls == calls == 32
    before = hashlib.sha256(Path(repository.path).read_bytes()).hexdigest()
    assert program.get(registered["id"]) == failed
    assert hashlib.sha256(Path(repository.path).read_bytes()).hexdigest() == before


def test_process_interrupt_requires_manual_recovery_without_retry(tmp_path: Path):
    adapter = HardenedAdapter(failure="interrupt")
    repository, program, intervention, registered = _fixture(tmp_path, adapter)
    with pytest.raises(KeyboardInterrupt, match="simulated process interruption"):
        program.execute(registered["id"], context_intervention=intervention)
    calls = adapter.calls
    before = hashlib.sha256(Path(repository.path).read_bytes()).hexdigest()
    status = program.get(registered["id"])
    assert status is not None
    assert status["status"] == "recovery_required"
    assert status["operational_hardening"]["automatic_retry_permitted"] is False
    assert status["operational_hardening"]["attempt"]["state"] == "running"
    assert hashlib.sha256(Path(repository.path).read_bytes()).hexdigest() == before
    retry = program.execute(registered["id"], context_intervention=intervention)
    assert retry["status"] == "recovery_required"
    assert adapter.calls == calls == 1


def test_resigned_attempt_tamper_and_execution_rebind_fail_closed(tmp_path: Path):
    adapter = HardenedAdapter()
    repository, program, intervention, registered = _fixture(tmp_path, adapter)
    executed = program.execute(registered["id"], context_intervention=intervention)
    assert executed["status"] == "pending_human_review"
    attempt_key = f"{PROGRAM_ATTEMPT_PREFIX}{registered['id']}"
    attempt = asyncio.run(repository.load(attempt_key))
    assert isinstance(attempt, dict)
    tampered = deepcopy(attempt)
    tampered["automatic_retry_permitted"] = True
    unsigned = {key: value for key, value in tampered.items() if key != "attempt_sha256"}
    tampered["attempt_sha256"] = canonical_live_pair_hash(unsigned)
    asyncio.run(repository.save(attempt_key, tampered))
    assert repository.verify_provenance()["valid"] is True
    blocked = program.get(registered["id"])
    assert blocked is not None
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "real_paired_outcome_attempt_binding_mismatch"

    other_adapter = HardenedAdapter()
    other_repository, other_program, other_intervention, other_registered = _fixture(
        tmp_path / "execution-rebind", other_adapter
    )
    assert other_program.execute(
        other_registered["id"], context_intervention=other_intervention
    )["status"] == "pending_human_review"
    execution_key = f"{PROGRAM_EXECUTION_PREFIX}{other_registered['id']}"
    execution = asyncio.run(other_repository.load(execution_key))
    assert isinstance(execution, dict)
    rebound = deepcopy(execution)
    rebound["attempt_running_sha256"] = "f" * 64
    execution_unsigned = {
        key: value for key, value in rebound.items() if key != "execution_sha256"
    }
    rebound["execution_sha256"] = canonical_live_pair_hash(execution_unsigned)
    asyncio.run(other_repository.save(execution_key, rebound))
    rebound_status = other_program.get(other_registered["id"])
    assert rebound_status is not None
    assert rebound_status["status"] == "blocked"
    assert rebound_status["reason"] == "real_paired_outcome_attempt_execution_binding_mismatch"


def test_transport_receipts_bind_every_call_without_reconciliation(tmp_path: Path):
    adapter = RecoverableTransportAdapter()
    repository, program, intervention, registered = _fixture(tmp_path, adapter)
    executed = program.execute(registered["id"], context_intervention=intervention)
    assert executed["status"] == "pending_human_review"
    hardening = executed["operational_hardening"]
    assert hardening["assurance"] == "attempt_and_transport_bound"
    transport = hardening["provider_transport"]
    assert transport["record_count"] == 32
    assert transport["completed_count"] == 32
    assert transport["receipt_set_complete"] is True
    assert transport["provider_idempotency_observed"] is True
    assert transport["provider_identity_authenticated"] is False
    assert transport["exactly_once_execution_proven"] is False
    assert executed["outcome"]["provider_transport_attempt_count_observed"] is True
    records = asyncio.run(repository.load_prefix(TRANSPORT_RECORD_PREFIX))
    keys = {
        value["transport_request"]["idempotency_key"]
        for value in records.values()
        if isinstance(value, dict)
    }
    assert len(keys) == 32
    assert adapter.calls == 32
    assert adapter.reconcile_calls == 0
    assert repository.verify_provenance()["valid"] is True


def test_lost_completed_response_reconciles_without_duplicate_inference(
    tmp_path: Path,
):
    adapter = RecoverableTransportAdapter(loss_mode="after_provider_completion")
    repository, program, intervention, registered = _fixture(tmp_path, adapter)
    with pytest.raises(KeyboardInterrupt, match="response lost"):
        program.execute(registered["id"], context_intervention=intervention)
    interrupted = program.get(registered["id"])
    assert interrupted is not None
    assert interrupted["status"] == "recovery_required"
    attempt = interrupted["operational_hardening"]["attempt"]
    authority = build_recovery_authority(
        program_id=registered["id"],
        attempt_id=attempt["attempt_id"],
        operator_id="human-recovery-reviewer",
    )
    recovered = program.recover(
        registered["id"],
        context_intervention=intervention,
        recovery_authority=authority,
    )
    assert recovered["status"] == "pending_human_review"
    assert recovered["operational_hardening"]["assurance"] == (
        "attempt_and_transport_bound"
    )
    assert recovered["operational_hardening"]["recovery_authority"] == {
        "cycle_count": 1,
        "active": False,
        "latest_outcome": "execution_completed",
        "operator_identity_verified": False,
        "verified": True,
        "reason": None,
    }
    assert adapter.calls == 32
    assert adapter.reconcile_calls == 1
    queries = asyncio.run(repository.load_prefix(TRANSPORT_QUERY_PREFIX))
    assert len(queries) == 1
    query = next(iter(queries.values()))
    assert query["state"] == "terminal"
    assert query["provider_result_observed"] is True
    assert len(query["result_transport_receipt_sha256"]) == 64
    assert query["query_is_inference"] is False
    assert repository.verify_provenance()["valid"] is True


def test_unresolved_provider_result_never_resumes_new_calls(tmp_path: Path):
    adapter = RecoverableTransportAdapter(loss_mode="before_provider_completion")
    repository, program, intervention, registered = _fixture(tmp_path, adapter)
    with pytest.raises(KeyboardInterrupt, match="outcome unresolved"):
        program.execute(registered["id"], context_intervention=intervention)
    interrupted = program.get(registered["id"])
    assert interrupted is not None
    attempt_id = interrupted["operational_hardening"]["attempt"]["attempt_id"]
    authority = build_recovery_authority(
        program_id=registered["id"],
        attempt_id=attempt_id,
        operator_id="human-recovery-reviewer",
    )
    first = program.recover(
        registered["id"],
        context_intervention=intervention,
        recovery_authority=authority,
    )
    assert first["status"] == "recovery_required"
    assert first["recovery"]["status"] == "held"
    assert first["recovery"]["reason"] == "provider_transport_reconciliation_unresolved"
    assert first["recovery"]["new_adapter_invocations"] == 0
    second = program.recover(
        registered["id"],
        context_intervention=intervention,
        recovery_authority=authority,
    )
    assert second["status"] == "recovery_required"
    assert adapter.calls == 1
    assert adapter.reconcile_calls == 2
    assert repository.verify_provenance()["valid"] is True


def test_reconciliation_tamper_and_wrong_authority_fail_closed(tmp_path: Path):
    adapter = RecoverableTransportAdapter(
        loss_mode="after_provider_completion",
        reconciliation_attack="altered_output",
    )
    repository, program, intervention, registered = _fixture(tmp_path, adapter)
    with pytest.raises(KeyboardInterrupt):
        program.execute(registered["id"], context_intervention=intervention)
    interrupted = program.get(registered["id"])
    assert interrupted is not None
    wrong = build_recovery_authority(
        program_id=registered["id"],
        attempt_id="RATT-wrong-authority",
        operator_id="human-recovery-reviewer",
    )
    held = program.recover(
        registered["id"],
        context_intervention=intervention,
        recovery_authority=wrong,
    )
    assert held["status"] == "recovery_required"
    assert held["recovery"]["reason"] == "provider_transport_receipt_boundary_invalid"
    assert adapter.calls == 1

    adapter.reconciliation_attack = None
    wrong_authority = program.recover(
        registered["id"],
        context_intervention=intervention,
        recovery_authority=wrong,
    )
    assert wrong_authority["status"] == "recovery_required"
    assert wrong_authority["recovery"]["reason"] == (
        "provider_recovery_authority_binding_mismatch"
    )
    assert adapter.calls == 1
    assert repository.verify_provenance()["valid"] is True


def test_resigned_recovery_authority_tamper_blocks_completed_evidence(
    tmp_path: Path,
):
    adapter = RecoverableTransportAdapter(loss_mode="after_provider_completion")
    repository, program, intervention, registered = _fixture(tmp_path, adapter)
    with pytest.raises(KeyboardInterrupt):
        program.execute(registered["id"], context_intervention=intervention)
    interrupted = program.get(registered["id"])
    assert interrupted is not None
    authority = build_recovery_authority(
        program_id=registered["id"],
        attempt_id=interrupted["operational_hardening"]["attempt"]["attempt_id"],
        operator_id="human-recovery-reviewer",
    )
    assert program.recover(
        registered["id"],
        context_intervention=intervention,
        recovery_authority=authority,
    )["status"] == "pending_human_review"
    records = asyncio.run(repository.load_prefix(RECOVERY_RECORD_PREFIX))
    assert len(records) == 1
    key, record = next(iter(records.items()))
    tampered = deepcopy(record)
    tampered_authority = deepcopy(tampered["authority"])
    tampered_authority["automatic_retry_authorized"] = True
    authority_unsigned = {
        field: value
        for field, value in tampered_authority.items()
        if field != "authority_sha256"
    }
    tampered_authority["authority_sha256"] = canonical_live_pair_hash(
        authority_unsigned
    )
    tampered["authority"] = tampered_authority
    record_unsigned = {
        field: value
        for field, value in tampered.items()
        if field != "recovery_record_sha256"
    }
    tampered["recovery_record_sha256"] = canonical_live_pair_hash(record_unsigned)
    asyncio.run(repository.save(key, tampered))
    assert repository.verify_provenance()["valid"] is True
    blocked = program.get(registered["id"])
    assert blocked is not None
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "provider_recovery_authority_binding_mismatch"


def test_transport_capability_declaration_is_complete_and_callable(tmp_path: Path):
    class PartialTransportAdapter(HardenedAdapter):
        def get_capabilities(self) -> dict[str, Any]:
            return {
                **super().get_capabilities(),
                "supports_transport_idempotency": True,
                "supports_transport_reconciliation": False,
            }

    class MissingReconcileAdapter(HardenedAdapter):
        def get_capabilities(self) -> dict[str, Any]:
            return {
                **super().get_capabilities(),
                "supports_transport_idempotency": True,
                "supports_transport_reconciliation": True,
            }

    repository = SQLiteRepository(str(tmp_path / "capabilities.db"))
    corpus = OperationalEvaluationCorpus(repository)
    partial = RealPairedOutcomeProgram(repository, corpus, PartialTransportAdapter())
    missing = RealPairedOutcomeProgram(repository, corpus, MissingReconcileAdapter())
    assert partial._adapter_contract(PartialTransportAdapter())[1] == (
        "provider_transport_capability_incomplete"
    )
    assert missing._adapter_contract(MissingReconcileAdapter())[1] == (
        "provider_transport_reconciliation_method_required"
    )


def test_missing_provider_idempotency_ack_is_unknown_and_never_retried(
    tmp_path: Path,
):
    adapter = RecoverableTransportAdapter(receipt_attack="missing_ack")
    repository, program, intervention, registered = _fixture(tmp_path, adapter)
    result = program.execute(registered["id"], context_intervention=intervention)
    assert result["status"] == "recovery_required"
    assert result["operational_hardening"]["provider_transport"]["submitted_count"] == 1
    retry = program.execute(registered["id"], context_intervention=intervention)
    assert retry["status"] == "recovery_required"
    assert adapter.calls == 1
    assert repository.verify_provenance()["valid"] is True


def test_reconciliation_query_cap_is_durable_and_blocks_further_queries(
    tmp_path: Path,
):
    adapter = RecoverableTransportAdapter(loss_mode="before_provider_completion")
    repository, program, intervention, registered = _fixture(tmp_path, adapter)
    with pytest.raises(KeyboardInterrupt):
        program.execute(registered["id"], context_intervention=intervention)
    interrupted = program.get(registered["id"])
    assert interrupted is not None
    authority = build_recovery_authority(
        program_id=registered["id"],
        attempt_id=interrupted["operational_hardening"]["attempt"]["attempt_id"],
        operator_id="human-recovery-reviewer",
    )
    maximum = registered["corpus"]["task_count"] * 4 * 2
    for _ in range(maximum):
        held = program.recover(
            registered["id"],
            context_intervention=intervention,
            recovery_authority=authority,
        )
        assert held["status"] == "recovery_required"
    exhausted = program.recover(
        registered["id"],
        context_intervention=intervention,
        recovery_authority=authority,
    )
    assert exhausted["recovery"]["reason"] == (
        "provider_transport_reconciliation_query_cap_exhausted"
    )
    assert adapter.calls == 1
    assert adapter.reconcile_calls == maximum
    assert repository.verify_provenance()["valid"] is True
