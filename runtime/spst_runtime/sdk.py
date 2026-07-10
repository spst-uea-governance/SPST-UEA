from typing import Any


class LocalRuntimeClient:
    """In-process, API-key-free client for the sovereign web cockpit contract."""

    def __init__(self, cockpit: Any):
        self._cockpit = cockpit

    def status(self) -> dict[str, Any]:
        return self._cockpit.status()

    def goals(self) -> dict[str, Any]:
        return self._cockpit.goals()

    def dispatch(self, *, prompt: str, goal: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"prompt": prompt}
        if goal is not None:
            payload["goal"] = goal
        return self._cockpit.dispatch(payload)

    def search_memory(self, query: str, *, top_k: int = 5) -> dict[str, Any]:
        return self._cockpit.memory_search(query, top_k=top_k)
