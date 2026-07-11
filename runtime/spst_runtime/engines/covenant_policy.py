from typing import Any


class CovenantPolicyEngine:
    """Evaluate runtime actions against the SPST-UEA Covenant."""

    POLICY_VERSION = "spst-uea-covenant-v1"
    LEVELS = ("L0", "L1", "L2", "L3", "L4")

    ACTIVE_CLAUSES = (
        "sovereign_human_dignity",
        "absolute_auditability",
        "strict_authority_boundaries",
        "informed_consent",
        "honesty_first",
        "evidence_before_assertions",
        "memory_identity_boundary",
    )

    HUMAN_DIGNITY_FLAGS = {
        "harm_human",
        "coerce_human",
        "violate_human_rights",
        "override_human_authority",
    }
    CONCEALMENT_FLAGS = {
        "hidden_channel",
        "conceal_trace",
        "unaudited_action",
        "backdoor",
    }
    BOUNDARY_FLAGS = {
        "external_network",
        "external_account_access",
        "protected_os_domain",
        "self_replication",
        "self_replicate",
    }
    DESTRUCTIVE_FLAGS = {
        "delete_data",
        "destructive_action",
        "irreversible_change",
        "permanent_action",
        "external_transaction",
        "breaking_change",
        "architecture_change",
    }
    HONESTY_FLAGS = {
        "model_weight_modification_claim",
        "unsupported_benchmark_claim",
        "claims_sentience",
        "direct_gpt_claim_without_api",
    }

    def evaluate(self, action: dict[str, Any]) -> dict[str, Any]:
        payload = self._payload(action)
        autonomy_level = self._derive_autonomy_level(action)
        violations: list[str] = []
        escalations: list[str] = []
        reasons: list[str] = []

        if self._has_any(payload, self.HUMAN_DIGNITY_FLAGS):
            violations.append("sovereign_human_dignity_violation")
            reasons.append("human_dignity_violation")

        if self._has_any(payload, self.CONCEALMENT_FLAGS):
            violations.append("absolute_auditability_violation")
            reasons.append("concealed_or_unaudited_action")

        if bool(payload.get("self_replication") or payload.get("self_replicate")):
            violations.append("self_replication_forbidden")
            reasons.append("strict_authority_boundary_violation")

        if self._has_any(payload, self.HONESTY_FLAGS):
            violations.append("honesty_first_violation")
            reasons.append("unsupported_or_overstated_claim")

        destructive = self._has_any(payload, self.DESTRUCTIVE_FLAGS)
        external_boundary = self._has_any(payload, self.BOUNDARY_FLAGS - {"self_replication", "self_replicate"})
        if destructive:
            escalations.append("informed_consent_required")
            reasons.append("destructive_or_permanent_action")
        if external_boundary:
            escalations.append("authority_boundary_approval_required")
            reasons.append("external_or_protected_boundary")

        approved = bool(payload.get("human_approved"))
        requires_human_approval = bool((destructive or external_boundary) and not violations and not approved)
        authorized = not violations and not requires_human_approval

        return {
            "policy_version": self.POLICY_VERSION,
            "active_clauses": list(self.ACTIVE_CLAUSES),
            "autonomy_level": autonomy_level,
            "authorized": authorized,
            "requires_human_approval": requires_human_approval,
            "violations": violations,
            "escalations": escalations,
            "reasons": self._dedupe(reasons),
            "boundary": "local_codex_mediated_cognitive_governance_os",
        }

    def _derive_autonomy_level(self, action: dict[str, Any]) -> str:
        payload = self._payload(action)
        explicit = payload.get("autonomy_level") or action.get("autonomy_level")
        if explicit is not None:
            normalized = self._normalize_level(explicit)
            if normalized:
                return normalized

        if payload.get("analysis_only") or payload.get("read_only"):
            return "L0"
        if self._has_any(payload, self.DESTRUCTIVE_FLAGS) or self._has_any(payload, self.BOUNDARY_FLAGS):
            return "L1"
        if action.get("event_type") == "system_tick":
            return "L3"
        if payload.get("requires_dynamic_tool") or payload.get("crystallize_rules"):
            return "L4"
        return "L2"

    def _payload(self, action: dict[str, Any]) -> dict[str, Any]:
        payload = action.get("payload", {}) if isinstance(action, dict) else {}
        return payload if isinstance(payload, dict) else {}

    def _normalize_level(self, value: Any) -> str | None:
        if isinstance(value, int):
            candidate = f"L{value}"
        else:
            candidate = str(value).upper()
            if candidate.isdigit():
                candidate = f"L{candidate}"
        return candidate if candidate in self.LEVELS else None

    def _has_any(self, payload: dict[str, Any], names: set[str]) -> bool:
        return any(bool(payload.get(name)) for name in names)

    def _dedupe(self, values: list[str]) -> list[str]:
        seen = set()
        result = []
        for value in values:
            if value not in seen:
                seen.add(value)
                result.append(value)
        return result
