import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from spst_runtime.chat_bridge import preview_context, run_chat_turn
from spst_runtime.context_review import (
    SEMANTIC_REVIEW_INDEX_KEY,
    SEMANTIC_REVIEW_SCOPE,
    ContextSemanticReviewError,
    ContextSemanticReviewLedger,
)
from spst_runtime.context_utility import ContextUtilityAttributionLedger
from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.evaluation.operational_shadow import OperationalShadowRunner
from spst_runtime.evaluation.paired_quality import PairedQualityEvidenceLedger
from spst_runtime.evidence_context import EvidenceContextCompiler, EvidenceContextVerifier
from spst_runtime.interfaces.model_adapter import ModelAdapter
from spst_runtime.persistence.sqlite_repository import SQLiteRepository


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


class ContextEffectAdapter(ModelAdapter):
    """Deterministic local producer whose only varying input is reviewed context presence."""

    def __init__(self, effect: str):
        self.effect = effect

    async def infer(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        has_context = isinstance(
            (context or {}).get("evidence_context_intervention"),
            dict,
        )
        if self.effect == "benefit":
            answer = 1 if has_context else 0
        elif self.effect == "harm":
            answer = 0 if has_context else 1
        else:
            answer = 1
        return {
            "provider": "arch03-offline-context-producer",
            "available": True,
            "requires_api_key": False,
            "text": json.dumps({"answer": answer}),
        }

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": "arch03-offline-context-producer",
            "model_version": "deterministic-context-v1",
            "requires_api_key": False,
        }

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "interface": "ModelAdapter",
            "supports_structured_evaluation": True,
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


def _context_fixture(tmp_path: Path, *, decision: str = "supported") -> dict[str, Any]:
    repository = tmp_path / "repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "SPST Test")
    _git(repository, "config", "user.email", "spst@example.invalid")
    _git(repository, "config", "core.autocrlf", "false")
    (repository / "reviewed-context.md").write_text(
        "The reviewed local experiment supplies one bounded JSON-answer context artifact.\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "reviewed context source")
    session = tmp_path / "session.db"
    memory = tmp_path / "memory.db"
    route = run_chat_turn(
        "Compile the bounded reviewed context experiment artifact.",
        steps=1,
        profile="strict",
        session_path=str(session),
        memory_path=str(memory),
        repository_root=str(repository),
    )
    receipt_id = route["routing_receipt"]["receipt_id"]
    compiler = EvidenceContextCompiler(
        repository_root=repository,
        session_path=session,
        memory_path=memory,
    )
    compiled = compiler.compile_repository_file(
        producer_receipt_id=receipt_id,
        artifact_kind="architecture_decision",
        title="Reviewed bounded context experiment",
        statement=(
            "The local context experiment supplies one source-bound JSON-answer artifact."
        ),
        relative_path="reviewed-context.md",
        tags=["arch-03", "context-utility"],
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
            "reviewer_id": "human-reviewer-local",
            "reviewer_kind": "human",
            "review_scope": SEMANTIC_REVIEW_SCOPE,
            "decision": decision,
            "artifact_sha256": compiled["artifact"]["artifact_sha256"],
            "source_sha256": compiled["artifact"]["source"]["source_sha256"],
            "review_note": "The bounded statement is supported by the exact bound source.",
        },
        now=NOW,
    )
    intervention = None
    if decision == "supported":
        intervention = reviews.build_intervention(record, structural)
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
        "intervention": intervention,
    }


def _task(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "prompt": f"Return the exact JSON answer for isolated task {task_id}.",
        "domain": "context-utility",
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


def _reference(report: dict[str, Any], task_id: str, arm: str) -> dict[str, str]:
    binding = next(
        item
        for item in report["producer_evidence"]
        if item["task_id"] == task_id and item["arm"] == arm
    )
    return {"producer_run_id": report["id"], "binding_id": binding["binding_id"]}


def _paired_fixture(
    tmp_path: Path,
    context: dict[str, Any],
    *,
    effect: str,
    sample_count: int = 8,
    isolated: bool = True,
) -> tuple[SQLiteRepository, PairedQualityEvidenceLedger, dict[str, Any]]:
    evaluation_repository = SQLiteRepository(str(tmp_path / "evaluation.db"))
    corpus = OperationalEvaluationCorpus(evaluation_repository)
    task_ids = [f"context-{index:02d}" for index in range(sample_count)]
    for task_id in task_ids:
        assert corpus.register(_task(task_id))["status"] == "registered"
    runner = OperationalShadowRunner(ContextEffectAdapter(effect), corpus)
    baseline = runner.run(
        candidate_id="context-control",
        baseline_candidate_id="context-control-baseline",
        context_experiment=isolated,
    )
    candidate = runner.run(
        candidate_id="context-treatment",
        baseline_candidate_id="context-control-baseline",
        context_experiment=isolated,
        context_intervention=context["intervention"] if isolated else None,
    )
    for report in (baseline, candidate):
        asyncio.run(
            evaluation_repository.save(
                f"runtime:operational_shadow:{report['id']}",
                report,
            )
        )
    pairs = [
        {
            "task_id": task_id,
            "baseline": _reference(baseline, task_id, "baseline"),
            "candidate": _reference(candidate, task_id, "maximized"),
        }
        for task_id in task_ids
    ]
    paired = PairedQualityEvidenceLedger(
        evaluation_repository,
        corpus,
        nonce_factory=lambda: "arch03-fixed-blinding-nonce",
    )
    evaluation = paired.evaluate({"pairs": pairs})
    if evaluation["status"] == "pending_human_review":
        evaluation = paired.review(
            evaluation["id"],
            {
                "reviewer_id": "human-outcome-reviewer-local",
                "reviewer_kind": "human",
                "review_scope": "paired_quality_scoring_artifact",
                "decision": "accepted",
                "scoring_artifact_digest": evaluation["scoring_artifact"][
                    "artifact_digest"
                ],
            },
        )
    return evaluation_repository, paired, evaluation


def _utility(
    context: dict[str, Any],
    repository: SQLiteRepository,
    paired: PairedQualityEvidenceLedger,
) -> ContextUtilityAttributionLedger:
    return ContextUtilityAttributionLedger(
        repository,
        paired,
        ContextSemanticReviewLedger(str(context["memory"]), read_only=True),
        EvidenceContextVerifier(
            repository_root=context["repository"],
            session_path=context["session"],
        ),
    )


def test_semantic_review_is_exact_gates_context_and_binds_model_input(tmp_path: Path):
    repository = tmp_path / "missing-review"
    repository.mkdir()
    context = _context_fixture(repository)
    artifact = context["compiled"]["artifact"]

    packet = preview_context(
        "reviewed bounded context experiment",
        session_path=str(context["session"]),
        memory_path=str(context["memory"]),
        repository_root=str(context["repository"]),
    )
    item = next(
        value for value in packet["items"] if value["source"] == "evidence_context_compiler"
    )
    review = item["origin"]["semantic_review"]
    assert item["source_trust"] == "human_supported_source_verified_untrusted"
    assert review["decision"] == "supported"
    assert review["artifact_sha256"] == artifact["artifact_sha256"]
    assert review["source_sha256"] == artifact["source"]["source_sha256"]
    assert review["human_identity_cryptographically_verified"] is False
    assert review["reviewer_independence_verified"] is False

    routed = run_chat_turn(
        "Use the reviewed bounded context experiment evidence.",
        steps=1,
        profile="strict",
        session_path=str(context["session"]),
        memory_path=str(context["memory"]),
        repository_root=str(context["repository"]),
    )
    binding = routed["routing_receipt"]["payload"]["pipeline"][
        "model_input_binding"
    ]
    assert binding["schema"] == "spst-model-input-binding-v2"
    assert binding["delivered_artifact_sha256s"] == [artifact["artifact_sha256"]]
    assert binding["delivered_semantic_review_sha256s"] == [review["review_sha256"]]
    assert routed["routing_receipt"]["verification"]["verified"] is True


def test_missing_unsupported_mismatched_and_tampered_reviews_fail_closed(tmp_path: Path):
    missing_root = tmp_path / "missing"
    missing_root.mkdir()
    repository = missing_root / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "SPST Test")
    _git(repository, "config", "user.email", "spst@example.invalid")
    (repository / "reviewed-context.md").write_text("bounded source\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "source")
    session = missing_root / "session.db"
    memory = missing_root / "memory.db"
    receipt = run_chat_turn(
        "Compile review-gated context.",
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
        artifact_kind="code_fact",
        title="Review gate",
        statement="Only an exact supported review makes this artifact eligible.",
        relative_path="reviewed-context.md",
        now=NOW,
    )
    missing = preview_context(
        "exact supported review artifact eligible",
        session_path=str(session),
        memory_path=str(memory),
        repository_root=str(repository),
    )
    assert missing["selection"]["rejection_reasons"][
        "artifact_semantic_review_missing"
    ] == 1
    record = next(item for item in compiler.memory.list_all() if item.id == compiled["memory_record"]["id"])
    structural = compiler.verifier.verify_record(record, now=NOW)
    reviews = ContextSemanticReviewLedger(str(memory))
    payload = {
        "reviewer_id": "human-reviewer-local",
        "reviewer_kind": "human",
        "review_scope": SEMANTIC_REVIEW_SCOPE,
        "decision": "supported",
        "artifact_sha256": compiled["artifact"]["artifact_sha256"],
        "source_sha256": "0" * 64,
        "review_note": "Exact source support was inspected.",
    }
    with pytest.raises(
        ContextSemanticReviewError,
        match="semantic_review_source_digest_mismatch",
    ):
        reviews.review(record, structural, payload, now=NOW)

    unsupported_root = tmp_path / "unsupported"
    unsupported_root.mkdir()
    unsupported = _context_fixture(unsupported_root, decision="unsupported")
    unsupported_packet = preview_context(
        "reviewed bounded context experiment",
        session_path=str(unsupported["session"]),
        memory_path=str(unsupported["memory"]),
        repository_root=str(unsupported["repository"]),
    )
    assert unsupported_packet["selection"]["rejection_reasons"][
        "artifact_semantic_review_unsupported"
    ] == 1

    tampered_root = tmp_path / "tampered"
    tampered_root.mkdir()
    tampered = _context_fixture(tampered_root)
    index = asyncio.run(tampered["reviews"].repository.load(SEMANTIC_REVIEW_INDEX_KEY))
    assert isinstance(index, dict)
    index["records"][0]["review_note"] = "tampered"
    asyncio.run(tampered["reviews"].repository.save(SEMANTIC_REVIEW_INDEX_KEY, index))
    tampered_packet = preview_context(
        "reviewed bounded context experiment",
        session_path=str(tampered["session"]),
        memory_path=str(tampered["memory"]),
        repository_root=str(tampered["repository"]),
    )
    assert tampered_packet["selection"]["rejection_reasons"][
        "semantic_review_digest_mismatch"
    ] == 1


@pytest.mark.parametrize(
    ("effect", "direction"),
    (
        ("benefit", "beneficial_association_supported"),
        ("harm", "harmful_association_supported"),
        ("tie", "direction_inconclusive"),
    ),
)
def test_context_utility_uses_reviewed_paired_evidence_and_reports_harm(
    tmp_path: Path,
    effect: str,
    direction: str,
):
    context = _context_fixture(tmp_path / "context")
    evaluation_repository, paired, evaluation = _paired_fixture(
        tmp_path / "paired",
        context,
        effect=effect,
    )
    assert evaluation["status"] == "reviewed_evidence"
    ledger = _utility(context, evaluation_repository, paired)
    result = ledger.attribute(
        {
            "evaluation_id": evaluation["id"],
            "artifact_sha256": context["compiled"]["artifact"]["artifact_sha256"],
        }
    )
    assert result["status"] == "attributed"
    assert result["utility"]["available"] is True
    assert result["utility"]["direction"] == direction
    assert result["utility"]["sample_count"] == 8
    assert result["utility"]["uncertainty"]["available"] is True
    assert result["utility"]["causal_context_utility_established"] is False
    assert result["context"]["delivery_status"] == "submitted_to_adapter"
    assert result["context"]["provider_uptake_verified"] is False
    assert result["revalidation"]["valid"] is True
    assert evaluation_repository.verify_provenance()["valid"] is True
    if effect == "benefit":
        status = subprocess.run(
            [
                sys.executable,
                "-m",
                "spst_runtime.context_utility_bridge",
                "status",
                "--repository-root",
                str(context["repository"]),
                "--session-db",
                str(context["session"]),
                "--memory-db",
                str(context["memory"]),
                "--evaluation-db",
                str(evaluation_repository.path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert status.returncode == 0, status.stderr
        status_payload = json.loads(status.stdout)
        assert status_payload["coverage"]["available_count"] == 1
        assert status_payload["history"][0]["revalidation"]["valid"] is True


def test_context_utility_rejects_scores_insufficient_samples_and_confounded_runs(
    tmp_path: Path,
):
    context = _context_fixture(tmp_path / "context")
    repository, paired, evaluation = _paired_fixture(
        tmp_path / "confounded",
        context,
        effect="tie",
        isolated=False,
    )
    assert evaluation["status"] == "reviewed_evidence"
    ledger = _utility(context, repository, paired)
    caller_score = ledger.attribute(
        {
            "evaluation_id": evaluation["id"],
            "artifact_sha256": context["compiled"]["artifact"]["artifact_sha256"],
            "paired_delta": 1.0,
        }
    )
    assert caller_score["status"] == "blocked"
    assert caller_score["reason"] == "context_utility_payload_shape_invalid"
    confounded = ledger.attribute(
        {
            "evaluation_id": evaluation["id"],
            "artifact_sha256": context["compiled"]["artifact"]["artifact_sha256"],
        }
    )
    assert confounded["status"] == "blocked"
    assert confounded["reason"] == "context_utility_isolated_experiment_required"

    insufficient_repository, insufficient_paired, insufficient = _paired_fixture(
        tmp_path / "insufficient",
        context,
        effect="benefit",
        sample_count=7,
    )
    assert insufficient["status"] == "insufficient_evidence"
    insufficient_result = _utility(
        context,
        insufficient_repository,
        insufficient_paired,
    ).attribute(
        {
            "evaluation_id": insufficient["id"],
            "artifact_sha256": context["compiled"]["artifact"]["artifact_sha256"],
        }
    )
    assert insufficient_result["status"] == "blocked"
    assert insufficient_result["utility"]["available"] is False
    assert insufficient_result["reason"] in {
        "context_utility_paired_evaluation_not_reviewed",
        "context_utility_task_quality_unavailable",
        "context_utility_minimum_samples_not_met",
    }
