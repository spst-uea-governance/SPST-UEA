from abc import ABC, abstractmethod
from typing import Any

class Governance(ABC):
    """Authorize or reject an action before protected state changes."""

    @abstractmethod
    def authorize(self, action: dict[str, Any]) -> bool:
        """Return whether an action satisfies active governance policy."""
        ...
