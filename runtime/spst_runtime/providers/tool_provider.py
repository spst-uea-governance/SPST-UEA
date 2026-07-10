from dataclasses import dataclass
from typing import Any

from spst_runtime.interfaces.tool_adapter import ToolAdapter


@dataclass
class ToolProvider(ToolAdapter):
    """Deterministic local sandbox for diagnosis and self-repair planning."""

    sandbox_name: str = "local-self-repair"

    def run(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        if tool == "diagnose":
            return {
                "tool": tool,
                "status": "diagnosed",
                "issue": payload.get("issue", "unknown"),
                "sandbox": self.sandbox_name,
            }
        if tool == "repair":
            return {
                "tool": tool,
                "status": "repaired",
                "patch": {
                    "phase2_reflection": {"esi": 0.95, "status": "accepted"},
                    "self_repair_applied": True,
                },
                "sandbox": self.sandbox_name,
            }
        return {
            "tool": tool,
            "status": "noop",
            "sandbox": self.sandbox_name,
        }

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": self.sandbox_name,
            "external_network": False,
        }
