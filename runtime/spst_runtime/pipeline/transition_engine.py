from copy import deepcopy
from typing import Any

from spst_runtime.engines.autopoiesis_engine import AutopoiesisEngine
from spst_runtime.engines.goal_engine import GoalEngine
from spst_runtime.intelligence_amplifier import IntelligenceAmplifier
from spst_runtime.managers.goal_manager import GoalManager
from spst_runtime.providers.tool_provider import ToolProvider


class TransitionEngine:
    """Execute the RFC-0003 state transition phases in deterministic order."""

    STEPS = ("observe", "retrieve", "infer", "reflect", "govern", "commit", "act")

    def __init__(
        self,
        intelligence_amplifier: IntelligenceAmplifier | None = None,
        goal_engine: GoalEngine | None = None,
        goal_manager: GoalManager | None = None,
        tool_provider: ToolProvider | None = None,
        autopoiesis_engine: AutopoiesisEngine | None = None,
    ):
        self.intelligence_amplifier = intelligence_amplifier or IntelligenceAmplifier()
        self.goal_engine = goal_engine or GoalEngine()
        self.goal_manager = goal_manager or GoalManager()
        self.tool_provider = tool_provider or ToolProvider()
        self.autopoiesis_engine = autopoiesis_engine or AutopoiesisEngine()

    def execute(self, state: Any, event: Any) -> Any:
        if not hasattr(state, "metadata"):
            return state

        candidate = deepcopy(state)
        trace: list[str] = []
        transition_log: list[dict[str, Any]] = []
        event_type = getattr(event, "type", None)
        for index, step in enumerate(self.STEPS, start=1):
            trace.append(step)
            transition_log.append(
                {"index": index, "step": step, "event_type": event_type}
            )
            handler = getattr(self, f"_{step}")
            handler(candidate, event)

        candidate.metadata["last_trace"] = trace
        candidate.metadata["transition_log"] = transition_log
        return candidate

    def _observe(self, state: Any, event: Any) -> None:
        state.metadata["last_event_type"] = getattr(event, "type", None)
        state.metadata["last_event_payload"] = deepcopy(getattr(event, "payload", {}) or {})

    def _retrieve(self, state: Any, event: Any) -> None:
        state.metadata.setdefault("retrieved_context", [])

    def _infer(self, state: Any, event: Any) -> None:
        state.metadata.setdefault("inference_ready", True)
        payload = getattr(event, "payload", {}) or {}
        prompt = payload.get("prompt") or payload.get("command") or ""
        session_state = {
            "turn_count": state.metadata.get("version", 0),
            "prompts": [prompt] if prompt else [],
            "goals": state.metadata.get("goals", []),
            "audit": state.metadata.get("audit", []),
            "evaluation": state.metadata.get("evaluation", {}),
            "memory": {"retrieved": state.metadata.get("retrieved_context", [])},
        }
        state.metadata["intelligence_amplification"] = self.intelligence_amplifier.amplify(
            prompt,
            session_state,
        )
        goals = self.goal_manager.sync(state.metadata.get("goals", []))
        goal_plan = self.goal_engine.plan(
            goals,
            state.metadata["intelligence_amplification"],
        )
        if goal_plan["goal_id"]:
            state.metadata["goal_plan"] = goal_plan
            state.metadata["next_actions"] = goal_plan["next_actions"]
            state.metadata["goals"] = self.goal_manager.mark_in_progress(
                goals,
                goal_plan["goal_id"],
            )

    def _reflect(self, state: Any, event: Any) -> None:
        state.metadata.setdefault("reflection_required", True)
        payload = getattr(event, "payload", {}) or {}
        amplification = state.metadata.get("intelligence_amplification", {})
        score = 0.25 if payload.get("force_low_esi") else float(amplification.get("amplification_score", 0.0))
        esi = min(1.0, max(0.0, score))
        accepted = esi >= 0.8
        state.metadata["phase2_reflection"] = {
            "esi": esi,
            "status": "accepted" if accepted else "corrected",
            "corrective_flags": [] if accepted else ["increase_task_structure"],
            "checks": amplification.get("reflection_checks", []),
        }
        if not accepted or payload.get("simulate_tool_error"):
            issue = "low_esi" if not accepted else "execution_error"
            diagnose = self.tool_provider.run("diagnose", {"issue": issue})
            state.metadata["self_repair"] = {
                "performed": False,
                "authorized": False,
                "issue": issue,
                "attempts": [diagnose],
            }

    def _govern(self, state: Any, event: Any) -> None:
        state.metadata.setdefault("governance_required", True)
        repair = state.metadata.get("self_repair")
        if repair:
            repair_action = {
                "type": "self_repair",
                "source": "tool_provider",
                "provenance_verified": True,
            }
            repair["authorized"] = True
            repair["governance_action"] = repair_action
            result = self.tool_provider.run("repair", {"issue": repair.get("issue")})
            repair["attempts"].append(result)
            repair["performed"] = result.get("status") == "repaired"
            if repair["performed"]:
                state.metadata["phase2_reflection"] = {
                    **state.metadata.get("phase2_reflection", {}),
                    **result.get("patch", {}).get("phase2_reflection", {}),
                }

    def _commit(self, state: Any, event: Any) -> None:
        state.metadata.setdefault("commit_pending", True)
        goal_plan = state.metadata.get("goal_plan", {})
        state.metadata["goals"] = self.goal_manager.mark_completed(
            state.metadata.get("goals", []),
            goal_plan.get("goal_id"),
        )

    def _act(self, state: Any, event: Any) -> None:
        state.metadata.setdefault("action_ready", True)
        if state.metadata.get("goal_plan"):
            state.metadata["autonomous_action"] = {
                "goal_id": state.metadata["goal_plan"].get("goal_id"),
                "executed_actions": state.metadata.get("next_actions", []),
            }
        state.metadata["autopoiesis"] = self.autopoiesis_engine.evaluate(state.metadata)
