from typing import Any


class DecisionExplainer:
    """Render compact, deterministic governance decisions for human review."""

    REASON_MESSAGES = {
        "breaking_or_architectural_change": "The proposed change is marked as breaking or architectural.",
        "concealed_or_unaudited_action": "The action lacks the required visible audit trail.",
        "destructive_or_permanent_action": "The action may be destructive or permanent.",
        "direct_model_identity_mutation": "Direct model output cannot mutate protected identity.",
        "external_or_protected_boundary": "The action crosses a protected authority boundary.",
        "human_dignity_violation": "The action conflicts with the human dignity safeguard.",
        "quality_gate_failed": "At least one required local verification check failed.",
        "quality_gate_incomplete": "Required local verification evidence is incomplete.",
        "quality_gate_unverified": "The quality gate requires locally verified evidence.",
        "calibration_regression_requires_human_approval": (
            "The candidate regressed against its comparable calibration baseline."
        ),
        "capability_evaluation_runner_source_required": "Capability evaluation must use the governed runner.",
        "capability_evaluation_must_be_read_only": "Capability evaluation is restricted to read-only analysis.",
        "operational_corpus_source_required": "Operational corpus registration must use the governed corpus service.",
        "operational_corpus_must_be_local": "Operational corpus tasks must remain inside the local workspace boundary.",
        "operational_corpus_consent_required": "Operational corpus registration requires explicit local evaluation consent.",
        "operational_corpus_contract_invalid": "Operational corpus registration requires a valid bounded task contract.",
        "operational_shadow_source_required": "Operational shadow evaluation must use the governed runner.",
        "operational_shadow_must_be_l0_local": "Operational shadow evaluation is limited to local L0 read-only execution.",
        "operational_shadow_tasks_required": "Operational shadow evaluation requires at least one eligible consented task.",
        "artifact_outcome_source_required": "Artifact outcome evidence must use the governed evidence ledger.",
        "artifact_outcome_must_be_local_evidence_only": "Artifact outcome evidence is limited to local evidence-only recording.",
        "artifact_outcome_active_task_required": "Artifact outcome evidence requires an active consented task contract.",
        "artifact_outcome_snapshot_required": "Artifact outcome evidence requires an expected source snapshot.",
        "artifact_outcome_human_consent_required": "Artifact outcome evidence requires explicit human calibration consent.",
        "artifact_outcome_human_decision_required": "Artifact outcome evidence requires an accepted or rejected human decision.",
        "artifact_outcome_retention_mismatch": "Human calibration retention must not outlive the task consent window.",
        "security_red_team_block": "The local security review blocked the action.",
        "unverified_provenance": "The state provenance chain is not verified.",
        "unsupported_or_overstated_claim": "The action makes an unsupported or overstated claim.",
    }

    def explain(self, decision: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
        """Return a stable explanation without reproducing raw model output."""
        decision_status = decision.get("status")
        if decision_status == "rejected_by_human":
            status = "rejected_by_human"
            summary = "A human reviewer rejected the proposed action."
        elif decision.get("authorized", False):
            status = "authorized"
            summary = "The evidence and governance policy allow commit and act to proceed."
        elif decision.get("requires_human_approval", False):
            status = "pending_human_approval"
            summary = "The action is held for explicit human review before commit."
        else:
            status = "blocked"
            summary = "The action is blocked by governance and cannot proceed."

        reason_codes = decision.get("reasons", [])
        reasons = reason_codes if isinstance(reason_codes, list) else []
        return {
            "status": status,
            "summary": summary,
            "evidence_id": evidence.get("id"),
            "quality_gate": evidence.get("quality_gate", {}),
            "reasons": [
                {
                    "code": str(code),
                    "message": self.REASON_MESSAGES.get(
                        str(code),
                        str(code).replace("_", " ").capitalize() + ".",
                    ),
                }
                for code in reasons
            ],
            "recommended_action": self._recommended_action(status, evidence),
        }

    def _recommended_action(self, status: str, evidence: dict[str, Any]) -> str:
        quality_gate = evidence.get("quality_gate", {})
        if status == "authorized":
            return "Record the resulting audit trace and retain the evidence ledger entry."
        if quality_gate.get("status") in {"incomplete", "failed"}:
            return "Provide the missing or corrected verification evidence, or explicitly reject the proposal."
        if status == "pending_human_approval":
            return "Review the evidence, diff, and policy reasons before approving or rejecting."
        return "Correct the blocking policy or security condition and dispatch a new action."
