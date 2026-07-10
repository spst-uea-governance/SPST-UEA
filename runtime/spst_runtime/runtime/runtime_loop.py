from dataclasses import dataclass, field
from typing import Any

from spst_runtime.events.event import Event
from spst_runtime.models.subject_state import SubjectState

@dataclass
class RuntimeContext:
    running: bool = False
    tick: int = 0
    history: list[str] = field(default_factory=list)

class RuntimeLoop:
    def __init__(self, orchestrator: Any | None = None, state: Any | None = None):
        self.ctx = RuntimeContext()
        self.orchestrator = orchestrator
        self.state = state or SubjectState()

    def start(self):
        self.ctx.running = True

    def stop(self):
        self.ctx.running = False

    def step(self, state: Any | None = None, event: Any | None = None):
        if not self.ctx.running:
            return None
        self.ctx.tick += 1
        self.ctx.history.append(f"tick:{self.ctx.tick}")
        if self.orchestrator is None:
            return None

        active_state = state or self.state
        active_event = event or Event(
            type="system_tick",
            payload={"tick": self.ctx.tick, "autonomous": True},
        )
        self.state = self.orchestrator.dispatch(active_state, active_event)
        return self.state
