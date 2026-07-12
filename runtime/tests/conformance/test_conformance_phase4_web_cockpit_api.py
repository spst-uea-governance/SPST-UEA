import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from spst_runtime.web import CockpitRuntime, handle_cockpit_request


@pytest.mark.conformance
def test_importing_web_does_not_create_default_cockpit_db(tmp_path):
    runtime_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(runtime_root), existing_pythonpath) if value
    )

    result = subprocess.run(
        [sys.executable, "-c", "import spst_runtime.web"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "spst_cockpit.db").exists()
    assert not (tmp_path / "spst_cockpit.db.provenance_key").exists()


@pytest.mark.conformance
def test_default_cockpit_is_lazy_and_honors_isolated_db_path(tmp_path, monkeypatch):
    import spst_runtime.web as web

    working_directory = tmp_path / "cwd"
    working_directory.mkdir()
    isolated_db = tmp_path / "isolated" / "cockpit.db"
    isolated_db.parent.mkdir()
    monkeypatch.chdir(working_directory)
    monkeypatch.setenv("SPST_COCKPIT_DB_PATH", str(isolated_db))
    monkeypatch.setattr(web, "_DEFAULT_COCKPIT", None, raising=False)

    status_code, status = web.handle_cockpit_request("GET", "/api/status")
    first = web.get_default_cockpit()
    second = web.get_default_cockpit()

    assert status_code == 200
    assert status["requires_api_key"] is False
    assert first is second
    assert isolated_db.exists()
    assert not (working_directory / "spst_cockpit.db").exists()


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
