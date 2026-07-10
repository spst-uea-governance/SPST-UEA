"""
Conformance Test for Codex Step 9: ACID/WAL Persistence & State Recovery.
Authority Order: 4. tests (Must satisfy RFC-0003 & normative specification).
"Persistence Is Reconstructed, Not Preserved."
"""
import pytest
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.models.subject_state import SubjectState

@pytest.mark.conformance
@pytest.mark.asyncio
async def test_conformance_step9_state_reconstruction(tmp_path):
    """
    Verify full state recovery:
    1. Serialize and commit SubjectState to SQLite in WAL mode.
    2. Simulate disconnect / process termination.
    3. Connect to database and reconstruct SubjectState exactly.
    """
    db_path = str(tmp_path / "test_conformance.db")
    repo = SQLiteRepository(path=db_path)
    
    # Create initial state with distinct metadata and goal tracking
    state = SubjectState()
    state.metadata["version"] = 42
    state.metadata["goals"] = ["achieve_conformance"]
    
    # Save state
    await repo.save("subject_main", state.metadata)
    
    # Verify WAL mode enabled
    conn = repo.connect()
    mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    conn.close()
    assert mode.upper() == "WAL", "SQLiteRepository MUST enable WAL journal mode per RFC-0003."
    
    # Simulate fresh load
    loaded_repo = SQLiteRepository(path=db_path)
    loaded_data = await loaded_repo.load("subject_main")
    
    assert loaded_data is not None
    assert loaded_data["version"] == 42
    assert loaded_data["goals"] == ["achieve_conformance"]
