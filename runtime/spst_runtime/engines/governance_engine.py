from typing import Any

class GovernanceEngine:
    """Authorize runtime actions that can affect protected state."""

    def __init__(self):
        self.immunity_rules: list[dict[str, Any]] = []

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

    def register_immunity_rule(self, rule: dict[str, Any]) -> dict[str, Any]:
        if self.authorize({**rule, "type": "state_transition"}):
            normalized = dict(rule)
            normalized["registered"] = True
            self.immunity_rules.append(normalized)
            return normalized
        return {**rule, "registered": False}
