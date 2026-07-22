import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any

from spst_runtime.chat_bridge import run_chat_turn
from spst_runtime.context_review import (
    SEMANTIC_REVIEW_SCOPE,
    ContextSemanticReviewLedger,
)
from spst_runtime.context_utility import ContextUtilityAttributionLedger
from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.evaluation.producer_evidence import build_producer_evidence
from spst_runtime.evidence_context import EvidenceContextCompiler, EvidenceContextVerifier
from spst_runtime.interfaces.model_adapter import ModelAdapter
from spst_runtime.live_context_evaluation import LiveContextPairedEvaluator
from spst_runtime.paired_quality_bridge import main as paired_quality_bridge_main
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.provider_observation import (
    build_provider_observation,
    build_provider_request_binding,
    verify_provider_observation,
)
from spst_runtime.providers.local_codex_adapter import LocalCodexAdapter


NOW = datetime(2026, 7, 22, 18, 0, tzinfo=timezone.utc)


class ObservableContextAdapter(ModelAdapter):
    """In-process provider whose response explicitly echoes the request binding."""

    def __init__(
        self,
        *,
        effect: str = "benefit",
        acknowledgement_mismatch: bool = False,
        replay_response_id: bool = False,
    ):
        self.effect = effect
        self.acknowledgement_mismatch = acknowledgement_mismatch
        self.replay_response_id = replay_response_id
        self.calls = 0
        self.selected_condition_calls: list[tuple[str, str, int]] = []

    async def infer(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        source = context or {}
        evaluation = source.get("evaluation", {})
        live_execution = (
            evaluation.get("live_pair_execution")
            if isinstance(evaluation, dict)
            else None
        )
        if (
            isinstance(live_execution, dict)
            and evaluation.get("arm") == live_execution.get("selected_arm")
        ):
            self.selected_condition_calls.append(
                (
                    str(live_execution["task_id"]),
                    str(live_execution["condition"]),
                    int(live_execution["condition_position"]),
                )
            )
        has_context = isinstance(source.get("evidence_context_intervention"), dict)
        if self.effect == "benefit":
            answer = 1 if has_context else 0
        elif self.effect == "harm":
            answer = 0 if has_context else 1
        else:
            answer = 1
        text = json.dumps({"answer": answer})
        request_binding = build_provider_request_binding(prompt, source)
        acknowledged = (
            "0" * 64
            if self.acknowledgement_mismatch and has_context
            else request_binding["request_binding_sha256"]
        )
        response_id = (
            "observable-response-replayed"
            if self.replay_response_id
            else f"observable-response-{self.calls:04d}"
        )
        observation = build_provider_observation(
            request_binding,
            provider_name="observable-local-provider",
            model_version="observable-context-v1",
            response_id=response_id,
            response_status="completed",
            output_text=text,
            observation_source="in_process_provider_echo",
            acknowledged_request_binding_sha256=acknowledged,
        )
        return {
            "provider": "observable-local-provider",
            "model_version": "observable-context-v1",
            "available": True,
            "text": text,
            "provider_observation": observation,
        }

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": "observable-local-provider",
            "model_version": "observable-context-v1",
            "requires_api_key": False,
        }

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "interface": "ModelAdapter",
            "supports_structured_evaluation": True,
            "supports_provider_observation": True,
            "requires_api_key": False,
        }


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _context_fixture(tmp_path: Path) -> dict[str, Any]:
    repository = tmp_path / "repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "SPST Test")
    _git(repository, "config", "user.email", "spst@example.invalid")
    _git(repository, "config", "core.autocrlf", "false")
    (repository / "provider-context.md").write_text(
        "The local provider experiment supplies one bounded JSON-answer artifact.\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "provider observed context source")
    session = tmp_path / "session.db"
    memory = tmp_path / "memory.db"
    receipt = run_chat_turn(
        "Compile the provider-observed context experiment artifact.",
        steps=1,
        profile="strict",
        session_path=str(session),
        memory_path=str(memory),
        repository_root=str(repository),
    )["routing_receipt"]["receipt_id"]
    compiler = EvidenceContextCompiler(
        repository_root=repository,
        session_path=session,
        memory_path=memory,
    )
    compiled = compiler.compile_repository_file(
        producer_receipt_id=receipt,
        artifact_kind="architecture_decision",
        title="Provider-observed bounded context experiment",
        statement="The experiment supplies one source-bound JSON-answer artifact.",
        relative_path="provider-context.md",
        tags=["arch-04", "provider-observation"],
        now=NOW,
    )
    record = next(
        item
        for item in compiler.memory.list_all()
        if item.id == compiled["memory_record"]["id"]
    )
    structural = compiler.verifier.verify_record(record, now=NOW)
    reviews = ContextSemanticReviewLedger(str(memory))
    reviewed = reviews.review(
        record,
        structural,
        {
            "reviewer_id": "semantic-context-reviewer",
            "reviewer_kind": "human",
            "review_scope": SEMANTIC_REVIEW_SCOPE,
            "decision": "supported",
            "artifact_sha256": compiled["artifact"]["artifact_sha256"],
            "source_sha256": compiled["artifact"]["source"]["source_sha256"],
            "review_note": "The exact bounded statement is supported by the source.",
        },
        now=NOW,
    )
    return {
        "repository": repository,
        "session": session,
        "memory": memory,
        "compiler": compiler,
        "compiled": compiled,
        "record": record,
        "structural": structural,
        "reviews": reviews,
        "reviewed": reviewed,
        "intervention": reviews.build_intervention(record, structural),
    }


def _task(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "prompt": f"Return the exact JSON answer for live task {task_id}.",
        "domain": "provider-observed-context",
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


def _live_fixture(
    tmp_path: Path,
    context: dict[str, Any],
    adapter: ModelAdapter,
) -> tuple[SQLiteRepository, OperationalEvaluationCorpus, LiveContextPairedEvaluator, dict[str, Any]]:
    repository = SQLiteRepository(str(tmp_path / "live-evaluation.db"))
    corpus = OperationalEvaluationCorpus(repository)
    for index in range(8):
        assert corpus.register(_task(f"live-{index:02d}"))["status"] == "registered"
    nonces = iter(("a" * 64, "b" * 64))
    evaluator = LiveContextPairedEvaluator(
        repository,
        corpus,
        adapter,
        randomization_nonce_factory=lambda: next(nonces),
        scoring_nonce_factory=lambda: "c" * 64,
    )
    result = evaluator.run(
        context_intervention=context["intervention"],
        candidate_id="provider-context-treatment",
        baseline_candidate_id="provider-context-control",
    )
    return repository, corpus, evaluator, result


def _blind_review_payload(evaluation: dict[str, Any], *, reviewer_id: str) -> dict[str, Any]:
    return {
        "reviewer_id": reviewer_id,
        "reviewer_kind": "human",
        "review_scope": "paired_quality_scoring_artifact",
        "decision": "accepted",
        "scoring_artifact_digest": evaluation["scoring_artifact"][
            "artifact_digest"
        ],
        "blind_review_surface_sha256": evaluation["blind_review_surface"][
            "surface_sha256"
        ],
        "arm_mapping_not_accessed": True,
        "reviewer_independence_attested": True,
    }


def test_live_provider_observation_blind_review_and_utility_attribution(
    tmp_path: Path,
    capsys,
):
    context = _context_fixture(tmp_path / "context")
    adapter = ObservableContextAdapter(effect="benefit")
    repository, _, evaluator, live = _live_fixture(
        tmp_path / "evaluation",
        context,
        adapter,
    )

    assert live["status"] == "pending_human_review"
    assert live["execution"]["fresh_adapter_calls_executed"] is True
    assert live["execution"]["plan_registered_before_adapter_invocations"] is True
    assert live["execution"]["verified_provider_observations"] == 16
    assert live["execution"]["selected_condition_order_randomized"] is True
    assert live["execution"]["condition_order_balance"] == {
        "control_first": 4,
        "treatment_first": 4,
    }
    assert live["execution"]["total_adapter_attempts"] == 32
    assert live["execution"]["per_task_order_disclosed"] is False
    assert adapter.calls == 32
    assert len(adapter.selected_condition_calls) == 16
    selected_by_task: dict[str, list[tuple[str, int]]] = {}
    for task_id, condition, position in adapter.selected_condition_calls:
        selected_by_task.setdefault(task_id, []).append((condition, position))
    assert len(selected_by_task) == 8
    assert all(
        [condition for condition, _ in calls]
        == [
            condition
            for condition, _ in sorted(calls, key=lambda item: item[1])
        ]
        and sorted(position for _, position in calls) == [1, 2]
        for calls in selected_by_task.values()
    )
    assert live["claims"]["provider_transport_observed"] is True
    pending = live["evaluation"]
    assert pending["provider_observation_required"] is True
    assert pending["source_pairs"] == []
    assert pending["measurement"]["reason"] == "withheld_until_blind_review"
    assert pending["blind_review_surface"]["arm_mapping_disclosed"] is False
    assert pending["scoring_artifact"]["contains_arm_labels"] is False
    assert all(
        item.get("provider_observation_commitment")
        for item in pending["scoring_artifact"]["task_artifacts"]
    )

    inspected_exit = paired_quality_bridge_main(
        [
            "get",
            "--evaluation-db",
            str(repository.path),
            "--evaluation-id",
            pending["id"],
        ],
    )
    assert inspected_exit == 0
    inspected_payload = json.loads(capsys.readouterr().out)
    assert inspected_payload["status"] == "pending_human_review"
    assert inspected_payload["source_pairs"] == []
    assert inspected_payload["measurement"]["reason"] == (
        "withheld_until_blind_review"
    )

    incomplete_review = evaluator.review(
        pending["id"],
        {
            "reviewer_id": "blind-outcome-reviewer",
            "reviewer_kind": "human",
            "review_scope": "paired_quality_scoring_artifact",
            "decision": "accepted",
            "scoring_artifact_digest": pending["scoring_artifact"][
                "artifact_digest"
            ],
        },
    )
    assert incomplete_review["operation"]["status"] == "blocked"
    assert incomplete_review["operation"]["reason"] == "quality_review_payload_shape_invalid"

    review_exit = paired_quality_bridge_main(
        [
            "review",
            "--evaluation-db",
            str(repository.path),
            "--evaluation-id",
            pending["id"],
            "--reviewer-id",
            "blind-outcome-reviewer",
            "--decision",
            "accepted",
            "--scoring-artifact-digest",
            pending["scoring_artifact"]["artifact_digest"],
            "--blind-review-surface-sha256",
            pending["blind_review_surface"]["surface_sha256"],
            "--arm-mapping-not-accessed",
            "--reviewer-independence-attested",
        ],
    )
    assert review_exit == 0
    reviewed = json.loads(capsys.readouterr().out)
    assert reviewed["status"] == "reviewed_evidence"
    assert len(reviewed["source_pairs"]) == 8
    assert reviewed["measurement"]["available"] is True
    assert reviewed["human_review"]["blind_review_surface_enforced"] is True
    assert reviewed["human_review"]["arm_mapping_not_accessed_attested"] is True
    assert (
        reviewed["human_review"]["reviewer_blindness_cryptographically_verified"]
        is False
    )

    utility = ContextUtilityAttributionLedger(
        repository,
        evaluator.paired_quality,
        ContextSemanticReviewLedger(str(context["memory"]), read_only=True),
        EvidenceContextVerifier(
            repository_root=context["repository"],
            session_path=context["session"],
        ),
    ).attribute(
        {
            "evaluation_id": reviewed["id"],
            "artifact_sha256": context["compiled"]["artifact"]["artifact_sha256"],
        }
    )
    assert utility["schema"] == "spst-context-utility-attribution-v2"
    assert utility["status"] == "attributed"
    assert utility["context"]["provider_uptake_observed"] is True
    assert utility["context"]["provider_uptake_verified"] is False
    assert utility["context"]["provider_observation_count"] == 16
    assert utility["utility"]["direction"] == "beneficial_association_supported"
    assert utility["utility"]["causal_context_utility_established"] is False
    assert utility["claims"]["reviewer_blindness_cryptographically_verified"] is False
    assert utility["revalidation"]["valid"] is True
    assert repository.verify_provenance()["valid"] is True

    status_exit = paired_quality_bridge_main(
        ["status", "--evaluation-db", str(repository.path)]
    )
    assert status_exit == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["coverage"]["reviewed_count"] == 1

    missing_exit = paired_quality_bridge_main(
        [
            "get",
            "--evaluation-db",
            str(repository.path),
            "--evaluation-id",
            "PQE-missing",
        ]
    )
    assert missing_exit == 2
    assert json.loads(capsys.readouterr().out)["status"] == "not_found"

    plan_record = asyncio.run(
        repository.load(
            f"runtime:live_context_plan:{live['execution']['experiment_id']}"
        )
    )
    assert isinstance(plan_record, dict)
    calls_before_duplicate = adapter.calls
    duplicate_plan = evaluator.run(
        context_intervention=context["intervention"],
        candidate_id="provider-context-treatment",
        baseline_candidate_id="provider-context-control",
        execution_plan=plan_record["manifest"],
    )
    assert duplicate_plan["status"] == "blocked"
    assert duplicate_plan["reason"] == "live_pair_plan_already_registered"
    assert adapter.calls == calls_before_duplicate


def test_mismatched_acknowledgement_and_response_replay_fail_closed(tmp_path: Path):
    context = _context_fixture(tmp_path / "context")
    _, _, _, mismatched = _live_fixture(
        tmp_path / "mismatched",
        context,
        ObservableContextAdapter(acknowledgement_mismatch=True),
    )
    assert mismatched["status"] == "blocked"
    assert mismatched["execution"]["verified_provider_observations"] < 16
    assert mismatched["claims"]["provider_transport_observed"] is False
    assert "provider_observation" in mismatched["reason"]

    _, _, _, replayed = _live_fixture(
        tmp_path / "replayed",
        context,
        ObservableContextAdapter(replay_response_id=True),
    )
    assert replayed["status"] == "blocked"
    assert replayed["reason"] == "provider_response_replay_detected"


def test_provider_observation_tampering_and_local_scaffold_are_not_uptake(
    tmp_path: Path,
):
    request = build_provider_request_binding("Bound request.", {})
    observation = build_provider_observation(
        request,
        provider_name="observable-local-provider",
        model_version="observable-context-v1",
        response_id="response-tamper-test",
        response_status="completed",
        output_text='{"answer": 1}',
        observation_source="in_process_provider_echo",
        acknowledged_request_binding_sha256=request["request_binding_sha256"],
    )
    observation["response"]["output_sha256"] = "0" * 64
    valid, reason = verify_provider_observation(
        observation,
        request,
        output_text='{"answer": 1}',
    )
    assert valid is False
    assert reason == "provider_observation_digest_mismatch"

    context = _context_fixture(tmp_path / "context")
    repository = SQLiteRepository(str(tmp_path / "local-scaffold.db"))
    corpus = OperationalEvaluationCorpus(repository)
    corpus.register(_task("local-scaffold-00"))
    evaluator = LiveContextPairedEvaluator(
        repository,
        corpus,
        LocalCodexAdapter(),
    )
    blocked = evaluator.run(
        context_intervention=context["intervention"],
        candidate_id="local-scaffold-treatment",
        baseline_candidate_id="local-scaffold-control",
    )
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "provider_observation_capability_required"
    assert blocked["claims"]["provider_transport_observed"] is False


def test_same_semantic_and_outcome_reviewer_blocks_live_utility(tmp_path: Path):
    context = _context_fixture(tmp_path / "context")
    repository, _, evaluator, live = _live_fixture(
        tmp_path / "evaluation",
        context,
        ObservableContextAdapter(effect="benefit"),
    )
    reviewed = evaluator.review(
        live["evaluation"]["id"],
        _blind_review_payload(
            live["evaluation"],
            reviewer_id="semantic-context-reviewer",
        ),
    )
    assert reviewed["status"] == "reviewed_evidence"
    utility = ContextUtilityAttributionLedger(
        repository,
        evaluator.paired_quality,
        ContextSemanticReviewLedger(str(context["memory"]), read_only=True),
        EvidenceContextVerifier(
            repository_root=context["repository"],
            session_path=context["session"],
        ),
    ).attribute(
        {
            "evaluation_id": reviewed["id"],
            "artifact_sha256": context["compiled"]["artifact"]["artifact_sha256"],
        }
    )
    assert utility["status"] == "blocked"
    assert utility["reason"] == "context_utility_reviewer_role_separation_required"


def test_live_pair_schedule_tampering_is_rejected_after_repository_resign(
    tmp_path: Path,
):
    context = _context_fixture(tmp_path / "context")
    repository, _, evaluator, live = _live_fixture(
        tmp_path / "evaluation",
        context,
        ObservableContextAdapter(effect="benefit"),
    )
    experiment_id = live["execution"]["experiment_id"]
    execution_record = asyncio.run(
        repository.load(f"runtime:live_context_execution:{experiment_id}")
    )
    assert isinstance(execution_record, dict)
    first_pair = execution_record["pairs"][0]
    report_id = first_pair["baseline"]["producer_run_id"]
    report = asyncio.run(repository.load(f"runtime:operational_shadow:{report_id}"))
    assert isinstance(report, dict)
    tampered = deepcopy(report)
    result = tampered["cases"][0]["baseline"]
    result["live_pair_execution"]["condition_position"] = 2
    tampered["producer_evidence"] = build_producer_evidence(tampered)
    asyncio.run(
        repository.save(f"runtime:operational_shadow:{report_id}", tampered)
    )
    assert repository.verify_provenance()["valid"] is True

    revalidated = evaluator.paired_quality.get(live["evaluation"]["id"])
    assert revalidated is not None
    assert revalidated["revalidation"]["valid"] is False
    assert revalidated["revalidation"]["reason"] == "producer_binding_invalid"
