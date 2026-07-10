from spst_runtime.models.subject_state import SubjectState

class SubjectRepository:
    def __init__(self):
        self._state=SubjectState()
        self._subjects: dict[str, SubjectState] = {"default": self._state}

    def create(
        self,
        subject_id: str,
        *,
        role: str,
        goals: list[dict] | None = None,
        workspace_id: str | None = None,
    ) -> SubjectState:
        state = SubjectState(metadata={
            "subject_id": subject_id,
            "role": role,
            "workspace_id": workspace_id,
            "version": 0,
            "goals": goals or [],
            "swarm": {"messages_sent": 0, "messages_received": 0},
        })
        self._subjects[subject_id] = state
        return state

    def load(self, subject_id: str = "default")->SubjectState:
        return self._subjects.get(subject_id, self._state)

    def list_subjects(self) -> list[SubjectState]:
        return [self._subjects[key] for key in sorted(self._subjects)]

    def save(self,state:SubjectState, subject_id: str = "default")->None:
        self._subjects[subject_id]=state
        if subject_id == "default":
            self._state=state

    def route_message(self, source_id: str, target_id: str, message: dict) -> None:
        source = self.load(source_id)
        target = self.load(target_id)
        source.metadata.setdefault("swarm", {}).setdefault("messages_sent", 0)
        target.metadata.setdefault("swarm", {}).setdefault("messages_received", 0)
        source.metadata["swarm"]["messages_sent"] += 1
        target.metadata["swarm"]["messages_received"] += 1
        target.metadata.setdefault("swarm", {}).setdefault("inbox", []).append({
            "from": source_id,
            "message": message,
        })
        self.save(source, source_id)
        self.save(target, target_id)

    def load_legacy(self)->SubjectState:
        return self._state
