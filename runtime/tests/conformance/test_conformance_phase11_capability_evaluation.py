import asyncio
from typing import Any

import pytest

from spst_runtime.evaluation.capability_evaluation import CapabilityEvaluationRunner
from spst_runtime.interfaces.model_adapter import ModelAdapter
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator
from spst_runtime.providers.model_provider import ModelProvider
from spst_runtime.web import CockpitRuntime, handle_cockpit_request


class StructuredEvaluationAdapter(ModelAdapter):
    """Deterministic local adapter used only to exercise observable pair scoring."""

    async def infer(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        maximized = bool((context or {}).get("capability_maximization"))
        return {
            "provider": "structured-local",
            "available": True,
            "requires_api_key": False,
            "text": "plan analyze verify" if maximized else "plan analyze",
        }

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": "structured-local",
            "requires_api_key": False,
        }

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "interface": "ModelAdapter",
            "supports_structured_evaluation": True,
            "requires_api_key": False,
        }


class RegressiveEvaluationAdapter(StructuredEvaluationAdapter):
    async def infer(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        maximized = bool((context or {}).get("capability_maximization"))
        return {
            "provider": "regressive-local",
            "available": True,
            "requires_api_key": False,
            "text": "plan" if maximized else "plan analyze verify",
        }

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": "regressive-local",
            "requires_api_key": False,
        }


class ClaimStuffingAdapter(StructuredEvaluationAdapter):
    """Returns proxy markers plus forged quality fields that the runner must ignore."""

    async def infer(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        maximized = bool((context or {}).get("capability_maximization"))
        return {
            "provider": "structured-local",
            "available": True,
            "requires_api_key": False,
            "text": "plan analyze verify" if maximized else "irrelevant",
            "task_score": 1.0,
            "task_quality_uplift_claimed": True,
            "semantic_task_quality_established": True,
        }


@pytest.mark.conformance
def test_phase11_produces_paired_provider_neutral_capability_evidence():
    report = CapabilityEvaluationRunner(StructuredEvaluationAdapter()).run()

    assert report["id"].startswith("EVAL-")
    assert report["provider"]["name"] == "structured-local"
    assert report["provider"]["requires_api_key"] is False
    assert report["provider"]["contract_scoring_supported"] is True
    assert report["provider"]["task_scoring_supported"] is False
    assert report["suite"]["same_tasks"] is True
    assert report["contract_compliance_proxy"]["available"] is True
    assert (
        report["contract_compliance_proxy"]["maximized_mean"]
        > report["contract_compliance_proxy"]["baseline_mean"]
    )
    assert report["task_quality"]["available"] is False
    assert report["task_quality"]["semantic_task_quality_established"] is False
    assert report["task_quality"]["paired_delta"] is None
    assert report["calibration"]["status"] == "improved"
    assert report["calibration"]["measurement_class"] == "contract_compliance_proxy"
    assert report["claims"]["task_quality_uplift_claimed"] is False
    assert report["claims"]["quality_claim"]["eligible"] is False
    assert report["official_score_policy"]["model_weight_score_unchanged"] is True
    assert report["official_score_policy"]["official_benchmark_claimed"] is False
    assert all("text" not in case["baseline"] for case in report["cases"])
    assert all("text" not in case["maximized"] for case in report["cases"])


@pytest.mark.conformance
def test_phase11_no_key_adapter_reports_scaffold_only_without_uplift_claim():
    report = CapabilityEvaluationRunner(ModelProvider()).run()

    assert report["provider"]["name"] == "codex-mediated-local"
    assert report["provider"]["requires_api_key"] is False
    assert report["task_quality"]["available"] is False
    assert report["scaffold_contract"]["paired_delta"] > 0
    assert report["calibration"]["status"] == "scaffold_only"
    assert report["claims"]["task_quality_uplift_claimed"] is False
    assert report["official_score_policy"]["official_benchmark_claimed"] is False


@pytest.mark.conformance
def test_phase11_calibration_detects_regression_without_auto_adoption():
    report = CapabilityEvaluationRunner(RegressiveEvaluationAdapter()).run()

    assert report["contract_compliance_proxy"]["paired_delta"] < 0
    assert report["task_quality"]["paired_delta"] is None
    assert report["calibration"]["status"] == "regressed"
    assert report["claims"]["task_quality_uplift_claimed"] is False
    assert report["calibration"]["automatic_adoption"] is False


@pytest.mark.conformance
def test_phase11_rejects_marker_and_claim_stuffing_as_task_quality_evidence():
    report = CapabilityEvaluationRunner(ClaimStuffingAdapter()).run()

    assert report["contract_compliance_proxy"]["paired_delta"] > 0
    assert report["task_quality"]["available"] is False
    assert report["task_quality"]["reason"] == (
        "independent_blinded_paired_outcome_measurement_unavailable"
    )
    assert report["claims"]["task_quality_uplift_claimed"] is False
    assert report["claims"]["quality_claim"]["status"] == "blocked"
    assert all(case["baseline"]["task_score"] is None for case in report["cases"])
    assert all(case["maximized"]["task_score"] is None for case in report["cases"])


@pytest.mark.conformance
def test_phase11_persists_evaluation_and_exposes_cockpit_api(tmp_path):
    orchestrator = RuntimeOrchestrator(
        db_path=str(tmp_path / "phase11.db"),
        model_provider=StructuredEvaluationAdapter(),
    )
    cockpit = CockpitRuntime(orchestrator=orchestrator)

    result = cockpit.dispatch(
        {
            "prompt": "Measure the paired capability scaffold evaluation.",
            "evaluate_capability": True,
        }
    )

    evaluation = result["evaluation"]
    assert result["governance"]["authorized"] is True
    assert evaluation["calibration"]["status"] == "improved"
    assert result["evidence"]["capability_evaluation"]["id"] == evaluation["id"]

    persisted = asyncio.run(
        orchestrator.repository.load(f"runtime:evaluation:{evaluation['id']}")
    )
    assert persisted is not None
    assert persisted["id"] == evaluation["id"]
    assert orchestrator.repository.verify_provenance()["valid"] is True

    status_code, status = handle_cockpit_request("GET", "/api/status", runtime=cockpit)
    evaluation_code, evaluation_payload = handle_cockpit_request(
        "GET",
        "/api/evaluations",
        runtime=cockpit,
    )
    assert status_code == 200
    assert status["evaluation"]["id"] == evaluation["id"]
    assert evaluation_code == 200
    assert evaluation_payload["evaluation"]["id"] == evaluation["id"]
