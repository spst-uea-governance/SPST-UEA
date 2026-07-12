import asyncio
import hashlib
import json
from typing import Any

import pytest

from spst_runtime.evaluation.artifact_outcome import ArtifactOutcomeLedger
from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.evaluation.producer_evidence import build_producer_evidence
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


class MissingProfilesVerificationRunner(StaticVerificationRunner):
    """Claims success while omitting the fixed verification evidence."""

    def run(self) -> dict[str, Any]:
        result = super().run()
        result["profiles"] = {}
        return result


class IncompleteProfilesVerificationRunner(StaticVerificationRunner):
    """Keeps a passing summary while one fixed profile is absent or failed."""

    def __init__(self, profile_status: str | None):
        super().__init__()
        self.profile_status = profile_status

    def run(self) -> dict[str, Any]:
        result = super().run()
        if self.profile_status is None:
            result["profiles"].pop("mypy")
        else:
            result["profiles"]["mypy"]["status"] = self.profile_status
        return result


class RaisingVerificationRunner:
    def run(self) -> dict[str, Any]:
        raise RuntimeError("verification process failed")


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


def _producer_reference(
    repository: SQLiteRepository,
    *,
    task_id: str = "artifact-analysis",
    candidate_id: str = "candidate-artifact-v1",
    baseline_candidate_id: str = "candidate-baseline",
    arm: str = "maximized",
    artifact_seed: str | None = None,
    producer_status: str = "completed",
) -> dict[str, str]:
    seed = artifact_seed or task_id
    report_id = f"SHADOW-{hashlib.sha256(f'{task_id}:{candidate_id}:{seed}:{producer_status}'.encode()).hexdigest()[:16]}"
    report = {
        "id": report_id,
        "candidate_id": candidate_id,
        "baseline_candidate_id": baseline_candidate_id,
        "suite": {
            "version": "phase13-operational-shadow-v1",
            "hash": f"suite-{task_id}",
            "split": "holdout",
        },
        "provider": {
            "name": "shadow-local",
            "model_version": "shadow-v1",
            "task_scoring_supported": True,
        },
        "cases": [
            {
                "case_id": task_id,
                "domain": "implementation",
                "prompt_digest": f"prompt-{task_id}",
                "baseline": {
                    "status": "completed",
                    "output_digest": f"artifact-baseline-{seed}",
                    "verification": {"status": "passed"},
                    "plan": {},
                },
                "maximized": {
                    "status": producer_status,
                    "output_digest": f"artifact-maximized-{seed}",
                    "verification": {"status": "passed"},
                    "plan": {"mode": "capability_maximization", "strategy_count": 6},
                },
            }
        ],
    }
    report["producer_evidence"] = build_producer_evidence(report)
    asyncio.run(repository.save(f"runtime:operational_shadow:{report_id}", report))
    binding = next(item for item in report["producer_evidence"] if item["arm"] == arm)
    return {
        "producer_run_id": report_id,
        "binding_id": binding["binding_id"],
    }


def _payload(
    runner: StaticVerificationRunner,
    *,
    task_id: str = "artifact-analysis",
    candidate_id: str = "candidate-artifact-v1",
    decision: str = "accepted",
    producer_evidence: dict[str, str] | None = None,
) -> dict[str, Any]:
    snapshot = runner.run()["source_snapshot"]
    payload = {
        "task_id": task_id,
        "candidate_id": candidate_id,
        "expected_source_snapshot": snapshot,
        "human_calibration": _human_calibration(decision),
    }
    if producer_evidence is not None:
        payload["producer_evidence"] = producer_evidence
    return payload


@pytest.mark.conformance
def test_phase14_requires_human_consent_and_redacts_task_and_human_input(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "phase14-consent.db"))
    corpus = OperationalEvaluationCorpus(repository)
    corpus.register(_task())
    runner = StaticVerificationRunner()
    ledger = ArtifactOutcomeLedger(repository, corpus, runner)
    producer_evidence = _producer_reference(repository)
    missing_consent = _payload(runner, producer_evidence=producer_evidence)
    missing_consent["human_calibration"]["consent"]["granted"] = False

    denied = ledger.record(missing_consent)
    accepted = ledger.record(_payload(runner, producer_evidence=producer_evidence))
    public_payload = json.dumps({"denied": denied, "accepted": accepted, "history": ledger.history()})

    assert denied["status"] == "blocked"
    assert "artifact_outcome_human_consent_required" in denied["governance"]["reasons"]
    assert accepted["status"] == "verified_accepted"
    assert accepted["calibration"]["eligible"] is True
    assert _task()["prompt"] not in public_payload
    assert "human_calibration" not in public_payload
    assert repository.verify_provenance()["valid"] is True


@pytest.mark.conformance
def test_phase14_requires_persisted_producer_evidence_reference(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "phase14-producer-required.db"))
    corpus = OperationalEvaluationCorpus(repository)
    corpus.register(_task())
    runner = StaticVerificationRunner()
    ledger = ArtifactOutcomeLedger(repository, corpus, runner)

    result = ledger.record(_payload(runner))

    assert result["status"] == "blocked"
    assert "artifact_outcome_producer_evidence_required" in result["governance"]["reasons"]
    assert result["calibration"]["eligible"] is False


@pytest.mark.conformance
def test_phase14_rejects_tampered_or_relabeled_producer_binding(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "phase14-producer-invalid.db"))
    corpus = OperationalEvaluationCorpus(repository)
    corpus.register(_task())
    runner = StaticVerificationRunner()
    ledger = ArtifactOutcomeLedger(repository, corpus, runner)
    producer_evidence = _producer_reference(repository)
    tampered_reference = {
        **producer_evidence,
        "binding_id": f"{producer_evidence['binding_id'][:-1]}0",
    }

    tampered = ledger.record(
        _payload(runner, producer_evidence=tampered_reference)
    )
    relabeled = ledger.record(
        _payload(
            runner,
            candidate_id="candidate-relabeled",
            producer_evidence=producer_evidence,
        )
    )

    assert tampered["status"] == "blocked"
    assert tampered["producer_evidence"]["status"] == "binding_invalid"
    assert relabeled["status"] == "blocked"
    assert relabeled["producer_evidence"]["status"] == "candidate_mismatch"
    assert "artifact_outcome_producer_evidence_invalid" in relabeled["governance"]["reasons"]


@pytest.mark.conformance
def test_phase14_rejects_missing_incomplete_and_task_mismatched_before_verification(
    tmp_path,
):
    repository = SQLiteRepository(str(tmp_path / "phase14-reference-boundaries.db"))
    corpus = OperationalEvaluationCorpus(repository)
    corpus.register(_task())
    corpus.register(_task("artifact-other"))
    runner = StaticVerificationRunner()
    ledger = ArtifactOutcomeLedger(repository, corpus, runner)
    valid_reference = _producer_reference(repository)
    incomplete_reference = _producer_reference(
        repository,
        artifact_seed="incomplete",
        producer_status="unavailable",
    )

    missing = ledger.record(
        _payload(
            runner,
            producer_evidence={
                "producer_run_id": "SHADOW-missing",
                "binding_id": "PBIND-missing",
            },
        )
    )
    incomplete = ledger.record(
        _payload(runner, producer_evidence=incomplete_reference)
    )
    task_mismatched = ledger.record(
        _payload(
            runner,
            task_id="artifact-other",
            producer_evidence=valid_reference,
        )
    )

    assert missing["producer_evidence"]["status"] == "producer_run_not_found"
    assert incomplete["producer_evidence"]["status"] == "producer_not_completed"
    assert task_mismatched["producer_evidence"]["status"] == "task_mismatch"
    assert all(
        result["status"] == "blocked" and result["verification"] == {}
        for result in (missing, incomplete, task_mismatched)
    )


@pytest.mark.conformance
def test_phase14_blocks_stale_or_failed_artifact_evidence_from_calibration(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "phase14-stale.db"))
    corpus = OperationalEvaluationCorpus(repository)
    corpus.register(_task())
    current_runner = StaticVerificationRunner(revision="b" * 40)
    stale_ledger = ArtifactOutcomeLedger(repository, corpus, current_runner)
    producer_evidence = _producer_reference(repository)
    stale_payload = _payload(
        StaticVerificationRunner(revision="a" * 40),
        producer_evidence=producer_evidence,
    )
    failed_repository = SQLiteRepository(str(tmp_path / "phase14-failed.db"))
    failed_corpus = OperationalEvaluationCorpus(failed_repository)
    failed_corpus.register(_task())
    failed_runner = StaticVerificationRunner(status="failed")
    failed_ledger = ArtifactOutcomeLedger(failed_repository, failed_corpus, failed_runner)
    failed_producer_evidence = _producer_reference(failed_repository)

    stale = stale_ledger.record(stale_payload)
    failed = failed_ledger.record(
        _payload(failed_runner, producer_evidence=failed_producer_evidence)
    )

    assert stale["status"] == "stale_source_snapshot"
    assert stale["calibration"]["eligible"] is False
    assert failed["status"] == "verification_failed"
    assert failed["calibration"]["eligible"] is False


@pytest.mark.conformance
def test_phase14_rejects_passed_run_when_fixed_profiles_are_missing(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "phase14-missing-profiles.db"))
    corpus = OperationalEvaluationCorpus(repository)
    corpus.register(_task())
    runner = MissingProfilesVerificationRunner()
    ledger = ArtifactOutcomeLedger(repository, corpus, runner)
    producer_evidence = _producer_reference(repository)

    result = ledger.record(_payload(runner, producer_evidence=producer_evidence))

    assert result["status"] == "verification_failed"
    assert result["calibration"]["eligible"] is False
    assert result["claims"]["artifact_outcome_verified"] is False


@pytest.mark.conformance
@pytest.mark.parametrize("profile_status", [None, "failed"])
def test_phase14_requires_each_fixed_profile_to_complete(tmp_path, profile_status):
    repository = SQLiteRepository(str(tmp_path / f"phase14-profile-{profile_status}.db"))
    corpus = OperationalEvaluationCorpus(repository)
    corpus.register(_task())
    runner = IncompleteProfilesVerificationRunner(profile_status)
    ledger = ArtifactOutcomeLedger(repository, corpus, runner)
    producer_evidence = _producer_reference(repository)

    result = ledger.record(_payload(runner, producer_evidence=producer_evidence))

    assert result["status"] == "verification_failed"
    assert result["calibration"]["eligible"] is False


@pytest.mark.conformance
def test_phase14_records_runner_exception_as_ineligible_failure(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "phase14-runner-exception.db"))
    corpus = OperationalEvaluationCorpus(repository)
    corpus.register(_task())
    snapshot_runner = StaticVerificationRunner()
    ledger = ArtifactOutcomeLedger(repository, corpus, RaisingVerificationRunner())
    producer_evidence = _producer_reference(repository)

    result = ledger.record(
        _payload(snapshot_runner, producer_evidence=producer_evidence)
    )

    assert result["status"] == "verification_failed"
    assert result["calibration"]["eligible"] is False
    assert result["verification"]["error_digest"]


@pytest.mark.conformance
def test_phase14_human_rejection_is_persisted_as_ineligible_evidence(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "phase14-rejected.db"))
    corpus = OperationalEvaluationCorpus(repository)
    corpus.register(_task())
    runner = StaticVerificationRunner()
    ledger = ArtifactOutcomeLedger(repository, corpus, runner)
    producer_evidence = _producer_reference(repository)

    rejected = ledger.record(
        _payload(runner, decision="rejected", producer_evidence=producer_evidence)
    )

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
    shadow_code, shadow = handle_cockpit_request(
        "POST",
        "/api/shadow-evaluations",
        body=json.dumps(
            {
                "candidate_id": "candidate-artifact-v1",
                "baseline_candidate_id": "candidate-baseline",
            }
        ).encode("utf-8"),
        runtime=cockpit,
    )
    producer_binding = next(
        item for item in shadow["producer_evidence"] if item["arm"] == "maximized"
    )
    outcome_code, outcome = handle_cockpit_request(
        "POST",
        "/api/artifact-outcomes",
        body=json.dumps(
            _payload(
                runner,
                producer_evidence={
                    "producer_run_id": shadow["id"],
                    "binding_id": producer_binding["binding_id"],
                },
            )
        ).encode("utf-8"),
        runtime=cockpit,
    )
    status_code, status = handle_cockpit_request("GET", "/api/status", runtime=cockpit)
    history_code, history = handle_cockpit_request(
        "GET",
        "/api/artifact-outcomes",
        runtime=cockpit,
    )

    assert registration_code == 200
    assert shadow_code == 200
    assert registration["status"] == "registered"
    assert outcome_code == 200
    assert outcome["status"] == "verified_accepted"
    assert outcome["verification"]["source_snapshot"]["revision"] == "a" * 40
    assert outcome["calibration"]["eligible"] is True
    assert outcome["producer_evidence"]["status"] == "verified"
    assert cockpit.last_state.metadata["version"] == initial_version
    assert status_code == 200
    assert status["artifact_outcomes"]["latest"]["id"] == outcome["id"]
    assert history_code == 200
    assert history["coverage"]["eligible_count"] == 1
    assert len(history["records"]) == 1
    assert _task()["prompt"] not in json.dumps(history)
    assert orchestrator.repository.verify_provenance()["valid"] is True
