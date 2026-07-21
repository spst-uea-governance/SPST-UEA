import asyncio
import subprocess

import pytest

from spst_runtime.engines.governance_engine import GovernanceEngine
from spst_runtime.engines.verification_runner import VerificationRunner
from spst_runtime.events.event import Event
from spst_runtime.models.subject_state import SubjectState
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator
from spst_runtime.providers.tool_provider import ToolProvider
from spst_runtime.web import CockpitRuntime, handle_cockpit_request


def _passing_profile(profile: str, authorized: bool) -> dict:
    assert authorized is True
    return {
        "profile": profile,
        "status": "completed",
        "returncode": 0,
        "duration_ms": 1,
        "output_digest": f"digest-{profile}",
        "stdout": "a" * 40 if profile == "git_head" else "unpersisted output",
        "stderr": "",
    }


def _failing_profile(profile: str, authorized: bool) -> dict:
    result = _passing_profile(profile, authorized)
    if profile == "ruff":
        return {
            **result,
            "status": "failed",
            "returncode": 1,
            "output_digest": "digest-ruff-failed",
        }
    return result


def _snapshot_failure_profile(profile: str, authorized: bool) -> dict:
    result = _passing_profile(profile, authorized)
    if profile == "git_head":
        return {
            **result,
            "status": "failed",
            "returncode": 1,
            "output_digest": "digest-git-head-failed",
            "stdout": "",
        }
    return result


@pytest.mark.conformance
def test_phase10_runs_only_governed_fixed_profiles_and_compacts_outputs(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="clean", stderr="")

    monkeypatch.setattr("spst_runtime.providers.tool_provider.subprocess.run", fake_run)
    provider = ToolProvider(workspace_root=str(tmp_path))
    profile_result = provider.execute_verification_profile(
        "pytest",
        governance_authorized=True,
    )
    assert profile_result["status"] == "completed"
    assert profile_result["output_digest"]
    assert captured["shell"] is False
    assert captured["cwd"] == tmp_path.resolve()
    assert captured["timeout"] == 300
    assert "pytest" in captured["command"]
    historical_result = provider.execute_verification_profile(
        "pytest",
        governance_authorized=True,
        profile_contract_version=2,
    )
    assert historical_result["status"] == "completed"
    assert captured["timeout"] == 120
    assert provider.execute_verification_profile(
        "arbitrary_command",
        governance_authorized=True,
    )["status"] == "denied"
    denied_decision = GovernanceEngine().decide(
        {
            "type": "local_verification",
            "source": "verification_runner",
            "payload": {
                "analysis_only": True,
                "read_only": True,
                "verification_profile": "arbitrary_command",
            },
        }
    )
    assert denied_decision["authorized"] is False
    assert "verification_profile_not_permitted" in denied_decision["reasons"]

    runner = VerificationRunner(
        provider,
        GovernanceEngine(),
        profile_executor=_passing_profile,
    )
    verification = runner.run()

    assert verification["id"].startswith("VRUN-")
    assert verification["status"] == "passed"
    assert verification["source_snapshot"]["revision"] == "a" * 40
    assert verification["verification"]["pytest"]["source"] == "verified_local"
    assert verification["profiles"]["ruff"]["output_digest"] == "digest-ruff"
    assert "stdout" not in verification["profiles"]["pytest"]
    assert "stderr" not in verification["profiles"]["pytest"]


@pytest.mark.conformance
def test_phase10_verified_gate_persists_runner_evidence_and_cockpit_status(tmp_path):
    provider = ToolProvider(workspace_root=str(tmp_path))
    runner = VerificationRunner(
        provider,
        GovernanceEngine(),
        profile_executor=_passing_profile,
    )
    orchestrator = RuntimeOrchestrator(
        db_path=str(tmp_path / "phase10.db"),
        verification_runner=runner,
    )
    cockpit = CockpitRuntime(orchestrator=orchestrator)

    result = cockpit.dispatch(
        {
            "prompt": "Run a reproducible local verification gate.",
            "verified_quality_gate": True,
        }
    )

    evidence = result["evidence"]
    verification = result["verification"]
    assert result["governance"]["authorized"] is True
    assert verification["status"] == "passed"
    assert evidence["quality_gate"]["status"] == "passed"
    assert evidence["quality_gate"]["verification_source"] == "verified_local"
    assert evidence["quality_gate"]["checks"]["git_diff_check"] == "passed"
    assert evidence["quality_gate"]["verification_run_id"] == verification["id"]

    persisted = asyncio.run(
        orchestrator.repository.load(f"runtime:evidence:{evidence['id']}")
    )
    assert persisted is not None
    assert persisted["quality_gate"]["verification_run_id"] == verification["id"]
    assert orchestrator.repository.verify_provenance()["valid"] is True

    status_code, status = handle_cockpit_request("GET", "/api/status", runtime=cockpit)
    verification_code, verification_payload = handle_cockpit_request(
        "GET",
        "/api/verification",
        runtime=cockpit,
    )
    assert status_code == 200
    assert status["verification"]["id"] == verification["id"]
    assert verification_code == 200
    assert verification_payload["verification"]["id"] == verification["id"]


@pytest.mark.conformance
def test_phase10_failed_verified_profile_holds_commit_for_human_review(tmp_path):
    runner = VerificationRunner(
        ToolProvider(workspace_root=str(tmp_path)),
        GovernanceEngine(),
        profile_executor=_failing_profile,
    )
    orchestrator = RuntimeOrchestrator(
        db_path=str(tmp_path / "phase10_failed.db"),
        verification_runner=runner,
    )

    pending = orchestrator.dispatch(
        SubjectState(),
        Event(
            type="user_action",
            payload={
                "prompt": "Apply a change only after verified quality checks.",
                "verified_quality_gate": True,
            },
        ),
    )

    assert pending.metadata["governance_pending"] is True
    assert pending.metadata["verification_run"]["status"] == "failed"
    assert pending.metadata["evidence"]["quality_gate"]["status"] == "failed"
    assert pending.metadata["evidence"]["quality_gate"]["checks"]["ruff"] == "failed"
    assert "quality_gate_failed" in pending.metadata["governance"]["reasons"]
    assert "action_event" not in pending.metadata


@pytest.mark.conformance
def test_phase10_requires_a_reproducible_source_snapshot(tmp_path):
    runner = VerificationRunner(
        ToolProvider(workspace_root=str(tmp_path)),
        GovernanceEngine(),
        profile_executor=_snapshot_failure_profile,
    )
    orchestrator = RuntimeOrchestrator(
        db_path=str(tmp_path / "phase10_snapshot_failure.db"),
        verification_runner=runner,
    )

    pending = orchestrator.dispatch(
        SubjectState(),
        Event(
            type="user_action",
            payload={
                "prompt": "Require a source snapshot before verified quality approval.",
                "verified_quality_gate": True,
            },
        ),
    )

    quality_gate = pending.metadata["evidence"]["quality_gate"]
    assert pending.metadata["verification_run"]["status"] == "failed"
    assert quality_gate["status"] == "failed"
    assert "verification_runner" in quality_gate["failed"]
