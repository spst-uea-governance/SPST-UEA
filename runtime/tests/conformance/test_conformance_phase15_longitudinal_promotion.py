import json
from typing import Any

import pytest

from spst_runtime.evaluation.artifact_outcome import ArtifactOutcomeLedger
from spst_runtime.evaluation.longitudinal_promotion import (
    LongitudinalPromotionGovernance,
)
from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.web import CockpitRuntime, handle_cockpit_request


class StaticVerificationRunner:
    """Fixed local verification evidence for Phase 15 promotion tests."""

    def run(self) -> dict[str, Any]:
        profiles = {
            name: {
                "profile": name,
                "status": "completed",
                "output_digest": f"digest-{name}",
            }
            for name in ("git_head", "git_status", "pytest", "ruff", "mypy", "git_diff_check")
        }
        return {
            "id": "VRUN-phase15",
            "runner_version": "verified-local-runner-v1",
            "status": "passed",
            "source_snapshot": {
                "revision": "a" * 40,
                "git_status_digest": "status-phase15",
            },
            "profiles": profiles,
        }


def _task(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "prompt": f"Private {task_id} task: analyze and verify the local contract.",
        "domain": "implementation",
        "split": "holdout",
        "required_markers": ["verify"],
        "consent": {
            "granted": True,
            "scope": "local_operational_evaluation",
            "retention_until": "2099-12-31",
        },
    }


def _outcome_payload(
    runner: StaticVerificationRunner,
    *,
    task_id: str,
    candidate_id: str,
    decision: str,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "candidate_id": candidate_id,
        "expected_source_snapshot": runner.run()["source_snapshot"],
        "human_calibration": {
            "decision": decision,
            "consent": {
                "granted": True,
                "scope": "local_operational_evaluation",
                "retention_until": "2099-12-31",
            },
        },
    }


def _policy() -> dict[str, Any]:
    return {
        "policy_id": "verifier-contract",
        "version": "v1",
        "strategies": ["constraint_extraction", "verifier_loop"],
    }


def _record_paired_outcomes(
    ledger: ArtifactOutcomeLedger,
    runner: StaticVerificationRunner,
    task_ids: tuple[str, ...],
    *,
    baseline_decision: str = "rejected",
    candidate_decision: str = "accepted",
) -> None:
    for task_id in task_ids:
        ledger.record(
            _outcome_payload(
                runner,
                task_id=task_id,
                candidate_id="baseline",
                decision=baseline_decision,
            )
        )
        ledger.record(
            _outcome_payload(
                runner,
                task_id=task_id,
                candidate_id="candidate-verified",
                decision=candidate_decision,
            )
        )


@pytest.mark.conformance
def test_phase15_synthesizes_all_paired_artifact_evidence_without_model_uplift_claim(
    tmp_path,
):
    repository = SQLiteRepository(str(tmp_path / "phase15-synthesis.db"))
    corpus = OperationalEvaluationCorpus(repository)
    runner = StaticVerificationRunner()
    ledger = ArtifactOutcomeLedger(repository, corpus, runner)
    for task_id in ("artifact-a", "artifact-b", "artifact-c"):
        corpus.register(_task(task_id))
    _record_paired_outcomes(ledger, runner, ("artifact-a", "artifact-b", "artifact-c"))

    promotions = LongitudinalPromotionGovernance(repository, corpus, ledger)
    proposal = promotions.propose(
        {
            "candidate_id": "candidate-verified",
            "baseline_candidate_id": "baseline",
            "policy": _policy(),
        }
    )
    public_payload = json.dumps({"proposal": proposal, "history": promotions.history()})

    assert proposal["status"] == "held_for_human_review"
    assert proposal["evidence"]["paired"]["sample_count"] == 3
    assert proposal["evidence"]["paired"]["mean_delta"] == 1.0
    assert proposal["evidence"]["source_outcomes"]["rejected_by_human"] == 3
    assert proposal["promotion"]["eligible"] is True
    assert proposal["promotion"]["automatic_adoption"] is False
    assert proposal["claims"]["task_quality_uplift_claimed"] is False
    assert _task("artifact-a")["prompt"] not in public_payload
    assert "human_calibration" not in public_payload
    assert repository.verify_provenance()["valid"] is True


@pytest.mark.conformance
def test_phase15_rejects_insufficient_or_ambiguous_evidence_without_auto_promotion(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "phase15-insufficient.db"))
    corpus = OperationalEvaluationCorpus(repository)
    runner = StaticVerificationRunner()
    ledger = ArtifactOutcomeLedger(repository, corpus, runner)
    for task_id in ("artifact-a", "artifact-b"):
        corpus.register(_task(task_id))
    _record_paired_outcomes(ledger, runner, ("artifact-a", "artifact-b"))

    promotions = LongitudinalPromotionGovernance(repository, corpus, ledger)
    insufficient = promotions.propose(
        {
            "candidate_id": "candidate-verified",
            "baseline_candidate_id": "baseline",
            "policy": _policy(),
        }
    )
    ledger.record(
        _outcome_payload(
            runner,
            task_id="artifact-a",
            candidate_id="candidate-verified",
            decision="rejected",
        )
    )
    ambiguous = promotions.propose(
        {
            "candidate_id": "candidate-verified",
            "baseline_candidate_id": "baseline",
            "policy": {**_policy(), "version": "v2"},
        }
    )

    assert insufficient["status"] == "insufficient_evidence"
    assert "minimum_paired_tasks_not_met" in insufficient["evidence"]["reasons"]
    assert insufficient["promotion"]["eligible"] is False
    assert ambiguous["status"] == "insufficient_evidence"
    assert "ambiguous_task_evidence" in ambiguous["evidence"]["reasons"]
    assert ambiguous["promotion"]["automatic_adoption"] is False


@pytest.mark.conformance
def test_phase15_requires_human_approval_and_records_reversible_shadow_rollback(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "phase15-reversible.db"))
    corpus = OperationalEvaluationCorpus(repository)
    runner = StaticVerificationRunner()
    ledger = ArtifactOutcomeLedger(repository, corpus, runner)
    for task_id in ("artifact-a", "artifact-b", "artifact-c"):
        corpus.register(_task(task_id))
    _record_paired_outcomes(ledger, runner, ("artifact-a", "artifact-b", "artifact-c"))
    promotions = LongitudinalPromotionGovernance(repository, corpus, ledger)
    proposal = promotions.propose(
        {
            "candidate_id": "candidate-verified",
            "baseline_candidate_id": "baseline",
            "policy": _policy(),
        }
    )
    rejected_proposal = promotions.propose(
        {
            "candidate_id": "candidate-verified",
            "baseline_candidate_id": "baseline",
            "policy": {**_policy(), "version": "v2"},
        }
    )
    rejected = promotions.resolve(rejected_proposal["id"], approved=False)

    active = promotions.resolve(proposal["id"], approved=True)
    rolled_back = promotions.rollback(proposal["id"], approved=True)

    assert proposal["status"] == "held_for_human_review"
    assert proposal["promotion"]["requires_human_approval"] is True
    assert rejected["status"] == "rejected_by_human"
    assert rejected["events"][0]["kind"] == "approval"
    assert active["status"] == "shadow_active"
    assert active["promotion"]["scope"] == "shadow_only"
    assert rolled_back["status"] == "rolled_back"
    assert [event["kind"] for event in rolled_back["events"]] == ["approval", "rollback"]
    assert promotions.coverage()["active_count"] == 0
    assert repository.verify_provenance()["valid"] is True


@pytest.mark.conformance
def test_phase15_cockpit_exposes_proposal_approval_and_rollback_without_subject_commit(tmp_path):
    runner = StaticVerificationRunner()
    orchestrator = RuntimeOrchestrator(
        db_path=str(tmp_path / "phase15-cockpit.db"),
        verification_runner=runner,
    )
    cockpit = CockpitRuntime(orchestrator=orchestrator)
    initial_version = cockpit.last_state.metadata["version"]

    for task_id in ("artifact-a", "artifact-b", "artifact-c"):
        registration_code, registration = handle_cockpit_request(
            "POST",
            "/api/corpus/tasks",
            body=json.dumps(_task(task_id)).encode("utf-8"),
            runtime=cockpit,
        )
        assert registration_code == 200
        assert registration["status"] == "registered"
        for candidate_id, decision in (("baseline", "rejected"), ("candidate-verified", "accepted")):
            outcome_code, outcome = handle_cockpit_request(
                "POST",
                "/api/artifact-outcomes",
                body=json.dumps(
                    _outcome_payload(
                        runner,
                        task_id=task_id,
                        candidate_id=candidate_id,
                        decision=decision,
                    )
                ).encode("utf-8"),
                runtime=cockpit,
            )
            assert outcome_code == 200
            assert outcome["status"] in {"verified_accepted", "rejected_by_human"}

    proposal_code, proposal = handle_cockpit_request(
        "POST",
        "/api/promotions",
        body=json.dumps(
            {
                "candidate_id": "candidate-verified",
                "baseline_candidate_id": "baseline",
                "policy": _policy(),
            }
        ).encode("utf-8"),
        runtime=cockpit,
    )
    approval_code, active = handle_cockpit_request(
        "POST",
        "/api/promotions/approval",
        body=json.dumps({"promotion_id": proposal["id"], "approved": True}).encode("utf-8"),
        runtime=cockpit,
    )
    rollback_code, rolled_back = handle_cockpit_request(
        "POST",
        "/api/promotions/rollback",
        body=json.dumps({"promotion_id": proposal["id"], "approved": True}).encode("utf-8"),
        runtime=cockpit,
    )
    status_code, status = handle_cockpit_request("GET", "/api/status", runtime=cockpit)
    history_code, history = handle_cockpit_request("GET", "/api/promotions", runtime=cockpit)

    assert proposal_code == 200
    assert proposal["status"] == "held_for_human_review"
    assert approval_code == 200
    assert active["status"] == "shadow_active"
    assert rollback_code == 200
    assert rolled_back["status"] == "rolled_back"
    assert cockpit.last_state.metadata["version"] == initial_version
    assert status_code == 200
    assert status["promotions"]["latest"]["id"] == proposal["id"]
    assert history_code == 200
    assert history["coverage"]["active_count"] == 0
    assert _task("artifact-a")["prompt"] not in json.dumps(history)
    assert orchestrator.repository.verify_provenance()["valid"] is True
