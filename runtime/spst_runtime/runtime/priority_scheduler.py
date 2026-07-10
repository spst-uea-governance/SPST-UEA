from dataclasses import dataclass
from typing import Any

from spst_runtime.events.event import Event
from spst_runtime.interfaces.scheduler import Scheduler


@dataclass
class ScheduledEvent:
    identifier: str
    event: Event
    priority: int
    urgency: int
    dependencies: frozenset[str]
    resource_units: int
    governance_allowed: bool
    sequence: int
    created_step: int
    timeout_steps: int | None
    wait_cycles: int = 0


class PriorityScheduler(Scheduler):
    """Deterministic queue honoring priority, dependencies, resources, and fairness."""

    def __init__(self, resource_limit: int = 1):
        if resource_limit < 1:
            raise ValueError("resource_limit must be positive")
        self.resource_limit = resource_limit
        self._pending: list[ScheduledEvent] = []
        self._completed: set[str] = set()
        self._expired: list[str] = []
        self._sequence = 0
        self._step = 0

    def schedule(
        self,
        event: Event,
        *,
        priority: int = 0,
        urgency: int = 0,
        dependencies: set[str] | None = None,
        resource_units: int = 1,
        governance_allowed: bool = True,
        timeout_steps: int | None = None,
    ) -> str:
        if resource_units < 1 or resource_units > self.resource_limit:
            raise ValueError("resource_units must be within the scheduler resource limit")
        if timeout_steps is not None and timeout_steps < 0:
            raise ValueError("timeout_steps cannot be negative")
        self._sequence += 1
        identifier = f"scheduled:{self._sequence:08d}"
        self._pending.append(
            ScheduledEvent(
                identifier=identifier,
                event=event,
                priority=priority,
                urgency=urgency,
                dependencies=frozenset(dependencies or set()),
                resource_units=resource_units,
                governance_allowed=governance_allowed,
                sequence=self._sequence,
                created_step=self._step,
                timeout_steps=timeout_steps,
            )
        )
        return identifier

    def next_event(self) -> Event | None:
        self._step += 1
        expired = [
            item
            for item in self._pending
            if item.timeout_steps is not None and self._step - item.created_step > item.timeout_steps
        ]
        for item in expired:
            self._pending.remove(item)
            self._expired.append(item.identifier)
        eligible = [
            item
            for item in self._pending
            if item.governance_allowed and item.dependencies <= self._completed
        ]
        if not eligible:
            return None
        selected = min(
            eligible,
            key=lambda item: (-(item.priority + item.urgency + item.wait_cycles), item.sequence),
        )
        self._pending.remove(selected)
        for item in eligible:
            if item is not selected:
                item.wait_cycles += 1
        return Event(
            type=selected.event.type,
            payload={**selected.event.payload, "_schedule_id": selected.identifier},
        )

    def mark_completed(self, identifier: str) -> None:
        self._completed.add(identifier)

    def status(self) -> dict[str, Any]:
        return {
            "pending": len(self._pending),
            "completed": sorted(self._completed),
            "expired": list(self._expired),
            "resource_limit": self.resource_limit,
            "step": self._step,
        }
