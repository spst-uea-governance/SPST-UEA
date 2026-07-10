from spst_runtime.bus.event_bus import EventBus
from spst_runtime.core.lifecycle import RuntimePhase
from spst_runtime.core.lifecycle_manager import RuntimeLifecycle
from spst_runtime.events.event import Event
from spst_runtime.managers.checkpoint_manager import CheckpointManager
from spst_runtime.models.subject_state import SubjectState
from spst_runtime.pipeline.state_transition import StateTransitionPipeline
from spst_runtime.runtime.priority_scheduler import PriorityScheduler
from spst_runtime.state.checkpoint import Checkpoint


class RuntimeEngine:
    """Lifecycle-aware dispatcher for the RFC-0003 transition pipeline."""

    def __init__(
        self,
        pipeline: StateTransitionPipeline | None = None,
        scheduler: PriorityScheduler | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        bus: EventBus | None = None,
    ):
        self.state = SubjectState()
        self.pipeline = pipeline or StateTransitionPipeline()
        self.bus = bus or EventBus()
        self.lifecycle = RuntimeLifecycle(bus=self.bus)
        self.scheduler = scheduler or PriorityScheduler()
        self.checkpoint_manager = checkpoint_manager or CheckpointManager()

    @property
    def phase(self) -> RuntimePhase:
        return self.lifecycle.phase

    def start(self) -> None:
        self.lifecycle.activate()

    def stop(self) -> None:
        if self.phase == RuntimePhase.ACTIVE:
            self.lifecycle.transition(RuntimePhase.PAUSED, reason="stop")

    def dispatch(self, event: Event) -> SubjectState:
        if self.phase == RuntimePhase.UNINITIALIZED:
            self.start()
        if self.phase != RuntimePhase.ACTIVE:
            raise RuntimeError(f"Runtime cannot dispatch while {self.phase.value}")
        self.state = self.pipeline.run(self.state, event)
        self.bus.publish(Event(type="runtime.transition", payload={
            "event_type": event.type,
            "version": self.state.metadata.get("version", 0),
        }))
        return self.state

    def handle(self, event: Event) -> SubjectState:
        return self.dispatch(event)

    def step(self) -> SubjectState | None:
        event = self.scheduler.next_event()
        if event is None:
            return None
        state = self.dispatch(event)
        schedule_id = event.payload.get("_schedule_id")
        if schedule_id:
            self.scheduler.mark_completed(str(schedule_id))
        return state

    def checkpoint(self) -> Checkpoint:
        checkpoint = self.checkpoint_manager.save_checkpoint(self.state)
        self.bus.publish(Event(type="runtime.checkpoint", payload={"version": checkpoint.version}))
        return checkpoint

    def restore(self, version: int) -> SubjectState:
        self.state = self.checkpoint_manager.restore_checkpoint(version)
        self.lifecycle.recover(reason=f"restore_checkpoint:{version}")
        self.bus.publish(Event(type="runtime.recovered", payload={"version": version}))
        return self.state

    def terminate(self, *, reason: str) -> None:
        if self.phase == RuntimePhase.ARCHIVED:
            return
        action = {
            "type": "state_transition",
            "event_type": "runtime.termination",
            "provenance_verified": True,
            "reason": reason,
        }
        governance = getattr(self.pipeline, "governance_engine", None)
        if governance is not None and not governance.authorize(action):
            raise PermissionError("Governance rejected runtime termination")
        self.lifecycle.transition(RuntimePhase.ARCHIVED, reason=reason, event_type="termination")
        self.bus.publish(Event(type="runtime.terminated", payload={"reason": reason, "action": action}))
