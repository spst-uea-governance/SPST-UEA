import asyncio

import pytest

from spst_runtime.events.event import Event
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator
from spst_runtime.persistence.sqlite_repository import SQLiteRepository


@pytest.mark.conformance
def test_conformance_phase5_swarm_self_repair(tmp_path):
    db_path = str(tmp_path / "phase5_swarm.db")
    orchestrator = RuntimeOrchestrator(db_path=db_path)
    planner = orchestrator.create_subject(
        "planner",
        role="PlannerSubject",
        goals=[
            {
                "id": "phase5_swarm_goal",
                "description": "Plan and delegate Phase 5 swarm task.",
                "status": "pending",
                "priority": 1.0,
            }
        ],
    )
    executor = orchestrator.create_subject(
        "executor",
        role="ExecutorSubject",
        goals=[
            {
                "id": "phase5_swarm_goal",
                "description": "Execute delegated Phase 5 swarm task.",
                "status": "pending",
                "priority": 0.9,
            }
        ],
    )

    orchestrator.dispatch_subject(
        "planner",
        Event(
            type="inter_subject_message",
            payload={
                "to": "executor",
                "task": "execute with forced low ESI repair",
                "prompt": "Delegate Phase 5 swarm task to executor.",
            },
        ),
    )
    result = orchestrator.dispatch_subject(
        "executor",
        Event(
            type="user_action",
            payload={
                "prompt": "Execute delegated task with forced repair.",
                "force_low_esi": True,
                "simulate_tool_error": True,
            },
        ),
    )

    planner_after = orchestrator.subject_repository.load("planner")
    executor_after = orchestrator.subject_repository.load("executor")
    repair = result.metadata["self_repair"]
    persisted_executor = asyncio.run(
        SQLiteRepository(path=db_path).load("runtime:subject:executor")
    )

    assert planner.metadata["subject_id"] == "planner"
    assert executor.metadata["subject_id"] == "executor"
    assert planner_after.metadata["swarm"]["messages_sent"] == 1
    assert executor_after.metadata["swarm"]["messages_received"] == 1
    assert result.metadata["phase2_reflection"]["status"] == "accepted"
    assert repair["performed"] is True
    assert repair["authorized"] is True
    assert repair["attempts"][0]["tool"] == "diagnose"
    assert repair["attempts"][-1]["status"] == "repaired"
    assert executor_after.metadata["goals"][0]["status"] == "completed"
    assert persisted_executor["metadata"]["self_repair"]["performed"] is True
    assert orchestrator.bus.published[-1].type == "runtime.action"
