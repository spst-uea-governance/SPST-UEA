from typing import Any

class GovernanceEngine:
    """Authorize runtime actions that can affect protected state."""

    def authorize(self, action: Any) -> bool:
        if not isinstance(action, dict):
            return False

        action_type = action.get("type")
        source = action.get("source")
        provenance_verified = bool(action.get("provenance_verified"))

        if action_type == "commit_identity_change":
            return provenance_verified and source != "model_output_direct"

        if action_type == "state_transition":
            return provenance_verified

        return True
