from typing import Any
import asyncio

from spst_runtime.persistence.sqlite_repository import SQLiteRepository

class GoalManager:
    """Track goals and provide a conservative drift check."""

    def __init__(self, repository: SQLiteRepository | None = None):
        self._goals: dict[str, dict[str, Any]] = {}
        self.repository = repository

    def register_goal(self, goal_id: str, description: str, priority: float = 1.0) -> None:
        self._goals[goal_id] = {
            "id": goal_id,
            "description": description,
            "priority": priority,
            "status": "pending",
        }

    def verify_goal_drift(self, current_state: dict[str, Any]) -> bool:
        current_goals = current_state.get("goals", [])
        current_ids = {
            goal.get("id")
            for goal in current_goals
            if isinstance(goal, dict) and goal.get("id") is not None
        }
        return set(self._goals).issubset(current_ids)

    def sync(self, goals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for goal in goals:
            if isinstance(goal, dict) and goal.get("id"):
                normalized = {**goal, "status": goal.get("status", "pending")}
                self._goals[normalized["id"]] = normalized
        return list(goals)

    def mark_in_progress(self, goals: list[dict[str, Any]], goal_id: str | None) -> list[dict[str, Any]]:
        return self._update_status(goals, goal_id, "in_progress", only_from={"pending"})

    def mark_completed(self, goals: list[dict[str, Any]], goal_id: str | None) -> list[dict[str, Any]]:
        return self._update_status(goals, goal_id, "completed", only_from={"pending", "in_progress"})

    def persist(self, goals: list[dict[str, Any]]) -> None:
        if self.repository is None:
            return
        for goal in goals:
            goal_id = goal.get("id")
            if goal_id:
                asyncio.run(self.repository.save(f"runtime:goal:{goal_id}", goal))

    def _update_status(
        self,
        goals: list[dict[str, Any]],
        goal_id: str | None,
        status: str,
        *,
        only_from: set[str],
    ) -> list[dict[str, Any]]:
        updated = []
        for goal in goals:
            if goal.get("id") == goal_id and goal.get("status", "pending") in only_from:
                goal = {**goal, "status": status}
            updated.append(goal)
            if goal.get("id"):
                self._goals[goal["id"]] = goal
        return updated
