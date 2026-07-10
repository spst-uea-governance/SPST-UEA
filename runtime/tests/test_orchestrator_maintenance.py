from spst_runtime.events.event import Event
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator


def test_non_autonomous_dispatch_defers_expensive_maintenance(tmp_path):
    orchestrator = RuntimeOrchestrator(db_path=str(tmp_path / "runtime.db"))
    state = orchestrator.create_subject("worker", role="executor")

    result = orchestrator.dispatch(state, Event(type="stress_event", payload={"prompt": "fast path"}))

    assert result.metadata["maintenance"] == {
        "status": "deferred",
        "reason": "non_autonomous_event",
    }
