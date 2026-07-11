import re
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CapabilityPlan:
    mode: str
    target_domains: list[str]
    strategies: list[str]
    prompt_contract: dict[str, Any]
    verification_plan: dict[str, Any]
    provider_policy: dict[str, Any]
    official_score_policy: dict[str, Any]
    realized_performance: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class CapabilityMaximizer:
    """Build provider-neutral scaffolds that help a model realize more of its ability."""

    def maximize(
        self,
        prompt: str,
        *,
        amplification: dict[str, Any],
        session_state: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        domains = self._target_domains(prompt, payload)
        strategies = self._strategies(domains, prompt, amplification, session_state)
        plan = CapabilityPlan(
            mode="capability_maximization",
            target_domains=domains,
            strategies=strategies,
            prompt_contract=self._prompt_contract(prompt, domains, strategies),
            verification_plan=self._verification_plan(domains, strategies),
            provider_policy={
                "requires_specific_model": False,
                "api_key_required": False,
                "adapter_boundary": "ModelAdapter",
                "compatible_with": ["codex-mediated-local", "openai", "local", "mcp-hosted"],
            },
            official_score_policy={
                "model_weight_score_unchanged": True,
                "reported_baseline_score": payload.get("official_baseline_score"),
                "claim": (
                    "The scaffold may improve realized task performance under a system "
                    "condition, but it does not alter official normal-model benchmark truth."
                ),
            },
            realized_performance=self._realized_performance(strategies),
        )
        return plan.as_dict()

    def _target_domains(self, prompt: str, payload: dict[str, Any]) -> list[str]:
        requested = payload.get("benchmark_suite")
        if isinstance(requested, str) and requested:
            return [self._normalize_domain(requested)]

        text = prompt.lower()
        domains = []
        if any(token in text for token in ("code", "coding", "pytest", "bug", "hidden tests")):
            domains.append("coding")
        if any(token in text for token in ("math", "proof", "calculate", "equation")):
            domains.append("math")
        if any(token in text for token in ("reason", "analyze", "compare", "logic", "hard")):
            domains.append("reasoning")
        if any(token in text for token in ("retrieve", "knowledge", "context", "memory")):
            domains.append("knowledge")
        return domains or ["general"]

    def _normalize_domain(self, value: str) -> str:
        normalized = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
        aliases = {
            "swe_bench": "coding",
            "coding_agent": "coding",
            "software": "coding",
            "gpqa": "reasoning",
            "math_benchmark": "math",
        }
        return aliases.get(normalized, normalized or "general")

    def _strategies(
        self,
        domains: list[str],
        prompt: str,
        amplification: dict[str, Any],
        session_state: dict[str, Any],
    ) -> list[str]:
        strategies = [
            "prompt_contract",
            "spec_decomposition",
            "constraint_extraction",
            "verifier_loop",
        ]
        if "coding" in domains:
            strategies.extend(["test_first_reasoning", "edge_case_inventory"])
        if "math" in domains or "reasoning" in domains:
            strategies.extend(["multi_path_reasoning", "self_consistency_vote"])
        if "knowledge" in domains or session_state.get("memory", {}).get("retrieved"):
            strategies.append("retrieval_grounding")
        if amplification.get("work_units"):
            strategies.append("ia_task_alignment")
        if len(prompt) > 120:
            strategies.append("context_compression")
        return sorted(dict.fromkeys(strategies))

    def _prompt_contract(
        self,
        prompt: str,
        domains: list[str],
        strategies: list[str],
    ) -> dict[str, Any]:
        return {
            "objective": prompt.strip(),
            "domains": domains,
            "response_shape": "answer_with_verification_trace",
            "must_include": [
                "assumptions",
                "solution_steps",
                "verification_result",
            ],
            "must_avoid": [
                "benchmark_answer_leakage",
                "unsupported_score_claims",
                "provider_specific_coupling",
            ],
            "strategy_count": len(strategies),
        }

    def _verification_plan(self, domains: list[str], strategies: list[str]) -> dict[str, Any]:
        checks = ["constraint_match", "internal_consistency", "final_answer_contract"]
        if "coding" in domains:
            checks.extend(["unit_test_plan", "hidden_test_edge_cases"])
        if "math" in domains or "reasoning" in domains:
            checks.extend(["alternate_derivation", "contradiction_scan"])
        return {
            "minimum_passes": 2 if "verifier_loop" in strategies else 1,
            "checks": sorted(dict.fromkeys(checks)),
            "requires_external_answer_leakage": False,
            "requires_official_benchmark_mutation": False,
        }

    def _realized_performance(self, strategies: list[str]) -> dict[str, Any]:
        gain_index = min(1.0, round(0.08 * len(strategies), 2))
        return {
            "metric": "realized_system_performance",
            "expected_direction": "increase" if gain_index else "neutral",
            "scaffold_gain_index": gain_index,
            "evidence_required": [
                "normal_adapter_baseline",
                "same_model_same_tasks",
                "maximized_adapter_run",
                "paired_delta_report",
            ],
        }
