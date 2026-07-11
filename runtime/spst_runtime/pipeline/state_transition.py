from typing import Any
import asyncio

from spst_runtime.engines.decision_explainer import DecisionExplainer
from spst_runtime.engines.evidence_ledger import EvidenceLedger
from spst_runtime.engines.governance_engine import GovernanceEngine
from spst_runtime.engines.reflection_engine import ReflectionEngine
from spst_runtime.exceptions import GovernanceViolation, StateValidationError
from spst_runtime.interfaces.model_adapter import ModelAdapter
from spst_runtime.managers.state_manager import StateManager
from spst_runtime.pipeline.transition_engine import TransitionEngine


class StateTransitionPipeline:
    """Coordinate Observe -> Retrieve -> Infer -> Reflect -> Govern -> Commit -> Act."""

    def __init__(
        self,
        transition_engine: TransitionEngine | None = None,
        reflection_engine: ReflectionEngine | None = None,
        governance_engine: GovernanceEngine | None = None,
        state_manager: StateManager | None = None,
        model_adapter: ModelAdapter | None = None,
        evidence_ledger: EvidenceLedger | None = None,
        decision_explainer: DecisionExplainer | None = None,
    ):
        self.transition_engine = transition_engine or TransitionEngine()
        self.reflection_engine = reflection_engine or ReflectionEngine()
        self.governance_engine = governance_engine or GovernanceEngine()
        self.state_manager = state_manager or StateManager()
        self.model_adapter = model_adapter
        self.evidence_ledger = evidence_ledger or EvidenceLedger()
        self.decision_explainer = decision_explainer or DecisionExplainer()

    def run(self, state: Any, event: Any) -> Any:
        if not hasattr(state, "metadata"):
            return state

        candidate = self.transition_engine.execute(state, event)
        self._infer(candidate, event)
        review = self.reflection_engine.review(candidate)
        candidate.metadata["reflection"] = {
            "approved": bool(review.get("approved", False)),
        }
        if not review.get("approved", False):
            raise StateValidationError("Reflection rejected candidate state.")

        provenance = candidate.metadata.get("state_provenance", {})
        has_persistent_provenance = isinstance(provenance, dict) and "valid" in provenance
        provenance_verified = (
            bool(provenance.get("valid")) if has_persistent_provenance else False
        )
        action = {
            "type": "state_transition",
            "event_type": getattr(event, "type", None),
            "provenance_verified": provenance_verified,
            "provenance_scope": "sqlite" if has_persistent_provenance else "ephemeral",
            "payload": getattr(event, "payload", {}) or {},
            "change_risk": candidate.metadata.get("change_risk", {}),
            "security_assessment": candidate.metadata.get("security_assessment", {}),
            "trace": candidate.metadata.get("last_trace", []),
        }
        evidence = self.evidence_ledger.prepare(candidate, event, action)
        action["evidence"] = evidence
        decide = getattr(self.governance_engine, "decide", None)
        if callable(decide):
            decision = decide(action)
        else:
            decision = {"authorized": self.governance_engine.authorize(action)}
        decision["evidence_id"] = evidence["id"]
        self.evidence_ledger.attach_decision(evidence, decision)
        candidate.metadata["governance"] = {"action": action, **decision}
        candidate.metadata["evidence"] = evidence
        candidate.metadata["decision_explanation"] = self.decision_explainer.explain(
            decision,
            evidence,
        )
        if "covenant_policy" in decision:
            candidate.metadata["covenant_policy"] = decision["covenant_policy"]
        if decision.get("requires_human_approval") and not decision.get("authorized"):
            candidate.metadata["governance_pending"] = True
            return candidate
        if not decision.get("authorized", False):
            raise GovernanceViolation("Governance rejected state transition.")

        committed = self.state_manager.commit(candidate)
        self.evidence_ledger.assign_state_version(
            evidence,
            committed.metadata.get("version", 0),
        )
        committed.metadata["governance"]["evidence_id"] = evidence["id"]
        committed.metadata["decision_explanation"] = self.decision_explainer.explain(
            committed.metadata["governance"],
            evidence,
        )
        return committed

    def _infer(self, state: Any, event: Any) -> None:
        if self.model_adapter is None:
            return

        payload = getattr(event, "payload", {}) or {}
        prompt = payload.get("prompt")
        if not prompt:
            return

        result = asyncio.run(
            self.model_adapter.infer(
                prompt,
                {
                    "instructions": (
                        "You are running inside SPST-UEA. Return model output only; "
                        "do not directly mutate protected identity or governance state."
                    ),
                    "retrieved_context": state.metadata.get("retrieved_context", []),
                    "capability_maximization": state.metadata.get(
                        "capability_maximization",
                        {},
                    ),
                },
            )
        )
        state.metadata["model_inference"] = result
