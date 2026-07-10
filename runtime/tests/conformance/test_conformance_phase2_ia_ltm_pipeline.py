import pytest

from spst_runtime.events.event import Event
from spst_runtime.models.subject_state import SubjectState
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator


@pytest.mark.conformance
def test_conformance_phase2_ia_ltm_pipeline(tmp_path):
    db_path = str(tmp_path / "phase2_ia_ltm.db")
    orchestrator = RuntimeOrchestrator(db_path=db_path)
    orchestrator.memory_provider.remember_long_term(
        "Phase 2 architecture needs deterministic intelligence amplification and long-term memory retrieval.",
        tags=["phase2", "architecture"],
        salience=0.95,
        metadata={"source": "conformance_seed"},
    )

    prompt = (
        "Implement Phase 2 architecture: connect intelligence amplification, "
        "task decomposition, reflection scoring, and long-term memory retrieval."
    )
    result = orchestrator.dispatch(
        SubjectState(metadata={"version": 0}),
        Event(type="user_action", payload={"prompt": prompt}),
    )

    amplification = result.metadata["intelligence_amplification"]
    phase2_reflection = result.metadata["phase2_reflection"]
    retrieved_context = result.metadata["retrieved_context"]
    stored = orchestrator.memory_provider.retrieve("runtime:1")
    long_term_results = orchestrator.memory_provider.search_long_term("Phase 2 architecture", top_k=5)

    assert amplification["intent"] in {"implementation", "design"}
    assert "implement_minimal_safe_change" in amplification["work_units"]
    assert amplification["amplification_score"] >= 0.8
    assert phase2_reflection["esi"] >= 0.8
    assert phase2_reflection["status"] == "accepted"
    assert any("Phase 2 architecture" in item.get("text", "") for item in retrieved_context)
    assert result.metadata["model_inference"]["requires_api_key"] is False
    assert result.metadata["governance"]["authorized"] is True
    assert result.metadata["version"] == 1
    assert stored["metadata"]["phase"] == "act"
    assert any(item["metadata"].get("phase") == "act" for item in long_term_results)
