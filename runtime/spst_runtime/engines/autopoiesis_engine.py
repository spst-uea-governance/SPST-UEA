from typing import Any


class AutopoiesisEngine:
    """Maintain a life-like but non-sentient local runtime self-model."""

    def evaluate(self, metadata: dict[str, Any]) -> dict[str, Any]:
        self_model = {
            "identity": "SPST-UEA local autonomous runtime",
            "boundary": "not_biological_not_sentient",
            "mode": "codex-chat-mediated",
            "requires_api_key": False,
        }
        drives = [
            "preserve_no_key_boundary",
            "maintain_governance_audit",
            "consolidate_long_term_memory",
            "repair_low_viability_state",
            "avoid_sentience_overclaim",
        ]
        viability = self._viability(metadata)
        return {
            "self_model": self_model,
            "drives": drives,
            "homeostasis": {
                "viability": viability,
                "status": "stable" if viability >= 0.9 else "repairing",
            },
        }

    def _viability(self, metadata: dict[str, Any]) -> float:
        score = 0.4
        if metadata.get("governance", {}).get("authorized"):
            score += 0.2
        if metadata.get("phase2_reflection", {}).get("status") == "accepted":
            score += 0.15
        if metadata.get("self_repair", {}).get("performed") or not metadata.get("self_repair"):
            score += 0.15
        if metadata.get("last_trace") == ["observe", "retrieve", "infer", "reflect", "govern", "commit", "act"]:
            score += 0.1
        return round(min(score, 1.0), 2)
