from typing import Any
from datetime import datetime
from spst_runtime.models.subject_state import SubjectState
from spst_runtime.state.checkpoint import Checkpoint

class CheckpointManager:
    """Manage in-process state checkpoints for recovery-oriented tests and demos."""

    def __init__(self):
        self._checkpoints: dict[int, SubjectState] = {}
        self._latest_version = 0

    def save_checkpoint(self, state: SubjectState) -> Checkpoint:
        self._latest_version += 1
        state.metadata["checkpoint_version"] = self._latest_version
        self._checkpoints[self._latest_version] = state
        return Checkpoint(version=self._latest_version, timestamp=datetime.utcnow())

    def restore_checkpoint(self, version: int) -> SubjectState:
        try:
            return self._checkpoints[version]
        except KeyError as exc:
            raise KeyError(f"Checkpoint version not found: {version}") from exc
