import asyncio
import json

from spst_runtime.events.event import Event
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator
from spst_runtime.persistence.sqlite_repository import SQLiteRepository


def test_dispatch_audit_context_remains_bounded_after_sustained_run(tmp_path):
    orchestrator = RuntimeOrchestrator(db_path=str(tmp_path / "bounded_runtime.db"))
    state = orchestrator.create_subject("bounded", role="VerifierSubject")

    sizes = []
    for index in range(20):
        state = orchestrator.dispatch(
            state,
            Event(type="benchmark", payload={"prompt": f"bounded audit context turn {index}"}),
        )
        serialized = json.dumps(orchestrator._serialize_state(state), sort_keys=True)
        sizes.append(len(serialized))

    model_context = state.metadata["model_inference"]["context"]

    assert max(sizes) < 150_000
    assert sizes[-1] < sizes[0] * 8
    assert model_context["retrieved_context_count"] <= 5
    assert '"retrieved_context":' not in json.dumps(model_context, sort_keys=True)


def test_sqlite_provenance_uses_local_key_material_and_can_rotate_legacy_chain(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("SPST_PROVENANCE_KEY", raising=False)
    monkeypatch.delenv("SPST_PROVENANCE_KEY_FILE", raising=False)

    db_path = tmp_path / "provenance.db"
    repository = SQLiteRepository(str(db_path))
    asyncio.run(repository.save("state", {"status": "trusted"}))

    first_verification = repository.verify_provenance()
    assert first_verification["valid"] is True
    assert first_verification["key_source"] == "local_key_file"
    assert db_path.with_suffix(db_path.suffix + ".provenance_key").exists()

    legacy_repository = SQLiteRepository(str(tmp_path / "legacy.db"), use_legacy_default_key=True)
    asyncio.run(legacy_repository.save("state", {"status": "legacy"}))
    assert legacy_repository.verify_provenance()["key_source"] == "legacy_default"

    rotation = legacy_repository.rotate_provenance_key()
    assert rotation["valid"] is True
    assert rotation["key_source"] == "local_key_file"
