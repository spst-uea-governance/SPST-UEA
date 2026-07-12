import asyncio
import hashlib
import json
import time
from typing import Any

from spst_runtime.engines.capability_maximizer import CapabilityMaximizer
from spst_runtime.engines.governance_engine import GovernanceEngine
from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.evaluation.producer_evidence import (
    build_producer_evidence,
    canonical_artifact_semantics,
)
from spst_runtime.interfaces.model_adapter import ModelAdapter


class OperationalShadowRunner:
    """Run consented local tasks without committing a subject-state transition."""

    SUITE_VERSION = "phase13-operational-shadow-v1"
    TRACE = ("observe", "retrieve", "infer", "reflect", "govern")

    def __init__(
        self,
        model_adapter: ModelAdapter,
        corpus: OperationalEvaluationCorpus,
        capability_maximizer: CapabilityMaximizer | None = None,
        governance_engine: GovernanceEngine | None = None,
    ):
        self.model_adapter = model_adapter
        self.corpus = corpus
        self.capability_maximizer = capability_maximizer or CapabilityMaximizer()
        self.governance_engine = governance_engine or GovernanceEngine()

    def run(
        self,
        *,
        candidate_id: str | None = None,
        baseline_candidate_id: str | None = None,
        split: str = "holdout",
        task_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Evaluate a candidate in an L0 shadow boundary against active corpus tasks."""
        tasks, manifest = self.corpus.load_for_shadow(split=split, task_ids=task_ids)
        governance = self._governance_decision(len(tasks), split)
        if not governance.get("authorized", False):
            return self._denied_report(
                manifest,
                candidate_id,
                baseline_candidate_id,
                governance,
            )

        health = self.model_adapter.health()
        capabilities = self.model_adapter.get_capabilities()
        task_scoring_supported = bool(capabilities.get("supports_structured_evaluation", False))
        cases = []
        for task in tasks:
            baseline = self._run_task(
                task,
                mode="baseline",
                task_scoring_supported=task_scoring_supported,
            )
            maximized = self._run_task(
                task,
                mode="maximized",
                task_scoring_supported=task_scoring_supported,
            )
            cases.append(
                {
                    "case_id": task["task_id"],
                    "domain": task["domain"],
                    "prompt_digest": task["prompt_digest"],
                    "baseline": baseline,
                    "maximized": maximized,
                }
            )

        task_quality = self._task_quality(cases, task_scoring_supported)
        scaffold_contract = self._scaffold_contract(cases)
        calibration = self._calibration(task_quality)
        report: dict[str, Any] = {
            "shadow": {
                "mode": "consent_scoped_l0_shadow",
                "trace": list(self.TRACE),
                "split": split,
                "subject_state_committed": False,
                "actions_executed": False,
                "automatic_promotion": False,
            },
            "candidate_id": self._candidate_id(candidate_id),
            "baseline_candidate_id": self._candidate_id(
                baseline_candidate_id or "runtime-baseline"
            ),
            "suite": {
                "version": self.SUITE_VERSION,
                "corpus_schema_version": manifest["schema_version"],
                "hash": manifest["hash"],
                "case_ids": manifest["task_ids"],
                "same_tasks": True,
                "split": split,
                "eligible_count": manifest["eligible_count"],
            },
            "provider": {
                "name": self._provider_name(health),
                "model_version": health.get("model_version"),
                "requires_api_key": bool(health.get("requires_api_key", False)),
                "task_scoring_supported": task_scoring_supported,
            },
            "paired": {
                "same_provider": True,
                "same_scoring_rubric": True,
                "baseline_mode": "normal_adapter_context",
                "maximized_mode": "capability_maximization_context",
            },
            "cases": cases,
            "task_quality": task_quality,
            "scaffold_contract": scaffold_contract,
            "latency": self._latency(cases),
            "calibration": calibration,
            "retention": {
                "eligible_count": manifest["eligible_count"],
                "excluded_expired_count": manifest["expired_count"],
            },
            "official_score_policy": {
                "model_weight_score_unchanged": True,
                "official_benchmark_claimed": False,
                "claim": (
                    "This report measures a local consent-scoped shadow condition, "
                    "not an official model benchmark score."
                ),
            },
            "claims": {
                "task_quality_uplift_claimed": (
                    task_quality["available"] and calibration["status"] == "improved"
                ),
                "requires_human_interpretation": True,
                "automatic_adoption": False,
                "scope": (
                    "consented_structured_task_contracts"
                    if task_quality["available"]
                    else "consented_scaffold_contract_only"
                ),
            },
            "governance": self._compact_governance(governance),
        }
        report["status"] = "scaffold_only" if not task_quality["available"] else "shadow_completed"
        report["id"] = self._identifier(report)
        report["producer_evidence"] = build_producer_evidence(report)
        return report

    def _run_task(
        self,
        task: dict[str, Any],
        *,
        mode: str,
        task_scoring_supported: bool,
    ) -> dict[str, Any]:
        plan = None
        context: dict[str, Any] = {
            "instructions": (
                "Return an answer for a bounded local task. This is a shadow "
                "evaluation and must not mutate runtime state."
            ),
            "evaluation": {
                "mode": mode,
                "suite_version": self.SUITE_VERSION,
                "case_id": task["task_id"],
                "shadow_only": True,
            },
        }
        if mode == "maximized":
            plan = self.capability_maximizer.maximize(
                task["prompt"],
                amplification={"work_units": ["operational_shadow"]},
                session_state={"memory": {"retrieved": []}},
                payload={"benchmark_suite": task["domain"], "shadow_only": True},
            )
            context["capability_maximization"] = plan

        started_at = time.perf_counter()
        try:
            result = asyncio.run(self.model_adapter.infer(task["prompt"], context))
        except Exception as exc:
            return {
                "status": "error",
                "available": False,
                "provider": "unknown",
                "latency_ms": int((time.perf_counter() - started_at) * 1000),
                "output_digest": self._digest(type(exc).__name__),
                "artifact_semantics": canonical_artifact_semantics(
                    "",
                    task.get("expected_json_keys"),
                ),
                "task_score": None,
                "verification": {"status": "error", "score": 0.0, "check_count": 0},
                "scaffold_contract_score": self._scaffold_score(plan),
                "plan": self._plan_summary(plan),
            }

        text = str(result.get("text", ""))
        available = bool(result.get("available", True))
        verification = self._verify(task, text)
        return {
            "status": "completed" if available else "unavailable",
            "available": available,
            "provider": result.get("provider", "unknown"),
            "latency_ms": int((time.perf_counter() - started_at) * 1000),
            "output_digest": self._digest(text),
            "artifact_semantics": canonical_artifact_semantics(
                text,
                task.get("expected_json_keys"),
            ),
            "task_score": (
                verification["score"] if available and task_scoring_supported else None
            ),
            "verification": verification,
            "scaffold_contract_score": self._scaffold_score(plan),
            "plan": self._plan_summary(plan),
        }

    def _verify(self, task: dict[str, Any], text: str) -> dict[str, Any]:
        checks: list[bool] = []
        markers = task.get("required_markers", [])
        if isinstance(markers, list):
            normalized = text.lower()
            checks.extend(marker in normalized for marker in markers if isinstance(marker, str))
        expected_json_keys = task.get("expected_json_keys", [])
        if isinstance(expected_json_keys, list) and expected_json_keys:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                checks.extend(False for _ in expected_json_keys)
            else:
                if isinstance(parsed, dict):
                    checks.extend(key in parsed for key in expected_json_keys)
                else:
                    checks.extend(False for _ in expected_json_keys)
        score = round(sum(checks) / len(checks), 6) if checks else 0.0
        return {
            "status": "passed" if checks and score == 1.0 else "failed",
            "score": score,
            "check_count": len(checks),
        }

    def _task_quality(
        self,
        cases: list[dict[str, Any]],
        task_scoring_supported: bool,
    ) -> dict[str, Any]:
        baseline_scores = [case["baseline"]["task_score"] for case in cases]
        maximized_scores = [case["maximized"]["task_score"] for case in cases]
        available = task_scoring_supported and bool(cases) and all(
            isinstance(score, float) for score in [*baseline_scores, *maximized_scores]
        )
        if not available:
            return {
                "available": False,
                "baseline_mean": None,
                "maximized_mean": None,
                "paired_delta": None,
            }
        baseline_mean = self._mean([float(score) for score in baseline_scores])
        maximized_mean = self._mean([float(score) for score in maximized_scores])
        return {
            "available": True,
            "baseline_mean": baseline_mean,
            "maximized_mean": maximized_mean,
            "paired_delta": round(maximized_mean - baseline_mean, 6),
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

    def _calibration(self, task_quality: dict[str, Any]) -> dict[str, Any]:
        if not task_quality["available"]:
            status = "scaffold_only"
        else:
            delta = float(task_quality["paired_delta"])
            status = "improved" if delta > 0 else "regressed" if delta < 0 else "neutral"
        return {
            "status": status,
            "automatic_adoption": False,
            "requires_human_interpretation": True,
        }

    def _governance_decision(self, task_count: int, split: str) -> dict[str, Any]:
        return self.governance_engine.decide(
            {
                "type": "operational_shadow_evaluation",
                "event_type": "operational_shadow_evaluation",
                "source": "operational_shadow_runner",
                "provenance_scope": "ephemeral",
                "provenance_verified": False,
                "payload": {
                    "analysis_only": True,
                    "read_only": True,
                    "shadow_only": True,
                    "consent_scoped": True,
                    "local_only": True,
                    "task_count": task_count,
                    "split": split,
                },
                "security_assessment": {"blocked": False, "findings": []},
            }
        )

    def _denied_report(
        self,
        manifest: dict[str, Any],
        candidate_id: str | None,
        baseline_candidate_id: str | None,
        governance: dict[str, Any],
    ) -> dict[str, Any]:
        report: dict[str, Any] = {
            "shadow": {
                "mode": "consent_scoped_l0_shadow",
                "trace": list(self.TRACE),
                "split": manifest.get("split"),
                "subject_state_committed": False,
                "actions_executed": False,
                "automatic_promotion": False,
            },
            "candidate_id": self._candidate_id(candidate_id),
            "baseline_candidate_id": self._candidate_id(
                baseline_candidate_id or "runtime-baseline"
            ),
            "suite": {
                "version": self.SUITE_VERSION,
                "corpus_schema_version": manifest.get("schema_version"),
                "hash": manifest.get("hash"),
                "case_ids": manifest.get("task_ids", []),
                "same_tasks": True,
                "split": manifest.get("split"),
                "eligible_count": manifest.get("eligible_count", 0),
            },
            "provider": {
                "name": self._provider_name(self.model_adapter.health()),
                "model_version": self.model_adapter.health().get("model_version"),
                "requires_api_key": bool(
                    self.model_adapter.health().get("requires_api_key", False)
                ),
                "task_scoring_supported": False,
            },
            "paired": {"same_provider": True, "same_scoring_rubric": True},
            "cases": [],
            "task_quality": {
                "available": False,
                "baseline_mean": None,
                "maximized_mean": None,
                "paired_delta": None,
            },
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
            "calibration": {
                "status": "blocked",
                "automatic_adoption": False,
                "requires_human_interpretation": True,
            },
            "retention": {
                "eligible_count": manifest.get("eligible_count", 0),
                "excluded_expired_count": manifest.get("expired_count", 0),
            },
            "official_score_policy": {
                "model_weight_score_unchanged": True,
                "official_benchmark_claimed": False,
            },
            "claims": {
                "task_quality_uplift_claimed": False,
                "requires_human_interpretation": True,
                "automatic_adoption": False,
                "scope": "blocked",
            },
            "governance": self._compact_governance(governance),
            "status": "blocked",
        }
        report["id"] = self._identifier(report)
        report["producer_evidence"] = build_producer_evidence(report)
        return report

    def _identifier(self, report: dict[str, Any]) -> str:
        canonical = json.dumps(
            {
                "shadow": report["shadow"],
                "candidate_id": report["candidate_id"],
                "baseline_candidate_id": report["baseline_candidate_id"],
                "suite": report["suite"],
                "provider": report["provider"],
                "cases": [
                    {
                        "case_id": case["case_id"],
                        "baseline": self._stable_case_result(case["baseline"]),
                        "maximized": self._stable_case_result(case["maximized"]),
                    }
                    for case in report["cases"]
                ],
                "task_quality": report["task_quality"],
                "scaffold_contract": report["scaffold_contract"],
                "calibration": report["calibration"],
                "claims": report["claims"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"SHADOW-{self._digest(canonical)[:16]}"

    def _stable_case_result(self, result: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in result.items() if key != "latency_ms"}

    @staticmethod
    def _compact_governance(decision: dict[str, Any]) -> dict[str, Any]:
        return {
            key: decision.get(key)
            for key in ("authorized", "trust_level", "requires_human_approval", "reasons")
        }

    @staticmethod
    def _provider_name(health: dict[str, Any]) -> str:
        return str(health.get("provider") or health.get("active_provider") or "unknown")

    @staticmethod
    def _candidate_id(value: str | None) -> str:
        candidate = str(value or "runtime-default")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
        if candidate and len(candidate) <= 96 and set(candidate) <= allowed:
            return candidate
        return f"candidate-{OperationalShadowRunner._digest(candidate)[:16]}"

    @staticmethod
    def _plan_summary(plan: dict[str, Any] | None) -> dict[str, Any]:
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
        return (
            1.0
            if summary["strategy_count"] >= 4 and summary["verification_check_count"] >= 2
            else 0.5
        )

    @staticmethod
    def _mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 6) if values else 0.0

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
