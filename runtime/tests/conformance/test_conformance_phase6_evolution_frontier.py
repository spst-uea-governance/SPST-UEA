import pytest

from spst_runtime.events.event import Event
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator


@pytest.mark.conformance
def test_conformance_phase6_evolution_frontier(tmp_path):
    orchestrator = RuntimeOrchestrator(db_path=str(tmp_path / "phase6_frontier.db"))
    orchestrator.create_subject(
        "frontier",
        role="EvolutionSubject",
        goals=[
            {
                "id": "phase6_frontier_goal",
                "description": "Solve an unknown task, crystallize the rule, and immunize against regressions.",
                "status": "pending",
                "priority": 1.0,
            }
        ],
    )

    result = orchestrator.dispatch_subject(
        "frontier",
        Event(
            type="user_action",
            payload={
                "prompt": (
                    "Unknown frontier task: synthesize a deterministic verifier tool, "
                    "solve it locally, distill the durable rule, and chaos-test immunity."
                ),
                "requires_dynamic_tool": True,
                "unknown_task": "frontier_verifier",
                "force_low_esi": True,
            },
        ),
    )

    genesis = result.metadata["dynamic_tool_genesis"]
    crystals = result.metadata["rule_crystals"]
    immunity = result.metadata["auto_immunity"]
    tools = orchestrator.pipeline.transition_engine.tool_provider.list_tools()
    memory_results = orchestrator.memory_provider.search("frontier deterministic verifier", top_k=10)

    assert genesis["status"] == "mounted"
    assert genesis["tool_name"] in tools
    assert genesis["validation"]["ok"] is True
    assert result.metadata["dynamic_tool_result"]["status"] == "solved"
    assert any(crystal["kind"] == "rule_crystal" for crystal in crystals)
    assert any("deterministic verifier" in crystal["text"] for crystal in crystals)
    assert any(item.get("kind") == "rule_crystal" for item in memory_results)
    assert immunity["status"] == "immune"
    assert immunity["chaos_event"]["type"] == "simulated_missing_context"
    assert immunity["governance_rule"]["action"] == "fallback_to_verified_dynamic_tool"
    assert result.metadata["phase2_reflection"]["status"] == "accepted"
    assert result.metadata["goals"][0]["status"] == "completed"
