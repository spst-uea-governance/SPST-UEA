from spst_runtime.events.event import Event
from spst_runtime.orchestrator import runtime_orchestrator
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator


def test_non_autonomous_dispatch_defers_expensive_maintenance(tmp_path):
    orchestrator = RuntimeOrchestrator(db_path=str(tmp_path / "runtime.db"))
    state = orchestrator.create_subject("worker", role="executor")

    result = orchestrator.dispatch(state, Event(type="stress_event", payload={"prompt": "fast path"}))

    assert result.metadata["maintenance"] == {
        "status": "deferred",
        "reason": "non_autonomous_event",
    }


def test_default_maintenance_is_bound_to_the_orchestrator_database(tmp_path, monkeypatch):
    database_path = str(tmp_path / "runtime.db")
    called_with = []

    def isolated_maintenance(path):
        called_with.append(path)
        return {"memory_compaction": {}, "generated_cleanup": {}, "memory_stats": {}}

    monkeypatch.setattr(runtime_orchestrator, "run_maintenance", isolated_maintenance)
    orchestrator = runtime_orchestrator.RuntimeOrchestrator(db_path=database_path)

    orchestrator.maintenance_service()

    assert called_with == [database_path]
