from dataclasses import dataclass
from typing import Any

@dataclass
class RuntimeHealth:
    continuity: float
    governance: float
    memory: float

class EvaluationService:
    """Derive bounded operational health from committed runtime metadata."""

    def evaluate(self, metadata: dict[str, Any] | None = None) -> RuntimeHealth:
        metadata = metadata or {}
        governance = float(metadata.get("governance", {}).get("authorized", True))
        trace = metadata.get("last_trace", [])
        continuity = 1.0 if not trace or len(trace) == 7 else 0.5
        memory = 1.0 if metadata.get("retrieved_context", []) or not metadata else 0.75
        return RuntimeHealth(continuity=continuity, governance=governance, memory=memory)
