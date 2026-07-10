from typing import Any

class MemoryManager:
    """Distill short runtime history into a simple structured memory summary."""

    def distill_memory(self, history: list[str]) -> dict[str, Any]:
        return {
            "entries": list(history),
            "count": len(history),
            "latest": history[-1] if history else None,
        }
