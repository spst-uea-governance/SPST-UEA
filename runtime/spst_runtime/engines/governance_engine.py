import hashlib
import json
from typing import Any

from spst_runtime.engines.covenant_policy import CovenantPolicyEngine

class GovernanceEngine:
    """Authorize runtime actions that can affect protected state."""

    def __init__(self, covenant_policy: CovenantPolicyEngine | None = None):
        self.immunity_rules: list[dict[str, Any]] = []
        self.covenant_policy = covenant_policy or CovenantPolicyEngine()

    def authorize(self, action: Any) -> bool:
        return bool(self.decide(action).get("authorized", False))

    def decide(self, action: Any) -> dict[str, Any]:
        """Return an auditable trust-level and approval decision for an action."""
        if not isinstance(action, dict):
            return self._decision(
                "Low",
                False,
                False,
                ["invalid_action"],
                action={},
                covenant=None,
            )

        covenant = self.covenant_policy.evaluate(action)
        if not covenant["authorized"] and not covenant["requires_human_approval"]:
            return self._decision(
                "Low",
                False,
                False,
                covenant["reasons"],
                action=action,
                covenant=covenant,
            )

        security = action.get("security_assessment", {})
        if security.get("blocked"):
            return self._decision(
                "Low",
                False,
                False,
                ["security_red_team_block"],
                action=action,
                covenant=covenant,
            )

        payload = action.get("payload", {}) or {}
        risk = action.get("change_risk", {}) or {}
        action_type = action.get("type")
        source = action.get("source")
        provenance_verified = bool(action.get("provenance_verified"))
        breaking = bool(
            payload.get("breaking_change")
            or payload.get("architecture_change")
            or risk.get("requires_human_approval")
        )
        if action_type == "commit_identity_change" and source == "model_output_direct":
            return self._decision(
                "Low",
                False,
                False,
                ["direct_model_identity_mutation"],
                action=action,
                covenant=covenant,
            )
        if action_type in {"commit_identity_change", "state_transition"} and not provenance_verified:
            return self._decision(
                "Low",
                False,
                False,
                ["unverified_provenance"],
                action=action,
                covenant=covenant,
            )
        if breaking or covenant["requires_human_approval"]:
            approved = bool(payload.get("human_approved"))
            reasons = ["breaking_or_architectural_change"] if breaking else []
            reasons.extend(covenant["reasons"])
            return self._decision(
                "Low" if not approved else "Medium",
                approved,
                not approved,
                self._dedupe(reasons),
                action=action,
                covenant={**covenant, "authorized": approved, "requires_human_approval": not approved},
            )
        return self._decision("High", True, False, [], action=action, covenant=covenant)

    def register_immunity_rule(self, rule: dict[str, Any]) -> dict[str, Any]:
        if self.authorize({**rule, "type": "state_transition"}):
            normalized = dict(rule)
            normalized["registered"] = True
            self.immunity_rules.append(normalized)
            return normalized
        return {**rule, "registered": False}

    def _decision(
        self,
        trust_level: str,
        authorized: bool,
        requires_human_approval: bool,
        reasons: list[str],
        *,
        action: dict[str, Any],
        covenant: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload = action.get("payload", {}) or {}
        canonical = json.dumps(
            {"event_type": action.get("event_type"), "payload": payload},
            sort_keys=True,
            default=str,
        )
        approval_id = payload.get("approval_id") or hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        decision = {
            "authorized": authorized,
            "trust_level": trust_level,
            "requires_human_approval": requires_human_approval,
            "approval_id": approval_id,
            "reasons": reasons,
            "diff": payload.get("diff", {}),
        }
        if covenant is not None:
            decision["covenant_policy"] = covenant
        return decision

    def _dedupe(self, values: list[str]) -> list[str]:
        seen = set()
        result = []
        for value in values:
            if value not in seen:
                seen.add(value)
                result.append(value)
        return result
