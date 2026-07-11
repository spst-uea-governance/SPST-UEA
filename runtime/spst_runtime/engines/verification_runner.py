import hashlib
import json
import re
from typing import Any, Callable

from spst_runtime.engines.governance_engine import GovernanceEngine
from spst_runtime.providers.tool_provider import ToolProvider
from spst_runtime.verification_profiles import (
    QUALITY_PROFILE_NAMES,
    VERIFICATION_PROFILE_NAMES,
)


ProfileExecutor = Callable[[str, bool], dict[str, Any]]


class VerificationRunner:
    """Run governed fixed profiles and return compact reproducibility evidence."""

    RUNNER_VERSION = "verified-local-runner-v1"

    def __init__(
        self,
        tool_provider: ToolProvider,
        governance_engine: GovernanceEngine,
        profile_executor: ProfileExecutor | None = None,
    ):
        self.tool_provider = tool_provider
        self.governance_engine = governance_engine
        self.profile_executor = profile_executor or self._execute_profile

    def run(self) -> dict[str, Any]:
        """Execute only approved local profiles and omit raw output from evidence."""
        raw_results: dict[str, dict[str, Any]] = {}
        profiles: dict[str, dict[str, Any]] = {}
        for profile in VERIFICATION_PROFILE_NAMES:
            decision = self._authorize(profile)
            raw_result = self.profile_executor(
                profile,
                bool(decision.get("authorized", False)),
            )
            raw_results[profile] = raw_result
            profiles[profile] = self._compact_profile(profile, raw_result, decision)

        source_snapshot = {
            "revision": self._revision(raw_results.get("git_head", {})),
            "git_status_digest": profiles["git_status"].get("output_digest"),
        }
        record = {
            "runner_version": self.RUNNER_VERSION,
            "status": self._run_status(profiles),
            "source_snapshot": source_snapshot,
            "profiles": profiles,
        }
        record["id"] = self._identifier(record)
        record["verification"] = {
            profile: {
                "status": self._evidence_status(profiles[profile].get("status")),
                "source": "verified_local",
                "verification_run_id": record["id"],
                "output_digest": profiles[profile].get("output_digest"),
            }
            for profile in QUALITY_PROFILE_NAMES
        }
        return record

    def _authorize(self, profile: str) -> dict[str, Any]:
        return self.governance_engine.decide(
            {
                "type": "local_verification",
                "event_type": "verification_run",
                "source": "verification_runner",
                "provenance_scope": "ephemeral",
                "provenance_verified": False,
                "payload": {
                    "analysis_only": True,
                    "read_only": True,
                    "verification_profile": profile,
                },
                "security_assessment": {"blocked": False, "findings": []},
            }
        )

    def _execute_profile(self, profile: str, authorized: bool) -> dict[str, Any]:
        return self.tool_provider.execute_verification_profile(
            profile,
            governance_authorized=authorized,
        )

    def _compact_profile(
        self,
        profile: str,
        result: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "profile": profile,
            "status": result.get("status", "unavailable"),
            "returncode": result.get("returncode"),
            "duration_ms": result.get("duration_ms"),
            "output_digest": result.get("output_digest"),
            "governance": {
                "authorized": bool(decision.get("authorized", False)),
                "trust_level": decision.get("trust_level"),
                "reasons": decision.get("reasons", []),
            },
        }

    def _run_status(self, profiles: dict[str, dict[str, Any]]) -> str:
        statuses = [profiles[name].get("status") for name in VERIFICATION_PROFILE_NAMES]
        if all(status == "completed" for status in statuses):
            return "passed"
        if any(status == "failed" for status in statuses):
            return "failed"
        return "incomplete"

    def _evidence_status(self, status: Any) -> str:
        if status == "completed":
            return "passed"
        if status == "failed":
            return "failed"
        return "unavailable"

    def _revision(self, result: dict[str, Any]) -> str | None:
        candidate = str(result.get("stdout", "")).strip()
        return candidate if re.fullmatch(r"[0-9a-fA-F]{7,64}", candidate) else None

    def _identifier(self, record: dict[str, Any]) -> str:
        canonical = json.dumps(
            {
                "runner_version": record["runner_version"],
                "status": record["status"],
                "source_snapshot": record["source_snapshot"],
                "profiles": {
                    name: {
                        key: value
                        for key, value in profile.items()
                        if key != "duration_ms"
                    }
                    for name, profile in record["profiles"].items()
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"VRUN-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"
