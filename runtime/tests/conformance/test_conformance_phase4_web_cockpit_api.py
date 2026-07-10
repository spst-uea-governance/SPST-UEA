import json

import pytest

from spst_runtime.web import CockpitRuntime, handle_cockpit_request


@pytest.mark.conformance
def test_conformance_phase4_web_cockpit_api(tmp_path):
    cockpit = CockpitRuntime(db_path=str(tmp_path / "phase4_cockpit.db"))
    cockpit.orchestrator.memory_provider.remember_long_term(
        "Phase 4 cockpit exposes runtime observability and memory search.",
        tags=["phase4", "cockpit"],
        salience=0.95,
        metadata={"source": "conformance_seed"},
    )

    status_code, status = handle_cockpit_request("GET", "/api/status", runtime=cockpit)
    assert status_code == 200
    assert status["tick"] == 0
    assert status["running"] is False
    assert status["requires_api_key"] is False

    dispatch_body = json.dumps(
        {
            "prompt": "Implement Phase 4 cockpit goal",
            "goal": {
                "id": "phase4_goal",
                "description": "Expose cockpit status and observability.",
                "priority": 1.0,
            },
        }
    ).encode()
    status_code, dispatch = handle_cockpit_request(
        "POST",
        "/api/dispatch",
        body=dispatch_body,
        runtime=cockpit,
    )

    assert status_code == 200
    assert dispatch["trace"] == [
        "observe",
        "retrieve",
        "infer",
        "reflect",
        "govern",
        "commit",
        "act",
    ]
    assert dispatch["governance"]["authorized"] is True
    assert dispatch["goal"]["id"] == "phase4_goal"

    status_code, goals = handle_cockpit_request("GET", "/api/goals", runtime=cockpit)
    assert status_code == 200
    assert goals["goals"][0]["id"] == "phase4_goal"
    assert goals["goals"][0]["status"] == "completed"

    status_code, memory = handle_cockpit_request(
        "GET",
        "/api/memory/search?q=Phase%204%20cockpit",
        runtime=cockpit,
    )
    assert status_code == 200
    assert memory["query"] == "Phase 4 cockpit"
    assert any("Phase 4 cockpit" in item.get("text", "") for item in memory["results"])

    status_code, status_after = handle_cockpit_request("GET", "/api/status", runtime=cockpit)
    assert status_code == 200
    assert status_after["tick"] == 1
    assert status_after["last_trace"] == dispatch["trace"]
    assert status_after["esi"] >= 0.8
