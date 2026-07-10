import asyncio

import pytest

from spst_runtime.events.event import Event
from spst_runtime.models.subject_state import SubjectState
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator
from spst_runtime.persistence.sqlite_repository import SQLiteRepository


@pytest.mark.conformance
def test_conformance_step10_runtime_lifecycle_certification(tmp_path):
    db_path = str(tmp_path / "runtime_lifecycle.db")
    orchestrator = RuntimeOrchestrator(db_path=db_path)
    state = SubjectState(metadata={"version": 0})
    event = Event(
        type="user_action",
        payload={
            "prompt": "certify lifecycle memory integration",
            "memory_key": "seed",
        },
    )

    orchestrator.memory_provider.store(
        "seed",
        {"text": "certify lifecycle memory integration context"},
        {"source": "conformance"},
    )

    result = orchestrator.dispatch(state, event)

    assert result.metadata["last_trace"] == [
        "observe",
        "retrieve",
        "infer",
        "reflect",
        "govern",
        "commit",
        "act",
    ]
    assert [entry["step"] for entry in result.metadata["transition_log"]] == result.metadata["last_trace"]
    assert result.metadata["retrieved_context"][0]["key"] == "seed"
    assert result.metadata["model_inference"]["provider"] == "codex-mediated-local"
    assert result.metadata["reflection"]["approved"] is True
    assert result.metadata["governance"]["authorized"] is True
    assert result.metadata["version"] == 1
    assert result.metadata["action_event"]["type"] == "runtime.action"
    assert orchestrator.bus.published[-1].type == "runtime.action"

    repo = SQLiteRepository(path=db_path)
    persisted_state = asyncio.run(repo.load("runtime:subject_main"))
    audit = asyncio.run(repo.load("runtime:audit:1"))
    stored_memory = orchestrator.memory_provider.retrieve("runtime:1")

    assert persisted_state is not None
    assert persisted_state["metadata"]["version"] == 1
    assert persisted_state["metadata"]["last_trace"] == result.metadata["last_trace"]
    assert audit["authorized"] is True
    assert audit["trace"] == result.metadata["last_trace"]
    assert stored_memory["value"]["prompt"] == "certify lifecycle memory integration"
    assert stored_memory["metadata"]["phase"] == "act"
