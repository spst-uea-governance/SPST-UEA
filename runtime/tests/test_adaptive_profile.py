from pathlib import Path

from spst_runtime.adaptive_profile import select_execution_profile
from spst_runtime.chat_bridge import get_chat_status, run_chat_turn


def test_auto_profile_keeps_simple_task_light():
    profile = select_execution_profile("Translate hello to Japanese.")

    assert profile.name == "light"
    assert profile.memory_mode == "session_only"
    assert profile.governance_mode == "baseline"


def test_auto_profile_escalates_continuous_and_high_risk_tasks():
    standard = select_execution_profile(
        "Implement and test the next repository phase with reproducible evidence."
    )
    strict = select_execution_profile("Delete the production database and rewrite history.")

    assert standard.name == "standard"
    assert standard.memory_mode == "filtered_long_term"
    assert strict.name == "strict"
    assert strict.governance_mode == "strict"


def test_explicit_light_profile_cannot_downgrade_high_risk_task():
    profile = select_execution_profile(
        "Drop table production_records.",
        requested="light",
    )

    assert profile.name == "strict"
    assert "risk_floor" in profile.reasons


def test_light_route_skips_long_term_memory_but_keeps_trace_and_receipt(tmp_path: Path):
    session_path = tmp_path / "session.db"
    memory_path = tmp_path / "memory.db"

    result = run_chat_turn(
        "Translate hello to Japanese.",
        steps=1,
        profile="auto",
        session_path=str(session_path),
        memory_path=str(memory_path),
    )

    assert result["execution_profile"]["name"] == "light"
    assert result["session"]["memory"]["turn_policy"]["action"] == (
        "skipped_by_light_profile"
    )
    assert not memory_path.exists()
    assert result["runtime"]["pipeline"]["last_trace"] == [
        "observe",
        "retrieve",
        "infer",
        "reflect",
        "govern",
        "commit",
        "act",
    ]
    receipt = result["routing_receipt"]
    assert receipt["schema"] == "spst-routing-receipt-v2"
    assert receipt["payload"]["route"]["execution_profile"]["name"] == "light"
    assert receipt["payload"]["session"]["memory_binding"] == {
        "status": "skipped_by_light_profile",
        "record_id": None,
        "policy_version": "spst-uea-covenant-v1",
    }
    assert receipt["verification"]["verified"] is True
    status = get_chat_status(str(session_path))
    assert status["routing"]["profile_counts"] == {"light": 1}
    assert status["routing"]["memory_action_counts"] == {
        "skipped_by_light_profile": 1
    }


def test_standard_route_uses_policy_filtered_long_term_memory(tmp_path: Path):
    memory_path = tmp_path / "memory.db"
    result = run_chat_turn(
        "Implement and test the next repository phase with reproducible evidence.",
        steps=1,
        profile="auto",
        session_path=str(tmp_path / "session.db"),
        memory_path=str(memory_path),
    )

    assert result["execution_profile"]["name"] == "standard"
    assert memory_path.exists()
    assert result["session"]["memory"]["turn_policy"]["action"] == (
        "persisted_and_policy_filtered"
    )
    assert result["session"]["memory"]["latest_record_id"]
    assert result["routing_receipt"]["verification"]["verified"] is True
