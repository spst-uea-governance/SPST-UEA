import asyncio
import json

import pytest

from spst_runtime.events.event import Event
from spst_runtime.models.subject_state import SubjectState
from spst_runtime.pipeline.state_transition import StateTransitionPipeline
from spst_runtime.web import CockpitRuntime, handle_cockpit_request


@pytest.mark.conformance
def test_phase9_persists_verified_evidence_and_exposes_it_in_cockpit(tmp_path):
    cockpit = CockpitRuntime(db_path=str(tmp_path / "phase9_evidence.db"))

    result = cockpit.dispatch(
        {
            "prompt": "Apply a verified local runtime governance improvement.",
            "requires_quality_gate": True,
            "verification": {
                "pytest": {"status": "passed", "command": "python -m pytest -q"},
                "ruff": {"status": "passed", "command": "ruff check ."},
                "mypy": {"status": "passed", "command": "python -m mypy spst_runtime"},
            },
        }
    )

    evidence = result["evidence"]
    assert result["governance"]["authorized"] is True
    assert evidence["id"].startswith("EVID-")
    assert evidence["state_version"] == result["version"]
    assert evidence["trace"] == [
        "observe",
        "retrieve",
        "infer",
        "reflect",
        "govern",
        "commit",
        "act",
    ]
    assert evidence["quality_gate"]["required"] is True
    assert evidence["quality_gate"]["status"] == "passed"
    assert evidence["checks"]["provenance"]["status"] == "passed"
    assert evidence["governance"]["authorized"] is True
    assert result["decision_explanation"]["status"] == "authorized"
    assert result["decision_explanation"]["evidence_id"] == evidence["id"]

    persisted = asyncio.run(
        cockpit.orchestrator.repository.load(f"runtime:evidence:{evidence['id']}")
    )
    assert persisted is not None
    assert persisted["id"] == evidence["id"]
    assert cockpit.orchestrator.repository.verify_provenance()["valid"] is True

    status_code, status = handle_cockpit_request("GET", "/api/status", runtime=cockpit)
    evidence_code, evidence_payload = handle_cockpit_request(
        "GET", "/api/evidence", runtime=cockpit
    )
    assert status_code == 200
    assert status["evidence"]["id"] == evidence["id"]
    assert evidence_code == 200
    assert evidence_payload["evidence"]["id"] == evidence["id"]


@pytest.mark.conformance
def test_phase9_holds_incomplete_quality_evidence_with_human_readable_reason(tmp_path):
    cockpit = CockpitRuntime(db_path=str(tmp_path / "phase9_pending.db"))

    pending = cockpit.dispatch(
        {
            "prompt": "Propose a change without complete validation evidence.",
            "requires_quality_gate": True,
            "verification": {"pytest": {"status": "passed"}},
        }
    )

    evidence = pending["evidence"]
    assert pending["governance"]["authorized"] is False
    assert pending["governance"]["requires_human_approval"] is True
    assert "quality_gate_incomplete" in pending["governance"]["reasons"]
    assert evidence["quality_gate"]["status"] == "incomplete"
    assert evidence["quality_gate"]["missing"] == ["ruff", "mypy"]
    assert pending["decision_explanation"]["status"] == "pending_human_approval"
    assert any(
        reason["code"] == "quality_gate_incomplete"
        for reason in pending["decision_explanation"]["reasons"]
    )

    approval_payload = json.dumps(
        {"approval_id": pending["governance"]["approval_id"], "approved": False}
    ).encode("utf-8")
    status_code, rejected = handle_cockpit_request(
        "POST", "/api/approval", body=approval_payload, runtime=cockpit
    )
    assert status_code == 200
    assert rejected["governance"]["status"] == "rejected_by_human"


@pytest.mark.conformance
def test_phase9_marks_ephemeral_pipeline_provenance_as_not_applicable():
    result = StateTransitionPipeline().run(
        SubjectState(),
        Event(type="user_action", payload={"prompt": "analyze local state"}),
    )

    assert result.metadata["governance"]["authorized"] is True
    assert result.metadata["evidence"]["checks"]["provenance"]["status"] == "not_applicable"
