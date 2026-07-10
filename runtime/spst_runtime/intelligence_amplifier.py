from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AmplificationResult:
    intent: str
    work_units: list[str]
    retrieved_context: list[str]
    reflection_checks: list[str]
    governance_focus: list[str]
    amplification_score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "work_units": self.work_units,
            "retrieved_context": self.retrieved_context,
            "reflection_checks": self.reflection_checks,
            "governance_focus": self.governance_focus,
            "amplification_score": self.amplification_score,
        }


class IntelligenceAmplifier:
    """Build local deterministic reasoning scaffolds for SPST-UEA turns."""

    def amplify(self, prompt: str, session_state: dict[str, Any]) -> dict[str, Any]:
        result = AmplificationResult(
            intent=self._classify_intent(prompt),
            work_units=self._decompose(prompt),
            retrieved_context=self._retrieve_context(session_state),
            reflection_checks=self._reflection_checks(prompt),
            governance_focus=[
                "keep_api_key_free_operation",
                "preserve_codex_mediated_boundary",
                "avoid_direct_identity_mutation",
                "record_audit_before_answering",
            ],
            amplification_score=self._score(prompt, session_state),
        )
        return result.as_dict()

    def _classify_intent(self, prompt: str) -> str:
        text = prompt.lower()
        if any(word in text for word in ("implement", "build", "add", "fix", "実装", "構築", "螳溯", "遏")):
            return "implementation"
        if any(word in text for word in ("design", "architecture", "設計", "アーキテクチャ")):
            return "design"
        if any(word in text for word in ("analysis", "analyze", "compare", "effect", "分析", "比較")):
            return "analysis"
        if any(word in text for word in ("operate", "operation", "status", "run", "起動", "運用", "稼働")):
            return "operation"
        return "general"

    def _decompose(self, prompt: str) -> list[str]:
        lowered = prompt.lower()
        units = ["capture_user_goal", "route_through_spst_boundary"]
        if len(prompt) > 40:
            units.append("summarize_core_request")
        if any(token in lowered for token in ("design", "architecture", "設計", "アーキテクチャ", "繝ｼ", "險")):
            units.append("draft_architecture")
        if any(token in lowered for token in ("implement", "build", "add", "fix", "実装", "構築", "螳溯", "遏")):
            units.append("implement_minimal_safe_change")
        units.extend(["evaluate_governance", "prepare_user_facing_answer"])
        return units

    def _retrieve_context(self, session_state: dict[str, Any]) -> list[str]:
        prompts = session_state.get("prompts", [])
        goals = session_state.get("goals", [])
        context = []
        if goals:
            context.append(f"active_goal:{goals[-1]}")
        context.extend(f"recent_prompt:{prompt}" for prompt in prompts[-3:])
        for memory in session_state.get("memory", {}).get("retrieved", [])[:3]:
            text = memory.get("text") or memory.get("value", {}).get("text")
            if text:
                context.append(f"memory:{text}")
        return context

    def _reflection_checks(self, prompt: str) -> list[str]:
        checks = [
            "Did the turn preserve the no-key constraint?",
            "Did the turn separate inference from commit?",
            "Is the answer explicit about SPST-UEA boundaries?",
        ]
        if "完全" in prompt or "極限" in prompt or "perfect" in prompt.lower():
            checks.append("Does the answer avoid overstating actual model capability?")
        return checks

    def _score(self, prompt: str, session_state: dict[str, Any]) -> float:
        score = 0.35
        if prompt:
            score += 0.25
        if len(prompt) > 10:
            score += 0.1
        if session_state.get("memory", {}).get("retrieved"):
            score += 0.2
        if session_state.get("turn_count", 0) > 0:
            score += 0.1
        if session_state.get("audit"):
            score += 0.1
        if session_state.get("evaluation", {}).get("esi", 0.0) >= 0.95:
            score += 0.1
        return round(min(score, 1.0), 2)
