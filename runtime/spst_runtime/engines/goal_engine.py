from typing import Any

class GoalEngine:
    """Evaluate goal collections in a provider-neutral shape."""

    def evaluate(self, goals: Any) -> Any:
        if isinstance(goals, list):
            return sorted(goals, key=lambda goal: goal.get("priority", 1.0), reverse=True)
        return goals

    def plan(self, goals: list[dict[str, Any]], amplification: dict[str, Any] | None = None) -> dict[str, Any]:
        active_goals = [
            goal for goal in self.evaluate(goals)
            if goal.get("status", "pending") in {"pending", "in_progress"}
        ]
        if not active_goals:
            return {"goal_id": None, "next_actions": [], "status": "idle"}

        goal = active_goals[0]
        work_units = list((amplification or {}).get("work_units", []))
        if not work_units:
            work_units = ["inspect_goal", "execute_goal_step", "verify_completion"]

        next_actions = [
            f"{goal['id']}:{unit}"
            for unit in work_units
            if unit in {"capture_user_goal", "summarize_core_request", "implement_minimal_safe_change", "evaluate_governance", "prepare_user_facing_answer", "execute_goal_step", "verify_completion"}
        ]
        if not next_actions:
            next_actions = [f"{goal['id']}:execute_goal_step"]

        return {
            "goal_id": goal["id"],
            "description": goal.get("description", ""),
            "next_actions": next_actions,
            "status": "planned",
        }
