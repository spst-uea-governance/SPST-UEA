import asyncio
from copy import deepcopy
import hashlib
import json
import re
from typing import Any

from spst_runtime.engines.governance_engine import GovernanceEngine
from spst_runtime.evaluation.artifact_outcome import ArtifactOutcomeLedger
from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.persistence.sqlite_repository import SQLiteRepository


class LongitudinalPromotionGovernance:
    """Synthesize local artifact evidence into reversible shadow-only proposals."""

    SCHEMA_VERSION = "longitudinal-promotion-governance-v1"
    INDEX_KEY = "runtime:longitudinal_promotion:index:v1"
    RECORD_KEY_PREFIX = "runtime:longitudinal_promotion:record:"
    MINIMUM_PAIRED_TASKS = 3
    MINIMUM_MEAN_DELTA = 0.05
    MAXIMUM_VARIANCE = 0.25
    _identifier_pattern = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
    _allowed_strategies = {
        "constraint_extraction",
        "context_compression",
        "ia_task_alignment",
        "prompt_contract",
        "spec_decomposition",
        "verifier_loop",
    }
    _terminal_statuses = {"verified_accepted", "rejected_by_human"}

    def __init__(
        self,
        repository: SQLiteRepository,
        corpus: OperationalEvaluationCorpus,
        artifact_outcome_ledger: ArtifactOutcomeLedger,
        governance_engine: GovernanceEngine | None = None,
    ):
        self.repository = repository
        self.corpus = corpus
        self.artifact_outcome_ledger = artifact_outcome_ledger
        self.governance_engine = governance_engine or GovernanceEngine()

    def propose(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create an evidence-only proposal; it never activates a runtime policy."""
        normalized = self._normalize_proposal(payload)
        provenance = self.repository.verify_provenance()
        evidence = self._synthesize(normalized, provenance_valid=bool(provenance.get("valid")))
        governance = self._synthesis_governance(normalized, evidence, provenance)
        status = self._proposal_status(evidence, governance)
        record = {
            "schema_version": self.SCHEMA_VERSION,
            "kind": "proposal",
            "initial_status": status,
            "candidate_id": normalized["candidate_id"],
            "baseline_candidate_id": normalized["baseline_candidate_id"],
            "policy": normalized["policy"],
            "evidence": evidence,
            "promotion": self._promotion(status),
            "claims": {
                "task_quality_uplift_claimed": False,
                "official_benchmark_claimed": False,
                "model_weight_score_unchanged": True,
                "automatic_adoption": False,
                "requires_human_interpretation": True,
            },
            "governance": self._compact_governance(governance),
        }
        record["id"] = self._proposal_id(record)
        if not provenance.get("valid", False):
            return self._with_provenance(record, provenance, "not_persisted")
        return self._persist_proposal(record)

    def resolve(self, promotion_id: str, *, approved: bool) -> dict[str, Any]:
        """Record an explicit human promotion decision without mutating subject state."""
        current = self.get(promotion_id)
        if current is None:
            return self._not_found(promotion_id)
        if current.get("status") != "held_for_human_review":
            return self._operation_blocked(current, "promotion_not_pending")
        provenance = self.repository.verify_provenance()
        governance = self._activation_governance(current, approved, provenance)
        event = {
            "kind": "approval",
            "promotion_id": promotion_id,
            "approved": bool(approved),
            "governance": self._compact_governance(governance),
        }
        event["id"] = self._event_id(event)
        if not provenance.get("valid", False) or not governance.get("authorized", False):
            result = self._operation_blocked(current, "promotion_activation_not_authorized")
            result["governance"] = self._compact_governance(governance)
            return self._with_provenance(result, provenance, "not_persisted")
        return self._append_event(event)

    def rollback(self, promotion_id: str, *, approved: bool) -> dict[str, Any]:
        """Record a human-authorized reversal of an active shadow-only promotion."""
        current = self.get(promotion_id)
        if current is None:
            return self._not_found(promotion_id)
        if current.get("status") != "shadow_active":
            return self._operation_blocked(current, "promotion_not_active")
        provenance = self.repository.verify_provenance()
        governance = self._rollback_governance(current, approved, provenance)
        event = {
            "kind": "rollback",
            "promotion_id": promotion_id,
            "approved": bool(approved),
            "governance": self._compact_governance(governance),
        }
        event["id"] = self._event_id(event)
        if not provenance.get("valid", False) or not governance.get("authorized", False):
            result = self._operation_blocked(current, "promotion_rollback_not_authorized")
            result["governance"] = self._compact_governance(governance)
            return self._with_provenance(result, provenance, "not_persisted")
        return self._append_event(event)

    def get(self, promotion_id: str) -> dict[str, Any] | None:
        """Return the current projection of one append-only promotion ledger."""
        with self.repository.locked():
            records = self._records(self._load_index())
        proposal = next(
            (
                record
                for record in records
                if record.get("kind") == "proposal" and record.get("id") == promotion_id
            ),
            None,
        )
        if proposal is None:
            return None
        return self._projection(proposal, records)

    def history(self) -> list[dict[str, Any]]:
        """Return current projections while preserving append-only decision events."""
        with self.repository.locked():
            records = self._records(self._load_index())
        proposals = [record for record in records if record.get("kind") == "proposal"]
        ordered = sorted(proposals, key=lambda record: int(record.get("sequence", 0)))
        return [self._projection(proposal, records) for proposal in ordered]

    def coverage(self) -> dict[str, Any]:
        """Summarize proposal state without exposing corpus prompts or review prose."""
        records = self.history()
        by_status: dict[str, int] = {}
        for record in records:
            status = str(record.get("status") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
        return {
            "schema_version": self.SCHEMA_VERSION,
            "total_count": len(records),
            "active_count": by_status.get("shadow_active", 0),
            "eligible_count": sum(
                record.get("promotion", {}).get("eligible") is True for record in records
            ),
            "by_status": by_status,
        }

    def _synthesize(
        self,
        normalized: dict[str, Any],
        *,
        provenance_valid: bool,
    ) -> dict[str, Any]:
        all_records = self.artifact_outcome_ledger.history()
        candidate_records = [
            record
            for record in all_records
            if record.get("candidate_id") == normalized["candidate_id"]
        ]
        baseline_records = [
            record
            for record in all_records
            if record.get("candidate_id") == normalized["baseline_candidate_id"]
        ]
        sources = [*candidate_records, *baseline_records]
        source_outcomes = self._count_statuses(sources)
        candidate_by_task, candidate_duplicates = self._unique_by_task(candidate_records)
        baseline_by_task, baseline_duplicates = self._unique_by_task(baseline_records)
        candidate_ids = set(candidate_by_task)
        baseline_ids = set(baseline_by_task)
        common_ids = sorted(candidate_ids & baseline_ids)
        unpaired_ids = sorted((candidate_ids ^ baseline_ids))
        expired_ids: list[str] = []
        invalid_ids: list[str] = []
        indistinct_ids: list[str] = []
        unresolved_semantic_ids: list[str] = []
        paired_task_ids: list[str] = []
        paired_deltas: list[float] = []
        candidate_acceptances: list[float] = []
        baseline_acceptances: list[float] = []
        binding_uses: dict[str, int] = {}

        for record in sources:
            binding_id = self._mapping(record.get("producer_evidence")).get("binding_id")
            if isinstance(binding_id, str) and binding_id:
                binding_uses[binding_id] = binding_uses.get(binding_id, 0) + 1
        reused_binding_ids = sorted(
            binding_id for binding_id, count in binding_uses.items() if count > 1
        )

        for record in sources:
            task_id = record.get("task_id")
            if not isinstance(task_id, str) or self.corpus.task_contract(task_id) is None:
                expired_ids.append(str(task_id or "unknown"))
            elif not self._valid_terminal_record(record):
                invalid_ids.append(task_id)

        for task_id in common_ids:
            candidate = candidate_by_task[task_id]
            baseline = baseline_by_task[task_id]
            if not self._valid_terminal_record(candidate) or not self._valid_terminal_record(
                baseline
            ):
                continue
            if self.corpus.task_contract(task_id) is None:
                continue
            candidate_binding = self._mapping(candidate.get("producer_evidence"))
            baseline_binding = self._mapping(baseline.get("producer_evidence"))
            if (
                candidate_binding.get("binding_id") in reused_binding_ids
                or baseline_binding.get("binding_id") in reused_binding_ids
            ):
                continue
            distinctness = self._producer_evidence_distinct(
                candidate_binding,
                baseline_binding,
            )
            if distinctness == "unresolved":
                unresolved_semantic_ids.append(task_id)
                continue
            if distinctness == "indistinct":
                indistinct_ids.append(task_id)
                continue
            candidate_value = self._acceptance_value(candidate)
            baseline_value = self._acceptance_value(baseline)
            paired_task_ids.append(task_id)
            candidate_acceptances.append(candidate_value)
            baseline_acceptances.append(baseline_value)
            paired_deltas.append(round(candidate_value - baseline_value, 6))

        sample_count = len(paired_deltas)
        mean_delta = self._mean(paired_deltas) if paired_deltas else None
        variance = self._variance(paired_deltas, mean_delta) if mean_delta is not None else None
        reasons: list[str] = []
        if not provenance_valid:
            reasons.append("provenance_invalid")
        if not normalized["valid"]:
            reasons.extend(normalized["errors"])
        if not candidate_records or not baseline_records:
            reasons.append("candidate_and_baseline_evidence_required")
        if candidate_duplicates or baseline_duplicates:
            reasons.append("ambiguous_task_evidence")
        if unpaired_ids:
            reasons.append("unpaired_task_evidence")
        if expired_ids:
            reasons.append("expired_or_missing_task_consent")
        if invalid_ids:
            reasons.append("invalid_artifact_outcome")
        if reused_binding_ids:
            reasons.append("producer_evidence_reused")
        if indistinct_ids:
            reasons.append("candidate_evidence_not_distinct")
        if unresolved_semantic_ids:
            reasons.append("semantic_distinctness_unresolved")
        if sample_count < self.MINIMUM_PAIRED_TASKS:
            reasons.append("minimum_paired_tasks_not_met")
        if variance is not None and variance > self.MAXIMUM_VARIANCE:
            reasons.append("variance_exceeds_bound")
        if mean_delta is not None and mean_delta < self.MINIMUM_MEAN_DELTA:
            reasons.append("candidate_not_better_than_baseline")

        blocked = not provenance_valid or not normalized["valid"]
        evidence_status = (
            "blocked"
            if blocked
            else "sufficient_for_shadow"
            if not reasons
            else "insufficient_evidence"
        )
        return {
            "status": evidence_status,
            "reasons": self._dedupe(reasons),
            "source_outcomes": source_outcomes,
            "source": {
                "candidate_record_count": len(candidate_records),
                "baseline_record_count": len(baseline_records),
                "total_record_count": len(sources),
                "expired_or_missing_consent_count": len(expired_ids),
                "invalid_record_count": len(invalid_ids),
                "candidate_duplicate_task_ids": sorted(candidate_duplicates),
                "baseline_duplicate_task_ids": sorted(baseline_duplicates),
                "unpaired_task_ids": unpaired_ids,
                "common_task_ids": common_ids,
                "indistinct_task_ids": sorted(indistinct_ids),
                "semantic_distinctness_unresolved_task_ids": sorted(
                    unresolved_semantic_ids
                ),
                "reused_binding_ids": reused_binding_ids,
            },
            "paired": {
                "task_ids": paired_task_ids,
                "sample_count": sample_count,
                "candidate_acceptance_rate": self._mean(candidate_acceptances),
                "baseline_acceptance_rate": self._mean(baseline_acceptances),
                "mean_delta": mean_delta,
                "variance": variance,
                "minimum_paired_tasks": self.MINIMUM_PAIRED_TASKS,
                "minimum_mean_delta": self.MINIMUM_MEAN_DELTA,
                "maximum_variance": self.MAXIMUM_VARIANCE,
            },
            "provenance_valid": provenance_valid,
            "metric_scope": "verified_artifact_acceptance_delta_not_task_quality",
        }

    def _synthesis_governance(
        self,
        normalized: dict[str, Any],
        evidence: dict[str, Any],
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        return self.governance_engine.decide(
            {
                "type": "longitudinal_evidence_synthesis",
                "event_type": "longitudinal_evidence_synthesis",
                "source": "longitudinal_promotion_governance",
                "provenance_scope": "sqlite",
                "provenance_verified": bool(provenance.get("valid")),
                "payload": {
                    "local_only": True,
                    "evidence_only": True,
                    "policy_contract_valid": normalized["valid"],
                    "candidate_baseline_distinct": (
                        normalized["candidate_id"] != normalized["baseline_candidate_id"]
                    ),
                    "provenance_valid": bool(provenance.get("valid")),
                    "evidence_status": evidence.get("status"),
                },
                "security_assessment": {"blocked": False, "findings": []},
            }
        )

    def _activation_governance(
        self,
        current: dict[str, Any],
        approved: bool,
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        return self.governance_engine.decide(
            {
                "type": "longitudinal_promotion_activation",
                "event_type": "longitudinal_promotion_activation",
                "source": "longitudinal_promotion_governance",
                "provenance_scope": "sqlite",
                "provenance_verified": bool(provenance.get("valid")),
                "payload": {
                    "local_only": True,
                    "reversible": True,
                    "shadow_only": True,
                    "promotion_eligible": bool(
                        current.get("promotion", {}).get("eligible", False)
                    ),
                    "promotion_status": current.get("status"),
                    "provenance_valid": bool(provenance.get("valid")),
                    "human_decision_recorded": True,
                    "human_approved": bool(approved),
                },
                "security_assessment": {"blocked": False, "findings": []},
            }
        )

    def _rollback_governance(
        self,
        current: dict[str, Any],
        approved: bool,
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        return self.governance_engine.decide(
            {
                "type": "longitudinal_promotion_rollback",
                "event_type": "longitudinal_promotion_rollback",
                "source": "longitudinal_promotion_governance",
                "provenance_scope": "sqlite",
                "provenance_verified": bool(provenance.get("valid")),
                "payload": {
                    "local_only": True,
                    "reversible": True,
                    "shadow_only": True,
                    "promotion_active": current.get("status") == "shadow_active",
                    "provenance_valid": bool(provenance.get("valid")),
                    "human_decision_recorded": True,
                    "human_approved": bool(approved),
                },
                "security_assessment": {"blocked": False, "findings": []},
            }
        )

    def _proposal_status(self, evidence: dict[str, Any], governance: dict[str, Any]) -> str:
        if not governance.get("authorized", False) or evidence.get("status") == "blocked":
            return "blocked"
        if evidence.get("status") == "sufficient_for_shadow":
            return "held_for_human_review"
        return "insufficient_evidence"

    def _promotion(self, status: str) -> dict[str, Any]:
        eligible = status == "held_for_human_review"
        return {
            "status": status,
            "eligible": eligible,
            "active": False,
            "scope": "shadow_only",
            "reversible": True,
            "automatic_adoption": False,
            "requires_human_approval": eligible,
        }

    def _persist_proposal(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.repository.locked():
            index = self._load_index()
            records = self._records(index)
            existing = next(
                (
                    item
                    for item in records
                    if item.get("kind") == "proposal" and item.get("id") == record["id"]
                ),
                None,
            )
            if existing is None:
                stored = {**record, "sequence": len(records) + 1}
                updated = {
                    "schema_version": self.SCHEMA_VERSION,
                    "records": [*records, stored],
                }
                asyncio.run(
                    self.repository.save(f"{self.RECORD_KEY_PREFIX}{stored['id']}", stored)
                )
                asyncio.run(self.repository.save(self.INDEX_KEY, updated))
            else:
                stored = existing
        projection = self.get(str(stored["id"])) or deepcopy(stored)
        provenance = self.repository.verify_provenance()
        return self._with_provenance(projection, provenance, "stored")

    def _append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        with self.repository.locked():
            index = self._load_index()
            records = self._records(index)
            existing = next(
                (item for item in records if item.get("id") == event["id"]),
                None,
            )
            if existing is None:
                persistence_status = "stored"
                stored = {**event, "sequence": len(records) + 1}
                updated = {
                    "schema_version": self.SCHEMA_VERSION,
                    "records": [*records, stored],
                }
                asyncio.run(
                    self.repository.save(f"{self.RECORD_KEY_PREFIX}{stored['id']}", stored)
                )
                asyncio.run(self.repository.save(self.INDEX_KEY, updated))
            else:
                persistence_status = "already_recorded"
                stored = existing
        projection = self.get(str(event["promotion_id"]))
        if projection is None:
            return self._not_found(str(event["promotion_id"]))
        provenance = self.repository.verify_provenance()
        return self._with_provenance(projection, provenance, persistence_status)

    def _projection(
        self,
        proposal: dict[str, Any],
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        view = deepcopy(proposal)
        events = sorted(
            [
                record
                for record in records
                if record.get("promotion_id") == proposal.get("id")
                and record.get("kind") in {"approval", "rollback"}
            ],
            key=lambda record: int(record.get("sequence", 0)),
        )
        status = str(proposal.get("initial_status") or "blocked")
        for event in events:
            governance = self._mapping(event.get("governance"))
            if not governance.get("authorized", False):
                continue
            if event.get("kind") == "approval":
                status = "shadow_active" if event.get("approved") else "rejected_by_human"
            elif event.get("kind") == "rollback" and status == "shadow_active":
                status = "rolled_back"
        promotion = self._mapping(view.get("promotion"))
        view["status"] = status
        view["promotion"] = {
            **promotion,
            "status": status,
            "active": status == "shadow_active",
            "requires_human_approval": status == "held_for_human_review",
        }
        view["events"] = [
            {
                "id": event.get("id"),
                "kind": event.get("kind"),
                "approved": bool(event.get("approved")),
                "governance": self._mapping(event.get("governance")),
            }
            for event in events
        ]
        return view

    def _load_index(self) -> dict[str, Any]:
        stored = asyncio.run(self.repository.load(self.INDEX_KEY))
        if not isinstance(stored, dict):
            return {"schema_version": self.SCHEMA_VERSION, "records": []}
        records = stored.get("records")
        if stored.get("schema_version") != self.SCHEMA_VERSION or not isinstance(records, list):
            raise ValueError("Invalid longitudinal promotion index")
        return stored

    @staticmethod
    def _records(index: dict[str, Any]) -> list[dict[str, Any]]:
        records = index.get("records", [])
        return [record for record in records if isinstance(record, dict)]

    def _normalize_proposal(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = payload if isinstance(payload, dict) else {}
        candidate_id, candidate_valid = self._normalize_identifier(source.get("candidate_id"))
        baseline_id, baseline_valid = self._normalize_identifier(
            source.get("baseline_candidate_id")
        )
        policy, policy_valid = self._normalize_policy(source.get("policy"))
        errors: list[str] = []
        if not candidate_valid or not baseline_valid:
            errors.append("candidate_and_baseline_required")
        if candidate_id == baseline_id:
            errors.append("candidate_and_baseline_must_differ")
        if not policy_valid:
            errors.append("policy_contract_invalid")
        return {
            "candidate_id": candidate_id,
            "baseline_candidate_id": baseline_id,
            "policy": policy,
            "valid": not errors,
            "errors": errors,
        }

    def _normalize_policy(self, value: Any) -> tuple[dict[str, Any], bool]:
        source = self._mapping(value)
        policy_id, policy_id_valid = self._normalize_identifier(source.get("policy_id"))
        version, version_valid = self._normalize_identifier(source.get("version"))
        strategies = source.get("strategies")
        values = strategies if isinstance(strategies, list) else []
        normalized = [item for item in values if isinstance(item, str)]
        strategy_valid = (
            bool(normalized)
            and len(normalized) == len(values)
            and len(normalized) == len(set(normalized))
            and len(normalized) <= len(self._allowed_strategies)
            and set(normalized) <= self._allowed_strategies
        )
        return (
            {
                "policy_id": policy_id,
                "version": version,
                "strategies": sorted(normalized) if strategy_valid else [],
                "scope": "shadow_only",
            },
            policy_id_valid and version_valid and strategy_valid,
        )

    def _normalize_identifier(self, value: Any) -> tuple[str, bool]:
        candidate = str(value or "")
        if self._identifier_pattern.fullmatch(candidate):
            return candidate, True
        return "invalid", False

    def _unique_by_task(
        self,
        records: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], set[str]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            task_id = record.get("task_id")
            if isinstance(task_id, str) and task_id:
                grouped.setdefault(task_id, []).append(record)
        duplicates = {task_id for task_id, items in grouped.items() if len(items) != 1}
        return (
            {task_id: items[0] for task_id, items in grouped.items() if len(items) == 1},
            duplicates,
        )

    def _valid_terminal_record(self, record: dict[str, Any]) -> bool:
        producer_evidence = self._mapping(record.get("producer_evidence"))
        return (
            record.get("status") in self._terminal_statuses
            and record.get("verification_status") == "passed"
            and producer_evidence.get("status") == "verified"
            and all(
                isinstance(producer_evidence.get(key), str)
                and bool(producer_evidence.get(key))
                for key in (
                    "producer_run_id",
                    "binding_id",
                    "configuration_digest",
                    "artifact_digest",
                )
            )
        )

    @staticmethod
    def _producer_evidence_distinct(
        candidate: dict[str, Any],
        baseline: dict[str, Any],
    ) -> str:
        if any(
            evidence.get("semantic_configuration_status") != "resolved"
            or evidence.get("artifact_semantic_status") != "resolved"
            or not LongitudinalPromotionGovernance._sha256_digest(
                evidence.get("semantic_configuration_digest")
            )
            or not LongitudinalPromotionGovernance._sha256_digest(
                evidence.get("artifact_semantic_digest")
            )
            for evidence in (candidate, baseline)
        ):
            return "unresolved"
        if (
            candidate.get("semantic_configuration_digest")
            == baseline.get("semantic_configuration_digest")
            or candidate.get("artifact_semantic_digest")
            == baseline.get("artifact_semantic_digest")
        ):
            return "indistinct"
        return "distinct"

    @staticmethod
    def _sha256_digest(value: Any) -> bool:
        return bool(
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    @staticmethod
    def _acceptance_value(record: dict[str, Any]) -> float:
        return 1.0 if record.get("status") == "verified_accepted" else 0.0

    @staticmethod
    def _count_statuses(records: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            status = str(record.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts

    def _proposal_id(self, record: dict[str, Any]) -> str:
        value = {
            "candidate_id": record["candidate_id"],
            "baseline_candidate_id": record["baseline_candidate_id"],
            "policy": record["policy"],
            "evidence": record["evidence"],
            "governance": record["governance"],
        }
        return f"PROM-{self._digest(value)[:16]}"

    def _event_id(self, event: dict[str, Any]) -> str:
        value = {
            "kind": event["kind"],
            "promotion_id": event["promotion_id"],
            "approved": event["approved"],
            "governance": event["governance"],
        }
        return f"PEVT-{self._digest(value)[:16]}"

    @staticmethod
    def _mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 6) if values else 0.0

    @staticmethod
    def _variance(values: list[float], mean: float | None) -> float:
        if not values or mean is None:
            return 0.0
        return round(sum((value - mean) ** 2 for value in values) / len(values), 6)

    def _not_found(self, promotion_id: str) -> dict[str, Any]:
        return {
            "id": promotion_id,
            "status": "not_found",
            "promotion": self._promotion("not_found"),
            "events": [],
        }

    @staticmethod
    def _operation_blocked(current: dict[str, Any], reason: str) -> dict[str, Any]:
        result = deepcopy(current)
        result["operation"] = {"status": "blocked", "reason": reason}
        return result

    @staticmethod
    def _compact_governance(decision: dict[str, Any]) -> dict[str, Any]:
        return {
            key: decision.get(key)
            for key in (
                "authorized",
                "trust_level",
                "requires_human_approval",
                "approval_id",
                "reasons",
            )
        }

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @staticmethod
    def _digest(value: Any) -> str:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _with_provenance(
        self,
        record: dict[str, Any],
        provenance: dict[str, Any],
        persistence_status: str,
    ) -> dict[str, Any]:
        result = deepcopy(record)
        result["persistence"] = {
            "key": f"{self.RECORD_KEY_PREFIX}{result.get('id', 'unknown')}",
            "status": persistence_status,
            "provenance_valid": provenance.get("valid", False),
        }
        result["state_provenance"] = {
            key: provenance.get(key)
            for key in ("valid", "entries", "latest_hash", "key_source")
            if key in provenance
        }
        return result
