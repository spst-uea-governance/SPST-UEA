from abc import ABC, abstractmethod

from spst_runtime.events.event import Event

class Scheduler(ABC):
    """Select eligible events using deterministic operational constraints."""

    @abstractmethod
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
        """Queue an event and return its stable schedule identifier."""
        ...

    @abstractmethod
    def next_event(self) -> Event | None:
        """Return the next eligible event without relying on model output."""
        ...
