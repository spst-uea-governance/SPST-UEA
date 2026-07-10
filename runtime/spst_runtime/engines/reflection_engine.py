from typing import Any

class ReflectionEngine:
    """Review candidate state objects before governance and commit."""

    def review(self, candidate: Any) -> dict[str, Any]:
        has_metadata = hasattr(candidate, "metadata")
        return {"approved": has_metadata, "candidate": candidate}
