from spst_runtime.models.subject_state import SubjectState
from spst_runtime.events.event import Event
from spst_runtime.pipeline.state_transition import StateTransitionPipeline

class RuntimeEngine:
    """Dispatch events through the RFC-0003 transition pipeline."""

    def __init__(self, pipeline: StateTransitionPipeline | None = None):
        self.state = SubjectState()
        self.pipeline = pipeline or StateTransitionPipeline()

    def dispatch(self, event: Event) -> SubjectState:
        self.state = self.pipeline.run(self.state, event)
        return self.state
