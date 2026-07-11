import hashlib
import json
from typing import Any

from spst_runtime.engines.covenant_policy import CovenantPolicyEngine
from spst_runtime.verification_profiles import VERIFICATION_PROFILE_NAMES


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

        if action.get("type") == "local_verification":
            return self._decide_local_verification(action, covenant)
        if action.get("type") == "capability_evaluation":
            return self._decide_capability_evaluation(action, covenant)
        if action.get("type") == "operational_corpus_registration":
            return self._decide_operational_corpus_registration(action, covenant)
        if action.get("type") == "operational_shadow_evaluation":
            return self._decide_operational_shadow_evaluation(action, covenant)
        if action.get("type") == "artifact_outcome_evidence":
            return self._decide_artifact_outcome_evidence(action, covenant)
        if action.get("type") == "longitudinal_evidence_synthesis":
            return self._decide_longitudinal_evidence_synthesis(action, covenant)
        if action.get("type") == "longitudinal_promotion_activation":
            return self._decide_longitudinal_promotion_activation(action, covenant)
        if action.get("type") == "longitudinal_promotion_rollback":
            return self._decide_longitudinal_promotion_rollback(action, covenant)

        payload = action.get("payload", {}) or {}
        risk = action.get("change_risk", {}) or {}
        action_type = action.get("type")
        source = action.get("source")
        provenance_verified = bool(action.get("provenance_verified"))
        provenance_scope = action.get("provenance_scope", "sqlite")
        quality_gate_reason = self._quality_gate_reason(action, payload, risk)
        calibration_gate_reason = self._calibration_gate_reason(payload)
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
        if (
            action_type in {"commit_identity_change", "state_transition"}
            and provenance_scope != "ephemeral"
            and not provenance_verified
        ):
            return self._decision(
                "Low",
                False,
                False,
                ["unverified_provenance"],
                action=action,
                covenant=covenant,
            )
        if (
            breaking
            or covenant["requires_human_approval"]
            or quality_gate_reason
            or calibration_gate_reason
        ):
            approved = bool(payload.get("human_approved"))
            reasons = ["breaking_or_architectural_change"] if breaking else []
            if quality_gate_reason:
                reasons.append(quality_gate_reason)
            if calibration_gate_reason:
                reasons.append(calibration_gate_reason)
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

    def _quality_gate_reason(
        self,
        action: dict[str, Any],
        payload: dict[str, Any],
        risk: dict[str, Any],
    ) -> str | None:
        evidence = action.get("evidence", {})
        evidence_data = evidence if isinstance(evidence, dict) else {}
        quality_gate = evidence_data.get("quality_gate", {})
        quality_data = quality_gate if isinstance(quality_gate, dict) else {}
        required = bool(
            quality_data.get("required")
            or payload.get("requires_quality_gate")
            or payload.get("breaking_change")
            or payload.get("architecture_change")
            or risk.get("requires_human_approval")
        )
        if not required:
            return None
        status = quality_data.get("status", "incomplete")
        if status != "passed":
            return "quality_gate_failed" if status == "failed" else "quality_gate_incomplete"
        if (
            payload.get("verified_quality_gate")
            and quality_data.get("verification_source") != "verified_local"
        ):
            return "quality_gate_unverified"
        return None

    def _calibration_gate_reason(self, payload: dict[str, Any]) -> str | None:
        calibration = payload.get("calibration_registry", {})
        calibration_data = calibration if isinstance(calibration, dict) else {}
        comparison = calibration_data.get("comparison", {})
        policy = calibration_data.get("policy", {})
        comparison_data = comparison if isinstance(comparison, dict) else {}
        policy_data = policy if isinstance(policy, dict) else {}
        if (
            comparison_data.get("status") == "regressed"
            and bool(policy_data.get("requires_human_approval"))
        ):
            return "calibration_regression_requires_human_approval"
        return None

    def _decide_local_verification(
        self,
        action: dict[str, Any],
        covenant: dict[str, Any],
    ) -> dict[str, Any]:
        payload = action.get("payload", {}) or {}
        profile = str(payload.get("verification_profile") or "")
        if action.get("source") != "verification_runner":
            return self._decision(
                "Low",
                False,
                False,
                ["verification_runner_source_required"],
                action=action,
                covenant=covenant,
            )
        if profile not in VERIFICATION_PROFILE_NAMES:
            return self._decision(
                "Low",
                False,
                False,
                ["verification_profile_not_permitted"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("analysis_only") or not payload.get("read_only"):
            return self._decision(
                "Low",
                False,
                False,
                ["verification_must_be_read_only"],
                action=action,
                covenant=covenant,
            )
        return self._decision(
            "High",
            True,
            False,
            ["verification_profile_authorized"],
            action=action,
            covenant=covenant,
        )

    def _decide_capability_evaluation(
        self,
        action: dict[str, Any],
        covenant: dict[str, Any],
    ) -> dict[str, Any]:
        payload = action.get("payload", {}) or {}
        if action.get("source") != "capability_evaluation_runner":
            return self._decision(
                "Low",
                False,
                False,
                ["capability_evaluation_runner_source_required"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("analysis_only") or not payload.get("read_only"):
            return self._decision(
                "Low",
                False,
                False,
                ["capability_evaluation_must_be_read_only"],
                action=action,
                covenant=covenant,
            )
        return self._decision(
            "High",
            True,
            False,
            ["capability_evaluation_authorized"],
            action=action,
            covenant=covenant,
        )

    def _decide_operational_corpus_registration(
        self,
        action: dict[str, Any],
        covenant: dict[str, Any],
    ) -> dict[str, Any]:
        payload = action.get("payload", {}) or {}
        if action.get("source") != "operational_evaluation_corpus":
            return self._decision(
                "Low",
                False,
                False,
                ["operational_corpus_source_required"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("local_only"):
            return self._decision(
                "Low",
                False,
                False,
                ["operational_corpus_must_be_local"],
                action=action,
                covenant=covenant,
            )
        if (
            not payload.get("consent_granted")
            or payload.get("consent_scope") != "local_operational_evaluation"
        ):
            return self._decision(
                "Low",
                False,
                False,
                ["operational_corpus_consent_required"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("contract_valid"):
            return self._decision(
                "Low",
                False,
                False,
                ["operational_corpus_contract_invalid"],
                action=action,
                covenant=covenant,
            )
        return self._decision(
            "High",
            True,
            False,
            ["operational_corpus_registration_authorized"],
            action=action,
            covenant=covenant,
        )

    def _decide_operational_shadow_evaluation(
        self,
        action: dict[str, Any],
        covenant: dict[str, Any],
    ) -> dict[str, Any]:
        payload = action.get("payload", {}) or {}
        if action.get("source") != "operational_shadow_runner":
            return self._decision(
                "Low",
                False,
                False,
                ["operational_shadow_source_required"],
                action=action,
                covenant=covenant,
            )
        if not (
            payload.get("analysis_only")
            and payload.get("read_only")
            and payload.get("shadow_only")
            and payload.get("consent_scoped")
            and payload.get("local_only")
        ):
            return self._decision(
                "Low",
                False,
                False,
                ["operational_shadow_must_be_l0_local"],
                action=action,
                covenant=covenant,
            )
        if int(payload.get("task_count", 0)) <= 0:
            return self._decision(
                "Low",
                False,
                False,
                ["operational_shadow_tasks_required"],
                action=action,
                covenant=covenant,
            )
        return self._decision(
            "High",
            True,
            False,
            ["operational_shadow_authorized"],
            action=action,
            covenant=covenant,
        )

    def _decide_artifact_outcome_evidence(
        self,
        action: dict[str, Any],
        covenant: dict[str, Any],
    ) -> dict[str, Any]:
        payload = action.get("payload", {}) or {}
        if action.get("source") != "artifact_outcome_ledger":
            return self._decision(
                "Low",
                False,
                False,
                ["artifact_outcome_source_required"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("local_only") or not payload.get("evidence_only"):
            return self._decision(
                "Low",
                False,
                False,
                ["artifact_outcome_must_be_local_evidence_only"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("task_contract_active"):
            return self._decision(
                "Low",
                False,
                False,
                ["artifact_outcome_active_task_required"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("expected_source_snapshot_present"):
            return self._decision(
                "Low",
                False,
                False,
                ["artifact_outcome_snapshot_required"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("human_calibration_consent"):
            return self._decision(
                "Low",
                False,
                False,
                ["artifact_outcome_human_consent_required"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("human_decision_valid"):
            return self._decision(
                "Low",
                False,
                False,
                ["artifact_outcome_human_decision_required"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("retention_within_task"):
            return self._decision(
                "Low",
                False,
                False,
                ["artifact_outcome_retention_mismatch"],
                action=action,
                covenant=covenant,
            )
        return self._decision(
            "High",
            True,
            False,
            ["artifact_outcome_evidence_authorized"],
            action=action,
            covenant=covenant,
        )

    def _decide_longitudinal_evidence_synthesis(
        self,
        action: dict[str, Any],
        covenant: dict[str, Any],
    ) -> dict[str, Any]:
        payload = action.get("payload", {}) or {}
        if action.get("source") != "longitudinal_promotion_governance":
            return self._decision(
                "Low",
                False,
                False,
                ["longitudinal_evidence_source_required"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("local_only") or not payload.get("evidence_only"):
            return self._decision(
                "Low",
                False,
                False,
                ["longitudinal_evidence_must_be_local_evidence_only"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("provenance_valid"):
            return self._decision(
                "Low",
                False,
                False,
                ["longitudinal_evidence_provenance_required"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("policy_contract_valid"):
            return self._decision(
                "Low",
                False,
                False,
                ["longitudinal_evidence_policy_contract_invalid"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("candidate_baseline_distinct"):
            return self._decision(
                "Low",
                False,
                False,
                ["longitudinal_evidence_candidate_baseline_required"],
                action=action,
                covenant=covenant,
            )
        return self._decision(
            "High",
            True,
            False,
            ["longitudinal_evidence_synthesis_authorized"],
            action=action,
            covenant=covenant,
        )

    def _decide_longitudinal_promotion_activation(
        self,
        action: dict[str, Any],
        covenant: dict[str, Any],
    ) -> dict[str, Any]:
        payload = action.get("payload", {}) or {}
        if action.get("source") != "longitudinal_promotion_governance":
            return self._decision(
                "Low",
                False,
                False,
                ["longitudinal_promotion_source_required"],
                action=action,
                covenant=covenant,
            )
        if not (
            payload.get("local_only")
            and payload.get("reversible")
            and payload.get("shadow_only")
        ):
            return self._decision(
                "Low",
                False,
                False,
                ["longitudinal_promotion_must_be_local_reversible_shadow"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("provenance_valid"):
            return self._decision(
                "Low",
                False,
                False,
                ["longitudinal_promotion_provenance_required"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("promotion_eligible") or payload.get("promotion_status") != (
            "held_for_human_review"
        ):
            return self._decision(
                "Low",
                False,
                False,
                ["longitudinal_promotion_eligible_evidence_required"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("human_decision_recorded"):
            return self._decision(
                "Low",
                False,
                True,
                ["longitudinal_promotion_human_approval_required"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("human_approved"):
            return self._decision(
                "High",
                True,
                False,
                ["longitudinal_promotion_human_rejection_recorded"],
                action=action,
                covenant=covenant,
            )
        return self._decision(
            "Medium",
            True,
            False,
            ["longitudinal_promotion_shadow_authorized"],
            action=action,
            covenant=covenant,
        )

    def _decide_longitudinal_promotion_rollback(
        self,
        action: dict[str, Any],
        covenant: dict[str, Any],
    ) -> dict[str, Any]:
        payload = action.get("payload", {}) or {}
        if action.get("source") != "longitudinal_promotion_governance":
            return self._decision(
                "Low",
                False,
                False,
                ["longitudinal_rollback_source_required"],
                action=action,
                covenant=covenant,
            )
        if not (
            payload.get("local_only")
            and payload.get("reversible")
            and payload.get("shadow_only")
        ):
            return self._decision(
                "Low",
                False,
                False,
                ["longitudinal_rollback_must_be_local_reversible_shadow"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("provenance_valid"):
            return self._decision(
                "Low",
                False,
                False,
                ["longitudinal_rollback_provenance_required"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("promotion_active"):
            return self._decision(
                "Low",
                False,
                False,
                ["longitudinal_rollback_active_promotion_required"],
                action=action,
                covenant=covenant,
            )
        if not payload.get("human_decision_recorded") or not payload.get("human_approved"):
            return self._decision(
                "Low",
                False,
                True,
                ["longitudinal_rollback_human_approval_required"],
                action=action,
                covenant=covenant,
            )
        return self._decision(
            "Medium",
            True,
            False,
            ["longitudinal_rollback_authorized"],
            action=action,
            covenant=covenant,
        )
