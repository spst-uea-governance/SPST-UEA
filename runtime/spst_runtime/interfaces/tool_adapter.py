from abc import ABC, abstractmethod
from typing import Any

class ToolAdapter(ABC):
    """Secure local tool boundary for deterministic self-repair."""

    @abstractmethod
    def run(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Run a local tool action and return a structured result."""
        ...

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Return local tool boundary health."""
        ...
