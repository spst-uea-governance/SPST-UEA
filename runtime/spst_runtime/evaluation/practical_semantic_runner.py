from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from spst_runtime.chat_bridge import run_chat_turn
from spst_runtime.evaluation.practical_semantic_study import (
    PracticalSemanticStudyLedger,
)
from spst_runtime.interfaces.model_adapter import ModelAdapter
from spst_runtime.model_artifact_contract import build_model_artifact_contract
from spst_runtime.provider_observation import (
    CODEX_CLI_OBSERVATION_SOURCE,
    build_provider_request_binding,
    verify_provider_observation,
)
from spst_runtime.repository_identity import capture_repository_identity


TREATMENT_CONTEXT_SCHEMA = "spst-practical-semantic-treatment-context-v1"
ARTIFACT_KEYS = [
    "diagnosis",
    "minimal_patch",
    "verification",
    "residual_risk",
]
_RouteTask = Callable[..., dict[str, Any]]


class PracticalSemanticRunnerError(RuntimeError):
    """Fail-closed practical study execution error; calls are never retried."""


def treatment_instructions(value: Any) -> str:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "authority",
        "instructions",
        "task_specific_answers_included",
        "reference_patches_included",
        "expected_scores_included",
        "arm_mapping_included",
    }:
        raise PracticalSemanticRunnerError("treatment_context_shape_invalid")
    instructions = value.get("instructions")
    if (
        value.get("schema") != TREATMENT_CONTEXT_SCHEMA
        or value.get("authority") != "process_guidance_only"
        or not isinstance(instructions, list)
        or not instructions
        or any(not isinstance(item, str) or not item.strip() for item in instructions)
        or value.get("task_specific_answers_included") is not False
        or value.get("reference_patches_included") is not False
        or value.get("expected_scores_included") is not False
        or value.get("arm_mapping_included") is not False
    ):
        raise PracticalSemanticRunnerError("treatment_context_invalid")
    return "\n".join(str(item).strip() for item in instructions)


class PracticalSemanticStudyRunner:
    """Run one fixed 2-call-per-task study under persisted execution authority."""

    def __init__(
        self,
        ledger: PracticalSemanticStudyLedger,
        adapter: ModelAdapter,
        *,
        repository_root: str,
        session_path: str,
        memory_path: str,
        treatment_context_path: str,
        route_task: _RouteTask = run_chat_turn,
    ) -> None:
        self.ledger = ledger
        self.adapter = adapter
        self.repository_root = str(Path(repository_root).resolve())
        self.session_path = session_path
        self.memory_path = memory_path
        self.treatment_context_path = treatment_context_path
        self.route_task = route_task

    def preflight(self, study_id: str) -> dict[str, Any]:
        study = self.ledger.get(study_id)
        if study is None:
            raise PracticalSemanticRunnerError("practical_semantic_study_not_found")
        if study.get("status") == "blocked":
            raise PracticalSemanticRunnerError(str(study.get("reason")))
        if study.get("execution_authority", {}).get("status") != "authorized":
            raise PracticalSemanticRunnerError("practical_semantic_execution_authority_required")
        if study.get("pair_collection", {}).get("pair_count") != 0:
            raise PracticalSemanticRunnerError("practical_semantic_fresh_execution_required")
        target = int(study["stop_rule"]["target_pair_count"])
        maximum = study["execution_authority"]["maximum_adapter_invocations"]
        if maximum != 2 * target:
            raise PracticalSemanticRunnerError("practical_semantic_call_budget_mismatch")

        current_identity = capture_repository_identity(self.repository_root)
        if current_identity.get("identity_sha256") != study["repository_identity"].get(
            "identity_sha256"
        ):
            raise PracticalSemanticRunnerError("practical_semantic_repository_identity_changed")

        raw_context = Path(self.treatment_context_path).read_bytes()
        try:
            context_value = json.loads(raw_context.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PracticalSemanticRunnerError("treatment_context_json_invalid") from error
        instructions = treatment_instructions(context_value)
        if hashlib.sha256(raw_context).hexdigest() != study["intervention"]["context_sha256"]:
            raise PracticalSemanticRunnerError("treatment_context_source_digest_mismatch")
        if hashlib.sha256(instructions.encode("utf-8")).hexdigest() != study[
            "intervention"
        ]["model_instructions_sha256"]:
            raise PracticalSemanticRunnerError("treatment_context_instruction_digest_mismatch")

        health = self.adapter.health()
        capabilities = self.adapter.get_capabilities()
        required_capabilities = {
            "requires_api_key": False,
            "billing_class": "chatgpt_plan_usage",
            "spendable_credits_must_be_absent": True,
            "included_plan_rate_limit_must_be_available": True,
            "api_key_environment_scrubbed": True,
            "ephemeral_session_required": True,
            "read_only_sandbox_required": True,
            "structured_output_binding_required": True,
            "supports_provider_observation": True,
            "provider_observation_source": CODEX_CLI_OBSERVATION_SOURCE,
        }
        if health.get("ok") is not True or any(
            capabilities.get(key) != expected
            for key, expected in required_capabilities.items()
        ):
            raise PracticalSemanticRunnerError("practical_semantic_adapter_capability_invalid")
        return {
            "schema": "spst-practical-semantic-execution-preflight-v1",
            "status": "ready",
            "study_id": study_id,
            "target_pair_count": target,
            "maximum_adapter_invocations": maximum,
            "repository_identity_sha256": current_identity["identity_sha256"],
            "treatment_context_sha256": study["intervention"]["context_sha256"],
            "model_instructions_sha256": study["intervention"][
                "model_instructions_sha256"
            ],
            "provider_calls_executed": 0,
            "state_changed": False,
        }

    def execute(self, study_id: str) -> dict[str, Any]:
        preflight = self.preflight(study_id)
        study = self.ledger.get(study_id)
        assert study is not None
        context_value = json.loads(Path(self.treatment_context_path).read_text(encoding="utf-8"))
        instructions = treatment_instructions(context_value)
        artifact_contract = build_model_artifact_contract(
            ARTIFACT_KEYS,
            {key: "string" for key in ARTIFACT_KEYS},
        )
        tasks = {item["task_id"]: item for item in study["task_pack"]["tasks"]}
        schedule = self.ledger.execution_schedule(study_id)
        calls_executed = 0
        completed_tasks: list[str] = []
        for schedule_item in schedule["items"]:
            task = tasks[schedule_item["task_id"]]
            receipt = self.route_task(
                task["prompt"],
                steps=1,
                event="practical_semantic_paired_study",
                profile="strict",
                session_path=self.session_path,
                memory_path=self.memory_path,
                repository_root=self.repository_root,
            )["routing_receipt"]
            first = schedule_item["first_condition"]
            condition_order = [first, "treatment" if first == "baseline" else "baseline"]
            arms: dict[str, dict[str, Any]] = {}
            for condition in condition_order:
                context: dict[str, Any] = {
                    "artifact_contract": artifact_contract,
                    "evaluation": {
                        "schema": "spst-practical-semantic-call-v1",
                        "study_id": study_id,
                        "task_id": task["task_id"],
                        "condition": condition,
                        "schedule_sha256": schedule["schedule_sha256"],
                    },
                }
                if condition == "treatment":
                    context["instructions"] = instructions
                result = asyncio.run(self.adapter.infer(task["prompt"], context))
                calls_executed += 1
                text = result.get("text")
                observation = result.get("provider_observation")
                if not isinstance(text, str) or not text:
                    raise PracticalSemanticRunnerError("practical_semantic_provider_output_invalid")
                request = build_provider_request_binding(task["prompt"], context)
                valid, reason = verify_provider_observation(
                    observation,
                    request,
                    output_text=text,
                    provider_name=str(result.get("provider") or ""),
                    model_version=str(result.get("model_version") or ""),
                )
                if not valid or not isinstance(observation, dict):
                    raise PracticalSemanticRunnerError(
                        reason or "practical_semantic_provider_observation_invalid"
                    )
                arms[condition] = {
                    "artifact_text": text,
                    "producer_response_id": observation["response"]["response_id_sha256"],
                    "provider_observation": deepcopy(observation),
                }
            recorded = self.ledger.record_pair(
                study_id,
                {
                    "task_id": task["task_id"],
                    "task_contract_sha256": task["task_contract_sha256"],
                    "routing_receipt_id": receipt["receipt_id"],
                    "baseline": arms["baseline"],
                    "treatment": arms["treatment"],
                },
            )
            if recorded.get("status") == "blocked":
                raise PracticalSemanticRunnerError(str(recorded.get("reason")))
            completed_tasks.append(task["task_id"])
        return {
            **preflight,
            "status": "pending_human_review",
            "provider_calls_executed": calls_executed,
            "completed_task_count": len(completed_tasks),
            "completed_task_ids": completed_tasks,
            "optional_stopping_used": False,
            "automatic_retries_used": 0,
            "blind_artifact": self.ledger.blind_artifact(study_id),
        }

    def execute_sync(self, study_id: str) -> dict[str, Any]:
        return self.execute(study_id)
