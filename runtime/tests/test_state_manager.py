from spst_runtime.managers.state_manager import StateManager
from spst_runtime.models.subject_state import SubjectState

def test_commit_increments_version():
    sm=StateManager()
    s=SubjectState()
    out=sm.commit(s)
    assert out.metadata["version"]==1
