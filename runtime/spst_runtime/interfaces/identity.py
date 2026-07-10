from abc import ABC, abstractmethod
from typing import Any

class Identity(ABC):
    """Verify protected identity invariants without model-side mutation."""

    @abstractmethod
    def verify_identity_continuity(self, previous: dict[str, Any], current: dict[str, Any]) -> bool:
        """Return whether the current identity preserves the previous invariant."""
        ...
