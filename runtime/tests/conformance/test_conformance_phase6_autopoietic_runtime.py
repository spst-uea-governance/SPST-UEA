import pytest

from spst_runtime.events.event import Event
from spst_runtime.models.subject_state import SubjectState
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator


@pytest.mark.conformance
def test_conformance_phase6_autopoietic_runtime(tmp_path):
    orchestrator = RuntimeOrchestrator(db_path=str(tmp_path / "phase6_autopoiesis.db"))
    state = SubjectState(
        metadata={
            "version": 0,
            "goals": [
                {
                    "id": "maintain_autopoietic_runtime",
                    "description": "Maintain self-model, memory, governance, and repair boundaries.",
                    "status": "pending",
                    "priority": 1.0,
                }
            ],
        }
    )

    result = orchestrator.dispatch(
        state,
        Event(
            type="system_tick",
            payload={
                "prompt": "Maintain complete life-like SPST-UEA runtime without claiming sentience.",
                "force_low_esi": True,
            },
        ),
    )

    autopoiesis = result.metadata["autopoiesis"]
    self_model = autopoiesis["self_model"]
    homeostasis = autopoiesis["homeostasis"]

    assert self_model["identity"] == "SPST-UEA local autonomous runtime"
    assert self_model["boundary"] == "not_biological_not_sentient"
    assert "preserve_no_key_boundary" in autopoiesis["drives"]
    assert homeostasis["viability"] >= 0.9
    assert homeostasis["status"] == "stable"
    assert result.metadata["self_repair"]["performed"] is True
    assert result.metadata["goals"][0]["status"] == "completed"
    assert result.metadata["autopoiesis_memory"]["metadata"]["phase"] == "autopoiesis"
