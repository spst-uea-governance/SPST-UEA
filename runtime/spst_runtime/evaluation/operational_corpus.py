import asyncio
from copy import deepcopy
from datetime import date
import hashlib
import json
import re
from typing import Any

from spst_runtime.engines.governance_engine import GovernanceEngine
from spst_runtime.engines.security_engine import SecurityEngine
from spst_runtime.evaluation.quality_evidence import (
    normalize_quality_rubric,
    quality_rubric_digest,
    validate_quality_rubric,
)
from spst_runtime.persistence.sqlite_repository import SQLiteRepository


class OperationalEvaluationCorpus:
    """Consent-scoped local task corpus with a redacted public manifest."""

    SCHEMA_VERSION = "operational-evaluation-corpus-v1"
    INDEX_KEY = "runtime:operational_corpus:index:v1"
    TASK_KEY_PREFIX = "runtime:operational_corpus:task:"
    CONSENT_SCOPE = "local_operational_evaluation"
    SPLITS = {"calibration", "holdout"}
    _task_id_pattern = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,95}$")

    def __init__(
        self,
        repository: SQLiteRepository,
        governance_engine: GovernanceEngine | None = None,
        security_engine: SecurityEngine | None = None,
    ):
        self.repository = repository
        self.governance_engine = governance_engine or GovernanceEngine()
        self.security_engine = security_engine or SecurityEngine()

    def register(self, task: dict[str, Any]) -> dict[str, Any]:
        """Persist one explicitly consented local task without exposing its prompt."""
        normalized, errors = self._normalize_task(task)
        security_assessment = self._security_assessment(normalized["prompt"])
        decision = self.governance_engine.decide(
            {
                "type": "operational_corpus_registration",
                "event_type": "operational_corpus_registration",
                "source": "operational_evaluation_corpus",
                "provenance_scope": "ephemeral",
                "provenance_verified": False,
                "payload": {
                    "local_only": True,
                    "consent_granted": normalized["consent"]["granted"],
                    "consent_scope": normalized["consent"]["scope"],
                    "retention_until": normalized["consent"]["retention_until"],
                    "task_id": normalized["task_id"],
                    "contract_valid": not errors,
                },
                "security_assessment": security_assessment,
            }
        )
        if not decision.get("authorized", False):
            return self._blocked(normalized["task_id"], decision, security_assessment)

        with self.repository.locked():
            index = self._load_index()
            entries = self._entries(index)
            if any(entry["id"] == normalized["task_id"] for entry in entries):
                duplicate_decision = {
                    "authorized": False,
                    "trust_level": "Low",
                    "requires_human_approval": False,
                    "reasons": ["operational_corpus_task_id_exists"],
                }
                return self._blocked(
                    normalized["task_id"],
                    duplicate_decision,
                    security_assessment,
                )

            entry = self._manifest_entry(normalized)
            stored_task = {**normalized, "schema_version": self.SCHEMA_VERSION}
            updated_index = {
                "schema_version": self.SCHEMA_VERSION,
                "tasks": [*entries, entry],
            }
            asyncio.run(
                self.repository.save(
                    f"{self.TASK_KEY_PREFIX}{normalized['task_id']}",
                    stored_task,
                )
            )
            asyncio.run(self.repository.save(self.INDEX_KEY, updated_index))

        provenance = self.repository.verify_provenance()
        return {
            "status": "registered",
            "task": deepcopy(entry),
            "governance": self._compact_governance(decision),
            "security_assessment": security_assessment,
            "state_provenance": self._compact_provenance(provenance),
        }

    def manifest(self, *, split: str | None = None) -> dict[str, Any]:
        """Return the non-sensitive task manifest available to a local operator."""
        entries = self._matching_entries(split)
        active_entries = [entry for entry in entries if not self._is_expired(entry)]
        expired_entries = [entry for entry in entries if self._is_expired(entry)]
        public_tasks = [self._public_entry(entry) for entry in active_entries]
        return {
            "schema_version": self.SCHEMA_VERSION,
            "split": split,
            "tasks": public_tasks,
            "task_ids": [entry["id"] for entry in active_entries],
            "eligible_count": len(active_entries),
            "expired_count": len(expired_entries),
            "total_count": len(entries),
            "hash": self._digest(public_tasks),
        }

    def stats(self) -> dict[str, Any]:
        """Return retention-aware corpus counts without task content."""
        entries = self._matching_entries(None)
        active_entries = [entry for entry in entries if not self._is_expired(entry)]
        return {
            "total_count": len(entries),
            "active_count": len(active_entries),
            "expired_count": len(entries) - len(active_entries),
            "holdout_count": sum(entry["split"] == "holdout" for entry in active_entries),
            "calibration_count": sum(
                entry["split"] == "calibration" for entry in active_entries
            ),
            "schema_version": self.SCHEMA_VERSION,
        }

    def task_contract(self, task_id: str) -> dict[str, Any] | None:
        """Return one active public contract without loading private task content."""
        for entry in self._matching_entries(None):
            if entry["id"] != task_id:
                continue
            if self._is_expired(entry):
                return None
            return deepcopy(self._public_entry(entry))
        return None

    def scoring_contract(self, task_id: str) -> dict[str, Any] | None:
        """Load one active private rubric for the independent evaluator only."""
        if self.task_contract(task_id) is None:
            return None
        with self.repository.locked():
            stored = asyncio.run(self.repository.load(f"{self.TASK_KEY_PREFIX}{task_id}"))
        if not isinstance(stored, dict):
            return None
        rubric = stored.get("quality_rubric")
        valid, _ = validate_quality_rubric(rubric)
        return deepcopy(rubric) if valid else None

    def load_for_shadow(
        self,
        *,
        split: str = "holdout",
        task_ids: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Load active consented task content for the internal shadow runner only."""
        selected_ids = set(task_ids) if task_ids is not None else None
        base_manifest = self.manifest(split=split)
        selected_entries = [
            entry
            for entry in base_manifest["tasks"]
            if selected_ids is None or entry["id"] in selected_ids
        ]
        tasks: list[dict[str, Any]] = []
        with self.repository.locked():
            for entry in selected_entries:
                stored = asyncio.run(
                    self.repository.load(f"{self.TASK_KEY_PREFIX}{entry['id']}")
                )
                if isinstance(stored, dict) and stored.get("prompt"):
                    tasks.append(stored)
        selected_manifest = {
            **base_manifest,
            "tasks": selected_entries,
            "task_ids": [entry["id"] for entry in selected_entries],
            "eligible_count": len(selected_entries),
            "hash": self._digest(selected_entries),
        }
        return tasks, selected_manifest

    def _matching_entries(self, split: str | None) -> list[dict[str, Any]]:
        with self.repository.locked():
            entries = self._entries(self._load_index())
        if split is None:
            return entries
        return [entry for entry in entries if entry["split"] == split]

    def _load_index(self) -> dict[str, Any]:
        stored = asyncio.run(self.repository.load(self.INDEX_KEY))
        if not isinstance(stored, dict):
            return {"schema_version": self.SCHEMA_VERSION, "tasks": []}
        tasks = stored.get("tasks")
        if stored.get("schema_version") != self.SCHEMA_VERSION or not isinstance(tasks, list):
            raise ValueError("Invalid operational evaluation corpus index")
        return stored

    def _entries(self, index: dict[str, Any]) -> list[dict[str, Any]]:
        tasks = index.get("tasks", [])
        return [task for task in tasks if isinstance(task, dict)]

    def _normalize_task(self, task: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        source = task if isinstance(task, dict) else {}
        task_id = source.get("task_id")
        prompt = source.get("prompt")
        domain = source.get("domain")
        split = source.get("split", "holdout")
        consent = source.get("consent")
        errors: list[str] = []
        if not isinstance(task_id, str) or not self._task_id_pattern.fullmatch(task_id):
            errors.append("invalid_task_id")
            task_id = "invalid-task"
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
            errors.append("invalid_prompt")
            prompt = ""
        if not isinstance(domain, str) or not domain.strip() or len(domain) > 64:
            errors.append("invalid_domain")
            domain = "unknown"
        if split not in self.SPLITS:
            errors.append("invalid_split")
            split = "holdout"
        required_markers = self._string_list(source.get("required_markers"), "required_markers", errors)
        expected_json_keys = self._string_list(
            source.get("expected_json_keys", []),
            "expected_json_keys",
            errors,
        )
        quality_rubric, rubric_error = normalize_quality_rubric(
            source.get("quality_rubric")
        )
        if rubric_error is not None:
            errors.append(rubric_error)
        if not required_markers and not expected_json_keys and quality_rubric is None:
            errors.append("verification_contract_required")
        consent_data = consent if isinstance(consent, dict) else {}
        retention_until = consent_data.get("retention_until")
        if not isinstance(retention_until, str) or not self._valid_date(retention_until):
            errors.append("invalid_retention_until")
            retention_until = ""
        return (
            {
                "task_id": task_id,
                "prompt": prompt,
                "domain": domain.strip().lower(),
                "split": split,
                "required_markers": required_markers,
                "expected_json_keys": expected_json_keys,
                "quality_rubric": quality_rubric,
                "prompt_digest": self._digest(prompt),
                "consent": {
                    "granted": bool(consent_data.get("granted", False)),
                    "scope": str(consent_data.get("scope") or ""),
                    "retention_until": retention_until,
                },
            },
            errors,
        )

    def _manifest_entry(self, task: dict[str, Any]) -> dict[str, Any]:
        contract = {
            "prompt_digest": task["prompt_digest"],
            "domain": task["domain"],
            "split": task["split"],
            "required_marker_count": len(task["required_markers"]),
            "expected_json_key_count": len(task["expected_json_keys"]),
            "quality_rubric_digest": quality_rubric_digest(
                task.get("quality_rubric")
            ),
        }
        criteria = (
            task["quality_rubric"].get("criteria", [])
            if isinstance(task.get("quality_rubric"), dict)
            else []
        )
        return {
            "id": task["task_id"],
            "domain": task["domain"],
            "split": task["split"],
            "prompt_digest": task["prompt_digest"],
            "contract_hash": self._digest(contract),
            "quality_rubric_digest": contract["quality_rubric_digest"],
            "quality_criterion_count": len(criteria),
            "consent_scope": task["consent"]["scope"],
            "retention_until": task["consent"]["retention_until"],
        }

    def _public_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": entry["id"],
            "domain": entry["domain"],
            "split": entry["split"],
            "prompt_digest": entry["prompt_digest"],
            "contract_hash": entry["contract_hash"],
            "quality_rubric_digest": entry.get("quality_rubric_digest"),
            "quality_criterion_count": int(entry.get("quality_criterion_count", 0)),
            "consent_scope": entry["consent_scope"],
            "retention_until": entry["retention_until"],
            "status": "expired" if self._is_expired(entry) else "active",
        }

    def _security_assessment(self, prompt: str) -> dict[str, Any]:
        assessment = self.security_engine.scan({"code": prompt})
        findings = [
            finding
            for finding in assessment.get("findings", [])
            if isinstance(finding, dict) and finding.get("kind") != "invalid_python"
        ]
        blocked = any(finding.get("severity") == "critical" for finding in findings)
        return {
            "scanner": assessment.get("scanner", "SecuritySubject"),
            "findings": findings,
            "severity": "critical" if blocked else "none",
            "blocked": blocked,
        }

    def _blocked(
        self,
        task_id: str,
        decision: dict[str, Any],
        security_assessment: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "status": "blocked",
            "task": {"id": task_id},
            "governance": self._compact_governance(decision),
            "security_assessment": security_assessment,
        }

    def _is_expired(self, entry: dict[str, Any]) -> bool:
        retention_until = entry.get("retention_until")
        if not isinstance(retention_until, str) or not self._valid_date(retention_until):
            return True
        return date.today() > date.fromisoformat(retention_until)

    @staticmethod
    def _compact_governance(decision: dict[str, Any]) -> dict[str, Any]:
        return {
            key: decision.get(key)
            for key in ("authorized", "trust_level", "requires_human_approval", "reasons")
        }

    @staticmethod
    def _compact_provenance(provenance: dict[str, Any]) -> dict[str, Any]:
        return {
            key: provenance.get(key)
            for key in ("valid", "entries", "latest_hash", "key_source")
            if key in provenance
        }

    @staticmethod
    def _string_list(value: Any, name: str, errors: list[str]) -> list[str]:
        if not isinstance(value, list):
            errors.append(f"invalid_{name}")
            return []
        normalized = [item.strip().lower() for item in value if isinstance(item, str) and item.strip()]
        if len(normalized) != len(value) or len(normalized) > 16:
            errors.append(f"invalid_{name}")
        return normalized[:16]

    @staticmethod
    def _valid_date(value: str) -> bool:
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True

    @staticmethod
    def _digest(value: Any) -> str:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
