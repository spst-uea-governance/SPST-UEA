import asyncio
from dataclasses import asdict, is_dataclass
from typing import Any

from spst_runtime.bus.event_bus import EventBus
from spst_runtime.events.event import Event
from spst_runtime.engines.autopoiesis_engine import AutopoiesisEngine
from spst_runtime.engines.goal_engine import GoalEngine
from spst_runtime.maintenance import run_maintenance
from spst_runtime.managers.goal_manager import GoalManager
from spst_runtime.models.subject_state import SubjectState
from spst_runtime.pipeline.transition_engine import TransitionEngine
from spst_runtime.pipeline.state_transition import StateTransitionPipeline
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.providers.memory_provider import MemoryProvider
from spst_runtime.providers.model_provider import ModelProvider
from spst_runtime.repository.subject_repository import SubjectRepository

class RuntimeOrchestrator:
    """Fully wired SPST runtime lifecycle orchestrator."""

    def __init__(
        self,
        db_path: str = "spst_runtime_lifecycle.db",
        bus: EventBus | None = None,
        memory_provider: MemoryProvider | None = None,
        model_provider: ModelProvider | None = None,
        pipeline: StateTransitionPipeline | None = None,
        repository: SQLiteRepository | None = None,
        maintenance_service: Any | None = None,
        subject_repository: SubjectRepository | None = None,
    ):
        self.bus = bus or EventBus()
        self.repository = repository or SQLiteRepository(db_path)
        self.memory_provider = memory_provider or MemoryProvider(path=db_path)
        self.model_provider = model_provider or ModelProvider()
        self.subject_repository = subject_repository or SubjectRepository()
        self.goal_manager = GoalManager(repository=self.repository)
        self.goal_engine = GoalEngine()
        self.autopoiesis_engine = AutopoiesisEngine()
        self.maintenance_service = maintenance_service or run_maintenance
        transition_engine = TransitionEngine(
            goal_engine=self.goal_engine,
            goal_manager=self.goal_manager,
        )
        self.pipeline = pipeline or StateTransitionPipeline(
            transition_engine=transition_engine,
            model_adapter=self.model_provider,
        )

    def create_subject(
        self,
        subject_id: str,
        *,
        role: str,
        goals: list[dict[str, Any]] | None = None,
    ) -> SubjectState:
        return self.subject_repository.create(subject_id, role=role, goals=goals)

    def dispatch_subject(self, subject_id: str, event: Event) -> SubjectState:
        state = self.subject_repository.load(subject_id)
        payload = getattr(event, "payload", {}) or {}
        target_id = payload.get("to")
        if getattr(event, "type", None) == "inter_subject_message" and target_id:
            self.subject_repository.route_message(subject_id, target_id, payload)
            self.bus.publish(event)
            state = self.subject_repository.load(subject_id)

        result = self.dispatch(state, event)
        self.subject_repository.save(result, subject_id)
        return result

    def dispatch(self, state: SubjectState, event: Event) -> SubjectState:
        if getattr(event, "type", None) == "system_tick":
            state.metadata["autonomous"] = True
        self._retrieve(state, event)
        result = self.pipeline.run(state, event)
        self._act(result, event)
        self._commit(result)
        return result

    def _retrieve(self, state: SubjectState, event: Event) -> None:
        payload = getattr(event, "payload", {}) or {}
        contexts = []
        memory_key = payload.get("memory_key")
        if memory_key:
            record = self.memory_provider.retrieve(memory_key)
            if record:
                contexts.append(record)

        prompt = payload.get("prompt")
        if not prompt and getattr(event, "type", None) == "system_tick":
            prompt = " ".join(
                goal.get("description", "")
                for goal in state.metadata.get("goals", [])
                if goal.get("status", "pending") in {"pending", "in_progress"}
            )
        if prompt:
            for record in self.memory_provider.search(prompt, top_k=5):
                if record not in contexts:
                    contexts.append(record)

        state.metadata["retrieved_context"] = contexts

    def _act(self, state: SubjectState, event: Event) -> None:
        version = state.metadata.get("version", 0)
        state.metadata["autopoiesis"] = self.autopoiesis_engine.evaluate(state.metadata)
        action_event = Event(
            type="runtime.action",
            payload={
                "source_event": getattr(event, "type", None),
                "version": version,
                "authorized": state.metadata.get("governance", {}).get("authorized", False),
            },
        )
        state.metadata["action_event"] = {
            "type": action_event.type,
            "payload": action_event.payload,
        }
        self.bus.publish(action_event)

        payload = getattr(event, "payload", {}) or {}
        prompt = payload.get("prompt")
        if not prompt and getattr(event, "type", None) == "system_tick":
            prompt = " | ".join(state.metadata.get("next_actions", []))
        self.memory_provider.store(
            f"runtime:{version}",
            {
                "prompt": prompt,
                "trace": state.metadata.get("last_trace", []),
                "model_inference": state.metadata.get("model_inference", {}),
            },
            {
                "phase": "act",
                "event_type": getattr(event, "type", None),
                "version": version,
            },
        )
        if state.metadata.get("autopoiesis"):
            state.metadata["autopoiesis_memory"] = self.memory_provider.store(
                f"autopoiesis:{version}",
                {
                    "self_model": state.metadata["autopoiesis"].get("self_model", {}),
                    "homeostasis": state.metadata["autopoiesis"].get("homeostasis", {}),
                    "drives": state.metadata["autopoiesis"].get("drives", []),
                },
                {
                    "phase": "autopoiesis",
                    "event_type": getattr(event, "type", None),
                    "version": version,
                },
            )

    def _commit(self, state: SubjectState) -> None:
        version = state.metadata.get("version", 0)
        state.metadata["maintenance"] = self.maintenance_service()
        self.goal_manager.persist(state.metadata.get("goals", []))
        asyncio.run(self.repository.save("runtime:subject_main", self._serialize_state(state)))
        subject_id = state.metadata.get("subject_id")
        if subject_id:
            asyncio.run(
                self.repository.save(
                    f"runtime:subject:{subject_id}",
                    self._serialize_state(state),
                )
            )
        asyncio.run(
            self.repository.save(
                f"runtime:audit:{version}",
                {
                    "version": version,
                    "trace": state.metadata.get("last_trace", []),
                    "transition_log": state.metadata.get("transition_log", []),
                    "authorized": state.metadata.get("governance", {}).get("authorized", False),
                    "action_event": state.metadata.get("action_event", {}),
                },
            )
        )

    def _serialize_state(self, state: Any) -> dict[str, Any]:
        if is_dataclass(state):
            return asdict(state)
        return {"metadata": getattr(state, "metadata", {})}
