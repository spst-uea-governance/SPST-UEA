import asyncio
from copy import deepcopy
import json
from typing import Any

import pytest

from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.evaluation.operational_shadow import OperationalShadowRunner
from spst_runtime.evaluation.paired_quality import PairedQualityEvidenceLedger
from spst_runtime.evaluation.producer_evidence import build_producer_evidence
from spst_runtime.evaluation.quality_evidence import IndependentExactJsonScorer
from spst_runtime.interfaces.model_adapter import ModelAdapter
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.web import CockpitRuntime, handle_cockpit_request


class PairedQualityAdapter(ModelAdapter):
    """Deterministic offline producer used only to prove the evaluation path."""

    def __init__(self) -> None:
        self.profile = "baseline"

    async def infer(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mode = (context or {}).get("evaluation", {}).get("mode")
        answer = 1 if self.profile == "improved" and mode == "maximized" else 0
        return {
            "provider": "phase16-offline-producer",
            "available": True,
            "requires_api_key": False,
            "text": json.dumps({"answer": answer}),
        }

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": "phase16-offline-producer",
            "model_version": "deterministic-v1",
            "requires_api_key": False,
        }

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "interface": "ModelAdapter",
            "supports_structured_evaluation": True,
            "requires_api_key": False,
        }


class CapturingScorer(IndependentExactJsonScorer):
    def __init__(self) -> None:
        self.packets: list[list[dict[str, Any]]] = []

    def score(
        self,
        rubric: dict[str, Any],
        anonymous_materials: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        self.packets.append(deepcopy(anonymous_materials))
        return super().score(rubric, anonymous_materials)


def _task(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "prompt": f"Return the bounded JSON answer for local task {task_id}.",
        "domain": "structured-reasoning",
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
    return {
        "producer_run_id": report["id"],
        "binding_id": binding["binding_id"],
    }


def _setup_evaluation(
    tmp_path,
    *,
    sample_count: int = 8,
    candidate_profile: str = "improved",
    scorer: IndependentExactJsonScorer | None = None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repository = SQLiteRepository(str(tmp_path / f"phase16-{sample_count}.db"))
    corpus = OperationalEvaluationCorpus(repository)
    task_ids = [f"quality-{index:02d}" for index in range(sample_count)]
    for task_id in task_ids:
        assert corpus.register(_task(task_id))["status"] == "registered"

    adapter = PairedQualityAdapter()
    runner = OperationalShadowRunner(adapter, corpus)
    baseline_report = runner.run(
        candidate_id="candidate",
        baseline_candidate_id="baseline",
    )
    asyncio.run(
        repository.save(
            f"runtime:operational_shadow:{baseline_report['id']}",
            baseline_report,
        )
    )
    adapter.profile = candidate_profile
    candidate_report = runner.run(
        candidate_id=(
            "candidate" if candidate_profile == "improved" else "candidate-relabelled"
        ),
        baseline_candidate_id="baseline",
    )
    asyncio.run(
        repository.save(
            f"runtime:operational_shadow:{candidate_report['id']}",
            candidate_report,
        )
    )
    pairs = [
        {
            "task_id": task_id,
            "baseline": _reference(baseline_report, task_id, "baseline"),
            "candidate": _reference(candidate_report, task_id, "maximized"),
        }
        for task_id in task_ids
    ]
    ledger = PairedQualityEvidenceLedger(
        repository,
        corpus,
        scorer=scorer,
        nonce_factory=lambda: "phase16-fixed-blinding-nonce",
    )
    return repository, ledger, baseline_report, candidate_report, pairs


def _accepted_review(evaluation: dict[str, Any]) -> dict[str, Any]:
    return {
        "reviewer_id": "human-reviewer-local",
        "reviewer_kind": "human",
        "review_scope": "paired_quality_scoring_artifact",
        "decision": "accepted",
        "scoring_artifact_digest": evaluation["scoring_artifact"]["artifact_digest"],
    }


@pytest.mark.conformance
def test_phase16_establishes_bounded_quality_only_after_blinded_scoring_and_human_review(
    tmp_path,
):
    scorer = CapturingScorer()
    repository, ledger, baseline_report, candidate_report, pairs = _setup_evaluation(
        tmp_path,
        scorer=scorer,
    )

    evaluation = ledger.evaluate({"pairs": pairs})

    assert evaluation["status"] == "pending_human_review"
    assert evaluation["measurement"]["sample_count"] == 8
    assert evaluation["measurement"]["paired_delta"] == 1.0
    assert evaluation["measurement"]["uncertainty"]["lower_bound"] == 0.039677
    assert evaluation["measurement"]["positive_effect_supported"] is True
    assert evaluation["task_quality"]["available"] is False
    assert evaluation["task_quality"]["reason"] == "human_review_pending"
    assert evaluation["claims"]["task_quality_uplift_claimed"] is False
    assert evaluation["claims"]["automatic_promotion"] is False
    scoring_json = json.dumps(evaluation["scoring_artifact"], sort_keys=True)
    assert baseline_report["id"] not in scoring_json
    assert candidate_report["id"] not in scoring_json
    assert all(
        set(packet) == {"slot_id", "material"}
        and not {"task_id", "arm", "candidate_id", "producer_run_id", "binding_id"}
        & set(packet["material"])
        for invocation in scorer.packets
        for packet in invocation
    )

    reviewed = ledger.review(evaluation["id"], _accepted_review(evaluation))

    assert reviewed["status"] == "reviewed_evidence"
    assert reviewed["task_quality"]["available"] is True
    assert reviewed["task_quality"]["semantic_task_quality_established"] is True
    assert reviewed["task_quality"]["claim_eligible"] is True
    assert reviewed["task_quality"]["generalization_beyond_registered_corpus"] is False
    assert reviewed["human_review"]["identity_assurance"] == "self_attested"
    assert reviewed["human_review"]["human_identity_cryptographically_verified"] is False
    assert repository.verify_provenance()["valid"] is True


@pytest.mark.conformance
def test_phase16_minimum_sample_and_human_review_digest_fail_closed(tmp_path):
    _, ledger, _, _, pairs = _setup_evaluation(tmp_path, sample_count=7)

    insufficient = ledger.evaluate({"pairs": pairs})

    assert insufficient["status"] == "insufficient_evidence"
    assert "minimum_paired_samples_not_met" in insufficient["reasons"]
    assert insufficient["measurement"]["sample_count"] == 7
    assert insufficient["task_quality"]["available"] is False

    _, complete_ledger, _, _, complete_pairs = _setup_evaluation(
        tmp_path / "complete",
    )
    pending = complete_ledger.evaluate({"pairs": complete_pairs})
    forged_review = _accepted_review(pending)
    forged_review["scoring_artifact_digest"] = "0" * 64

    blocked = complete_ledger.review(pending["id"], forged_review)

    assert blocked["operation"]["status"] == "blocked"
    assert blocked["operation"]["reason"] == "review_artifact_digest_mismatch"
    assert complete_ledger.get(pending["id"])["status"] == "pending_human_review"


@pytest.mark.conformance
@pytest.mark.parametrize(
    ("attack", "expected_reason"),
    (
        ("same_run", "distinct_producer_runs_required"),
        ("swapped_arm", "producer_arm_mismatch"),
        ("missing_run", "producer_run_not_found"),
        ("caller_scores", "paired_evaluation_payload_shape_invalid"),
    ),
)
def test_phase16_rejects_identity_and_caller_score_attacks(
    tmp_path,
    attack,
    expected_reason,
):
    _, ledger, baseline_report, candidate_report, pairs = _setup_evaluation(tmp_path)
    payload: dict[str, Any] = {"pairs": deepcopy(pairs)}
    if attack == "same_run":
        payload["pairs"][0]["candidate"] = _reference(
            baseline_report,
            payload["pairs"][0]["task_id"],
            "maximized",
        )
    elif attack == "swapped_arm":
        payload["pairs"][0]["candidate"] = _reference(
            candidate_report,
            payload["pairs"][0]["task_id"],
            "baseline",
        )
    elif attack == "missing_run":
        payload["pairs"][0]["candidate"]["producer_run_id"] = "SHADOW-missing"
    else:
        payload["task_scores"] = [1.0] * 8

    result = ledger.evaluate(payload)

    assert result["status"] == "blocked"
    assert expected_reason in result["reasons"]
    assert result["task_quality"]["available"] is False


@pytest.mark.conformance
def test_phase16_keeps_independent_identical_outcomes_as_zero_delta_ties(tmp_path):
    _, ledger, baseline_report, candidate_report, pairs = _setup_evaluation(
        tmp_path,
        candidate_profile="identical",
    )

    assert baseline_report["id"] != candidate_report["id"]
    evaluation = ledger.evaluate({"pairs": pairs})
    reviewed = ledger.review(evaluation["id"], _accepted_review(evaluation))

    assert evaluation["status"] == "pending_human_review"
    assert evaluation["measurement"]["sample_count"] == 8
    assert evaluation["measurement"]["paired_delta"] == 0.0
    assert evaluation["measurement"]["positive_effect_supported"] is False
    assert reviewed["task_quality"]["available"] is True
    assert reviewed["task_quality"]["claim_eligible"] is False


@pytest.mark.conformance
def test_phase16_rejects_relabelled_copy_without_distinct_run_instance(tmp_path):
    repository, ledger, baseline_report, _, pairs = _setup_evaluation(tmp_path)
    copied_report = deepcopy(baseline_report)
    copied_report["id"] = "SHADOW-relabelled-copy"
    copied_report["candidate_id"] = "candidate-relabelled"
    copied_report["producer_evidence"] = build_producer_evidence(copied_report)
    asyncio.run(
        repository.save(
            f"runtime:operational_shadow:{copied_report['id']}",
            copied_report,
        )
    )
    copied_pairs = deepcopy(pairs)
    for pair in copied_pairs:
        pair["candidate"] = _reference(
            copied_report,
            pair["task_id"],
            "maximized",
        )

    result = ledger.evaluate({"pairs": copied_pairs})

    assert result["status"] == "blocked"
    assert "candidate_evidence_not_distinct" in result["reasons"]
    assert result["task_quality"]["available"] is False


@pytest.mark.conformance
def test_phase16_revalidation_blocks_altered_producer_and_scoring_artifact(tmp_path):
    repository, ledger, _, candidate_report, pairs = _setup_evaluation(tmp_path)
    evaluation = ledger.evaluate({"pairs": pairs})
    assert evaluation["status"] == "pending_human_review"

    altered_report = asyncio.run(
        repository.load(f"runtime:operational_shadow:{candidate_report['id']}")
    )
    assert isinstance(altered_report, dict)
    altered_report["cases"][0]["maximized"]["scoring_material"]["observations"][0][
        "observed"
    ] = ["number", "0:9:0"]
    asyncio.run(
        repository.save(
            f"runtime:operational_shadow:{candidate_report['id']}",
            altered_report,
        )
    )

    producer_blocked = ledger.get(evaluation["id"])

    assert producer_blocked is not None
    assert producer_blocked["status"] == "blocked"
    assert producer_blocked["revalidation"]["reason"] == "producer_binding_invalid"
    assert producer_blocked["task_quality"]["available"] is False


@pytest.mark.conformance
def test_phase16_scoring_artifact_digest_tamper_is_detected(tmp_path):
    repository, ledger, _, _, pairs = _setup_evaluation(tmp_path)
    evaluation = ledger.evaluate({"pairs": pairs})
    index = asyncio.run(repository.load(ledger.INDEX_KEY))
    assert isinstance(index, dict)
    stored = next(item for item in index["records"] if item.get("id") == evaluation["id"])
    stored["scoring_artifact"]["task_artifacts"][0]["scores"][0]["score"] = 0.5
    asyncio.run(repository.save(ledger.INDEX_KEY, index))

    blocked = ledger.get(evaluation["id"])

    assert blocked is not None
    assert blocked["status"] == "blocked"
    assert blocked["revalidation"]["reason"] == "scoring_artifact_digest_mismatch"
    assert repository.verify_provenance()["valid"] is True


@pytest.mark.conformance
def test_phase16_cockpit_exposes_evaluate_review_and_read_only_history(tmp_path):
    repository, ledger, _, _, pairs = _setup_evaluation(tmp_path)
    orchestrator = RuntimeOrchestrator(
        db_path=str(tmp_path / "phase16-runtime.db"),
        repository=repository,
        operational_corpus=ledger.corpus,
        paired_quality_evidence_ledger=ledger,
        model_provider=PairedQualityAdapter(),
    )
    cockpit = CockpitRuntime(orchestrator=orchestrator)

    evaluation_code, evaluation = handle_cockpit_request(
        "POST",
        "/api/paired-quality-evaluations",
        body=json.dumps({"pairs": pairs}).encode("utf-8"),
        runtime=cockpit,
    )
    review_payload = {
        "evaluation_id": evaluation["id"],
        **_accepted_review(evaluation),
    }
    review_code, reviewed = handle_cockpit_request(
        "POST",
        "/api/paired-quality-evaluations/review",
        body=json.dumps(review_payload).encode("utf-8"),
        runtime=cockpit,
    )
    history_code, history = handle_cockpit_request(
        "GET",
        "/api/paired-quality-evaluations",
        runtime=cockpit,
    )

    assert evaluation_code == 200
    assert evaluation["status"] == "pending_human_review"
    assert review_code == 200
    assert reviewed["status"] == "reviewed_evidence"
    assert history_code == 200
    assert history["coverage"]["reviewed_count"] == 1
    assert history["coverage"]["claim_eligible_count"] == 1
    assert repository.verify_provenance()["valid"] is True
