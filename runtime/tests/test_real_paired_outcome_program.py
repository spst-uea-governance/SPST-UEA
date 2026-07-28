import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from spst_runtime.chat_bridge import run_chat_turn
from spst_runtime.context_review import (
    SEMANTIC_REVIEW_SCOPE,
    ContextSemanticReviewLedger,
)
from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.evidence_context import EvidenceContextCompiler
from spst_runtime.interfaces.model_adapter import ModelAdapter
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.provider_observation import (
    CODEX_CLI_OBSERVATION_SOURCE,
    build_provider_observation,
    build_provider_request_binding,
)
from spst_runtime.real_paired_outcome import (
    PROGRAM_EXECUTION_PREFIX,
    PROGRAM_RECORD_PREFIX,
    PROVIDER_EXECUTION_AUTHORITY_SCHEMA,
    PROVIDER_EXECUTION_AUTHORITY_V2_SCHEMA,
    RealPairedOutcomeProgram,
)
from spst_runtime.real_paired_outcome_bridge import main as outcome_bridge_main
from spst_runtime.live_pairing import canonical_live_pair_hash


NOW = datetime(2026, 7, 22, 20, 0, tzinfo=timezone.utc)


class ProgramObservableAdapter(ModelAdapter):
    def __init__(
        self,
        *,
        observation_source: str = "in_process_provider_echo",
        declared_observation_source: str | None = None,
        execution_environment: str = "in_process",
        billing_class: str = "no_charge",
        observed_provider_name: str = "program-observable-provider",
        observed_model_version: str | None = None,
    ):
        self.observation_source = observation_source
        self.declared_observation_source = (
            declared_observation_source or observation_source
        )
        self.execution_environment = execution_environment
        self.billing_class = billing_class
        self.observed_provider_name = observed_provider_name
        self.observed_model_version = observed_model_version
        self.calls = 0
        self.model_version = "real-paired-program-v1"

    async def infer(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        source = context or {}
        has_context = isinstance(source.get("evidence_context_intervention"), dict)
        text = json.dumps({"answer": 1 if has_context else 0})
        request = build_provider_request_binding(prompt, source)
        observation = build_provider_observation(
            request,
            provider_name=self.observed_provider_name,
            model_version=self.observed_model_version or self.model_version,
            response_id=f"program-response-{self.calls:04d}",
            response_status="completed",
            output_text=text,
            observation_source=self.observation_source,
            acknowledged_request_binding_sha256=request["request_binding_sha256"],
        )
        return {
            "provider": self.observed_provider_name,
            "model_version": self.observed_model_version or self.model_version,
            "available": True,
            "text": text,
            "provider_observation": observation,
        }

    def health(self) -> dict[str, Any]:
        health = {
            "ok": True,
            "provider": "program-observable-provider",
            "model_version": self.model_version,
            "requires_api_key": self.execution_environment == "external_network",
        }
        if self.declared_observation_source == CODEX_CLI_OBSERVATION_SOURCE:
            health["requires_api_key"] = False
            health["authentication_mode"] = "chatgpt_cached_session_required"
        return health

    def get_capabilities(self) -> dict[str, Any]:
        capabilities = {
            "interface": "ModelAdapter",
            "supports_structured_evaluation": True,
            "supports_provider_observation": True,
            "provider_observation_source": self.declared_observation_source,
            "execution_environment": self.execution_environment,
            "billing_class": self.billing_class,
        }
        if self.declared_observation_source == CODEX_CLI_OBSERVATION_SOURCE:
            capabilities.update(
                {
                    "authentication_mode": "chatgpt_cached_session_required",
                    "api_key_environment_scrubbed": True,
                    "ephemeral_session_required": True,
                    "read_only_sandbox_required": True,
                    "structured_output_binding_required": True,
                }
            )
        return capabilities


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _context_intervention(tmp_path: Path, *, suffix: str = "primary") -> dict[str, Any]:
    source_repository = tmp_path / f"context-source-{suffix}"
    source_repository.mkdir(parents=True)
    _git(source_repository, "init", "-q")
    _git(source_repository, "config", "user.name", "SPST Test")
    _git(source_repository, "config", "user.email", "spst@example.invalid")
    _git(source_repository, "config", "core.autocrlf", "false")
    source_file = source_repository / "reviewed-context.md"
    source_file.write_text(
        f"Bound context evidence for the {suffix} real-pair program fixture.\n",
        encoding="utf-8",
    )
    _git(source_repository, "add", ".")
    _git(source_repository, "commit", "-qm", "register reviewed context source")
    session_path = tmp_path / f"context-session-{suffix}.db"
    memory_path = tmp_path / f"context-memory-{suffix}.db"
    receipt_id = run_chat_turn(
        f"Compile the {suffix} real-pair program context.",
        steps=1,
        profile="strict",
        session_path=str(session_path),
        memory_path=str(memory_path),
        repository_root=str(source_repository),
    )["routing_receipt"]["receipt_id"]
    compiler = EvidenceContextCompiler(
        repository_root=source_repository,
        session_path=session_path,
        memory_path=memory_path,
    )
    compiled = compiler.compile_repository_file(
        producer_receipt_id=receipt_id,
        artifact_kind="architecture_decision",
        title=f"Real paired outcome {suffix} context",
        statement=f"The {suffix} fixture supplies one bounded JSON answer context.",
        relative_path="reviewed-context.md",
        tags=["arch-06", "real-paired-outcome"],
        now=NOW,
    )
    record = next(
        item
        for item in compiler.memory.list_all()
        if item.id == compiled["memory_record"]["id"]
    )
    structural = compiler.verifier.verify_record(record, now=NOW)
    reviews = ContextSemanticReviewLedger(str(memory_path))
    reviewed = reviews.review(
        record,
        structural,
        {
            "reviewer_id": f"semantic-reviewer-{suffix}",
            "reviewer_kind": "human",
            "review_scope": SEMANTIC_REVIEW_SCOPE,
            "decision": "supported",
            "artifact_sha256": compiled["artifact"]["artifact_sha256"],
            "source_sha256": compiled["artifact"]["source"]["source_sha256"],
            "review_note": "The bounded statement is supported by the exact source.",
        },
        now=NOW,
    )
    assert reviewed["semantic_support"]["verified"] is True
    return reviews.build_intervention(record, structural)


def _task(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "prompt": f"Return the exact JSON answer for outcome task {task_id}.",
        "domain": "real-paired-outcome",
        "split": "holdout",
        "required_markers": [],
        "expected_json_keys": ["answer"],
        "quality_rubric": {
            "schema": "task-specific-json-rubric-v1",
            "criteria": [
                {
                    "id": "correct-answer",
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


def _authority(
    *,
    external: bool = False,
    paid: bool = False,
    chatgpt_plan: bool | None = None,
    maximum_adapter_invocations: int = 32,
) -> dict[str, Any]:
    authority = {
        "schema": (
            PROVIDER_EXECUTION_AUTHORITY_V2_SCHEMA
            if chatgpt_plan is not None
            else PROVIDER_EXECUTION_AUTHORITY_SCHEMA
        ),
        "external_provider_calls_authorized": external,
        "paid_provider_calls_authorized": paid,
        "maximum_adapter_invocations": maximum_adapter_invocations,
    }
    if chatgpt_plan is not None:
        authority["chatgpt_plan_usage_authorized"] = chatgpt_plan
    return authority


def _registration_payload(
    task_ids: list[str],
    intervention: dict[str, Any],
    *,
    authority: dict[str, Any] | None = None,
    workload_class: str = "test_fixture",
    baseline_candidate_id: str = "normal-control",
    candidate_id: str = "spst-treatment",
) -> dict[str, Any]:
    return {
        "baseline_candidate_id": baseline_candidate_id,
        "candidate_id": candidate_id,
        "context_intervention": intervention,
        "execution_authority": authority or _authority(),
        "study": {"workload_class": workload_class},
        "task_ids": task_ids,
    }


def _program_fixture(
    tmp_path: Path,
    *,
    adapter: ProgramObservableAdapter | None = None,
    sample_count: int = 8,
    workload_class: str = "test_fixture",
    authority: dict[str, Any] | None = None,
    reverse_registration: bool = False,
) -> tuple[
    SQLiteRepository,
    RealPairedOutcomeProgram,
    ProgramObservableAdapter,
    dict[str, Any],
    dict[str, Any],
]:
    repository = SQLiteRepository(str(tmp_path / "outcome-program.db"))
    corpus = OperationalEvaluationCorpus(repository)
    task_ids = [f"outcome-{index:02d}" for index in range(sample_count)]
    registration_order = list(reversed(task_ids)) if reverse_registration else task_ids
    for task_id in registration_order:
        assert corpus.register(_task(task_id))["status"] == "registered"
    intervention = _context_intervention(tmp_path)
    selected_adapter = adapter or ProgramObservableAdapter()
    program = RealPairedOutcomeProgram(
        repository,
        corpus,
        selected_adapter,
        plan_nonce_factory=lambda: "a" * 64,
        scoring_nonce_factory=lambda: "b" * 64,
    )
    registered = program.register(
        _registration_payload(
            task_ids,
            intervention,
            authority=authority,
            workload_class=workload_class,
        )
    )
    return repository, program, selected_adapter, intervention, registered


def _review_payload(program: dict[str, Any]) -> dict[str, Any]:
    surface = program["blind_review"]["surface"]
    return {
        "reviewer_id": "independent-outcome-reviewer",
        "reviewer_kind": "human",
        "review_scope": "paired_quality_scoring_artifact",
        "decision": "accepted",
        "scoring_artifact_digest": surface["scoring_artifact"]["artifact_digest"],
        "blind_review_surface_sha256": surface["surface_sha256"],
        "arm_mapping_not_accessed": True,
        "reviewer_independence_attested": True,
    }


def test_preregistered_fixture_reaches_only_mechanism_validation(
    tmp_path: Path,
    capsys,
):
    repository, program, adapter, intervention, registered = _program_fixture(
        tmp_path
    )
    assert registered["status"] == "registered"
    assert registered["preregistration"]["per_task_order_disclosed"] is False
    assert registered["preregistration"]["stop_rule"] == {
        "target_pair_count": 8,
        "minimum_pair_count": 8,
        "optional_stopping_permitted": False,
    }
    assert adapter.calls == 0
    preflight_db_sha256 = hashlib.sha256(Path(repository.path).read_bytes()).hexdigest()
    preflight = program.preflight(registered["id"])
    assert preflight["status"] == "ready"
    assert preflight["adapter_invocations_executed"] == 0
    assert preflight["state_changed"] is False
    assert (
        hashlib.sha256(Path(repository.path).read_bytes()).hexdigest()
        == preflight_db_sha256
    )

    executed = program.execute(
        registered["id"],
        context_intervention=intervention,
    )
    assert executed["status"] == "pending_human_review"
    assert executed["execution"]["actual_adapter_invocations"] == 32
    assert executed["execution"]["expected_adapter_invocations"] == 32
    assert executed["execution"]["registration_order"]["verified"] is True
    assert executed["execution"]["evidence_class"] == "mechanism_validation"
    assert executed["blind_review"]["source_pairs_disclosed"] is False
    assert executed["outcome"]["measurement_available"] is False
    assert adapter.calls == 32

    review_payload = _review_payload(executed)
    assert outcome_bridge_main(
        [
            "review",
            "--evaluation-db",
            repository.path,
            "--program-id",
            registered["id"],
            "--reviewer-id",
            review_payload["reviewer_id"],
            "--decision",
            review_payload["decision"],
            "--scoring-artifact-digest",
            review_payload["scoring_artifact_digest"],
            "--blind-review-surface-sha256",
            review_payload["blind_review_surface_sha256"],
            "--arm-mapping-not-accessed",
            "--reviewer-independence-attested",
        ]
    ) == 0
    reviewed = json.loads(capsys.readouterr().out)
    assert reviewed["status"] == "mechanism_validation_only"
    assert reviewed["reason"] == "in_process_provider_is_not_real_outcome_evidence"
    assert reviewed["outcome"]["measurement_available"] is True
    assert reviewed["outcome"]["sample_count"] == 8
    assert reviewed["outcome"]["positive_effect_supported"] is True
    assert reviewed["outcome"]["observed_real_workload_outcome"] is False
    assert (
        reviewed["outcome"]["real_paired_outcome_cryptographically_verified"]
        is False
    )
    assert reviewed["claims"]["general_model_quality_claimed"] is False
    assert repository.verify_provenance()["valid"] is True

    calls_before = adapter.calls
    duplicate = program.execute(
        registered["id"],
        context_intervention=intervention,
    )
    assert duplicate["status"] == "blocked"
    assert duplicate["reason"] == "real_paired_outcome_execution_already_recorded"
    assert adapter.calls == calls_before


def test_task_set_binding_is_independent_of_corpus_insertion_order(tmp_path: Path):
    _, program, adapter, _, registered = _program_fixture(
        tmp_path,
        reverse_registration=True,
    )
    assert registered["status"] == "registered"
    assert registered["corpus"]["task_ids"] == [
        f"outcome-{index:02d}" for index in range(8)
    ]
    assert [item["task_id"] for item in registered["corpus"]["task_contracts"]] == [
        f"outcome-{index:02d}" for index in range(8)
    ]
    assert program.preflight(registered["id"])["status"] == "ready"
    assert adapter.calls == 0


def test_registration_contract_rejects_invalid_inputs_before_provider_calls(
    tmp_path: Path,
):
    repository, program, adapter, intervention, registered = _program_fixture(tmp_path)
    task_ids = [f"outcome-{index:02d}" for index in range(8)]
    valid = _registration_payload(task_ids, intervention)

    cases = [
        ({**valid, "unexpected": True}, "real_paired_outcome_registration_shape_invalid"),
        (
            {**valid, "context_intervention": {}},
            "context_intervention_shape_invalid",
        ),
        (
            {**valid, "task_ids": [task_ids[0]] * 8},
            "real_paired_outcome_task_set_invalid",
        ),
        (
            {**valid, "task_ids": [*task_ids[:7], "outcome-missing"]},
            "real_paired_outcome_task_set_unavailable",
        ),
        (
            {**valid, "study": {"workload_class": "synthetic-claim"}},
            "real_paired_outcome_study_invalid",
        ),
        (
            {**valid, "execution_authority": {"schema": "wrong"}},
            "real_paired_outcome_execution_authority_invalid",
        ),
        (
            {
                **valid,
                "execution_authority": _authority(maximum_adapter_invocations=31),
            },
            "real_paired_outcome_adapter_invocation_cap_too_low",
        ),
        (
            {**valid, "candidate_id": "unsafe candidate id"},
            "real_paired_outcome_candidate_id_invalid",
        ),
        (
            {
                **valid,
                "baseline_candidate_id": "same-arm",
                "candidate_id": "same-arm",
            },
            "real_paired_outcome_candidate_ids_not_distinct",
        ),
    ]
    for payload, reason in cases:
        blocked = program.register(payload)
        assert blocked["status"] == "blocked"
        assert blocked["reason"] == reason
        assert adapter.calls == 0

    duplicate = program.register(valid)
    assert duplicate["status"] == "blocked"
    assert duplicate["reason"] == "real_paired_outcome_program_exists"
    assert adapter.calls == 0

    read_only_repository = SQLiteRepository(repository.path, read_only=True)
    read_only_program = RealPairedOutcomeProgram(
        read_only_repository,
        OperationalEvaluationCorpus(read_only_repository),
        adapter,
    )
    read_only = read_only_program.register(valid)
    assert read_only["status"] == "blocked"
    assert read_only["reason"] == "real_paired_outcome_store_read_only"
    assert adapter.calls == 0

    no_adapter = RealPairedOutcomeProgram(repository, program.corpus)
    missing_adapter = no_adapter.register(valid)
    assert missing_adapter["status"] == "blocked"
    assert missing_adapter["reason"] == "real_paired_outcome_adapter_required"
    assert registered["status"] == "registered"


def test_remote_metadata_echo_remains_observed_not_authenticated(tmp_path: Path):
    adapter = ProgramObservableAdapter(
        observation_source="https_response_metadata_echo",
        execution_environment="external_network",
        billing_class="no_charge",
    )
    _, program, _, intervention, registered = _program_fixture(
        tmp_path,
        adapter=adapter,
        workload_class="real_user_workload",
        authority=_authority(external=True),
    )
    executed = program.execute(
        registered["id"],
        context_intervention=intervention,
    )
    assert executed["status"] == "pending_human_review"
    assert executed["execution"]["evidence_class"] == "remote_transport_observed"
    reviewed = program.review(registered["id"], _review_payload(executed))
    assert reviewed["status"] == "reviewed_observed_real_workload"
    assert reviewed["outcome"]["observed_real_workload_outcome"] is True
    assert reviewed["outcome"]["provider_identity_cryptographically_verified"] is False
    assert reviewed["outcome"]["workload_provenance_verified"] is False
    assert (
        reviewed["outcome"]["real_paired_outcome_cryptographically_verified"]
        is False
    )
    assert reviewed["claims"]["causal_context_utility_established"] is False


def test_paid_or_unknown_provider_is_blocked_before_first_call(tmp_path: Path):
    adapter = ProgramObservableAdapter(
        observation_source="https_response_metadata_echo",
        execution_environment="external_network",
        billing_class="paid",
    )
    _, program, _, intervention, registered = _program_fixture(
        tmp_path,
        adapter=adapter,
        workload_class="real_user_workload",
        authority=_authority(external=True, paid=False),
    )
    preflight = program.preflight(registered["id"])
    assert preflight["status"] == "blocked"
    assert preflight["reason"] == "paid_or_unknown_provider_calls_not_authorized"
    assert adapter.calls == 0
    blocked = program.execute(
        registered["id"],
        context_intervention=intervention,
    )
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "paid_or_unknown_provider_calls_not_authorized"
    assert adapter.calls == 0


def test_chatgpt_plan_provider_requires_exact_v2_authority_before_calls(
    tmp_path: Path,
):
    legacy_adapter = ProgramObservableAdapter(
        observation_source=CODEX_CLI_OBSERVATION_SOURCE,
        execution_environment="external_network",
        billing_class="chatgpt_plan_usage",
    )
    _, legacy_program, _, _, legacy_registration = _program_fixture(
        tmp_path / "legacy-authority",
        adapter=legacy_adapter,
        workload_class="real_user_workload",
        authority=_authority(external=True),
    )
    legacy = legacy_program.preflight(legacy_registration["id"])
    assert legacy["status"] == "blocked"
    assert legacy["reason"] == "chatgpt_plan_usage_not_authorized"
    assert legacy_adapter.calls == 0

    blocked_adapter = ProgramObservableAdapter(
        observation_source=CODEX_CLI_OBSERVATION_SOURCE,
        execution_environment="external_network",
        billing_class="chatgpt_plan_usage",
    )
    _, blocked_program, _, _, blocked_registration = _program_fixture(
        tmp_path / "blocked",
        adapter=blocked_adapter,
        workload_class="real_user_workload",
        authority=_authority(external=True, chatgpt_plan=False),
    )
    blocked = blocked_program.preflight(blocked_registration["id"])
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "chatgpt_plan_usage_not_authorized"
    assert blocked_adapter.calls == 0

    allowed_adapter = ProgramObservableAdapter(
        observation_source=CODEX_CLI_OBSERVATION_SOURCE,
        execution_environment="external_network",
        billing_class="chatgpt_plan_usage",
    )
    _, allowed_program, _, allowed_intervention, allowed_registration = _program_fixture(
        tmp_path / "allowed",
        adapter=allowed_adapter,
        workload_class="real_user_workload",
        authority=_authority(external=True, chatgpt_plan=True),
    )
    ready = allowed_program.preflight(allowed_registration["id"])
    assert ready["status"] == "ready"
    assert ready["chatgpt_plan_usage_authorized"] is True
    assert ready["paid_provider_calls_authorized"] is False
    assert allowed_adapter.calls == 0
    executed = allowed_program.execute(
        allowed_registration["id"],
        context_intervention=allowed_intervention,
    )
    assert executed["status"] == "pending_human_review"
    assert executed["execution"]["evidence_class"] == "remote_transport_observed"
    reviewed = allowed_program.review(
        allowed_registration["id"],
        _review_payload(executed),
    )
    assert reviewed["status"] == "reviewed_observed_real_workload"
    assert reviewed["outcome"]["observed_real_workload_outcome"] is True
    assert reviewed["outcome"]["provider_identity_cryptographically_verified"] is False
    assert reviewed["outcome"]["real_paired_outcome_cryptographically_verified"] is False

    fixture_adapter = ProgramObservableAdapter(
        observation_source=CODEX_CLI_OBSERVATION_SOURCE,
        execution_environment="external_network",
        billing_class="chatgpt_plan_usage",
    )
    (
        _,
        fixture_program,
        _,
        fixture_intervention,
        fixture_registration,
    ) = _program_fixture(
        tmp_path / "fixture-relabel",
        adapter=fixture_adapter,
        workload_class="test_fixture",
        authority=_authority(external=True, chatgpt_plan=True),
    )
    fixture_executed = fixture_program.execute(
        fixture_registration["id"],
        context_intervention=fixture_intervention,
    )
    fixture_reviewed = fixture_program.review(
        fixture_registration["id"],
        _review_payload(fixture_executed),
    )
    assert fixture_reviewed["status"] == "reviewed_fixture_outcome"
    assert fixture_reviewed["reason"] == "fixture_workload_is_not_real_outcome_evidence"
    assert fixture_reviewed["outcome"]["observed_real_workload_outcome"] is False


def test_source_relabel_and_provider_drift_fail_closed(tmp_path: Path):
    adapter = ProgramObservableAdapter(
        observation_source="in_process_provider_echo",
        declared_observation_source="https_response_metadata_echo",
        execution_environment="external_network",
        billing_class="no_charge",
    )
    _, program, _, intervention, registered = _program_fixture(
        tmp_path / "source-mismatch",
        adapter=adapter,
        authority=_authority(external=True),
    )
    mismatched = program.execute(
        registered["id"],
        context_intervention=intervention,
    )
    assert mismatched["status"] == "blocked"
    assert mismatched["reason"] == (
        "real_paired_outcome_provider_observation_source_mismatch"
    )
    assert mismatched["outcome"]["measurement_available"] is False

    _, drift_program, drift_adapter, drift_intervention, drift_registered = (
        _program_fixture(tmp_path / "provider-drift")
    )
    drift_adapter.model_version = "changed-after-registration"
    blocked = drift_program.execute(
        drift_registered["id"],
        context_intervention=drift_intervention,
    )
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "real_paired_outcome_provider_contract_mismatch"
    assert drift_adapter.calls == 0

    observation_drift_adapter = ProgramObservableAdapter(
        observed_provider_name="different-observed-provider",
        observed_model_version="different-observed-model",
    )
    (
        _,
        observation_drift_program,
        _,
        observation_drift_intervention,
        observation_drift_registered,
    ) = _program_fixture(
        tmp_path / "observation-provider-drift",
        adapter=observation_drift_adapter,
    )
    observation_drift = observation_drift_program.execute(
        observation_drift_registered["id"],
        context_intervention=observation_drift_intervention,
    )
    assert observation_drift["status"] == "blocked"
    assert observation_drift["reason"] == (
        "real_paired_outcome_provider_identity_mismatch"
    )
    assert observation_drift_adapter.calls == 32


def test_changed_intervention_and_insufficient_cohort_are_rejected(tmp_path: Path):
    _, program, adapter, _, registered = _program_fixture(tmp_path / "mismatch")
    other_intervention = _context_intervention(
        tmp_path / "mismatch",
        suffix="alternate",
    )
    blocked = program.execute(
        registered["id"],
        context_intervention=other_intervention,
    )
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "real_paired_outcome_intervention_mismatch"
    assert adapter.calls == 0

    _, _, _, _, insufficient = _program_fixture(
        tmp_path / "insufficient",
        sample_count=7,
        authority=_authority(maximum_adapter_invocations=28),
    )
    assert insufficient["status"] == "blocked"
    assert insufficient["reason"] == "real_paired_outcome_minimum_pairs_not_met"


def test_resigned_program_tampering_and_call_cap_fail_closed(tmp_path: Path):
    repository, program, adapter, intervention, registered = _program_fixture(
        tmp_path / "tamper"
    )
    key = f"{PROGRAM_RECORD_PREFIX}{registered['id']}"
    stored = asyncio.run(repository.load(key))
    assert isinstance(stored, dict)
    tampered = deepcopy(stored)
    tampered["execution_authority"]["maximum_adapter_invocations"] = 4096
    asyncio.run(repository.save(key, tampered))
    assert repository.verify_provenance()["valid"] is True
    revalidated = program.get(registered["id"])
    assert revalidated is not None
    assert revalidated["status"] == "blocked"
    assert revalidated["reason"] == "real_paired_outcome_program_digest_mismatch"
    assert adapter.calls == 0

    _, capped_program, capped_adapter, capped_intervention, capped_registered = (
        _program_fixture(tmp_path / "cap")
    )
    capped_record = asyncio.run(
        capped_program.repository.load(
            f"{PROGRAM_RECORD_PREFIX}{capped_registered['id']}"
        )
    )
    assert isinstance(capped_record, dict)
    capped_record["execution_authority"]["maximum_adapter_invocations"] = 31
    unsigned = {
        key: value
        for key, value in capped_record.items()
        if key not in {"id", "program_sha256"}
    }
    new_digest = hashlib.sha256(
        json.dumps(
            unsigned,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    capped_record["program_sha256"] = new_digest
    capped_record["id"] = f"RPOP-{new_digest[:16]}"
    asyncio.run(
        capped_program.repository.save(
            f"{PROGRAM_RECORD_PREFIX}{capped_registered['id']}",
            capped_record,
        )
    )
    blocked = capped_program.execute(
        capped_registered["id"],
        context_intervention=capped_intervention,
    )
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "real_paired_outcome_program_id_mismatch"
    assert capped_adapter.calls == 0


def test_posthoc_source_relabel_and_live_plan_resign_fail_closed(tmp_path: Path):
    repository, program, _, intervention, registered = _program_fixture(
        tmp_path / "source-relabel",
        workload_class="real_user_workload",
    )
    executed = program.execute(
        registered["id"],
        context_intervention=intervention,
    )
    assert executed["status"] == "pending_human_review"
    execution_key = f"{PROGRAM_EXECUTION_PREFIX}{registered['id']}"
    execution = asyncio.run(repository.load(execution_key))
    assert isinstance(execution, dict)
    relabelled = deepcopy(execution)
    relabelled["provider_observation_sources"] = [
        "https_response_metadata_echo"
    ]
    relabelled["evidence_class"] = "remote_transport_observed"
    relabelled_unsigned = {
        key: value
        for key, value in relabelled.items()
        if key != "execution_sha256"
    }
    relabelled["execution_sha256"] = canonical_live_pair_hash(
        relabelled_unsigned
    )
    asyncio.run(repository.save(execution_key, relabelled))
    assert repository.verify_provenance()["valid"] is True
    blocked = program.get(registered["id"])
    assert blocked is not None
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "real_paired_outcome_execution_recomputation_mismatch"

    plan_repository, plan_program, _, plan_intervention, plan_registered = (
        _program_fixture(tmp_path / "plan-resign")
    )
    plan_executed = plan_program.execute(
        plan_registered["id"],
        context_intervention=plan_intervention,
    )
    experiment_id = plan_executed["execution"]["experiment_id"]
    plan_key = f"runtime:live_context_plan:{experiment_id}"
    plan_record = asyncio.run(plan_repository.load(plan_key))
    assert isinstance(plan_record, dict)
    resigned = deepcopy(plan_record)
    resigned["adapter_invocations_started"] = True
    resigned_unsigned = {
        key: value for key, value in resigned.items() if key != "record_sha256"
    }
    resigned["record_sha256"] = canonical_live_pair_hash(resigned_unsigned)
    asyncio.run(plan_repository.save(plan_key, resigned))
    assert plan_repository.verify_provenance()["valid"] is True
    plan_blocked = plan_program.get(plan_registered["id"])
    assert plan_blocked is not None
    assert plan_blocked["status"] == "blocked"
    assert plan_blocked["reason"] == "real_paired_outcome_live_plan_mismatch"


def test_read_only_bridge_preserves_database_and_missing_program(
    tmp_path: Path,
    capsys,
):
    repository, program, _, _, registered = _program_fixture(tmp_path)
    before = hashlib.sha256(Path(repository.path).read_bytes()).hexdigest()
    assert outcome_bridge_main(
        [
            "get",
            "--evaluation-db",
            repository.path,
            "--program-id",
            registered["id"],
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "registered"
    assert outcome_bridge_main(
        ["status", "--evaluation-db", repository.path]
    ) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["coverage"]["total_programs"] == 1
    assert hashlib.sha256(Path(repository.path).read_bytes()).hexdigest() == before

    assert outcome_bridge_main(
        [
            "get",
            "--evaluation-db",
            repository.path,
            "--program-id",
            "RPOP-missing",
        ]
    ) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "not_found"
    assert hashlib.sha256(Path(repository.path).read_bytes()).hexdigest() == before
    assert program.coverage()["cryptographically_verified_real_outcome_count"] == 0
