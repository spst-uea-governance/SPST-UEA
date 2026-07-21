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

    @abstractmethod
    def handle_mcp(self, request: dict[str, Any], *, governance_authorized: bool) -> dict[str, Any]:
        """Handle a local MCP JSON-RPC request through the governed tool boundary."""
        ...

    @abstractmethod
    def execute_verification_profile(
        self,
        profile: str,
        *,
        governance_authorized: bool,
        profile_contract_version: int | None = None,
    ) -> dict[str, Any]:
        """Run one fixed local verification profile after governance approval."""
        ...
