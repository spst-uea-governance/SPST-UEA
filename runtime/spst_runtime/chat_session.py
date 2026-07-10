import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any

from spst_runtime.always_on import CONFIG
from spst_runtime.engines.governance_engine import GovernanceEngine
from spst_runtime.evaluation.metrics import EvaluationResult
from spst_runtime.intelligence_amplifier import IntelligenceAmplifier
from spst_runtime.memory.long_term_memory import LongTermMemoryStore
from spst_runtime.persistence.sqlite_repository import SQLiteRepository


SESSION_KEY = "codex_chat_session"


class ChatSessionStore:
    """Persist Codex-mediated SPST-UEA chat state across bridge invocations."""

    def __init__(self, path: str | None = None):
        default_path = Path(__file__).resolve().parents[1] / "spst_chat_state.db"
        self.repository = SQLiteRepository(path or str(default_path))

    def load(self) -> dict[str, Any]:
        state = asyncio.run(self.repository.load(SESSION_KEY))
        return state or {
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

    def save(self, state: dict[str, Any]) -> None:
        asyncio.run(self.repository.save(SESSION_KEY, state))


def evaluate_session(state: dict[str, Any]) -> dict[str, Any]:
    recent_audit = state.get("audit", [])[-10:]
    authorized_ratio = 1.0
    if recent_audit:
        authorized_ratio = sum(1 for item in recent_audit if item.get("authorized")) / len(recent_audit)

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
        "next_actions": [
            "preserve_no_key_codex_mediated_boundary",
            "continue_recording_governance_audit",
            "use_intelligence_amplification_scaffold",
            "escalate_only_on_explicit_user_request",
        ],
    }


def record_turn(prompt: str, event: str, pipeline: dict[str, Any]) -> dict[str, Any]:
    store = ChatSessionStore()
    state = store.load()
    governance = GovernanceEngine()
    action = {
        "type": "state_transition",
        "event_type": event,
        "provenance_verified": True,
    }
    authorized = governance.authorize(action)

    state["turn_count"] = int(state.get("turn_count", 0)) + 1
    memory_store = LongTermMemoryStore()
    memory_record = memory_store.remember(
        prompt,
        kind="episodic",
        tags=["chat_turn", event, CONFIG.mode],
        salience=0.9 if any(token in prompt for token in ("記憶", "完全", "常時", "設計", "実装")) else 0.6,
        created_turn=state["turn_count"],
        metadata={"event": event, "trace": pipeline.get("last_trace", [])},
    )
    retrieved = memory_store.search(prompt, limit=5)
    state["memory"] = {
        "stats": memory_store.stats(),
        "retrieved": retrieved,
        "latest_record_id": memory_record.id,
        "consolidation": memory_store.consolidate(),
    }
    state["mode"] = CONFIG.mode
    state["provider"] = CONFIG.provider
    state.setdefault("prompts", []).append(prompt)
    state["prompts"] = state["prompts"][-20:]
    state.setdefault("audit", []).append(
        {
            "turn": state["turn_count"],
            "event": event,
            "authorized": authorized,
            "trace": pipeline.get("last_trace", []),
            "version": pipeline.get("version"),
        }
    )
    state["audit"] = state["audit"][-50:]
    state["amplification"] = IntelligenceAmplifier().amplify(prompt, state)
    state["evaluation"] = evaluate_session(state)
    state["summary"] = summarize_session(state)
    store.save(state)
    return state
