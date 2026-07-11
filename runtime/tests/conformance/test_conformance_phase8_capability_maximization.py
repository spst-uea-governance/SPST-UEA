import pytest

from spst_runtime.events.event import Event
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator


@pytest.mark.conformance
def test_conformance_phase8_capability_maximization_pipeline(tmp_path):
    orchestrator = RuntimeOrchestrator(db_path=str(tmp_path / "phase8_capability.db"))
    state = orchestrator.create_subject("capability", role="VerifierSubject")

    result = orchestrator.dispatch(
        state,
        Event(
            type="benchmark_task",
            payload={
                "prompt": (
                    "Solve a hard coding benchmark task with hidden tests, "
                    "then verify correctness before final answer."
                ),
                "benchmark_suite": "coding",
                "official_baseline_score": 76.4,
            },
        ),
    )

    maximization = result.metadata["capability_maximization"]
    model_context = result.metadata["model_inference"]["context"]

    assert maximization["official_score_policy"]["model_weight_score_unchanged"] is True
    assert maximization["official_score_policy"]["reported_baseline_score"] == 76.4
    assert maximization["target_domains"] == ["coding"]
    assert maximization["verification_plan"]["minimum_passes"] >= 2
    assert maximization["realized_performance"]["expected_direction"] == "increase"
    assert model_context["capability_mode"] == "capability_maximization"
    assert model_context["strategy_count"] >= 4
    assert result.metadata["phase2_reflection"]["status"] == "accepted"
    assert result.metadata["governance"]["authorized"] is True
    assert result.metadata["last_trace"] == [
        "observe",
        "retrieve",
        "infer",
        "reflect",
        "govern",
        "commit",
        "act",
    ]
