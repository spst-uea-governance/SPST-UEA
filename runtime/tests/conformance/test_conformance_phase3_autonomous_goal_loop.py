import asyncio

import pytest

from spst_runtime.models.subject_state import SubjectState
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.runtime.runtime_loop import RuntimeLoop


@pytest.mark.conformance
def test_conformance_phase3_autonomous_goal_loop(tmp_path):
    db_path = str(tmp_path / "phase3_goal_loop.db")
    orchestrator = RuntimeOrchestrator(db_path=db_path)
    state = SubjectState(
        metadata={
            "version": 0,
            "goals": [
                {
                    "id": "achieve_conformance_phase3",
                    "description": "Autonomously complete Phase 3 conformance.",
                    "status": "pending",
                    "priority": 1.0,
                }
            ],
        }
    )
    runtime_loop = RuntimeLoop(orchestrator=orchestrator, state=state)

    runtime_loop.start()
    first = runtime_loop.step()
    second = runtime_loop.step()
    runtime_loop.stop()

    final_state = second or first
    goals = final_state.metadata["goals"]
    goal = goals[0]
    persisted_goal = asyncio.run(
        SQLiteRepository(path=db_path).load("runtime:goal:achieve_conformance_phase3")
    )
    maintenance = final_state.metadata["maintenance"]

    assert runtime_loop.ctx.tick == 2
    assert runtime_loop.ctx.history == ["tick:1", "tick:2"]
    assert final_state.metadata["last_event_type"] == "system_tick"
    assert final_state.metadata["autonomous"] is True
    assert final_state.metadata["goal_plan"]["goal_id"] == "achieve_conformance_phase3"
    assert final_state.metadata["next_actions"]
    assert goal["status"] == "completed"
    assert persisted_goal["status"] == "completed"
    assert maintenance["memory_compaction"]["after"] >= 0
    assert orchestrator.bus.published[-1].type == "runtime.action"
    assert final_state.metadata["governance"]["authorized"] is True
