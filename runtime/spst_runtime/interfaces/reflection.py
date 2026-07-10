from abc import ABC, abstractmethod
from typing import Any

class Reflection(ABC):
    """Review a candidate state before governance and commit."""

    @abstractmethod
    def review(self, candidate: Any) -> dict[str, Any]:
        """Return an auditable approval decision for a candidate state."""
        ...
