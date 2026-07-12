import asyncio
import hashlib
import json
from typing import Any

import pytest

from spst_runtime.evaluation.artifact_outcome import ArtifactOutcomeLedger
from spst_runtime.evaluation.longitudinal_promotion import (
    LongitudinalPromotionGovernance,
)
from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.evaluation.producer_evidence import (
    build_producer_evidence,
    canonical_artifact_semantics,
)
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
        "required_markers": [],
        "expected_json_keys": ["score"],
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
    producer_evidence: dict[str, str],
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "candidate_id": candidate_id,
        "producer_evidence": producer_evidence,
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


def _producer_references(
    repository: SQLiteRepository,
    *,
    task_id: str,
    candidate_id: str = "candidate-verified",
    baseline_candidate_id: str = "baseline",
    artifact_seed: str | None = None,
    baseline_artifact: str = '{"score":0}',
    candidate_artifact: str = '{"score":1}',
    baseline_plan: dict[str, Any] | None = None,
    candidate_plan: dict[str, Any] | None = None,
) -> dict[str, dict[str, str]]:
    seed = artifact_seed or task_id
    report_identity = json.dumps(
        {
            "task_id": task_id,
            "candidate_id": candidate_id,
            "baseline_candidate_id": baseline_candidate_id,
            "seed": seed,
            "baseline_artifact": baseline_artifact,
            "candidate_artifact": candidate_artifact,
        },
        sort_keys=True,
    )
    report_id = f"SHADOW-{hashlib.sha256(report_identity.encode()).hexdigest()[:16]}"
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
                    "output_digest": hashlib.sha256(
                        baseline_artifact.encode("utf-8")
                    ).hexdigest(),
                    "artifact_semantics": canonical_artifact_semantics(
                        baseline_artifact,
                        ["score"],
                    ),
                    "verification": {"status": "passed"},
                    "plan": baseline_plan or {},
                },
                "maximized": {
                    "status": "completed",
                    "output_digest": hashlib.sha256(
                        candidate_artifact.encode("utf-8")
                    ).hexdigest(),
                    "artifact_semantics": canonical_artifact_semantics(
                        candidate_artifact,
                        ["score"],
                    ),
                    "verification": {"status": "passed"},
                    "plan": candidate_plan
                    or {"mode": "capability_maximization", "strategy_count": 6},
                },
            }
        ],
    }
    report["producer_evidence"] = build_producer_evidence(report)
    asyncio.run(repository.save(f"runtime:operational_shadow:{report_id}", report))
    references = {}
    for binding in report["producer_evidence"]:
        references[binding["candidate_id"]] = {
            "producer_run_id": report_id,
            "binding_id": binding["binding_id"],
        }
    return references


def _policy() -> dict[str, Any]:
    return {
        "policy_id": "verifier-contract",
        "version": "v1",
        "strategies": ["constraint_extraction", "verifier_loop"],
    }


def _record_paired_outcomes(
    repository: SQLiteRepository,
    ledger: ArtifactOutcomeLedger,
    runner: StaticVerificationRunner,
    task_ids: tuple[str, ...],
    *,
    baseline_decision: str = "rejected",
    candidate_decision: str = "accepted",
) -> None:
    for task_id in task_ids:
        references = _producer_references(repository, task_id=task_id)
        ledger.record(
            _outcome_payload(
                runner,
                task_id=task_id,
                candidate_id="baseline",
                decision=baseline_decision,
                producer_evidence=references["baseline"],
            )
        )
        ledger.record(
            _outcome_payload(
                runner,
                task_id=task_id,
                candidate_id="candidate-verified",
                decision=candidate_decision,
                producer_evidence=references["candidate-verified"],
            )
        )


def _record_relabelled_identical_outcomes(
    repository: SQLiteRepository,
    ledger: ArtifactOutcomeLedger,
    runner: StaticVerificationRunner,
    task_ids: tuple[str, ...],
) -> None:
    for task_id in task_ids:
        seed = f"identical-{task_id}"
        baseline_reference = _producer_references(
            repository,
            task_id=task_id,
            candidate_id="baseline",
            baseline_candidate_id="unused-baseline",
            artifact_seed=seed,
        )["baseline"]
        candidate_reference = _producer_references(
            repository,
            task_id=task_id,
            candidate_id="candidate-verified",
            baseline_candidate_id="unused-candidate",
            artifact_seed=seed,
        )["candidate-verified"]
        ledger.record(
            _outcome_payload(
                runner,
                task_id=task_id,
                candidate_id="baseline",
                decision="rejected",
                producer_evidence=baseline_reference,
            )
        )
        ledger.record(
            _outcome_payload(
                runner,
                task_id=task_id,
                candidate_id="candidate-verified",
                decision="accepted",
                producer_evidence=candidate_reference,
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
    _record_paired_outcomes(
        repository,
        ledger,
        runner,
        ("artifact-a", "artifact-b", "artifact-c"),
    )

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
def test_phase15_rejects_relabelled_identical_execution_and_artifact(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "phase15-identical-binding.db"))
    corpus = OperationalEvaluationCorpus(repository)
    runner = StaticVerificationRunner()
    ledger = ArtifactOutcomeLedger(repository, corpus, runner)
    task_ids = ("artifact-a", "artifact-b", "artifact-c")
    for task_id in task_ids:
        corpus.register(_task(task_id))
    _record_relabelled_identical_outcomes(repository, ledger, runner, task_ids)

    proposal = LongitudinalPromotionGovernance(repository, corpus, ledger).propose(
        {
            "candidate_id": "candidate-verified",
            "baseline_candidate_id": "baseline",
            "policy": _policy(),
        }
    )

    assert proposal["status"] == "insufficient_evidence"
    assert "candidate_evidence_not_distinct" in proposal["evidence"]["reasons"]
    assert proposal["evidence"]["paired"]["sample_count"] == 0
    assert proposal["promotion"]["eligible"] is False


@pytest.mark.conformance
@pytest.mark.parametrize(
    ("attack_name", "baseline_artifact", "candidate_artifact", "reason"),
    (
        (
            "json_key_order",
            '{"score":1,"metadata":{"a":1,"b":2}}',
            '{"metadata":{"b":2,"a":1},"score":1}',
            "candidate_evidence_not_distinct",
        ),
        (
            "whitespace_newline_and_json_encoding",
            '{"score":1}',
            '{\r\n  "\\u0073core" : 1\r\n}',
            "candidate_evidence_not_distinct",
        ),
        (
            "nonsemantic_metadata",
            '{"score":1,"timestamp":"2026-01-01","environment":"host-a"}',
            '{"score":1,"timestamp":"2027-02-02","environment":"host-b"}',
            "candidate_evidence_not_distinct",
        ),
        (
            "unrelated_field",
            '{"score":1,"debug":"left"}',
            '{"score":1,"debug":"right","unused":true}',
            "candidate_evidence_not_distinct",
        ),
        (
            "different_filename",
            '{"score":1,"filename":"baseline.json"}',
            '{"score":1,"filename":"candidate-copy.json"}',
            "candidate_evidence_not_distinct",
        ),
        (
            "surface_reserialization",
            '{"score":1}',
            '{\n    "score": 1\n}',
            "candidate_evidence_not_distinct",
        ),
        (
            "meaningless_suffix",
            '{"score":1}',
            '{"score":1}\n.generated-copy',
            "semantic_distinctness_unresolved",
        ),
        (
            "meaningless_wrapper",
            '{"score":1}',
            '{"wrapper":{"score":1}}',
            "semantic_distinctness_unresolved",
        ),
        (
            "unsupported_free_text",
            'score: 1',
            'score: 1 (copy)',
            "semantic_distinctness_unresolved",
        ),
    ),
    ids=lambda value: value if isinstance(value, str) and "{" not in value else None,
)
def test_phase15_rejects_semantic_equivalence_surface_attacks(
    tmp_path,
    attack_name,
    baseline_artifact,
    candidate_artifact,
    reason,
):
    repository = SQLiteRepository(str(tmp_path / f"phase15-{attack_name}.db"))
    corpus = OperationalEvaluationCorpus(repository)
    runner = StaticVerificationRunner()
    ledger = ArtifactOutcomeLedger(repository, corpus, runner)
    task_ids = ("artifact-a", "artifact-b", "artifact-c")
    for task_id in task_ids:
        corpus.register(_task(task_id))
        references = _producer_references(
            repository,
            task_id=task_id,
            baseline_artifact=baseline_artifact,
            candidate_artifact=candidate_artifact,
        )
        ledger.record(
            _outcome_payload(
                runner,
                task_id=task_id,
                candidate_id="baseline",
                decision="rejected",
                producer_evidence=references["baseline"],
            )
        )
        ledger.record(
            _outcome_payload(
                runner,
                task_id=task_id,
                candidate_id="candidate-verified",
                decision="accepted",
                producer_evidence=references["candidate-verified"],
            )
        )

    proposal = LongitudinalPromotionGovernance(repository, corpus, ledger).propose(
        {
            "candidate_id": "candidate-verified",
            "baseline_candidate_id": "baseline",
            "policy": _policy(),
        }
    )

    assert proposal["status"] == "insufficient_evidence"
    assert reason in proposal["evidence"]["reasons"]
    assert proposal["evidence"]["paired"]["sample_count"] == 0
    assert proposal["promotion"]["eligible"] is False


@pytest.mark.conformance
def test_phase15_rejects_swapped_arm_labels_and_nonsemantic_plan_metadata(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "phase15-label-swap.db"))
    corpus = OperationalEvaluationCorpus(repository)
    runner = StaticVerificationRunner()
    ledger = ArtifactOutcomeLedger(repository, corpus, runner)
    task_ids = ("artifact-a", "artifact-b", "artifact-c")
    shared_plan = {
        "mode": "same-mode",
        "domains": ["implementation"],
        "strategy_count": 2,
        "verification_check_count": 1,
    }
    for task_id in task_ids:
        corpus.register(_task(task_id))
        references = _producer_references(
            repository,
            task_id=task_id,
            candidate_id="baseline",
            baseline_candidate_id="candidate-verified",
            baseline_artifact='{\n  "score": 1\n}',
            candidate_artifact='{"score":1}',
            baseline_plan={
                **shared_plan,
                "timestamp": "2026-01-01",
                "absolute_path": "C:/baseline/result.json",
            },
            candidate_plan={
                **shared_plan,
                "timestamp": "2027-01-01",
                "absolute_path": "D:/candidate/result.json",
                "environment": "other-host",
            },
        )
        ledger.record(
            _outcome_payload(
                runner,
                task_id=task_id,
                candidate_id="baseline",
                decision="rejected",
                producer_evidence=references["baseline"],
            )
        )
        ledger.record(
            _outcome_payload(
                runner,
                task_id=task_id,
                candidate_id="candidate-verified",
                decision="accepted",
                producer_evidence=references["candidate-verified"],
            )
        )

    proposal = LongitudinalPromotionGovernance(repository, corpus, ledger).propose(
        {
            "candidate_id": "candidate-verified",
            "baseline_candidate_id": "baseline",
            "policy": _policy(),
        }
    )

    assert "candidate_evidence_not_distinct" in proposal["evidence"]["reasons"]
    assert proposal["evidence"]["paired"]["sample_count"] == 0


@pytest.mark.conformance
def test_phase15_retains_legacy_unbound_records_but_excludes_them_from_promotion(
    tmp_path,
):
    repository = SQLiteRepository(str(tmp_path / "phase15-legacy-unbound.db"))
    corpus = OperationalEvaluationCorpus(repository)
    runner = StaticVerificationRunner()
    ledger = ArtifactOutcomeLedger(repository, corpus, runner)
    task_ids = ("artifact-a", "artifact-b", "artifact-c")
    for task_id in task_ids:
        corpus.register(_task(task_id))
    _record_paired_outcomes(repository, ledger, runner, task_ids)
    index = asyncio.run(repository.load(ledger.INDEX_KEY))
    assert isinstance(index, dict)
    records = index["records"]
    for record in records:
        record["producer_evidence"] = {"status": "legacy_unbound"}
    asyncio.run(repository.save(ledger.INDEX_KEY, index))

    proposal = LongitudinalPromotionGovernance(repository, corpus, ledger).propose(
        {
            "candidate_id": "candidate-verified",
            "baseline_candidate_id": "baseline",
            "policy": _policy(),
        }
    )

    assert len(ledger.history()) == 6
    assert "invalid_artifact_outcome" in proposal["evidence"]["reasons"]
    assert proposal["evidence"]["paired"]["sample_count"] == 0
    assert proposal["promotion"]["eligible"] is False


@pytest.mark.conformance
def test_phase15_rejects_insufficient_or_ambiguous_evidence_without_auto_promotion(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "phase15-insufficient.db"))
    corpus = OperationalEvaluationCorpus(repository)
    runner = StaticVerificationRunner()
    ledger = ArtifactOutcomeLedger(repository, corpus, runner)
    for task_id in ("artifact-a", "artifact-b"):
        corpus.register(_task(task_id))
    _record_paired_outcomes(repository, ledger, runner, ("artifact-a", "artifact-b"))

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
            producer_evidence=_producer_references(
                repository,
                task_id="artifact-a",
                artifact_seed="ambiguous",
            )["candidate-verified"],
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
    _record_paired_outcomes(
        repository,
        ledger,
        runner,
        ("artifact-a", "artifact-b", "artifact-c"),
    )
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
        references = _producer_references(orchestrator.repository, task_id=task_id)
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
                        producer_evidence=references[candidate_id],
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
