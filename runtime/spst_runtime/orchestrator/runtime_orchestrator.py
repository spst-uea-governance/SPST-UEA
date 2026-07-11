import asyncio
from copy import deepcopy
from typing import Any

from spst_runtime.bus.event_bus import EventBus
from spst_runtime.events.event import Event
from spst_runtime.engines.autopoiesis_engine import AutopoiesisEngine
from spst_runtime.engines.evidence_ledger import EvidenceLedger
from spst_runtime.engines.goal_engine import GoalEngine
from spst_runtime.evaluation.artifact_outcome import ArtifactOutcomeLedger
from spst_runtime.evaluation.calibration_registry import CalibrationRegistry
from spst_runtime.evaluation.capability_evaluation import CapabilityEvaluationRunner
from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.evaluation.operational_shadow import OperationalShadowRunner
from spst_runtime.interfaces.model_adapter import ModelAdapter
from spst_runtime.engines.verification_runner import VerificationRunner
from spst_runtime.maintenance import run_maintenance
from spst_runtime.managers.goal_manager import GoalManager
from spst_runtime.models.subject_state import SubjectState
from spst_runtime.pipeline.transition_engine import TransitionEngine
from spst_runtime.pipeline.state_transition import StateTransitionPipeline
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.providers.memory_provider import MemoryProvider
from spst_runtime.providers.model_provider import ModelProvider
from spst_runtime.repository.subject_repository import SubjectRepository
from spst_runtime.repository.workspace_orchestrator import WorkspaceOrchestrator

class RuntimeOrchestrator:
    """Fully wired SPST runtime lifecycle orchestrator."""

    SHADOW_INDEX_KEY = "runtime:operational_shadow:index:v1"
    SHADOW_INDEX_SCHEMA_VERSION = "operational-shadow-index-v1"

    def __init__(
        self,
        db_path: str = "spst_runtime_lifecycle.db",
        bus: EventBus | None = None,
        memory_provider: MemoryProvider | None = None,
        model_provider: ModelAdapter | None = None,
        pipeline: StateTransitionPipeline | None = None,
        repository: SQLiteRepository | None = None,
        maintenance_service: Any | None = None,
        subject_repository: SubjectRepository | None = None,
        workspace_orchestrator: WorkspaceOrchestrator | None = None,
        workspace_id: str | None = None,
        verification_runner: VerificationRunner | None = None,
        capability_evaluation_runner: CapabilityEvaluationRunner | None = None,
        calibration_registry: CalibrationRegistry | None = None,
        operational_corpus: OperationalEvaluationCorpus | None = None,
        operational_shadow_runner: OperationalShadowRunner | None = None,
        artifact_outcome_ledger: ArtifactOutcomeLedger | None = None,
    ):
        self.bus = bus or EventBus()
        self.repository = repository or SQLiteRepository(db_path)
        self.memory_provider = memory_provider or MemoryProvider(path=db_path)
        self.model_provider = model_provider or ModelProvider()
        self.subject_repository = subject_repository or SubjectRepository()
        self.workspace_orchestrator = workspace_orchestrator
        self.workspace_id = workspace_id
        self._pending_approvals: dict[str, dict[str, Any]] = {}
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
        self.verification_runner = verification_runner or VerificationRunner(
            self.pipeline.transition_engine.tool_provider,
            self.pipeline.governance_engine,
        )
        self.capability_evaluation_runner = (
            capability_evaluation_runner
            or CapabilityEvaluationRunner(
                self.model_provider,
                governance_engine=self.pipeline.governance_engine,
            )
        )
        self.calibration_registry = calibration_registry or CalibrationRegistry(self.repository)
        self.operational_corpus = operational_corpus or OperationalEvaluationCorpus(
            self.repository,
            governance_engine=self.pipeline.governance_engine,
            security_engine=self.pipeline.transition_engine.security_engine,
        )
        self.operational_shadow_runner = (
            operational_shadow_runner
            or OperationalShadowRunner(
                self.model_provider,
                self.operational_corpus,
                governance_engine=self.pipeline.governance_engine,
            )
        )
        self.artifact_outcome_ledger = artifact_outcome_ledger or ArtifactOutcomeLedger(
            self.repository,
            self.operational_corpus,
            self.verification_runner,
            governance_engine=self.pipeline.governance_engine,
        )

    def create_subject(
        self,
        subject_id: str,
        *,
        role: str,
        goals: list[dict[str, Any]] | None = None,
        workspace_id: str | None = None,
    ) -> SubjectState:
        return self.subject_repository.create(
            subject_id,
            role=role,
            goals=goals,
            workspace_id=workspace_id or self.workspace_id,
        )

    def create_security_subject(self, workspace_id: str | None = None) -> SubjectState:
        target_workspace = workspace_id or self.workspace_id
        subject_id = f"security:{target_workspace or 'local'}"
        return self.create_subject(
            subject_id,
            role="SecuritySubject",
            workspace_id=target_workspace,
        )

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
        event = self._prepare_capability_evaluation(state, event)
        event = self._prepare_verified_quality_gate(state, event)
        if getattr(event, "type", None) == "system_tick":
            state.metadata["autonomous"] = True
        state.metadata["state_provenance"] = self.repository.verify_provenance()
        self._retrieve(state, event)
        result = self.pipeline.run(state, event)
        self._compact_runtime_metadata(result)
        if result.metadata.get("governance_pending"):
            self._register_pending_approval(state, event, result)
            self._persist_evidence(result)
            return result
        self._act(result, event)
        self._commit(result, event)
        return result

    def _prepare_capability_evaluation(self, state: SubjectState, event: Event) -> Event:
        payload = getattr(event, "payload", {}) or {}
        if not payload.get("evaluate_capability"):
            return event
        evaluation = self.capability_evaluation_runner.run()
        candidate_value = payload.get("calibration_candidate")
        baseline_value = payload.get("calibration_baseline_id")
        calibration = self.calibration_registry.register(
            evaluation,
            candidate_id=candidate_value if isinstance(candidate_value, str) else None,
            baseline_id=baseline_value if isinstance(baseline_value, str) else None,
        )
        state.metadata["capability_evaluation"] = evaluation
        state.metadata["calibration_registry"] = calibration
        return Event(
            type=event.type,
            payload={
                **payload,
                "evaluation_report": evaluation,
                "calibration_registry": calibration,
            },
        )

    def _prepare_verified_quality_gate(self, state: SubjectState, event: Event) -> Event:
        payload = getattr(event, "payload", {}) or {}
        if not payload.get("verified_quality_gate"):
            return event
        verification_run = self.verification_runner.run()
        state.metadata["verification_run"] = verification_run
        return Event(
            type=event.type,
            payload={
                **payload,
                "requires_quality_gate": True,
                "verification": verification_run.get("verification", {}),
                "verification_run": verification_run,
            },
        )

    def register_operational_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Register a locally consented task without exposing its prompt in cockpit data."""
        return self.operational_corpus.register(task)

    def run_operational_shadow(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run a non-mutating L0 shadow evaluation and persist compact audit evidence."""
        candidate_value = payload.get("candidate_id")
        baseline_value = payload.get("baseline_id")
        split_value = payload.get("split")
        task_values = payload.get("task_ids")
        task_ids = (
            [value for value in task_values if isinstance(value, str)]
            if isinstance(task_values, list)
            else None
        )
        report = self.operational_shadow_runner.run(
            candidate_id=candidate_value if isinstance(candidate_value, str) else None,
            split=split_value if isinstance(split_value, str) else "holdout",
            task_ids=task_ids,
        )
        governance = report.get("governance", {})
        if isinstance(governance, dict) and governance.get("authorized", False):
            calibration = self.calibration_registry.register(
                report,
            candidate_id=candidate_value if isinstance(candidate_value, str) else None,
            baseline_id=(
                baseline_value
                if isinstance(baseline_value, str)
                and baseline_value.startswith("CAL-")
                and len(baseline_value) == 20
                else None
            ),
            )
            report["calibration"] = calibration
            requires_human_approval = bool(
                calibration.get("policy", {}).get("requires_human_approval", False)
            )
            report["promotion"] = {
                "status": (
                    "held_for_human_review"
                    if requires_human_approval
                    else "shadow_only_not_promoted"
                ),
                "automatic_adoption": False,
                "requires_human_approval": requires_human_approval,
            }
        else:
            report["calibration"] = {}
            report["promotion"] = {
                "status": "blocked",
                "automatic_adoption": False,
                "requires_human_approval": False,
            }
        self._record_operational_shadow(report)
        return report

    def operational_shadow_history(self) -> list[dict[str, Any]]:
        """Return compact shadow audit history without raw task content or model text."""
        with self.repository.locked():
            stored = asyncio.run(self.repository.load(self.SHADOW_INDEX_KEY))
        if not isinstance(stored, dict):
            return []
        records = stored.get("records", [])
        if not isinstance(records, list):
            return []
        compact_records = [record for record in records if isinstance(record, dict)]
        ordered = sorted(compact_records, key=lambda record: int(record.get("sequence", 0)))
        return deepcopy(ordered)

    def record_artifact_outcome(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record evidence-only task outcomes without changing the subject runtime state."""
        return self.artifact_outcome_ledger.record(payload)

    def artifact_outcomes(self) -> dict[str, Any]:
        """Return compact artifact outcome history and coverage for cockpit review."""
        records = self.artifact_outcome_ledger.history()
        return {
            "latest": records[-1] if records else {},
            "records": records,
            "coverage": self.artifact_outcome_ledger.coverage(),
        }

    def _record_operational_shadow(self, report: dict[str, Any]) -> None:
        report_id = report.get("id")
        if not isinstance(report_id, str) or not report_id:
            return
        with self.repository.locked():
            stored = asyncio.run(self.repository.load(self.SHADOW_INDEX_KEY))
            if not isinstance(stored, dict) or not isinstance(stored.get("records"), list):
                stored = {
                    "schema_version": self.SHADOW_INDEX_SCHEMA_VERSION,
                    "records": [],
                }
            records = [record for record in stored["records"] if isinstance(record, dict)]
            existing = next((record for record in records if record.get("id") == report_id), None)
            if existing is None:
                summary = self._operational_shadow_summary(report, len(records) + 1)
                updated_index = {
                    "schema_version": self.SHADOW_INDEX_SCHEMA_VERSION,
                    "records": [*records, summary],
                }
                asyncio.run(
                    self.repository.save(f"runtime:operational_shadow:{report_id}", report)
                )
                asyncio.run(self.repository.save(self.SHADOW_INDEX_KEY, updated_index))
                persistence_status = "stored"
            else:
                persistence_status = "already_recorded"
        provenance = self.repository.verify_provenance()
        report["persistence"] = {
            "key": f"runtime:operational_shadow:{report_id}",
            "status": persistence_status,
            "provenance_valid": provenance.get("valid", False),
        }
        report["state_provenance"] = {
            key: provenance.get(key)
            for key in ("valid", "entries", "latest_hash", "key_source")
            if key in provenance
        }

    @staticmethod
    def _operational_shadow_summary(report: dict[str, Any], sequence: int) -> dict[str, Any]:
        suite = report.get("suite", {})
        calibration = report.get("calibration", {})
        comparison = calibration.get("comparison", {}) if isinstance(calibration, dict) else {}
        promotion = report.get("promotion", {})
        return {
            "id": report.get("id"),
            "sequence": sequence,
            "candidate_id": report.get("candidate_id"),
            "status": report.get("status"),
            "suite_hash": suite.get("hash") if isinstance(suite, dict) else None,
            "eligible_count": suite.get("eligible_count") if isinstance(suite, dict) else None,
            "calibration_id": calibration.get("id") if isinstance(calibration, dict) else None,
            "comparison_status": comparison.get("status") if isinstance(comparison, dict) else None,
            "promotion_status": promotion.get("status") if isinstance(promotion, dict) else None,
        }

    def _retrieve(self, state: SubjectState, event: Event) -> None:
        payload = getattr(event, "payload", {}) or {}
        contexts = []
        memory_key = payload.get("memory_key")
        if memory_key:
            record = self.memory_provider.retrieve(memory_key)
            if record:
                contexts.append(self._compact_context_record(record))

        prompt = payload.get("prompt")
        if not prompt and getattr(event, "type", None) == "system_tick":
            prompt = " ".join(
                goal.get("description", "")
                for goal in state.metadata.get("goals", [])
                if goal.get("status", "pending") in {"pending", "in_progress"}
            )
        if prompt:
            for record in self.memory_provider.search(prompt, top_k=5):
                compact = self._compact_context_record(record)
                if compact not in contexts:
                    contexts.append(compact)

        if prompt and self.workspace_orchestrator is not None and self.workspace_id:
            workspace_context = self.workspace_orchestrator.retrieve_context(
                self.workspace_id,
                prompt,
                top_k=5,
            )
            state.metadata["workspace_context"] = workspace_context
            for record in workspace_context:
                compact = self._compact_context_record(record)
                if compact not in contexts:
                    contexts.append(compact)

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
                "model_inference": self._compact_model_inference(
                    state.metadata.get("model_inference", {})
                ),
            },
            {
                "phase": "act",
                "event_type": getattr(event, "type", None),
                "version": version,
            },
        )
        if state.metadata.get("dynamic_tool_result"):
            state.metadata["dynamic_tool_memory"] = self.memory_provider.store(
                f"dynamic_tool:{version}",
                {
                    "prompt": prompt,
                    "text": (
                        "Dynamic tool solved unknown frontier task with deterministic verifier "
                        "and local sandbox validation."
                    ),
                    "trace": state.metadata.get("last_trace", []),
                    "tool_result": state.metadata["dynamic_tool_result"],
                },
                {
                    "phase": "act",
                    "event_type": getattr(event, "type", None),
                    "version": version,
                    "capability": "dynamic_tool_genesis",
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
        if state.metadata.get("mcp_request"):
            tool_provider = self.pipeline.transition_engine.tool_provider
            state.metadata["mcp_result"] = tool_provider.handle_mcp(
                state.metadata["mcp_request"],
                governance_authorized=bool(state.metadata.get("governance", {}).get("authorized")),
            )

    def _commit(self, state: SubjectState, event: Event) -> None:
        version = state.metadata.get("version", 0)
        if state.metadata.get("dynamic_tool_result"):
            state.metadata["rule_crystals"] = self.memory_provider.crystallize_rules(top_k=5)
        if state.metadata.get("auto_immunity"):
            registered = self.pipeline.governance_engine.register_immunity_rule(
                state.metadata["auto_immunity"]["governance_rule"]
            )
            state.metadata["auto_immunity"]["governance_rule"] = registered
        if event.type == "system_tick":
            state.metadata["maintenance"] = self.maintenance_service()
        else:
            state.metadata["maintenance"] = {
                "status": "deferred",
                "reason": "non_autonomous_event",
            }
        self.goal_manager.persist(state.metadata.get("goals", []))
        serialized_state = self._serialize_state(state)
        asyncio.run(self.repository.save("runtime:subject_main", serialized_state))
        subject_id = state.metadata.get("subject_id")
        if subject_id:
            asyncio.run(
                self.repository.save(
                    f"runtime:subject:{subject_id}",
                    serialized_state,
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
                    "evidence": state.metadata.get("evidence", {}),
                    "decision_explanation": state.metadata.get(
                        "decision_explanation",
                        {},
                    ),
                    "verification_run": state.metadata.get("verification_run", {}),
                    "capability_evaluation": state.metadata.get(
                        "capability_evaluation",
                        {},
                    ),
                    "calibration_registry": state.metadata.get(
                        "calibration_registry",
                        {},
                    ),
                },
            )
        )
        evaluation = state.metadata.get("capability_evaluation", {})
        if isinstance(evaluation, dict) and evaluation.get("id"):
            asyncio.run(
                self.repository.save(
                    f"runtime:evaluation:{evaluation['id']}",
                    evaluation,
                )
            )
        state.metadata["state_provenance"] = self.repository.verify_provenance()
        self._persist_evidence(state)

    def pending_approvals(self) -> list[dict[str, Any]]:
        return [
            {
                "approval_id": approval_id,
                "diff": record["candidate"].metadata.get("governance", {}).get("diff", {}),
                "trust_level": record["candidate"].metadata.get("governance", {}).get(
                    "trust_level"
                ),
                "reasons": record["candidate"].metadata.get("governance", {}).get("reasons", []),
                "evidence_id": record["candidate"].metadata.get("evidence", {}).get("id"),
            }
            for approval_id, record in sorted(self._pending_approvals.items())
        ]

    def resolve_approval(self, approval_id: str, *, approved: bool) -> SubjectState:
        try:
            record = self._pending_approvals.pop(approval_id)
        except KeyError as exc:
            raise KeyError(f"Unknown approval request: {approval_id}") from exc
        candidate = record["candidate"]
        if not approved:
            candidate.metadata["governance"] = {
                **candidate.metadata.get("governance", {}),
                "authorized": False,
                "status": "rejected_by_human",
            }
            self._refresh_decision_evidence(candidate)
            self._persist_evidence(candidate)
            return candidate
        event = record["event"]
        approved_event = Event(
            type=event.type,
            payload={**event.payload, "human_approved": True, "approval_id": approval_id},
        )
        return self.dispatch(record["state"], approved_event)

    def _register_pending_approval(
        self,
        state: SubjectState,
        event: Event,
        candidate: SubjectState,
    ) -> None:
        governance = candidate.metadata.get("governance", {})
        approval_id = str(governance["approval_id"])
        self._pending_approvals[approval_id] = {
            "state": deepcopy(state),
            "event": deepcopy(event),
            "candidate": candidate,
        }
        candidate.metadata["pending_approval"] = {
            "approval_id": approval_id,
            "diff": governance.get("diff", {}),
            "trust_level": governance.get("trust_level"),
        }
        asyncio.run(
            self.repository.save(
                f"runtime:hitl:pending:{approval_id}",
                {
                    "approval_id": approval_id,
                    "diff": governance.get("diff", {}),
                    "reasons": governance.get("reasons", []),
                    "event_type": event.type,
                    "evidence": candidate.metadata.get("evidence", {}),
                    "decision_explanation": candidate.metadata.get(
                        "decision_explanation",
                        {},
                    ),
                    "verification_run": candidate.metadata.get("verification_run", {}),
                    "capability_evaluation": candidate.metadata.get(
                        "capability_evaluation",
                        {},
                    ),
                    "calibration_registry": candidate.metadata.get(
                        "calibration_registry",
                        {},
                    ),
                },
            )
        )
        candidate.metadata["state_provenance"] = self.repository.verify_provenance()

    def _persist_evidence(self, state: SubjectState) -> None:
        evidence = state.metadata.get("evidence", {})
        if not isinstance(evidence, dict) or not evidence.get("id"):
            return
        ledger = getattr(self.pipeline, "evidence_ledger", None)
        if not isinstance(ledger, EvidenceLedger):
            return
        evidence_key = f"runtime:evidence:{evidence['id']}"
        finalized = ledger.finalize(
            evidence,
            state.metadata.get("state_provenance", {}),
        )
        finalized["persistence"] = {
            "key": evidence_key,
            "status": "stored",
        }
        state.metadata["evidence"] = finalized
        asyncio.run(self.repository.save(evidence_key, finalized))
        state.metadata["state_provenance"] = self.repository.verify_provenance()
        finalized["persistence"]["provenance_valid"] = state.metadata[
            "state_provenance"
        ].get("valid", False)

    def _refresh_decision_evidence(self, state: SubjectState) -> None:
        evidence = state.metadata.get("evidence", {})
        decision = state.metadata.get("governance", {})
        ledger = getattr(self.pipeline, "evidence_ledger", None)
        explainer = getattr(self.pipeline, "decision_explainer", None)
        if not isinstance(evidence, dict) or not isinstance(decision, dict):
            return
        if isinstance(ledger, EvidenceLedger):
            ledger.attach_decision(evidence, decision)
        if explainer is not None:
            state.metadata["decision_explanation"] = explainer.explain(decision, evidence)

    def _serialize_state(self, state: SubjectState) -> dict[str, Any]:
        return {
            "identity": self._json_safe(state.identity),
            "goals": self._json_safe(state.goals),
            "memory_refs": list(state.memory_refs),
            "governance": self._json_safe(state.governance),
            "metadata": self._compact_metadata(state.metadata),
        }

    def _compact_runtime_metadata(self, state: SubjectState) -> None:
        if "retrieved_context" in state.metadata:
            state.metadata["retrieved_context"] = [
                self._compact_context_record(item)
                for item in state.metadata.get("retrieved_context", [])
            ][:5]
        if "workspace_context" in state.metadata:
            state.metadata["workspace_context"] = [
                self._compact_context_record(item)
                for item in state.metadata.get("workspace_context", [])
            ][:5]
        if "model_inference" in state.metadata:
            state.metadata["model_inference"] = self._compact_model_inference(
                state.metadata.get("model_inference", {})
            )

    def _compact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        compacted: dict[str, Any] = {}
        for key, value in metadata.items():
            if key == "retrieved_context":
                compacted[key] = [
                    self._compact_context_record(item)
                    for item in (value if isinstance(value, list) else [])
                ][:5]
            elif key == "workspace_context":
                compacted[key] = [
                    self._compact_context_record(item)
                    for item in (value if isinstance(value, list) else [])
                ][:5]
            elif key == "model_inference":
                compacted[key] = self._compact_model_inference(value)
            elif key in {"autopoiesis_memory", "dynamic_tool_memory", "dynamic_tool_result"}:
                compacted[key] = self._compact_context_record(value)
            else:
                compacted[key] = self._json_safe(value)
        return compacted

    def _compact_model_inference(self, inference: Any) -> dict[str, Any]:
        if not isinstance(inference, dict):
            return {}
        context = inference.get("context", {})
        if not isinstance(context, dict) or "retrieved_context_count" not in context:
            context = self._context_summary(context)
        return {
            key: self._json_safe(value)
            for key, value in inference.items()
            if key not in {"context", "context_summary"}
        } | {
            "context": context,
            "context_summary": context,
        }

    def _compact_context_record(self, record: Any) -> dict[str, Any]:
        if not isinstance(record, dict):
            return {"text": str(record)[:500]}

        raw_value = record.get("value")
        value: dict[str, Any] = raw_value if isinstance(raw_value, dict) else {}
        raw_metadata = record.get("metadata")
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        text = (
            record.get("text")
            or value.get("text")
            or value.get("prompt")
            or value.get("summary")
            or ""
        )
        compacted: dict[str, Any] = {
            "text": str(text)[:500],
            "metadata": self._compact_mapping(metadata),
        }
        for key in ("key", "id", "kind", "source", "salience", "score", "provenance"):
            if key in record:
                compacted[key] = self._json_safe(record[key])
        if "tags" in record:
            compacted["tags"] = self._json_safe(record.get("tags", [])[:8])
        return {key: value for key, value in compacted.items() if value not in ({}, [], "")}

    def _context_summary(self, context: Any) -> dict[str, Any]:
        retrieved = context.get("retrieved_context", []) if isinstance(context, dict) else []
        retrieved_list = retrieved if isinstance(retrieved, list) else []
        capability = context.get("capability_maximization", {}) if isinstance(context, dict) else {}
        capability_strategies = (
            capability.get("strategies", []) if isinstance(capability, dict) else []
        )
        keys = []
        for item in retrieved_list[:5]:
            if isinstance(item, dict):
                keys.append(str(item.get("key") or item.get("id") or "context"))
            else:
                keys.append("context")
        return {
            "has_instructions": (
                bool(context.get("instructions")) if isinstance(context, dict) else False
            ),
            "retrieved_context_count": len(retrieved_list),
            "retrieved_keys": keys,
            "capability_mode": (
                capability.get("mode") if isinstance(capability, dict) else None
            ),
            "strategy_count": (
                len(capability_strategies) if isinstance(capability_strategies, list) else 0
            ),
        }

    def _compact_mapping(self, mapping: dict[str, Any]) -> dict[str, Any]:
        blocked = {"context", "retrieved_context", "model_inference", "tool_result"}
        return {
            key: self._json_safe(value)
            for key, value in mapping.items()
            if key not in blocked
        }

    def _json_safe(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): self._json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._json_safe(item) for item in value]
        if isinstance(value, tuple):
            return [self._json_safe(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)
