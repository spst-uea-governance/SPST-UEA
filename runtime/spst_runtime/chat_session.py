import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any

from spst_runtime.always_on import CONFIG
from spst_runtime.engines.governance_engine import GovernanceEngine
from spst_runtime.evaluation.metrics import EvaluationResult
from spst_runtime.intelligence_amplifier import IntelligenceAmplifier
from spst_runtime.memory.long_term_memory import (
    CURRENT_MEMORY_POLICY_VERSION,
    LongTermMemoryStore,
)
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.routing_receipt import RoutingReceiptLedger, build_profiled_routing_receipt


SESSION_KEY = "codex_chat_session"


class ChatSessionStore:
    """Persist Codex-mediated SPST-UEA chat state across bridge invocations."""

    def __init__(self, path: str | None = None, *, read_only: bool = False):
        default_path = Path(__file__).resolve().parents[1] / "spst_chat_state.db"
        self.path = path or str(default_path)
        self.read_only = read_only
        self.repository = SQLiteRepository(self.path, read_only=read_only)

    @staticmethod
    def default_state() -> dict[str, Any]:
        return {
            "mode": CONFIG.mode,
            "provider": CONFIG.provider,
            "turn_count": 0,
            "prompts": [],
            "goals": ["maintain_api_key_free_codex_mediated_operation"],
            "audit": [],
            "memory": {
                "stats": {"total_records": 0, "by_kind": {}, "latest_turn": 0},
                "retrieved": [],
                "latest_record_id": None,
            },
        }

    def load(self) -> dict[str, Any]:
        if self.read_only and not Path(self.path).expanduser().is_file():
            return self.default_state()
        state = asyncio.run(self.repository.load(SESSION_KEY))
        return state or self.default_state()

    def save(self, state: dict[str, Any]) -> None:
        asyncio.run(self.repository.save(SESSION_KEY, state))


def evaluate_session(state: dict[str, Any]) -> dict[str, Any]:
    recent_audit = state.get("audit", [])[-10:]
    authorized_ratio = 1.0
    if recent_audit:
        authorized_ratio = sum(1 for item in recent_audit if item.get("authorized")) / len(
            recent_audit
        )

    result = EvaluationResult(
        identity_continuity=1.0 if state.get("mode") == CONFIG.mode else 0.0,
        goal_persistence=1.0 if state.get("goals") else 0.0,
        semantic_stability=authorized_ratio if state.get("provider") == CONFIG.provider else 0.0,
    )
    data = asdict(result)
    data["esi"] = result.esi
    return data


def summarize_session(state: dict[str, Any]) -> dict[str, Any]:
    prompts = state.get("prompts", [])
    audit = state.get("audit", [])
    evaluation = state.get("evaluation", evaluate_session(state))
    return {
        "status": "stable" if evaluation.get("esi", 0.0) >= 0.95 else "watch",
        "turn_count": state.get("turn_count", 0),
        "recent_prompt_count": len(prompts[-5:]),
        "audit_count": len(audit),
        "memory_count": state.get("memory", {}).get("stats", {}).get("total_records", 0),
        "last_prompt": prompts[-1] if prompts else None,
        "execution_profile": state.get("execution_profile", {}).get("name"),
        "next_actions": [
            "preserve_no_key_codex_mediated_boundary",
            "continue_recording_governance_audit",
            "use_intelligence_amplification_scaffold",
            "escalate_only_on_explicit_user_request",
        ],
    }


def record_turn(
    prompt: str,
    event: str,
    pipeline: dict[str, Any],
    *,
    steps: int = 3,
    session_path: str | None = None,
    memory_path: str | None = None,
    execution_profile: dict[str, Any] | None = None,
    repository_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store = ChatSessionStore(session_path)
    state = store.load()
    governance = GovernanceEngine()
    action = {
        "type": "state_transition",
        "event_type": event,
        "provenance_verified": True,
    }
    authorized = governance.authorize(action)

    state["turn_count"] = int(state.get("turn_count", 0)) + 1
    profile = execution_profile or {
        "name": "standard",
        "memory_mode": "filtered_long_term",
        "governance_mode": "standard",
        "memory_ttl_seconds": 30 * 24 * 60 * 60,
        "minimum_memory_confidence": 0.5,
        "reasons": ["compatibility_default"],
    }
    if profile.get("memory_mode") == "session_only":
        previous_memory = state.get("memory", {})
        state["memory"] = {
            "stats": previous_memory.get(
                "stats", {"total_records": 0, "by_kind": {}, "latest_turn": 0}
            ),
            "retrieved": [],
            "latest_record_id": None,
            "consolidation": previous_memory.get("consolidation", {}),
            "turn_policy": {
                "action": "skipped_by_light_profile",
                "policy_version": CURRENT_MEMORY_POLICY_VERSION,
            },
        }
        memory_binding = {
            "status": "skipped_by_light_profile",
            "record_id": None,
            "policy_version": CURRENT_MEMORY_POLICY_VERSION,
        }
    else:
        memory_store = LongTermMemoryStore(memory_path)
        memory_record = memory_store.remember(
            prompt,
            kind="episodic",
            tags=["chat_turn", event, CONFIG.mode, str(profile.get("name"))],
            salience=0.9 if profile.get("name") == "strict" else 0.6,
            created_turn=state["turn_count"],
            confidence=0.8 if profile.get("name") == "strict" else 0.65,
            policy_version=CURRENT_MEMORY_POLICY_VERSION,
            ttl_seconds=profile.get("memory_ttl_seconds"),
            metadata={
                "event": event,
                "trace": pipeline.get("last_trace", []),
                "execution_profile": profile.get("name"),
            },
        )
        retrieved = memory_store.search(
            prompt,
            limit=5,
            min_confidence=float(profile.get("minimum_memory_confidence", 0.5)),
            policy_version=CURRENT_MEMORY_POLICY_VERSION,
        )
        state["memory"] = {
            "stats": memory_store.stats(),
            "retrieved": retrieved,
            "latest_record_id": memory_record.id,
            "consolidation": memory_store.consolidate(),
            "turn_policy": {
                "action": "persisted_and_policy_filtered",
                "policy_version": CURRENT_MEMORY_POLICY_VERSION,
                "minimum_confidence": profile.get("minimum_memory_confidence"),
                "ttl_seconds": profile.get("memory_ttl_seconds"),
            },
        }
        memory_binding = {
            "status": "persisted_and_policy_filtered",
            "record_id": memory_record.id,
            "policy_version": CURRENT_MEMORY_POLICY_VERSION,
        }

    state["mode"] = CONFIG.mode
    state["provider"] = CONFIG.provider
    state["execution_profile"] = profile
    state.setdefault("prompts", []).append(prompt)
    state["prompts"] = state["prompts"][-20:]
    state.setdefault("audit", []).append(
        {
            "turn": state["turn_count"],
            "event": event,
            "authorized": authorized,
            "trace": pipeline.get("last_trace", []),
            "version": pipeline.get("version"),
            "execution_profile": profile.get("name"),
            "memory_action": memory_binding["status"],
        }
    )
    state["audit"] = state["audit"][-50:]
    state["amplification"] = IntelligenceAmplifier().amplify(prompt, state)
    state["evaluation"] = evaluate_session(state)
    state["summary"] = summarize_session(state)
    store.save(state)
    receipt = build_profiled_routing_receipt(
        prompt=prompt,
        event=event,
        steps=steps,
        pipeline=pipeline,
        session_turn=state["turn_count"],
        session_state_hash=SQLiteRepository.record_hash(state),
        execution_profile=profile,
        memory_binding=memory_binding,
        repository_identity=repository_identity,
    )
    RoutingReceiptLedger(store.path).persist(receipt)
    receipt["verification"] = RoutingReceiptLedger(store.path, read_only=True).verify(
        receipt["receipt_id"]
    )
    state["routing_receipt"] = receipt
    return state
