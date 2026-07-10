from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from spst_runtime.bus.event_bus import EventBus
from spst_runtime.core.lifecycle import RuntimePhase
from spst_runtime.events.event import Event


@dataclass
class RuntimeLifecycle:
    """Auditable RFC-0003 lifecycle state machine for one runtime instance."""

    bus: EventBus | None = None
    phase: RuntimePhase = RuntimePhase.UNINITIALIZED
    audit: list[dict[str, Any]] = field(default_factory=list)

    _allowed: dict[RuntimePhase, set[RuntimePhase]] = field(
        default_factory=lambda: {
            RuntimePhase.UNINITIALIZED: {RuntimePhase.INITIALIZING, RuntimePhase.ARCHIVED},
            RuntimePhase.INITIALIZING: {RuntimePhase.ACTIVE, RuntimePhase.DEGRADED, RuntimePhase.ARCHIVED},
            RuntimePhase.ACTIVE: {RuntimePhase.PAUSED, RuntimePhase.DEGRADED, RuntimePhase.ARCHIVED},
            RuntimePhase.PAUSED: {RuntimePhase.RECOVERING, RuntimePhase.ARCHIVED},
            RuntimePhase.DEGRADED: {RuntimePhase.RECOVERING, RuntimePhase.ARCHIVED},
            RuntimePhase.RECOVERING: {RuntimePhase.ACTIVE, RuntimePhase.DEGRADED, RuntimePhase.ARCHIVED},
            RuntimePhase.ARCHIVED: set(),
        }
    )

    def transition(self, target: RuntimePhase, *, reason: str, event_type: str = "lifecycle") -> None:
        if target not in self._allowed[self.phase]:
            raise ValueError(f"Invalid lifecycle transition: {self.phase.value} -> {target.value}")
        entry = {
            "from": self.phase.value,
            "to": target.value,
            "reason": reason,
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.phase = target
        self.audit.append(entry)
        if self.bus is not None:
            self.bus.publish(Event(type="runtime.lifecycle", payload=entry))

    def activate(self) -> None:
        if self.phase == RuntimePhase.UNINITIALIZED:
            self.transition(RuntimePhase.INITIALIZING, reason="start")
        if self.phase == RuntimePhase.INITIALIZING:
            self.transition(RuntimePhase.ACTIVE, reason="initialized")

    def recover(self, *, reason: str) -> None:
        if self.phase == RuntimePhase.ACTIVE:
            self.transition(RuntimePhase.DEGRADED, reason=reason)
        if self.phase in {RuntimePhase.PAUSED, RuntimePhase.DEGRADED}:
            self.transition(RuntimePhase.RECOVERING, reason=reason)
        if self.phase == RuntimePhase.RECOVERING:
            self.transition(RuntimePhase.ACTIVE, reason="recovery_complete")
