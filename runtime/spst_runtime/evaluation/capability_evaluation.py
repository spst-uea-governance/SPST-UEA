import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from spst_runtime.engines.capability_maximizer import CapabilityMaximizer
from spst_runtime.engines.governance_engine import GovernanceEngine
from spst_runtime.evaluation.quality_evidence import (
    contract_compliance_proxy,
    proxy_calibration,
    quality_claim_boundary,
    unresolved_task_quality,
)
from spst_runtime.interfaces.model_adapter import ModelAdapter


@dataclass(frozen=True)
class EvaluationCase:
    """A public, deterministic task contract used for paired evaluation."""

    case_id: str
    prompt: str
    domain: str
    required_markers: tuple[str, ...]

    @property
    def prompt_digest(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()


DEFAULT_CAPABILITY_SUITE = (
    EvaluationCase(
        "planning_contract",
        "Create a plan and include a verification step.",
        "planning",
        ("plan", "verify"),
    ),
    EvaluationCase(
        "analysis_contract",
        "Analyze assumptions and include a verification step.",
        "analysis",
        ("analyze", "verify"),
    ),
)


class CapabilityEvaluationRunner:
    """Produce reproducible paired scaffold evidence without benchmark overclaim."""

    SUITE_VERSION = "phase11-capability-suite-v1"

    def __init__(
        self,
        model_adapter: ModelAdapter,
        capability_maximizer: CapabilityMaximizer | None = None,
        governance_engine: GovernanceEngine | None = None,
        suite: tuple[EvaluationCase, ...] = DEFAULT_CAPABILITY_SUITE,
    ):
        self.model_adapter = model_adapter
        self.capability_maximizer = capability_maximizer or CapabilityMaximizer()
        self.governance_engine = governance_engine or GovernanceEngine()
        self.suite = suite

    def run(self) -> dict[str, Any]:
        """Run baseline and maximized contexts against the same adapter and suite."""
        governance = self._governance_decision()
        if not governance.get("authorized", False):
            return self._denied_report(governance)

        health = self.model_adapter.health()
        capabilities = self.model_adapter.get_capabilities()
        task_scoring_supported = bool(capabilities.get("supports_structured_evaluation", False))
        cases = []
        for case in self.suite:
            baseline = self._run_case(case, mode="baseline", task_scoring_supported=task_scoring_supported)
            maximized = self._run_case(case, mode="maximized", task_scoring_supported=task_scoring_supported)
            cases.append(
                {
                    "case_id": case.case_id,
                    "domain": case.domain,
                    "prompt_digest": case.prompt_digest,
                    "baseline": baseline,
                    "maximized": maximized,
                }
            )

        contract_proxy = contract_compliance_proxy(cases, task_scoring_supported)
        task_quality = unresolved_task_quality(contract_proxy)
        scaffold_contract = self._scaffold_contract(cases)
        latency = self._latency(cases)
        calibration = proxy_calibration(contract_proxy)
        report = {
            "suite": {
                "version": self.SUITE_VERSION,
                "hash": self._suite_hash(),
                "case_ids": [case.case_id for case in self.suite],
                "same_tasks": True,
            },
            "provider": {
                "name": self._provider_name(health),
                "model_version": health.get("model_version"),
                "requires_api_key": bool(health.get("requires_api_key", False)),
                "contract_scoring_supported": task_scoring_supported,
                "task_scoring_supported": False,
            },
            "paired": {
                "same_provider": True,
                "same_scoring_rubric": True,
                "same_contract_proxy": True,
                "independent_blinded_evaluator": False,
                "baseline_mode": "normal_adapter_context",
                "maximized_mode": "capability_maximization_context",
            },
            "cases": cases,
            "contract_compliance_proxy": contract_proxy,
            "task_quality": task_quality,
            "scaffold_contract": scaffold_contract,
            "latency": latency,
            "calibration": calibration,
            "official_score_policy": {
                "model_weight_score_unchanged": True,
                "official_benchmark_claimed": False,
                "claim": (
                    "This report measures local contract-compliance proxy evidence, "
                    "not semantic task quality or an official model benchmark score."
                ),
            },
            "claims": {
                "task_quality_uplift_claimed": False,
                "quality_claim": quality_claim_boundary(contract_proxy),
                "requires_human_interpretation": True,
                "automatic_adoption": False,
                "scope": (
                    "contract_compliance_proxy_only"
                    if contract_proxy["available"]
                    else "scaffold_contract_only"
                ),
            },
            "governance": self._compact_governance(governance),
        }
        report["status"] = calibration["status"]
        report["id"] = self._identifier(report)
        return report

    def _run_case(
        self,
        case: EvaluationCase,
        *,
        mode: str,
        task_scoring_supported: bool,
    ) -> dict[str, Any]:
        plan = None
        context: dict[str, Any] = {
            "instructions": "Return an answer for a bounded local evaluation case.",
            "evaluation": {
                "mode": mode,
                "suite_version": self.SUITE_VERSION,
                "case_id": case.case_id,
            },
        }
        if mode == "maximized":
            plan = self.capability_maximizer.maximize(
                case.prompt,
                amplification={"work_units": ["evaluation"]},
                session_state={"memory": {"retrieved": []}},
                payload={"benchmark_suite": case.domain},
            )
            context["capability_maximization"] = plan

        started_at = time.perf_counter()
        try:
            result = asyncio.run(self.model_adapter.infer(case.prompt, context))
        except Exception as exc:
            return {
                "status": "error",
                "available": False,
                "provider": "unknown",
                "latency_ms": int((time.perf_counter() - started_at) * 1000),
                "output_digest": self._digest(type(exc).__name__),
                "contract_score": None,
                "task_score": None,
                "score_semantics": "contract_compliance_proxy",
                "scaffold_contract_score": self._scaffold_score(plan),
                "plan": self._plan_summary(plan),
            }

        text = str(result.get("text", ""))
        available = bool(result.get("available", True))
        return {
            "status": "completed" if available else "unavailable",
            "available": available,
            "provider": result.get("provider", "unknown"),
            "latency_ms": int((time.perf_counter() - started_at) * 1000),
            "output_digest": self._digest(text),
            "contract_score": (
                self._marker_score(text, case.required_markers)
                if available and task_scoring_supported
                else None
            ),
            "task_score": None,
            "score_semantics": "contract_compliance_proxy",
            "scaffold_contract_score": self._scaffold_score(plan),
            "plan": self._plan_summary(plan),
        }

    def _scaffold_contract(self, cases: list[dict[str, Any]]) -> dict[str, float]:
        baseline_scores = [float(case["baseline"]["scaffold_contract_score"]) for case in cases]
        maximized_scores = [float(case["maximized"]["scaffold_contract_score"]) for case in cases]
        baseline_mean = self._mean(baseline_scores)
        maximized_mean = self._mean(maximized_scores)
        return {
            "baseline_mean": baseline_mean,
            "maximized_mean": maximized_mean,
            "paired_delta": round(maximized_mean - baseline_mean, 6),
        }

    def _latency(self, cases: list[dict[str, Any]]) -> dict[str, float]:
        baseline_values = [float(case["baseline"]["latency_ms"]) for case in cases]
        maximized_values = [float(case["maximized"]["latency_ms"]) for case in cases]
        return {
            "baseline_mean_ms": self._mean(baseline_values),
            "maximized_mean_ms": self._mean(maximized_values),
            "overhead_mean_ms": round(
                self._mean(maximized_values) - self._mean(baseline_values),
                6,
            ),
        }

    def _governance_decision(self) -> dict[str, Any]:
        return self.governance_engine.decide(
            {
                "type": "capability_evaluation",
                "event_type": "evaluation_run",
                "source": "capability_evaluation_runner",
                "provenance_scope": "ephemeral",
                "provenance_verified": False,
                "payload": {
                    "analysis_only": True,
                    "read_only": True,
                },
                "security_assessment": {"blocked": False, "findings": []},
            }
        )

    def _denied_report(self, governance: dict[str, Any]) -> dict[str, Any]:
        contract_proxy = contract_compliance_proxy([], False)
        report = {
            "suite": {
                "version": self.SUITE_VERSION,
                "hash": self._suite_hash(),
                "case_ids": [case.case_id for case in self.suite],
                "same_tasks": True,
            },
            "provider": {
                "name": self._provider_name(self.model_adapter.health()),
                "model_version": self.model_adapter.health().get("model_version"),
                "requires_api_key": bool(
                    self.model_adapter.health().get("requires_api_key", False)
                ),
                "contract_scoring_supported": False,
                "task_scoring_supported": False,
            },
            "paired": {"same_provider": True, "same_scoring_rubric": True},
            "cases": [],
            "contract_compliance_proxy": contract_proxy,
            "task_quality": unresolved_task_quality(contract_proxy),
            "scaffold_contract": {
                "baseline_mean": 0.0,
                "maximized_mean": 0.0,
                "paired_delta": 0.0,
            },
            "latency": {
                "baseline_mean_ms": 0.0,
                "maximized_mean_ms": 0.0,
                "overhead_mean_ms": 0.0,
            },
            "calibration": proxy_calibration(contract_proxy, blocked=True),
            "official_score_policy": {
                "model_weight_score_unchanged": True,
                "official_benchmark_claimed": False,
            },
            "claims": {
                "task_quality_uplift_claimed": False,
                "quality_claim": quality_claim_boundary(contract_proxy),
                "requires_human_interpretation": True,
                "automatic_adoption": False,
                "scope": "blocked",
            },
            "governance": self._compact_governance(governance),
            "status": "blocked",
        }
        report["id"] = self._identifier(report)
        return report

    def _suite_hash(self) -> str:
        canonical = json.dumps(
            [
                {
                    "case_id": case.case_id,
                    "domain": case.domain,
                    "prompt_digest": case.prompt_digest,
                    "required_markers": case.required_markers,
                }
                for case in self.suite
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        return self._digest(canonical)

    def _identifier(self, report: dict[str, Any]) -> str:
        canonical = json.dumps(
            {
                "suite": report["suite"],
                "provider": report["provider"],
                "paired": report["paired"],
                "cases": [
                    {
                        "case_id": case["case_id"],
                        "baseline": self._stable_case_result(case["baseline"]),
                        "maximized": self._stable_case_result(case["maximized"]),
                    }
                    for case in report["cases"]
                ],
                "contract_compliance_proxy": report["contract_compliance_proxy"],
                "task_quality": report["task_quality"],
                "scaffold_contract": report["scaffold_contract"],
                "calibration": report["calibration"],
                "claims": report["claims"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"EVAL-{self._digest(canonical)[:16]}"

    def _stable_case_result(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in result.items()
            if key not in {"latency_ms"}
        }

    def _compact_governance(self, decision: dict[str, Any]) -> dict[str, Any]:
        return {
            key: decision.get(key)
            for key in ("authorized", "trust_level", "requires_human_approval", "reasons")
        }

    def _provider_name(self, health: dict[str, Any]) -> str:
        return str(health.get("provider") or health.get("active_provider") or "unknown")

    def _plan_summary(self, plan: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(plan, dict):
            return {}
        strategies = plan.get("strategies", [])
        verification_plan = plan.get("verification_plan", {})
        return {
            "mode": plan.get("mode"),
            "domains": plan.get("target_domains", []),
            "strategy_count": len(strategies) if isinstance(strategies, list) else 0,
            "verification_check_count": len(
                verification_plan.get("checks", [])
                if isinstance(verification_plan, dict)
                else []
            ),
        }

    def _scaffold_score(self, plan: dict[str, Any] | None) -> float:
        summary = self._plan_summary(plan)
        if not summary:
            return 0.0
        return 1.0 if summary["strategy_count"] >= 4 and summary["verification_check_count"] >= 2 else 0.5

    def _marker_score(self, text: str, markers: tuple[str, ...]) -> float:
        if not markers:
            return 1.0
        normalized = text.lower()
        return round(sum(marker in normalized for marker in markers) / len(markers), 6)

    def _mean(self, values: list[float]) -> float:
        return round(sum(values) / len(values), 6) if values else 0.0

    def _digest(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
