from spst_runtime.models.subject_state import SubjectState

class StateManager:
    def __init__(self):
        self._version=0
        self._state=SubjectState()

    @property
    def state(self):
        return self._state

    def commit(self,state:SubjectState)->SubjectState:
        self._version+=1
        state.metadata["version"]=self._version
        self._state=state
        return state
