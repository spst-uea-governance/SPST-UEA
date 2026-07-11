import asyncio
from copy import deepcopy
from datetime import date
import hashlib
import json
import re
from typing import Any, Protocol

from spst_runtime.engines.governance_engine import GovernanceEngine
from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.persistence.sqlite_repository import SQLiteRepository


class VerificationRunnerProtocol(Protocol):
    def run(self) -> dict[str, Any]: ...


class ArtifactOutcomeLedger:
    """Bind a consented task, fixed verification, and human outcome into evidence."""

    SCHEMA_VERSION = "artifact-outcome-ledger-v1"
    INDEX_KEY = "runtime:artifact_outcome:index:v1"
    RECORD_KEY_PREFIX = "runtime:artifact_outcome:record:"
    CONSENT_SCOPE = "local_operational_evaluation"
    _safe_identifier = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
    _revision = re.compile(r"^[0-9a-fA-F]{7,64}$")

    def __init__(
        self,
        repository: SQLiteRepository,
        corpus: OperationalEvaluationCorpus,
        verification_runner: VerificationRunnerProtocol,
        governance_engine: GovernanceEngine | None = None,
    ):
        self.repository = repository
        self.corpus = corpus
        self.verification_runner = verification_runner
        self.governance_engine = governance_engine or GovernanceEngine()

    def record(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record one local outcome without retaining task prompts or review prose."""
        normalized = self._normalize_payload(payload)
        task_contract = self.corpus.task_contract(normalized["task_id"])
        task_active = task_contract is not None
        retention_within_task = self._retention_within_task(normalized, task_contract)
        governance = self._governance_decision(
            normalized,
            task_active=task_active,
            retention_within_task=retention_within_task,
        )
        verification: dict[str, Any] = {}
        if governance.get("authorized", False):
            verification = self._run_verification()
            status = self._outcome_status(normalized, verification)
        else:
            status = "blocked"

        record = {
            "schema_version": self.SCHEMA_VERSION,
            "status": status,
            "task": task_contract or {"id": normalized["task_id"]},
            "candidate_id": normalized["candidate_id"],
            "expected_source_snapshot": normalized["expected_source_snapshot"],
            "verification": verification,
            "human_review": {
                "decision": normalized["human_review"]["decision"],
                "consent_scope": normalized["human_review"]["consent_scope"],
                "retention_until": normalized["human_review"]["retention_until"],
            },
            "calibration": self._calibration(status),
            "claims": {
                "artifact_outcome_verified": status == "verified_accepted",
                "task_quality_uplift_claimed": False,
                "automatic_adoption": False,
                "requires_human_interpretation": True,
            },
            "governance": self._compact_governance(governance),
        }
        record["id"] = self._identifier(record)
        return self._persist(record)

    def history(self) -> list[dict[str, Any]]:
        """Return compact outcome history without private prompts or review prose."""
        with self.repository.locked():
            index = self._load_index()
        records = self._records(index)
        return deepcopy(sorted(records, key=lambda record: int(record.get("sequence", 0))))

    def coverage(self) -> dict[str, Any]:
        """Summarize evidence coverage by task domain and calibration eligibility."""
        records = self.history()
        by_domain: dict[str, int] = {}
        by_status: dict[str, int] = {}
        eligible_count = 0
        for record in records:
            domain = str(record.get("domain") or "unknown")
            status = str(record.get("status") or "unknown")
            by_domain[domain] = by_domain.get(domain, 0) + 1
            by_status[status] = by_status.get(status, 0) + 1
            if record.get("eligible") is True:
                eligible_count += 1
        return {
            "total_count": len(records),
            "eligible_count": eligible_count,
            "by_domain": by_domain,
            "by_status": by_status,
            "schema_version": self.SCHEMA_VERSION,
        }

    def _governance_decision(
        self,
        normalized: dict[str, Any],
        *,
        task_active: bool,
        retention_within_task: bool,
    ) -> dict[str, Any]:
        return self.governance_engine.decide(
            {
                "type": "artifact_outcome_evidence",
                "event_type": "artifact_outcome_evidence",
                "source": "artifact_outcome_ledger",
                "provenance_scope": "ephemeral",
                "provenance_verified": False,
                "payload": {
                    "local_only": True,
                    "evidence_only": True,
                    "task_contract_active": task_active,
                    "expected_source_snapshot_present": normalized[
                        "expected_source_snapshot_present"
                    ],
                    "human_calibration_consent": normalized["human_review"][
                        "consent_valid"
                    ],
                    "human_decision_valid": normalized["human_review"]["decision_valid"],
                    "retention_within_task": retention_within_task,
                },
                "security_assessment": {"blocked": False, "findings": []},
            }
        )

    def _run_verification(self) -> dict[str, Any]:
        try:
            result = self.verification_runner.run()
        except Exception as exc:
            return {
                "id": None,
                "runner_version": None,
                "status": "failed",
                "source_snapshot": {},
                "profiles": {},
                "error_digest": self._digest(type(exc).__name__),
            }
        source_snapshot = self._mapping(result.get("source_snapshot"))
        profiles = self._mapping(result.get("profiles"))
        return {
            "id": result.get("id"),
            "runner_version": result.get("runner_version"),
            "status": result.get("status"),
            "source_snapshot": {
                "revision": self._safe_revision(source_snapshot.get("revision")),
                "git_status_digest": self._safe_identifier_value(
                    source_snapshot.get("git_status_digest")
                ),
            },
            "profiles": {
                name: {
                    "status": profile.get("status"),
                    "output_digest": self._safe_identifier_value(
                        profile.get("output_digest")
                    ),
                }
                for name, value in profiles.items()
                if isinstance(name, str)
                and isinstance(value, dict)
                for profile in [value]
            },
        }

    def _outcome_status(
        self,
        normalized: dict[str, Any],
        verification: dict[str, Any],
    ) -> str:
        if verification.get("status") != "passed":
            return "verification_failed"
        if not self._same_snapshot(
            normalized["expected_source_snapshot"],
            self._mapping(verification.get("source_snapshot")),
        ):
            return "stale_source_snapshot"
        if normalized["human_review"]["decision"] == "rejected":
            return "rejected_by_human"
        return "verified_accepted"

    @staticmethod
    def _calibration(status: str) -> dict[str, Any]:
        eligible = status == "verified_accepted"
        return {
            "eligible": eligible,
            "status": "eligible" if eligible else "ineligible",
            "reason": status,
            "automatic_adoption": False,
        }

    def _persist(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.repository.locked():
            index = self._load_index()
            records = self._records(index)
            existing = next(
                (item for item in records if item.get("id") == record["id"]),
                None,
            )
            if existing is None:
                summary = self._summary(record, len(records) + 1)
                updated_index = {
                    "schema_version": self.SCHEMA_VERSION,
                    "records": [*records, summary],
                }
                asyncio.run(
                    self.repository.save(f"{self.RECORD_KEY_PREFIX}{record['id']}", record)
                )
                asyncio.run(self.repository.save(self.INDEX_KEY, updated_index))
                persistence_status = "stored"
            else:
                persistence_status = "already_recorded"
        provenance = self.repository.verify_provenance()
        record["persistence"] = {
            "key": f"{self.RECORD_KEY_PREFIX}{record['id']}",
            "status": persistence_status,
            "provenance_valid": provenance.get("valid", False),
        }
        record["state_provenance"] = {
            key: provenance.get(key)
            for key in ("valid", "entries", "latest_hash", "key_source")
            if key in provenance
        }
        return deepcopy(record)

    def _load_index(self) -> dict[str, Any]:
        stored = asyncio.run(self.repository.load(self.INDEX_KEY))
        if not isinstance(stored, dict):
            return {"schema_version": self.SCHEMA_VERSION, "records": []}
        records = stored.get("records")
        if stored.get("schema_version") != self.SCHEMA_VERSION or not isinstance(records, list):
            raise ValueError("Invalid artifact outcome index")
        return stored

    @staticmethod
    def _records(index: dict[str, Any]) -> list[dict[str, Any]]:
        records = index.get("records", [])
        return [record for record in records if isinstance(record, dict)]

    def _normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = payload if isinstance(payload, dict) else {}
        expected = self._mapping(source.get("expected_source_snapshot"))
        human = self._mapping(source.get("human_calibration"))
        consent = self._mapping(human.get("consent"))
        decision = human.get("decision")
        decision_value = decision if decision in {"accepted", "rejected"} else "invalid"
        retention_until = consent.get("retention_until")
        retention_value = retention_until if isinstance(retention_until, str) else ""
        return {
            "task_id": self._safe_task_id(source.get("task_id")),
            "candidate_id": self._candidate_id(source.get("candidate_id")),
            "expected_source_snapshot": {
                "revision": self._safe_revision(expected.get("revision")),
                "git_status_digest": self._safe_identifier_value(
                    expected.get("git_status_digest")
                ),
            },
            "expected_source_snapshot_present": bool(
                self._safe_revision(expected.get("revision"))
                and self._safe_identifier_value(expected.get("git_status_digest"))
            ),
            "human_review": {
                "decision": decision_value,
                "decision_valid": decision_value in {"accepted", "rejected"},
                "consent_scope": str(consent.get("scope") or ""),
                "retention_until": retention_value,
                "consent_valid": self._consent_valid(consent),
            },
        }

    def _retention_within_task(
        self,
        normalized: dict[str, Any],
        task_contract: dict[str, Any] | None,
    ) -> bool:
        if task_contract is None:
            return False
        human_retention = normalized["human_review"]["retention_until"]
        task_retention = task_contract.get("retention_until")
        if not isinstance(human_retention, str) or not isinstance(task_retention, str):
            return False
        if not self._valid_date(human_retention) or not self._valid_date(task_retention):
            return False
        return date.fromisoformat(human_retention) <= date.fromisoformat(task_retention)

    def _summary(self, record: dict[str, Any], sequence: int) -> dict[str, Any]:
        task = self._mapping(record.get("task"))
        verification = self._mapping(record.get("verification"))
        calibration = self._mapping(record.get("calibration"))
        return {
            "id": record["id"],
            "sequence": sequence,
            "task_id": task.get("id"),
            "domain": task.get("domain"),
            "candidate_id": record.get("candidate_id"),
            "status": record.get("status"),
            "verification_id": verification.get("id"),
            "verification_status": verification.get("status"),
            "eligible": calibration.get("eligible", False),
        }

    def _identifier(self, record: dict[str, Any]) -> str:
        value = {
            "status": record["status"],
            "task": record["task"],
            "candidate_id": record["candidate_id"],
            "expected_source_snapshot": record["expected_source_snapshot"],
            "verification": record["verification"],
            "human_review": record["human_review"],
            "calibration": record["calibration"],
            "governance": record["governance"],
        }
        return f"AOUT-{self._digest(value)[:16]}"

    @staticmethod
    def _same_snapshot(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
        return (
            expected.get("revision") == actual.get("revision")
            and expected.get("git_status_digest") == actual.get("git_status_digest")
        )

    def _candidate_id(self, value: Any) -> str:
        candidate = str(value or "runtime-default")
        if self._safe_identifier.fullmatch(candidate) and len(candidate) <= 96:
            return candidate
        return f"candidate-{self._digest(candidate)[:16]}"

    def _safe_task_id(self, value: Any) -> str:
        candidate = str(value or "")
        if self._safe_identifier.fullmatch(candidate) and len(candidate) <= 96:
            return candidate
        return "invalid-task"

    def _safe_revision(self, value: Any) -> str:
        candidate = str(value or "")
        return candidate if self._revision.fullmatch(candidate) else ""

    def _safe_identifier_value(self, value: Any) -> str:
        candidate = str(value or "")
        return candidate if self._safe_identifier.fullmatch(candidate) else ""

    def _consent_valid(self, consent: dict[str, Any]) -> bool:
        retention_until = consent.get("retention_until")
        return bool(
            consent.get("granted")
            and consent.get("scope") == self.CONSENT_SCOPE
            and isinstance(retention_until, str)
            and self._valid_date(retention_until)
            and date.today() <= date.fromisoformat(retention_until)
        )

    @staticmethod
    def _valid_date(value: str) -> bool:
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True

    @staticmethod
    def _compact_governance(decision: dict[str, Any]) -> dict[str, Any]:
        return {
            key: decision.get(key)
            for key in ("authorized", "trust_level", "requires_human_approval", "reasons")
        }

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _digest(value: Any) -> str:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
