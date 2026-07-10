from typing import Any, Protocol


class CockpitContract(Protocol):
    def status(self) -> dict[str, Any]: ...
    def goals(self) -> dict[str, Any]: ...
    def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def memory_search(self, query: str, top_k: int = 5) -> dict[str, Any]: ...


class LocalRuntimeClient:
    """Typed SDK facade for an in-process, API-key-free cockpit contract."""

    def __init__(self, cockpit: CockpitContract):
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
