from copy import deepcopy
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
        snapshot = deepcopy(state)
        snapshot.metadata["checkpoint_version"] = self._latest_version
        self._checkpoints[self._latest_version] = snapshot
        return Checkpoint(
            version=self._latest_version,
            timestamp=datetime.utcnow(),
            state_snapshot=deepcopy(snapshot.__dict__),
            reconstruction_metadata={
                "state_version": snapshot.metadata.get("version", 0),
                "memory_refs": list(snapshot.memory_refs),
                "governance": deepcopy(snapshot.governance),
            },
        )

    def restore_checkpoint(self, version: int) -> SubjectState:
        try:
            return deepcopy(self._checkpoints[version])
        except KeyError as exc:
            raise KeyError(f"Checkpoint version not found: {version}") from exc
