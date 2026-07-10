from typing import Any
import asyncio

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
    ):
        self.transition_engine = transition_engine or TransitionEngine()
        self.reflection_engine = reflection_engine or ReflectionEngine()
        self.governance_engine = governance_engine or GovernanceEngine()
        self.state_manager = state_manager or StateManager()
        self.model_adapter = model_adapter

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

        action = {
            "type": "state_transition",
            "event_type": getattr(event, "type", None),
            "provenance_verified": True,
            "payload": getattr(event, "payload", {}) or {},
            "change_risk": candidate.metadata.get("change_risk", {}),
            "security_assessment": candidate.metadata.get("security_assessment", {}),
        }
        decide = getattr(self.governance_engine, "decide", None)
        if callable(decide):
            decision = decide(action)
        else:
            decision = {"authorized": self.governance_engine.authorize(action)}
        candidate.metadata["governance"] = {"action": action, **decision}
        if decision.get("requires_human_approval") and not decision.get("authorized"):
            candidate.metadata["governance_pending"] = True
            return candidate
        if not decision.get("authorized", False):
            raise GovernanceViolation("Governance rejected state transition.")

        return self.state_manager.commit(candidate)

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
                },
            )
        )
        state.metadata["model_inference"] = result
