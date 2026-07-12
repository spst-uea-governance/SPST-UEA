import json
from typing import Any

import pytest

from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.evaluation.operational_shadow import OperationalShadowRunner
from spst_runtime.evaluation.producer_evidence import (
    build_producer_evidence,
    canonical_artifact_semantics,
)
from spst_runtime.interfaces.model_adapter import ModelAdapter
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.providers.model_provider import ModelProvider
from spst_runtime.web import CockpitRuntime, handle_cockpit_request


class ShadowAdapter(ModelAdapter):
    """Deterministic provider for consent-scoped shadow evaluation tests."""

    def __init__(self, profile: str = "baseline"):
        self.profile = profile

    async def infer(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        maximized = bool((context or {}).get("capability_maximization"))
        text = "plan analyze verify"
        if self.profile == "regressed" and maximized:
            text = "plan"
        return {
            "provider": "shadow-local",
            "available": True,
            "requires_api_key": False,
            "text": text,
        }

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": "shadow-local",
            "model_version": "shadow-v1",
            "requires_api_key": False,
        }

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "interface": "ModelAdapter",
            "supports_structured_evaluation": True,
            "requires_api_key": False,
        }


def _task(
    task_id: str = "holdout-analysis",
    *,
    retention_until: str = "2099-12-31",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "prompt": "Private master task: analyze the local design and verify the contract.",
        "domain": "analysis",
        "split": "holdout",
        "required_markers": ["plan", "verify"],
        "consent": {
            "granted": True,
            "scope": "local_operational_evaluation",
            "retention_until": retention_until,
        },
    }


@pytest.mark.conformance
def test_phase13_requires_consent_blocks_secret_like_tasks_and_redacts_manifest(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "phase13-consent.db"))
    corpus = OperationalEvaluationCorpus(repository)
    missing_consent = _task("missing-consent")
    missing_consent["consent"]["granted"] = False
    secret_like = _task("secret-like")
    secret_like["prompt"] = 'OPENAI_API_KEY = "must-not-be-stored"'

    denied = corpus.register(missing_consent)
    security_denied = corpus.register(secret_like)
    accepted = corpus.register(_task())
    manifest = corpus.manifest()
    public_payload = json.dumps({"accepted": accepted, "manifest": manifest})

    assert denied["status"] == "blocked"
    assert "operational_corpus_consent_required" in denied["governance"]["reasons"]
    assert security_denied["status"] == "blocked"
    assert security_denied["security_assessment"]["blocked"] is True
    assert accepted["status"] == "registered"
    assert manifest["eligible_count"] == 1
    assert _task()["prompt"] not in public_payload
    assert "must-not-be-stored" not in public_payload
    assert repository.verify_provenance()["valid"] is True


@pytest.mark.conformance
def test_phase13_shadow_excludes_expired_tasks_and_never_records_raw_prompt(tmp_path):
    corpus = OperationalEvaluationCorpus(SQLiteRepository(str(tmp_path / "phase13-shadow.db")))
    active_task = _task("active-holdout")
    expired_task = _task("expired-holdout", retention_until="2000-01-01")
    corpus.register(active_task)
    corpus.register(expired_task)

    report = OperationalShadowRunner(ShadowAdapter(), corpus).run(candidate_id="baseline")

    assert report["status"] == "shadow_completed"
    assert report["suite"]["eligible_count"] == 1
    assert report["retention"]["excluded_expired_count"] == 1
    assert report["shadow"]["subject_state_committed"] is False
    assert report["shadow"]["actions_executed"] is False
    assert report["task_quality"]["available"] is True
    assert active_task["prompt"] not in json.dumps(report)
    assert report["claims"]["automatic_adoption"] is False


@pytest.mark.conformance
def test_phase13_binds_each_task_arm_to_compact_producer_evidence(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "phase13-producer-evidence.db"))
    corpus = OperationalEvaluationCorpus(repository)
    corpus.register(_task())

    report = OperationalShadowRunner(ShadowAdapter(), corpus).run(
        candidate_id="candidate-maximized",
        baseline_candidate_id="candidate-baseline",
    )
    bindings = report["producer_evidence"]

    assert len(bindings) == 2
    assert {binding["candidate_id"] for binding in bindings} == {
        "candidate-baseline",
        "candidate-maximized",
    }
    assert {binding["arm"] for binding in bindings} == {"baseline", "maximized"}
    assert {binding["producer_run_id"] for binding in bindings} == {report["id"]}
    assert len({binding["configuration_digest"] for binding in bindings}) == 2
    assert all(binding["artifact_digest"] for binding in bindings)
    assert all(binding["binding_id"].startswith("PBIND-") for binding in bindings)
    assert all(
        binding["artifact_semantic_status"] == "unresolved" for binding in bindings
    )
    assert _task()["prompt"] not in json.dumps(bindings)


@pytest.mark.conformance
def test_phase13_canonicalizes_contract_json_without_treating_wrappers_as_evidence():
    canonical = canonical_artifact_semantics(
        '{"score":1,"timestamp":"2026-07-12T00:00:00Z","filename":"a.json"}',
        ["score"],
    )
    surface_variants = (
        '{\n  "filename": "renamed.json", "score": 1, "environment": "other"\n}',
        '{"\\u0073core":1,"absolute_path":"C:/different/result.json"}',
        '{"unrelated":{"debug":true},"score":1}',
    )

    assert canonical["status"] == "resolved"
    assert all(
        canonical_artifact_semantics(value, ["score"])["semantic_digest"]
        == canonical["semantic_digest"]
        for value in surface_variants
    )
    assert canonical_artifact_semantics('{"payload":{"score":1}}', ["score"])[
        "status"
    ] == "unresolved"
    assert canonical_artifact_semantics('{"score":1}\nmeaningless-suffix', ["score"])[
        "status"
    ] == "unresolved"
    assert canonical_artifact_semantics('{"result":"free text"}', ["result"])[
        "reason"
    ] == "free_text_semantics_unresolved"


@pytest.mark.conformance
def test_phase13_semantic_configuration_ignores_only_identity_and_metadata_labels():
    def report(
        run_id: str,
        task_id: str,
        candidate_id: str,
        baseline_candidate_id: str,
        *,
        suite_hash: str,
    ) -> dict[str, Any]:
        semantics = canonical_artifact_semantics('{"score":1}', ["score"])
        return {
            "id": run_id,
            "candidate_id": candidate_id,
            "baseline_candidate_id": baseline_candidate_id,
            "suite": {
                "version": "phase13-operational-shadow-v1",
                "hash": suite_hash,
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
                    "domain": "analysis",
                    "prompt_digest": "same-prompt-contract",
                    "baseline": {
                        "status": "completed",
                        "output_digest": "same-artifact",
                        "artifact_semantics": semantics,
                        "verification": {"status": "passed"},
                        "plan": {},
                    },
                    "maximized": {
                        "status": "completed",
                        "output_digest": "same-artifact",
                        "artifact_semantics": semantics,
                        "verification": {"status": "passed"},
                        "plan": {
                            "mode": "capability_maximization",
                            "domains": ["analysis"],
                            "strategy_count": 6,
                            "verification_check_count": 2,
                            "timestamp": "ignored",
                            "absolute_path": "C:/ignored",
                            "unrelated": "ignored",
                        },
                    },
                }
            ],
        }

    first = build_producer_evidence(
        report("SHADOW-run-a", "task-a", "candidate-a", "baseline-a", suite_hash="a")
    )
    relabeled = build_producer_evidence(
        report("SHADOW-run-b", "task-a", "candidate-b", "baseline-b", suite_hash="a")
    )
    retasked = build_producer_evidence(
        report("SHADOW-run-c", "task-b", "candidate-c", "baseline-c", suite_hash="c")
    )

    first_maximized = next(item for item in first if item["arm"] == "maximized")
    relabeled_maximized = next(item for item in relabeled if item["arm"] == "maximized")
    retasked_maximized = next(item for item in retasked if item["arm"] == "maximized")
    assert first_maximized["binding_id"] != relabeled_maximized["binding_id"]
    assert (
        first_maximized["configuration_digest"]
        == relabeled_maximized["configuration_digest"]
    )
    assert (
        first_maximized["semantic_configuration_digest"]
        == relabeled_maximized["semantic_configuration_digest"]
        == retasked_maximized["semantic_configuration_digest"]
    )
    assert (
        first_maximized["configuration_digest"]
        != retasked_maximized["configuration_digest"]
    )


@pytest.mark.conformance
def test_phase13_no_key_shadow_remains_scaffold_only_without_uplift_claim(tmp_path):
    corpus = OperationalEvaluationCorpus(SQLiteRepository(str(tmp_path / "phase13-no-key.db")))
    corpus.register(_task())

    report = OperationalShadowRunner(ModelProvider(), corpus).run(candidate_id="no-key")

    assert report["provider"]["name"] == "codex-mediated-local"
    assert report["provider"]["requires_api_key"] is False
    assert report["status"] == "scaffold_only"
    assert report["task_quality"]["available"] is False
    assert report["claims"]["task_quality_uplift_claimed"] is False


@pytest.mark.conformance
def test_phase13_cockpit_runs_shadow_history_without_subject_commit_and_holds_regression(
    tmp_path,
):
    adapter = ShadowAdapter()
    orchestrator = RuntimeOrchestrator(
        db_path=str(tmp_path / "phase13-cockpit.db"),
        model_provider=adapter,
    )
    cockpit = CockpitRuntime(orchestrator=orchestrator)
    initial_version = cockpit.last_state.metadata["version"]

    for task_id in ("holdout-analysis", "holdout-design"):
        registration_code, registration = handle_cockpit_request(
            "POST",
            "/api/corpus/tasks",
            body=json.dumps(_task(task_id)).encode("utf-8"),
            runtime=cockpit,
        )
        assert registration_code == 200
        assert registration["status"] == "registered"

    baseline_code, baseline = handle_cockpit_request(
        "POST",
        "/api/shadow-evaluations",
        body=json.dumps({"candidate_id": "baseline"}).encode("utf-8"),
        runtime=cockpit,
    )
    adapter.profile = "regressed"
    candidate_code, candidate = handle_cockpit_request(
        "POST",
        "/api/shadow-evaluations",
        body=json.dumps(
            {
                "candidate_id": "candidate-regressed",
                "baseline_id": baseline["calibration"]["id"],
            }
        ).encode("utf-8"),
        runtime=cockpit,
    )
    status_code, status = handle_cockpit_request("GET", "/api/status", runtime=cockpit)
    corpus_code, corpus = handle_cockpit_request("GET", "/api/corpus", runtime=cockpit)
    history_code, history = handle_cockpit_request(
        "GET",
        "/api/shadow-evaluations",
        runtime=cockpit,
    )

    assert baseline_code == 200
    assert candidate_code == 200
    assert candidate["calibration"]["comparison"]["status"] == "regressed"
    assert candidate["calibration"]["policy"]["requires_human_approval"] is True
    assert candidate["promotion"]["status"] == "held_for_human_review"
    assert cockpit.last_state.metadata["version"] == initial_version
    assert status_code == 200
    assert status["shadow"]["id"] == candidate["id"]
    assert corpus_code == 200
    assert _task()["prompt"] not in json.dumps(corpus)
    assert history_code == 200
    assert len(history["records"]) == 2
    assert orchestrator.repository.verify_provenance()["valid"] is True
