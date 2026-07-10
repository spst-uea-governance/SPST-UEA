from abc import ABC, abstractmethod
from typing import Any

class Memory(ABC):
    """Vendor-neutral memory adapter interface for SPST state transitions."""

    @abstractmethod
    def store(
        self,
        key: str,
        value: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a memory record and return the stored envelope."""
        ...

    @abstractmethod
    def retrieve(self, key: str) -> dict[str, Any] | None:
        """Retrieve a memory record by stable key."""
        ...

    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Return deterministic search results ordered by score and key."""
        ...

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Return storage health without mutating memory."""
        ...
