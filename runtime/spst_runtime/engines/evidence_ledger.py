import hashlib
import json
from typing import Any

from spst_runtime.verification_profiles import QUALITY_PROFILE_NAMES


class EvidenceLedger:
    """Create deterministic, compact evidence records for runtime decisions."""

    SCHEMA_VERSION = "evidence-ledger-v1"
    PIPELINE_STEPS = (
        "observe",
        "retrieve",
        "infer",
        "reflect",
        "govern",
        "commit",
        "act",
    )
    QUALITY_CHECKS = ("pytest", "ruff", "mypy")

    def prepare(self, state: Any, event: Any, action: dict[str, Any]) -> dict[str, Any]:
        """Capture pre-decision evidence without retaining raw prompts or output."""
        metadata = getattr(state, "metadata", {})
        payload = self._payload(action)
        trace = self._trace(action)
        provenance_status = self._provenance_status(action)
        quality_gate = self._quality_gate(action, payload, provenance_status)
        checks = {
            "pipeline_trace": {
                "status": "passed" if tuple(trace) == self.PIPELINE_STEPS else "failed",
            },
            "reflection": {
                "status": (
                    "passed"
                    if bool(metadata.get("reflection", {}).get("approved", False))
                    else "failed"
                ),
            },
            "provenance": {
                "status": provenance_status,
            },
        }
        record = {
            "schema_version": self.SCHEMA_VERSION,
            "event_type": getattr(event, "type", action.get("event_type")),
            "state_version": metadata.get("version", 0),
            "trace": trace,
            "checks": checks,
            "quality_gate": quality_gate,
            "governance": {},
            "covenant_policy": {},
            "status": "pending_decision",
        }
        record["id"] = self._identifier(record)
        return record

    def attach_decision(
        self,
        record: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach the compact governance decision to an evidence record."""
        record["governance"] = {
            key: decision.get(key)
            for key in (
                "authorized",
                "trust_level",
                "requires_human_approval",
                "approval_id",
                "reasons",
            )
        }
        covenant = decision.get("covenant_policy", {})
        record["covenant_policy"] = covenant if isinstance(covenant, dict) else {}
        if decision.get("authorized", False):
            record["status"] = "authorized"
        elif decision.get("requires_human_approval", False):
            record["status"] = "pending_human_approval"
        else:
            record["status"] = "blocked"
        return record

    def assign_state_version(
        self,
        record: dict[str, Any],
        state_version: Any,
    ) -> dict[str, Any]:
        """Bind authorized evidence to the version produced by the state commit."""
        record["state_version"] = state_version
        record["id"] = self._identifier(record)
        return record

    def finalize(
        self,
        record: dict[str, Any],
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach post-state-commit provenance without exposing key material."""
        record["provenance_after_state_commit"] = {
            key: provenance.get(key)
            for key in ("valid", "entries", "latest_hash", "reason", "key_source")
            if key in provenance
        }
        return record

    def _quality_gate(
        self,
        action: dict[str, Any],
        payload: dict[str, Any],
        provenance_status: str,
    ) -> dict[str, Any]:
        verification = payload.get("verification", {})
        verification_data = verification if isinstance(verification, dict) else {}
        quality_checks = self._quality_check_names(payload)
        verification_run = payload.get("verification_run", {})
        verification_run_data = (
            verification_run if isinstance(verification_run, dict) else {}
        )
        required = bool(
            payload.get("requires_quality_gate")
            or payload.get("verified_quality_gate")
            or payload.get("breaking_change")
            or payload.get("architecture_change")
            or action.get("change_risk", {}).get("requires_human_approval", False)
        )
        checks = {
            name: self._verification_status(verification_data.get(name))
            for name in quality_checks
        }
        checks["provenance"] = provenance_status
        verification_source = self._verification_source(
            verification_data,
            quality_checks,
            payload,
        )
        if not required:
            return {
                "required": False,
                "status": "not_required",
                "checks": checks,
                "missing": [],
                "failed": [],
                "verification_source": verification_source,
                "verification_run_id": verification_run_data.get("id"),
                "verification_run_status": verification_run_data.get("status"),
            }

        required_checks = (*quality_checks, "provenance")
        missing = [
            name
            for name in required_checks
            if checks[name] not in {"passed", "failed"}
        ]
        failed = [name for name in required_checks if checks[name] == "failed"]
        if payload.get("verified_quality_gate"):
            run_status = verification_run_data.get("status")
            if run_status == "failed":
                failed.append("verification_runner")
            elif run_status != "passed":
                missing.append("verification_runner")
        status = "failed" if failed else "incomplete" if missing else "passed"
        return {
            "required": True,
            "status": status,
            "checks": checks,
            "missing": missing,
            "failed": failed,
            "verification_source": verification_source,
            "verification_run_id": verification_run_data.get("id"),
            "verification_run_status": verification_run_data.get("status"),
        }

    def _identifier(self, record: dict[str, Any]) -> str:
        canonical = json.dumps(
            {
                "schema_version": record["schema_version"],
                "event_type": record["event_type"],
                "state_version": record["state_version"],
                "trace": record["trace"],
                "checks": record["checks"],
                "quality_gate": record["quality_gate"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"EVID-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"

    def _payload(self, action: dict[str, Any]) -> dict[str, Any]:
        payload = action.get("payload", {})
        return payload if isinstance(payload, dict) else {}

    def _trace(self, action: dict[str, Any]) -> list[str]:
        trace = action.get("trace", [])
        return [str(step) for step in trace] if isinstance(trace, list) else []

    def _provenance_status(self, action: dict[str, Any]) -> str:
        if action.get("provenance_scope") == "ephemeral":
            return "not_applicable"
        return "passed" if bool(action.get("provenance_verified", False)) else "failed"

    def _quality_check_names(self, payload: dict[str, Any]) -> tuple[str, ...]:
        if payload.get("verified_quality_gate"):
            return QUALITY_PROFILE_NAMES
        return self.QUALITY_CHECKS

    def _verification_source(
        self,
        verification: dict[str, Any],
        check_names: tuple[str, ...],
        payload: dict[str, Any],
    ) -> str:
        if not payload.get("verified_quality_gate"):
            return "attested"
        values = [verification.get(name) for name in check_names]
        if all(
            isinstance(value, dict) and value.get("source") == "verified_local"
            for value in values
        ):
            return "verified_local"
        return "unverified"

    def _verification_status(self, value: Any) -> str:
        if isinstance(value, dict):
            value = value.get("status")
        if value is True:
            return "passed"
        if value is False:
            return "failed"
        normalized = str(value).lower() if value is not None else ""
        if normalized in {"passed", "pass", "success", "ok"}:
            return "passed"
        if normalized in {"failed", "fail", "error"}:
            return "failed"
        if normalized in {"unavailable", "timed_out", "denied"}:
            return normalized
        return "not_provided"
