import asyncio
import json

import pytest

from spst_runtime.events.event import Event
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.providers.memory_provider import MemoryProvider
from spst_runtime.repository.workspace_orchestrator import WorkspaceOrchestrator
from spst_runtime.web import CockpitRuntime, handle_cockpit_request


@pytest.mark.conformance
def test_conformance_phase7_sovereign_governance_os(tmp_path):
    workspace_a_memory = MemoryProvider(path=str(tmp_path / "workspace_a.db"))
    workspace_b_memory = MemoryProvider(path=str(tmp_path / "workspace_b.db"))
    federation = WorkspaceOrchestrator(policy_version="sovereign-policy-v1")
    federation.register_workspace("workspace-a", workspace_a_memory)
    federation.register_workspace("workspace-b", workspace_b_memory)
    federation.publish_rule_crystal(
        "workspace-a",
        "RuleCrystal: Always run deterministic verification before cross-workspace commit.",
    )

    orchestrator = RuntimeOrchestrator(
        db_path=str(tmp_path / "runtime_b.db"),
        memory_provider=workspace_b_memory,
        workspace_orchestrator=federation,
        workspace_id="workspace-b",
    )
    state = orchestrator.create_subject("executor", role="ExecutorSubject")
    result = orchestrator.dispatch(
        state,
        Event(type="user_action", payload={"prompt": "deterministic verification"}),
    )

    shared_context = result.metadata["workspace_context"]
    assert shared_context[0]["provenance"]["workspace_id"] == "workspace-a"
    assert result.metadata["state_provenance"]["valid"] is True

    repository = SQLiteRepository(str(tmp_path / "provenance.db"))
    asyncio.run(repository.save("state", {"status": "trusted"}))
    assert repository.verify_provenance()["valid"] is True
    with repository.connection() as connection:
        connection.execute("UPDATE state_store SET value = ? WHERE key = ?", ('{"status":"tampered"}', "state"))
    assert repository.verify_provenance()["valid"] is False

    security = orchestrator.create_security_subject("workspace-b")
    assert security.metadata["role"] == "SecuritySubject"
    with pytest.raises(Exception, match="Governance rejected"):
        orchestrator.dispatch(
            result,
            Event(
                type="user_action",
                payload={"prompt": "scan", "code": "OPENAI_API_KEY = 'sk-sensitive'"},
            ),
        )

    cockpit = CockpitRuntime(db_path=str(tmp_path / "cockpit.db"))
    pending = cockpit.dispatch(
        {
            "prompt": "remove deprecated public API",
            "breaking_change": True,
            "diff": {"removed": ["public_api.v1"]},
        }
    )
    approval_id = pending["governance"]["approval_id"]
    assert pending["governance"]["requires_human_approval"] is True
    assert cockpit.status()["pending_approvals"][0]["approval_id"] == approval_id
    status_code, status_payload = handle_cockpit_request("GET", "/api/status", runtime=cockpit)
    assert status_code == 200
    assert status_payload["pending_approvals"][0]["diff"] == {"removed": ["public_api.v1"]}
    approval_code, approved = handle_cockpit_request(
        "POST",
        "/api/approval",
        body=json.dumps({"approval_id": approval_id, "approved": True}).encode("utf-8"),
        runtime=cockpit,
    )
    assert approval_code == 200
    assert approved["governance"]["authorized"] is True
    assert approved["trace"] == ["observe", "retrieve", "infer", "reflect", "govern", "commit", "act"]

    mcp_result = orchestrator.dispatch(
        result,
        Event(
            type="tool_request",
            payload={
                "prompt": "check local interpreter",
                "mcp_request": {
                    "jsonrpc": "2.0",
                    "id": "tool-1",
                    "method": "tools/call",
                    "params": {
                        "name": "local.execute",
                        "arguments": {"command": ["python", "--version"]},
                    },
                },
            },
        ),
    )
    assert mcp_result.metadata["mcp_result"]["jsonrpc"] == "2.0"
    assert mcp_result.metadata["mcp_result"]["result"]["status"] == "completed"
