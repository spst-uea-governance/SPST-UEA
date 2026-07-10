import pytest

from spst_runtime.core.lifecycle import RuntimePhase
from spst_runtime.core.runtime_engine import RuntimeEngine
from spst_runtime.events.event import Event
from spst_runtime.pipeline.state_transition import StateTransitionPipeline
from spst_runtime.runtime.priority_scheduler import PriorityScheduler
from spst_runtime.services.migration_service import MigrationIntegrityError, MigrationService


@pytest.mark.conformance
def test_conformance_phase7_lifecycle_scheduler_checkpoint_and_migration():
    scheduler = PriorityScheduler(resource_limit=2)
    scheduler.schedule(Event(type="blocked"), priority=10, dependencies={"missing"})
    scheduler.schedule(Event(type="ready"), priority=2, urgency=3, resource_units=1)

    runtime = RuntimeEngine(scheduler=scheduler)
    runtime.start()
    state = runtime.step()

    assert state is not None
    assert state.metadata["last_event_type"] == "ready"
    assert runtime.phase == RuntimePhase.ACTIVE

    checkpoint = runtime.checkpoint()
    runtime.state.metadata["transient"] = "discard"
    restored = runtime.restore(checkpoint.version)

    assert "transient" not in restored.metadata
    assert runtime.phase == RuntimePhase.ACTIVE

    exported = MigrationService().export_state(restored, runtime_version="0.1.0")
    imported, report = MigrationService().import_state(exported)

    assert imported.metadata == restored.metadata
    assert report["integrity_verified"] is True
    assert report["continuity_evaluation"]["esi"] == 1.0

    exported["state"]["metadata"]["tampered"] = True
    with pytest.raises(MigrationIntegrityError):
        MigrationService().import_state(exported)

    runtime.terminate(reason="conformance_complete")

    assert runtime.phase == RuntimePhase.ARCHIVED
    assert runtime.lifecycle.audit[-1]["to"] == RuntimePhase.ARCHIVED.value


@pytest.mark.conformance
def test_conformance_phase7_scheduler_expires_timeouts_and_ages_waiting_work():
    scheduler = PriorityScheduler(resource_limit=1)
    expired = scheduler.schedule(Event(type="expired"), priority=9, timeout_steps=0)
    scheduler.schedule(Event(type="waiting"), priority=1)

    selected = scheduler.next_event()

    assert selected is not None
    assert selected.type == "waiting"
    assert expired in scheduler.status()["expired"]


@pytest.mark.conformance
def test_conformance_phase7_termination_requires_governance_authorization():
    class DenyTerminationGovernance:
        def authorize(self, action):
            return False

    runtime = RuntimeEngine(
        pipeline=StateTransitionPipeline(governance_engine=DenyTerminationGovernance())
    )
    runtime.start()

    with pytest.raises(PermissionError):
        runtime.terminate(reason="unapproved")

    assert runtime.phase == RuntimePhase.ACTIVE


@pytest.mark.conformance
def test_conformance_phase7_sdk_and_benchmark_are_local_and_reproducible():
    from spst_runtime.benchmark import run_benchmark
    from spst_runtime.sdk import LocalRuntimeClient
    from spst_runtime.web import CockpitRuntime

    cockpit = CockpitRuntime()
    client = LocalRuntimeClient(cockpit)
    dispatch = client.dispatch(prompt="verify local sdk integration")
    report = run_benchmark(dispatches=3)

    assert dispatch["governance"]["authorized"] is True
    assert report["specification_version"] == "RFC-0003/RFC-0005"
    assert report["reproducibility"]["requires_api_key"] is False
    assert report["results"]["all_authorized"] is True
