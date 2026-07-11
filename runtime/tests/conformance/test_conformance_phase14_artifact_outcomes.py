import json
from typing import Any

import pytest

from spst_runtime.evaluation.artifact_outcome import ArtifactOutcomeLedger
from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.web import CockpitRuntime, handle_cockpit_request


class StaticVerificationRunner:
    """Compact fixed-profile result used to exercise Phase 14 evidence policy."""

    def __init__(self, *, status: str = "passed", revision: str = "a" * 40):
        self.status = status
        self.revision = revision

    def run(self) -> dict[str, Any]:
        profile_status = "completed" if self.status == "passed" else "failed"
        profiles = {
            name: {
                "profile": name,
                "status": profile_status,
                "output_digest": f"digest-{name}-{self.status}",
            }
            for name in ("git_head", "git_status", "pytest", "ruff", "mypy", "git_diff_check")
        }
        return {
            "id": f"VRUN-{self.status}-{self.revision[:8]}",
            "runner_version": "verified-local-runner-v1",
            "status": self.status,
            "source_snapshot": {
                "revision": self.revision,
                "git_status_digest": f"status-{self.revision[:8]}",
            },
            "profiles": profiles,
            "verification": {
                name: {"status": "passed" if self.status == "passed" else "failed", "source": "verified_local"}
                for name in ("pytest", "ruff", "mypy", "git_diff_check")
            },
        }


def _task(task_id: str = "artifact-analysis") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "prompt": "Private artifact task: verify a local implementation without exposing this prompt.",
        "domain": "implementation",
        "split": "holdout",
        "required_markers": ["verify"],
        "consent": {
            "granted": True,
            "scope": "local_operational_evaluation",
            "retention_until": "2099-12-31",
        },
    }


def _human_calibration(decision: str = "accepted") -> dict[str, Any]:
    return {
        "decision": decision,
        "consent": {
            "granted": True,
            "scope": "local_operational_evaluation",
            "retention_until": "2099-12-31",
        },
    }


def _payload(
    runner: StaticVerificationRunner,
    *,
    task_id: str = "artifact-analysis",
    decision: str = "accepted",
) -> dict[str, Any]:
    snapshot = runner.run()["source_snapshot"]
    return {
        "task_id": task_id,
        "candidate_id": "candidate-artifact-v1",
        "expected_source_snapshot": snapshot,
        "human_calibration": _human_calibration(decision),
    }


@pytest.mark.conformance
def test_phase14_requires_human_consent_and_redacts_task_and_human_input(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "phase14-consent.db"))
    corpus = OperationalEvaluationCorpus(repository)
    corpus.register(_task())
    runner = StaticVerificationRunner()
    ledger = ArtifactOutcomeLedger(repository, corpus, runner)
    missing_consent = _payload(runner)
    missing_consent["human_calibration"]["consent"]["granted"] = False

    denied = ledger.record(missing_consent)
    accepted = ledger.record(_payload(runner))
    public_payload = json.dumps({"denied": denied, "accepted": accepted, "history": ledger.history()})

    assert denied["status"] == "blocked"
    assert "artifact_outcome_human_consent_required" in denied["governance"]["reasons"]
    assert accepted["status"] == "verified_accepted"
    assert accepted["calibration"]["eligible"] is True
    assert _task()["prompt"] not in public_payload
    assert "human_calibration" not in public_payload
    assert repository.verify_provenance()["valid"] is True


@pytest.mark.conformance
def test_phase14_blocks_stale_or_failed_artifact_evidence_from_calibration(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "phase14-stale.db"))
    corpus = OperationalEvaluationCorpus(repository)
    corpus.register(_task())
    current_runner = StaticVerificationRunner(revision="b" * 40)
    stale_ledger = ArtifactOutcomeLedger(repository, corpus, current_runner)
    stale_payload = _payload(StaticVerificationRunner(revision="a" * 40))
    failed_ledger = ArtifactOutcomeLedger(
        SQLiteRepository(str(tmp_path / "phase14-failed.db")),
        corpus,
        StaticVerificationRunner(status="failed"),
    )

    stale = stale_ledger.record(stale_payload)
    failed = failed_ledger.record(_payload(StaticVerificationRunner(status="failed")))

    assert stale["status"] == "stale_source_snapshot"
    assert stale["calibration"]["eligible"] is False
    assert failed["status"] == "verification_failed"
    assert failed["calibration"]["eligible"] is False


@pytest.mark.conformance
def test_phase14_human_rejection_is_persisted_as_ineligible_evidence(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "phase14-rejected.db"))
    corpus = OperationalEvaluationCorpus(repository)
    corpus.register(_task())
    runner = StaticVerificationRunner()
    ledger = ArtifactOutcomeLedger(repository, corpus, runner)

    rejected = ledger.record(_payload(runner, decision="rejected"))

    assert rejected["status"] == "rejected_by_human"
    assert rejected["calibration"]["eligible"] is False
    assert rejected["human_review"]["decision"] == "rejected"
    assert rejected["claims"]["task_quality_uplift_claimed"] is False


@pytest.mark.conformance
def test_phase14_cockpit_exposes_verified_artifact_outcomes_without_subject_commit(tmp_path):
    runner = StaticVerificationRunner()
    orchestrator = RuntimeOrchestrator(
        db_path=str(tmp_path / "phase14-cockpit.db"),
        verification_runner=runner,
    )
    cockpit = CockpitRuntime(orchestrator=orchestrator)
    initial_version = cockpit.last_state.metadata["version"]

    registration_code, registration = handle_cockpit_request(
        "POST",
        "/api/corpus/tasks",
        body=json.dumps(_task()).encode("utf-8"),
        runtime=cockpit,
    )
    outcome_code, outcome = handle_cockpit_request(
        "POST",
        "/api/artifact-outcomes",
        body=json.dumps(_payload(runner)).encode("utf-8"),
        runtime=cockpit,
    )
    status_code, status = handle_cockpit_request("GET", "/api/status", runtime=cockpit)
    history_code, history = handle_cockpit_request(
        "GET",
        "/api/artifact-outcomes",
        runtime=cockpit,
    )

    assert registration_code == 200
    assert registration["status"] == "registered"
    assert outcome_code == 200
    assert outcome["status"] == "verified_accepted"
    assert outcome["verification"]["source_snapshot"]["revision"] == "a" * 40
    assert outcome["calibration"]["eligible"] is True
    assert cockpit.last_state.metadata["version"] == initial_version
    assert status_code == 200
    assert status["artifact_outcomes"]["latest"]["id"] == outcome["id"]
    assert history_code == 200
    assert history["coverage"]["eligible_count"] == 1
    assert len(history["records"]) == 1
    assert _task()["prompt"] not in json.dumps(history)
    assert orchestrator.repository.verify_provenance()["valid"] is True
