from abc import ABC, abstractmethod
from typing import Any

class ModelAdapter(ABC):
    """
    SPST Model Adapter Interface per RFC-0002 & docs/codex-handoff.md.
    
    Codex MUST NOT:
    - couple runtime logic to one model provider
    - commit identity changes directly from model output
    - invent new core theory
    """
    @abstractmethod
    async def infer(self, prompt: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Execute model inference behind an adapter boundary.
        Returns structured dictionary containing inference results and metadata.
        """
        ...

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Return vendor-neutral adapter health without performing inference."""
        ...

    @abstractmethod
    def get_capabilities(self) -> dict[str, Any]:
        """Describe adapter capabilities without exposing provider-specific coupling."""
        ...
