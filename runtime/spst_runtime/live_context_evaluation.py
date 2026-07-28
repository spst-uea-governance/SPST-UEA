import asyncio
from copy import deepcopy
import secrets
from typing import Any, Callable

from spst_runtime.context_review import validate_context_intervention
from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.evaluation.operational_shadow import OperationalShadowRunner
from spst_runtime.evaluation.paired_quality import PairedQualityEvidenceLedger
from spst_runtime.evaluation.producer_evidence import UPTAKE_SCHEMA_VERSION
from spst_runtime.interfaces.model_adapter import ModelAdapter
from spst_runtime.live_pairing import (
    LivePairingError,
    build_live_pair_execution,
    build_live_pair_plan,
    canonical_live_pair_hash,
    validate_live_pair_plan,
)
from spst_runtime.persistence.sqlite_repository import SQLiteRepository


LIVE_CONTEXT_EVALUATION_SCHEMA = "spst-live-context-paired-evaluation-v1"
LIVE_CONTEXT_PLAN_RECORD_SCHEMA = "spst-live-context-plan-record-v1"


class LiveContextPairedEvaluator:
    """Execute fresh adapter calls and expose only an arm-blind pending review."""

    def __init__(
        self,
        repository: SQLiteRepository,
        corpus: OperationalEvaluationCorpus,
        model_adapter: ModelAdapter,
        *,
        randomization_nonce_factory: Callable[[], str] | None = None,
        scoring_nonce_factory: Callable[[], str] | None = None,
    ):
        self.repository = repository
        self.corpus = corpus
        self.model_adapter = model_adapter
        self.randomization_nonce_factory = (
            randomization_nonce_factory or (lambda: secrets.token_hex(32))
        )
        self.runner = OperationalShadowRunner(model_adapter, corpus)
        self.paired_quality = PairedQualityEvidenceLedger(
            repository,
            corpus,
            nonce_factory=scoring_nonce_factory,
        )

    def run(
        self,
        *,
        context_intervention: dict[str, Any],
        candidate_id: str,
        baseline_candidate_id: str,
        task_ids: list[str] | None = None,
        execution_plan: dict[str, Any] | None = None,
        resume_registered_plan: bool = False,
        evaluation_mode: str = PairedQualityEvidenceLedger.HUMAN_REVIEW_MODE,
    ) -> dict[str, Any]:
        if evaluation_mode not in {
            PairedQualityEvidenceLedger.HUMAN_REVIEW_MODE,
            PairedQualityEvidenceLedger.MACHINE_EXACT_CONTRACT_MODE,
        }:
            return self._blocked("live_context_evaluation_mode_invalid")
        valid, reason = validate_context_intervention(context_intervention)
        if not valid:
            return self._blocked(reason or "live_context_intervention_invalid")
        health = self.model_adapter.health()
        capabilities = self.model_adapter.get_capabilities()
        if health.get("ok") is not True:
            return self._blocked("live_provider_unavailable")
        if capabilities.get("supports_provider_observation") is not True:
            return self._blocked("provider_observation_capability_required")
        tasks, _ = self.corpus.load_for_shadow(split="holdout", task_ids=task_ids)
        selected_task_ids = [str(task["task_id"]) for task in tasks]
        if execution_plan is None:
            try:
                plan = build_live_pair_plan(
                    selected_task_ids,
                    self.randomization_nonce_factory(),
                    str(context_intervention["intervention_sha256"]),
                )
            except LivePairingError as error:
                return self._blocked(str(error))
        else:
            plan = deepcopy(execution_plan)
            plan_valid, plan_reason = validate_live_pair_plan(plan)
            if not plan_valid:
                return self._blocked(plan_reason or "live_pair_plan_invalid")
            if plan.get("task_ids") != sorted(selected_task_ids):
                return self._blocked("live_pair_plan_task_set_mismatch")
            if (
                plan.get("context_intervention_sha256")
                != context_intervention["intervention_sha256"]
            ):
                return self._blocked("live_pair_plan_context_mismatch")

        plan_record_key = f"runtime:live_context_plan:{plan['experiment_id']}"
        plan_record = {
            "schema": LIVE_CONTEXT_PLAN_RECORD_SCHEMA,
            "kind": "pre_execution_plan",
            "experiment_id": plan["experiment_id"],
            "manifest": deepcopy(plan),
            "adapter_invocations_started": False,
        }
        plan_record["record_sha256"] = canonical_live_pair_hash(plan_record)
        with self.repository.locked():
            stored_plan = asyncio.run(self.repository.load(plan_record_key))
            if stored_plan is not None:
                if not resume_registered_plan:
                    return self._blocked("live_pair_plan_already_registered")
                if stored_plan != plan_record:
                    return self._blocked("live_pair_registered_plan_mismatch")
            else:
                asyncio.run(self.repository.save(plan_record_key, plan_record))

        reports_by_task: dict[str, dict[str, dict[str, Any]]] = {}
        all_reports: list[dict[str, Any]] = []
        for order_entry in plan["orders"]:
            task_id = str(order_entry["task_id"])
            reports_by_task[task_id] = {}
            for condition in order_entry["condition_order"]:
                execution = build_live_pair_execution(plan, task_id, condition)
                report = self.runner.run(
                    candidate_id=(
                        f"{baseline_candidate_id}-control-run"
                        if condition == "control"
                        else candidate_id
                    ),
                    baseline_candidate_id=baseline_candidate_id,
                    task_ids=[task_id],
                    context_experiment=True,
                    context_intervention=(
                        context_intervention if condition == "treatment" else None
                    ),
                    live_pair_execution=execution,
                )
                asyncio.run(
                    self.repository.save(
                        f"runtime:operational_shadow:{report['id']}",
                        report,
                    )
                )
                reports_by_task[task_id][condition] = report
                all_reports.append(report)

        pairs = []
        selected_bindings: list[dict[str, Any]] = []
        for task_id in selected_task_ids:
            control_report = reports_by_task[task_id]["control"]
            treatment_report = reports_by_task[task_id]["treatment"]
            pairs.append(
                {
                    "task_id": task_id,
                    "baseline": self._reference(
                        control_report,
                        task_id,
                        "baseline",
                    ),
                    "candidate": self._reference(
                        treatment_report,
                        task_id,
                        "maximized",
                    ),
                }
            )
            selected_bindings.extend(
                (
                    self._binding(control_report, task_id, "baseline"),
                    self._binding(treatment_report, task_id, "maximized"),
                )
            )
        evaluation = self.paired_quality.evaluate(
            {"pairs": pairs, "evaluation_mode": evaluation_mode}
        )
        verified_observations = sum(
            binding.get("schema_version") == UPTAKE_SCHEMA_VERSION
            and binding.get("provider_observation_status") == "verified"
            for binding in selected_bindings
        )
        expected_observations = len(selected_task_ids) * 2
        adapter_attempts = sum(
            len(report.get("cases", [])) * 2 for report in all_reports
        )
        expected_adapter_attempts = len(selected_task_ids) * 4
        expected_evaluation_status = (
            "machine_exact_contract_ready"
            if evaluation_mode
            == PairedQualityEvidenceLedger.MACHINE_EXACT_CONTRACT_MODE
            else "pending_human_review"
        )
        live_evidence_complete = (
            evaluation.get("status") == expected_evaluation_status
            and verified_observations == expected_observations
            and adapter_attempts == expected_adapter_attempts
        )
        execution_record = {
            "schema": LIVE_CONTEXT_EVALUATION_SCHEMA,
            "kind": "selected_condition_execution",
            "experiment_id": plan["experiment_id"],
            "evaluation_mode": evaluation_mode,
            "manifest": plan,
            "evaluation_id": evaluation.get("id"),
            "pairs": pairs,
            "selected_provider_observation_count": verified_observations,
            "total_adapter_attempts": adapter_attempts,
        }
        execution_record["record_sha256"] = canonical_live_pair_hash(
            execution_record
        )
        asyncio.run(
            self.repository.save(
                f"runtime:live_context_execution:{plan['experiment_id']}",
                execution_record,
            )
        )
        result = {
            "schema": LIVE_CONTEXT_EVALUATION_SCHEMA,
            "status": (
                expected_evaluation_status if live_evidence_complete else "blocked"
            ),
            "reason": (
                None
                if live_evidence_complete
                else next(
                    iter(evaluation.get("reasons", [])),
                    "provider_observation_coverage_incomplete",
                )
            ),
            "provider": {
                "name": health.get("provider") or health.get("active_provider"),
                "model_version": health.get("model_version") or health.get("model"),
                "provider_identity_cryptographically_verified": False,
            },
            "execution": {
                "plan_registered_before_adapter_invocations": True,
                "plan_record_sha256": plan_record["record_sha256"],
                "fresh_adapter_calls_executed": (
                    adapter_attempts == expected_adapter_attempts
                ),
                "experiment_id": plan["experiment_id"],
                "execution_manifest_sha256": plan["manifest_sha256"],
                "execution_record_sha256": execution_record["record_sha256"],
                "selected_pair_count": len(selected_task_ids),
                "expected_provider_observations": expected_observations,
                "verified_provider_observations": verified_observations,
                "total_adapter_attempts": adapter_attempts,
                "selected_condition_order_randomized": True,
                "randomization_method": plan["randomization_method"],
                "condition_order_balance": deepcopy(
                    plan["condition_order_balance"]
                ),
                "per_task_order_disclosed": False,
            },
            "blind_review": {
                "surface": deepcopy(evaluation.get("blind_review_surface", {})),
                "source_pairs_disclosed": bool(evaluation.get("source_pairs")),
                "measurement_reason": evaluation.get("measurement", {}).get("reason"),
                "human_identity_cryptographically_verified": False,
                "reviewer_blindness_cryptographically_verified": False,
            },
            "evaluation": evaluation,
            "claims": {
                "provider_transport_observed": verified_observations
                == expected_observations,
                "semantic_context_use_verified": False,
                "causal_context_utility_established": False,
                "general_model_quality_claimed": False,
                "automatic_context_adoption": False,
            },
        }
        return result

    def review(self, evaluation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.paired_quality.review(evaluation_id, payload)

    @staticmethod
    def _reference(
        report: dict[str, Any],
        task_id: str,
        arm: str,
    ) -> dict[str, str]:
        binding = LiveContextPairedEvaluator._binding(report, task_id, arm)
        return {
            "producer_run_id": str(report["id"]),
            "binding_id": str(binding["binding_id"]),
        }

    @staticmethod
    def _binding(
        report: dict[str, Any],
        task_id: str,
        arm: str,
    ) -> dict[str, Any]:
        return next(
            binding
            for binding in report.get("producer_evidence", [])
            if binding.get("task_id") == task_id and binding.get("arm") == arm
        )

    @staticmethod
    def _blocked(reason: str) -> dict[str, Any]:
        return {
            "schema": LIVE_CONTEXT_EVALUATION_SCHEMA,
            "status": "blocked",
            "reason": reason,
            "claims": {
                "provider_transport_observed": False,
                "semantic_context_use_verified": False,
                "causal_context_utility_established": False,
                "general_model_quality_claimed": False,
                "automatic_context_adoption": False,
            },
        }
