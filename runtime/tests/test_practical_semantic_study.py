from __future__ import annotations

from copy import deepcopy
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess

from spst_runtime.chat_bridge import get_chat_status, run_chat_turn
from spst_runtime.evaluation.practical_semantic_study import (
    CORRECTION_POLICY,
    CORRECTION_SCHEMA,
    EXECUTION_AUTHORITY_SCHEMA,
    INTERVENTION_SCHEMA,
    REVIEW_POLICY,
    REVIEW_SCHEMA,
    RECORD_PREFIX,
    PracticalSemanticStudyLedger,
)
from spst_runtime.evaluation.practical_semantic_runner import (
    PracticalSemanticRunnerError,
    PracticalSemanticStudyRunner,
    treatment_instructions,
)
from spst_runtime.interfaces.model_adapter import ModelAdapter
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.provider_observation import (
    CODEX_CLI_OBSERVATION_SOURCE,
    build_provider_observation,
    build_provider_request_binding,
)
from spst_runtime.repository_identity import capture_repository_identity


TREATMENT_INSTRUCTIONS = "Apply the preregistered SPST process guidance."
SPEND_GUARD = {
    "schema": "codex_app_server_rate_limits_v1",
    "account_type": "chatgpt",
    "plan_type": "plus",
    "included_usage_percent": 10,
    "maximum_included_usage_percent": 95,
    "spendable_credits_present": False,
    "credit_balance_zero": True,
    "rate_limit_reached": False,
    "source": "codex_app_server_account_rate_limits_read",
    "source_authenticated": False,
}


class _FakeCodexAdapter(ModelAdapter):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def health(self) -> dict:
        return {"ok": True}

    def get_capabilities(self) -> dict:
        return {
            "requires_api_key": False,
            "billing_class": "chatgpt_plan_usage",
            "spendable_credits_must_be_absent": True,
            "included_plan_rate_limit_must_be_available": True,
            "api_key_environment_scrubbed": True,
            "ephemeral_session_required": True,
            "read_only_sandbox_required": True,
            "structured_output_binding_required": True,
            "supports_provider_observation": True,
            "provider_observation_source": CODEX_CLI_OBSERVATION_SOURCE,
        }

    async def infer(self, prompt: str, context: dict | None = None) -> dict:
        source = context or {}
        condition = source["evaluation"]["condition"]
        response_id = f"fake-{len(self.calls):03d}-{condition}"
        self.calls.append(condition)
        text = json.dumps(
            {
                "diagnosis": f"{condition} diagnosis",
                "minimal_patch": f"{condition} patch",
                "verification": f"{condition} verification",
                "residual_risk": f"{condition} risk",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        request = build_provider_request_binding(prompt, source)
        observation = build_provider_observation(
            request,
            provider_name="codex-cli-chatgpt",
            model_version="gpt-5.6-sol",
            response_id=response_id,
            response_status="completed",
            output_text=text,
            observation_source=CODEX_CLI_OBSERVATION_SOURCE,
            acknowledged_request_binding_sha256=request["request_binding_sha256"],
            execution_guard=SPEND_GUARD,
        )
        return {
            "provider": "codex-cli-chatgpt",
            "model_version": "gpt-5.6-sol",
            "text": text,
            "provider_observation": observation,
        }


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "SPST Test")
    _git(repository, "config", "user.email", "spst@example.invalid")
    _git(repository, "config", "core.autocrlf", "false")
    (repository / "README.md").write_text("semantic study fixture\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "initialize fixture")
    return repository


def _task_pack(count: int = 30) -> dict:
    return {
        "schema": "spst-novel-practical-task-pack-v1",
        "workload_class": "novel_practical_repository_task",
        "generalization_target": "bounded repository tasks",
        "tasks": [
            {
                "task_id": f"novel-{index:02d}",
                "domain": f"domain-{index % 6}",
                "prompt": f"Diagnose novel repository failure {index:02d} without a reference answer.",
            }
            for index in range(count)
        ],
    }


def _preregistration(repository: Path, count: int = 30) -> dict:
    return {
        "task_pack": _task_pack(count),
        "generator": {
            "generator_id": "codex-cli-generator",
            "generator_kind": "model_adapter",
        },
        "intervention": {
            "schema": INTERVENTION_SCHEMA,
            "context_sha256": "c" * 64,
            "model_instructions_sha256": hashlib.sha256(
                TREATMENT_INSTRUCTIONS.encode("utf-8")
            ).hexdigest(),
            "context_kind": "spst_process_guidance",
            "task_specific_answers_absent_attested": True,
        },
        "review_policy": deepcopy(REVIEW_POLICY),
        "correction_policy": deepcopy(CORRECTION_POLICY),
        "stop_rule": {
            "target_pair_count": count,
            "minimum_pair_count": 30,
            "maximum_pair_count": 50,
            "optional_stopping_permitted": False,
            "automatic_retry_permitted": False,
            "partial_execution_claim_eligible": False,
        },
        "repository_identity": capture_repository_identity(repository),
    }


def _score_payload(blind: dict, *, reviewer_id: str = "human-reviewer") -> dict:
    scores = []
    for slot in blind["slots"]:
        treatment = "treatment" in slot["artifact_text"]
        value = 4 if treatment else 0
        scores.append(
            {
                "slot_id": slot["slot_id"],
                "criteria": {criterion: value for criterion in REVIEW_POLICY["criterion_ids"]},
                "comment": "",
            }
        )
    return {
        "schema": REVIEW_SCHEMA,
        "reviewer_id": reviewer_id,
        "reviewer_kind": "human",
        "decision": "scored",
        "blind_artifact_sha256": blind["blind_artifact_sha256"],
        "arm_mapping_not_accessed": True,
        "reviewer_independence_attested": True,
        "scores": scores,
    }


def _execution_authority(registered: dict) -> dict:
    return {
        "schema": EXECUTION_AUTHORITY_SCHEMA,
        "decision": "authorized",
        "authority_label": "owner-authority",
        "study_id": registered["id"],
        "study_sha256": registered["study_sha256"],
        "repository_identity_sha256": registered["repository_identity"][
            "identity_sha256"
        ],
        "task_pack_sha256": registered["task_pack"]["task_pack_sha256"],
        "intervention_sha256": registered["intervention"]["context_sha256"],
        "external_provider_calls_authorized": True,
        "chatgpt_plan_usage_authorized": True,
        "paid_provider_calls_authorized": False,
        "maximum_adapter_invocations": 2
        * registered["stop_rule"]["target_pair_count"],
        "per_call_no_paid_guard_required": True,
        "optional_stopping_permitted": False,
        "automatic_retry_permitted": False,
    }


def _arm(task: dict, condition: str) -> dict:
    artifact_text = json.dumps(
        {
            "diagnosis": f"{condition} diagnosis for {task['task_id']}",
            "minimal_patch": f"{condition} patch",
            "verification": f"{condition} verification",
            "residual_risk": f"{condition} residual risk",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    context = {"instructions": TREATMENT_INSTRUCTIONS} if condition == "treatment" else {}
    request = build_provider_request_binding(task["prompt"], context)
    response_id = f"{condition}-{task['task_id']}"
    observation = build_provider_observation(
        request,
        provider_name="codex-cli-chatgpt",
        model_version="gpt-5.6-sol",
        response_id=response_id,
        response_status="completed",
        output_text=artifact_text,
        observation_source=CODEX_CLI_OBSERVATION_SOURCE,
        acknowledged_request_binding_sha256=request["request_binding_sha256"],
        execution_guard=SPEND_GUARD,
    )
    return {
        "artifact_text": artifact_text,
        "producer_response_id": observation["response"]["response_id_sha256"],
        "provider_observation": observation,
    }


def _complete_study(tmp_path: Path) -> tuple[Path, Path, PracticalSemanticStudyLedger, dict, dict]:
    source_repository = _repository(tmp_path)
    session_path = tmp_path / "study.db"
    memory_path = tmp_path / "memory.db"
    writable = SQLiteRepository(str(session_path))
    ledger = PracticalSemanticStudyLedger(
        writable,
        repository_root=str(source_repository),
        nonce_factory=lambda: "fixed-blinding-nonce",
    )
    registered = ledger.preregister(_preregistration(source_repository))
    assert registered["status"] == "collecting_pairs"
    authorized = ledger.authorize_execution(
        registered["id"],
        _execution_authority(registered),
    )
    assert authorized["execution_authority"]["status"] == "authorized"

    for task in registered["task_pack"]["tasks"]:
        receipt = run_chat_turn(
            task["prompt"],
            steps=1,
            profile="light",
            session_path=str(session_path),
            memory_path=str(memory_path),
            repository_root=str(source_repository),
        )["routing_receipt"]
        pair = ledger.record_pair(
            registered["id"],
            {
                "task_id": task["task_id"],
                "task_contract_sha256": task["task_contract_sha256"],
                "routing_receipt_id": receipt["receipt_id"],
                "baseline": _arm(task, "baseline"),
                "treatment": _arm(task, "treatment"),
            },
        )
        assert pair["status"] in {"collecting_pairs", "pending_human_review"}
    blind = ledger.blind_artifact(registered["id"])
    return source_repository, session_path, ledger, registered, blind


def test_30_pair_study_requires_receipts_role_separation_and_read_only_status(tmp_path):
    source_repository, session_path, ledger, registered, blind = _complete_study(tmp_path)
    assert blind["slot_count"] == 60

    conflicted = ledger.submit_review(
        registered["id"],
        _score_payload(blind, reviewer_id="codex-cli-generator"),
    )
    assert conflicted["reason"] == "practical_semantic_reviewer_generator_role_conflict"

    reviewed = ledger.submit_review(registered["id"], _score_payload(blind))
    assert reviewed["status"] == "pending_unblinding"
    assert reviewed["human_review"]["reviewer_role_separated"] is True

    correction = {
        "schema": CORRECTION_SCHEMA,
        "reviewer_id": "human-reviewer",
        "reason": "clerical_data_entry_error",
        "prior_review_sha256": reviewed["human_review"]["latest_review_sha256"],
        "blind_artifact_sha256": blind["blind_artifact_sha256"],
        "arm_mapping_not_accessed_since_prior_review": True,
        "scores": _score_payload(blind)["scores"],
    }
    corrected = ledger.correct_review(registered["id"], correction)
    assert corrected["human_review"]["correction_count"] == 1

    finalized = ledger.finalize(registered["id"])
    assert finalized["status"] == "reviewed_semantic_evidence"
    assert finalized["routing_receipts"] == {
        **finalized["routing_receipts"],
        "verified_count": 30,
        "required_count": 30,
        "coverage": 1.0,
        "complete": True,
    }
    assert finalized["measurement"]["paired_delta"] == 1.0
    assert finalized["measurement"]["direction"] == "beneficial"
    assert finalized["task_quality"]["claim_eligible"] is True

    before = hashlib.sha256(session_path.read_bytes()).hexdigest()
    status = get_chat_status(str(session_path), repository_root=str(source_repository))
    after = hashlib.sha256(session_path.read_bytes()).hexdigest()
    assert before == after
    assert status["practical_semantic_study"]["read_only_projection"] is True
    assert status["routing"]["task_quality_delta"] == 1.0
    assert status["routing"]["task_quality_pair_count"] == 30
    assert status["routing"]["task_quality_study_id"] == registered["id"]


def test_task_count_receipt_mismatch_reuse_and_post_unblind_correction_fail_closed(tmp_path):
    source_repository = _repository(tmp_path)
    session_path = tmp_path / "failure.db"
    memory_path = tmp_path / "failure-memory.db"
    ledger = PracticalSemanticStudyLedger(
        SQLiteRepository(str(session_path)),
        repository_root=str(source_repository),
        nonce_factory=lambda: "fixed-nonce",
    )
    too_small = ledger.preregister(_preregistration(source_repository, 29))
    assert too_small["reason"] == "practical_semantic_task_count_invalid"
    too_large = ledger.preregister(_preregistration(source_repository, 51))
    assert too_large["reason"] == "practical_semantic_task_count_invalid"

    registered = ledger.preregister(_preregistration(source_repository))
    first, second = registered["task_pack"]["tasks"][:2]
    receipt = run_chat_turn(
        first["prompt"],
        steps=1,
        profile="light",
        session_path=str(session_path),
        memory_path=str(memory_path),
        repository_root=str(source_repository),
    )["routing_receipt"]
    base_pair = {
        "task_id": first["task_id"],
        "task_contract_sha256": first["task_contract_sha256"],
        "routing_receipt_id": receipt["receipt_id"],
        "baseline": _arm(first, "baseline"),
        "treatment": _arm(first, "treatment"),
    }
    assert ledger.record_pair(registered["id"], base_pair)["reason"] == (
        "practical_semantic_execution_authority_required"
    )
    invalid_authority = _execution_authority(registered)
    invalid_authority["maximum_adapter_invocations"] -= 1
    assert ledger.authorize_execution(
        registered["id"], invalid_authority
    )["reason"] == "practical_semantic_execution_authority_invalid"
    assert ledger.authorize_execution(
        registered["id"],
        _execution_authority(registered),
    )["execution_authority"]["status"] == "authorized"
    assert ledger.record_pair(registered["id"], base_pair)["status"] == "collecting_pairs"

    reused = deepcopy(base_pair)
    reused["task_id"] = second["task_id"]
    reused["task_contract_sha256"] = second["task_contract_sha256"]
    assert ledger.record_pair(registered["id"], reused)["reason"] == (
        "practical_semantic_routing_receipt_reused"
    )

    mismatched_receipt = run_chat_turn(
        "a different task",
        steps=1,
        profile="light",
        session_path=str(session_path),
        memory_path=str(memory_path),
        repository_root=str(source_repository),
    )["routing_receipt"]
    mismatched = deepcopy(reused)
    mismatched["routing_receipt_id"] = mismatched_receipt["receipt_id"]
    assert ledger.record_pair(registered["id"], mismatched)["reason"] == (
        "practical_semantic_routing_receipt_task_mismatch"
    )

    second_receipt = run_chat_turn(
        second["prompt"],
        steps=1,
        profile="light",
        session_path=str(session_path),
        memory_path=str(memory_path),
        repository_root=str(source_repository),
    )["routing_receipt"]
    altered_observation = {
        "task_id": second["task_id"],
        "task_contract_sha256": second["task_contract_sha256"],
        "routing_receipt_id": second_receipt["receipt_id"],
        "baseline": _arm(second, "baseline"),
        "treatment": _arm(second, "treatment"),
    }
    altered_observation["treatment"]["provider_observation"]["response"][
        "output_sha256"
    ] = "0" * 64
    assert ledger.record_pair(registered["id"], altered_observation)["reason"] == (
        "practical_semantic_treatment_provider_observation_invalid"
    )

    _, _, complete_ledger, complete, blind = _complete_study(tmp_path / "complete")
    reviewed = complete_ledger.submit_review(complete["id"], _score_payload(blind))
    finalized = complete_ledger.finalize(complete["id"])
    correction = {
        "schema": CORRECTION_SCHEMA,
        "reviewer_id": "human-reviewer",
        "reason": "clerical_data_entry_error",
        "prior_review_sha256": reviewed["human_review"]["latest_review_sha256"],
        "blind_artifact_sha256": blind["blind_artifact_sha256"],
        "arm_mapping_not_accessed_since_prior_review": True,
        "scores": _score_payload(blind)["scores"],
    }
    assert finalized["status"] == "reviewed_semantic_evidence"
    assert complete_ledger.correct_review(complete["id"], correction)["reason"] == (
        "practical_semantic_correction_after_unblinding_forbidden"
    )


def test_checked_in_task_pack_has_32_unique_tasks_and_no_reference_answers():
    path = (
        Path(__file__).resolve().parents[1]
        / "spst_runtime"
        / "evaluation"
        / "novel_practical_tasks_v1.json"
    )
    task_pack = json.loads(path.read_text(encoding="utf-8"))
    tasks = task_pack["tasks"]
    assert len(tasks) == 32
    assert len({task["task_id"] for task in tasks}) == 32
    assert len({task["prompt"] for task in tasks}) == 32
    serialized = json.dumps(task_pack, sort_keys=True).lower()
    assert "reference_answer" not in serialized
    assert "expected_patch" not in serialized


def test_preregistration_digest_tamper_is_detected_even_with_new_provenance(tmp_path):
    source_repository = _repository(tmp_path)
    database = SQLiteRepository(str(tmp_path / "tamper.db"))
    ledger = PracticalSemanticStudyLedger(database, repository_root=str(source_repository))
    registered = ledger.preregister(_preregistration(source_repository))
    key = f"{RECORD_PREFIX}{registered['id']}"
    stored = asyncio.run(database.load(key))
    assert isinstance(stored, dict)
    stored["events"][0]["generator"]["generator_id"] = "altered-generator"
    asyncio.run(database.save(key, stored))

    blocked = ledger.get(registered["id"])

    assert blocked is not None
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "practical_semantic_preregistration_digest_mismatch"
    assert blocked["task_quality"]["available"] is False


def test_fixed_runner_executes_60_guarded_calls_and_emits_scorable_blind_artifact(
    tmp_path,
):
    source_repository = _repository(tmp_path / "source")
    session_path = tmp_path / "isolated-study.db"
    memory_path = tmp_path / "isolated-memory.db"
    treatment_path = tmp_path / "treatment.json"
    treatment_value = {
        "schema": "spst-practical-semantic-treatment-context-v1",
        "authority": "process_guidance_only",
        "instructions": [TREATMENT_INSTRUCTIONS],
        "task_specific_answers_included": False,
        "reference_patches_included": False,
        "expected_scores_included": False,
        "arm_mapping_included": False,
    }
    treatment_path.write_text(
        json.dumps(treatment_value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    payload = _preregistration(source_repository, count=32)
    payload["intervention"] = {
        "schema": INTERVENTION_SCHEMA,
        "context_sha256": hashlib.sha256(treatment_path.read_bytes()).hexdigest(),
        "model_instructions_sha256": hashlib.sha256(
            treatment_instructions(treatment_value).encode("utf-8")
        ).hexdigest(),
        "context_kind": "spst_process_guidance",
        "task_specific_answers_absent_attested": True,
    }
    ledger = PracticalSemanticStudyLedger(
        SQLiteRepository(str(session_path)),
        repository_root=str(source_repository),
        nonce_factory=lambda: "fixed-runner-nonce",
    )
    registered = ledger.preregister(payload)
    assert registered["execution_plan"]["condition_order_balance"] == {
        "baseline_first": 16,
        "treatment_first": 16,
    }
    authorized = ledger.authorize_execution(
        registered["id"],
        _execution_authority(registered),
    )
    assert authorized["execution_authority"]["maximum_adapter_invocations"] == 64

    adapter = _FakeCodexAdapter()
    runner = PracticalSemanticStudyRunner(
        ledger,
        adapter,
        repository_root=str(source_repository),
        session_path=str(session_path),
        memory_path=str(memory_path),
        treatment_context_path=str(treatment_path),
    )
    before = hashlib.sha256(session_path.read_bytes()).hexdigest()
    preflight = runner.preflight(registered["id"])
    after = hashlib.sha256(session_path.read_bytes()).hexdigest()
    assert before == after
    assert preflight["provider_calls_executed"] == 0
    assert preflight["state_changed"] is False

    result = runner.execute(registered["id"])

    assert result["status"] == "pending_human_review"
    assert result["provider_calls_executed"] == 64
    assert result["completed_task_count"] == 32
    assert adapter.calls.count("baseline") == 32
    assert adapter.calls.count("treatment") == 32
    assert adapter.calls[:2] in (["baseline", "treatment"], ["treatment", "baseline"])
    blind = result["blind_artifact"]
    assert blind["slot_count"] == 64
    assert all(slot["prompt"] and slot["artifact_text"] for slot in blind["slots"])
    assert all("arm" not in slot and "condition" not in slot for slot in blind["slots"])
    assert ledger.get(registered["id"])["routing_receipts"]["verified_count"] == 32
    try:
        runner.preflight(registered["id"])
    except PracticalSemanticRunnerError as error:
        assert str(error) == "practical_semantic_fresh_execution_required"
    else:
        raise AssertionError("completed study must not be executed again")
