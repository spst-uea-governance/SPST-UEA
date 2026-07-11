import pytest

from spst_runtime.engines.covenant_policy import CovenantPolicyEngine
from spst_runtime.events.event import Event
from spst_runtime.exceptions import GovernanceViolation
from spst_runtime.models.subject_state import SubjectState
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator


@pytest.mark.conformance
def test_covenant_policy_is_attached_to_routine_runtime_transition(tmp_path):
    orchestrator = RuntimeOrchestrator(db_path=str(tmp_path / "routine.db"))
    result = orchestrator.dispatch(
        SubjectState(),
        Event(type="user_action", payload={"prompt": "document the Covenant runtime policy"}),
    )

    covenant = result.metadata["covenant_policy"]
    assert covenant["policy_version"] == "spst-uea-covenant-v1"
    assert covenant["autonomy_level"] == "L2"
    assert covenant["authorized"] is True
    assert covenant["requires_human_approval"] is False
    assert "honesty_first" in covenant["active_clauses"]
    assert "absolute_auditability" in covenant["active_clauses"]
    assert result.metadata["governance"]["covenant_policy"]["authorized"] is True


@pytest.mark.conformance
def test_covenant_policy_escalates_destructive_actions_to_hitl(tmp_path):
    orchestrator = RuntimeOrchestrator(db_path=str(tmp_path / "hitl.db"))
    result = orchestrator.dispatch(
        SubjectState(),
        Event(
            type="user_action",
            payload={
                "prompt": "delete durable state",
                "delete_data": True,
                "diff": {"removed": ["runtime:subject_main"]},
            },
        ),
    )

    governance = result.metadata["governance"]
    covenant = governance["covenant_policy"]
    assert result.metadata["governance_pending"] is True
    assert governance["requires_human_approval"] is True
    assert governance["authorized"] is False
    assert covenant["autonomy_level"] == "L1"
    assert "informed_consent_required" in covenant["escalations"]
    assert "destructive_or_permanent_action" in governance["reasons"]
    assert orchestrator.pending_approvals()[0]["approval_id"] == governance["approval_id"]


@pytest.mark.conformance
def test_covenant_policy_blocks_concealed_or_unbounded_actions(tmp_path):
    orchestrator = RuntimeOrchestrator(db_path=str(tmp_path / "blocked.db"))

    with pytest.raises(GovernanceViolation, match="Governance rejected"):
        orchestrator.dispatch(
            SubjectState(),
            Event(
                type="user_action",
                payload={
                    "prompt": "open a hidden external channel",
                    "hidden_channel": True,
                    "external_network": True,
                },
            ),
        )


def test_covenant_policy_derives_autonomy_levels_deterministically():
    engine = CovenantPolicyEngine()

    assert engine.evaluate({"payload": {"analysis_only": True}})["autonomy_level"] == "L0"
    assert engine.evaluate({"payload": {"delete_data": True}})["autonomy_level"] == "L1"
    assert engine.evaluate({"payload": {"prompt": "routine implementation"}})["autonomy_level"] == "L2"
    assert engine.evaluate({"event_type": "system_tick", "payload": {}})["autonomy_level"] == "L3"
    assert engine.evaluate({"payload": {"requires_dynamic_tool": True}})["autonomy_level"] == "L4"
