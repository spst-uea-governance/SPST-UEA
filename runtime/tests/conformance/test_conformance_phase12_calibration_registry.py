import json
from typing import Any

import pytest

from spst_runtime.evaluation.calibration_registry import CalibrationRegistry
from spst_runtime.evaluation.capability_evaluation import (
    CapabilityEvaluationRunner,
    EvaluationCase,
)
from spst_runtime.interfaces.model_adapter import ModelAdapter
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.providers.model_provider import ModelProvider
from spst_runtime.web import CockpitRuntime, handle_cockpit_request


class CalibrationAdapter(ModelAdapter):
    """Deterministic adapter with a stable provider contract for calibration tests."""

    def __init__(self, profile: str = "baseline", model_version: str = "v1"):
        self.profile = profile
        self.model_version = model_version

    async def infer(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        maximized = bool((context or {}).get("capability_maximization"))
        if self.profile == "regressed" and maximized:
            text = "plan"
        else:
            text = "plan analyze verify"
        return {
            "provider": "calibration-local",
            "available": True,
            "requires_api_key": False,
            "text": text,
        }

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": "calibration-local",
            "model_version": self.model_version,
            "requires_api_key": False,
        }

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "interface": "ModelAdapter",
            "supports_structured_evaluation": True,
            "requires_api_key": False,
        }


def _report(profile: str = "baseline", model_version: str = "v1") -> dict[str, Any]:
    return CapabilityEvaluationRunner(
        CalibrationAdapter(profile=profile, model_version=model_version)
    ).run()


@pytest.mark.conformance
def test_phase12_persists_immutable_baseline_and_detects_regression(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "calibration.db"))
    registry = CalibrationRegistry(repository)

    baseline = registry.register(_report(), candidate_id="baseline")
    regressed = registry.register(
        _report("regressed"),
        candidate_id="candidate-regressed",
        baseline_id=baseline["id"],
    )

    assert baseline["role"] == "baseline"
    assert baseline["comparison"]["status"] == "baseline_recorded"
    assert regressed["comparison"]["status"] == "regressed"
    assert regressed["comparison"]["sample_count"] == 2
    assert regressed["policy"]["requires_human_approval"] is True
    assert regressed["policy"]["automatic_adoption"] is False

    baseline["candidate_id"] = "mutated-locally"
    persisted_baseline = registry.get("CAL-" + baseline["id"].split("CAL-", 1)[-1])
    assert persisted_baseline is not None
    assert persisted_baseline["candidate_id"] == "baseline"
    assert len(registry.history()) == 2
    assert "prompt" not in json.dumps(registry.history())
    assert "text" not in json.dumps(registry.history())
    assert repository.verify_provenance()["valid"] is True


@pytest.mark.conformance
def test_phase12_marks_contract_mismatch_and_no_key_runs_without_uplift_claim(tmp_path):
    repository = SQLiteRepository(str(tmp_path / "calibration-contracts.db"))
    registry = CalibrationRegistry(repository)
    baseline = registry.register(_report(), candidate_id="baseline")
    changed_suite = (
        EvaluationCase(
            "changed_contract",
            "Produce a verifiable design with explicit assumptions.",
            "design",
            ("design", "verify"),
        ),
    )
    changed_report = CapabilityEvaluationRunner(
        CalibrationAdapter(),
        suite=changed_suite,
    ).run()

    mismatch = registry.register(
        changed_report,
        candidate_id="changed-suite",
        baseline_id=baseline["id"],
    )
    no_key = registry.register(
        CapabilityEvaluationRunner(ModelProvider()).run(),
        candidate_id="no-key-default",
    )

    assert mismatch["comparison"]["status"] == "not_comparable"
    assert "suite_hash_mismatch" in mismatch["comparison"]["reasons"]
    assert no_key["comparison"]["status"] == "scaffold_only"
    assert no_key["claims"]["task_quality_uplift_claimed"] is False
    assert no_key["policy"]["automatic_adoption"] is False


@pytest.mark.conformance
def test_phase12_blocks_cross_model_version_comparison(tmp_path):
    registry = CalibrationRegistry(SQLiteRepository(str(tmp_path / "calibration-version.db")))
    baseline = registry.register(_report(model_version="v1"), candidate_id="baseline")

    version_mismatch = registry.register(
        _report(model_version="v2"),
        candidate_id="candidate-v2",
        baseline_id=baseline["id"],
    )

    assert version_mismatch["comparison"]["status"] == "not_comparable"
    assert "provider_model_version_mismatch" in version_mismatch["comparison"]["reasons"]


@pytest.mark.conformance
def test_phase12_cockpit_exposes_history_and_escalates_regression_to_hitl(tmp_path):
    adapter = CalibrationAdapter()
    orchestrator = RuntimeOrchestrator(
        db_path=str(tmp_path / "calibration-cockpit.db"),
        model_provider=adapter,
    )
    cockpit = CockpitRuntime(orchestrator=orchestrator)

    baseline = cockpit.dispatch(
        {
            "prompt": "Register a bounded baseline calibration.",
            "evaluate_capability": True,
            "calibration_candidate": "baseline",
        }
    )
    adapter.profile = "regressed"
    candidate = cockpit.dispatch(
        {
            "prompt": "Evaluate a candidate runtime configuration.",
            "evaluate_capability": True,
            "calibration_candidate": "candidate-regressed",
            "calibration_baseline_id": baseline["calibration"]["id"],
        }
    )

    status_code, status = handle_cockpit_request("GET", "/api/status", runtime=cockpit)
    history_code, history = handle_cockpit_request(
        "GET",
        "/api/calibrations",
        runtime=cockpit,
    )

    assert baseline["governance"]["authorized"] is True
    assert candidate["calibration"]["comparison"]["status"] == "regressed"
    assert candidate["governance"]["authorized"] is False
    assert candidate["governance"]["requires_human_approval"] is True
    assert "calibration_regression_requires_human_approval" in candidate["governance"]["reasons"]
    assert status_code == 200
    assert status["calibration"]["id"] == candidate["calibration"]["id"]
    assert history_code == 200
    assert len(history["records"]) == 2
    assert orchestrator.repository.verify_provenance()["valid"] is True
