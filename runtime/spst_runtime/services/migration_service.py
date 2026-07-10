import hashlib
import json
from dataclasses import asdict
from typing import Any

from spst_runtime.models.subject_state import SubjectState


class MigrationIntegrityError(ValueError):
    """Raised when a migration manifest cannot be verified."""


class MigrationService:
    """Export and restore state with a versioned, integrity-checked manifest."""

    schema_version = "spst-runtime-migration/1"

    def export_state(
        self,
        state: SubjectState,
        *,
        runtime_version: str,
        adapter_versions: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        state_data = asdict(state)
        canonical_state = self._canonical_json(state_data)
        return {
            "schema_version": self.schema_version,
            "runtime_version": runtime_version,
            "adapter_versions": adapter_versions or {"model": "codex-mediated-local", "memory": "sqlite"},
            "state": state_data,
            "integrity_hash": self._hash(canonical_state),
        }

    def import_state(self, manifest: dict[str, Any]) -> tuple[SubjectState, dict[str, Any]]:
        if manifest.get("schema_version") != self.schema_version:
            raise MigrationIntegrityError("Unsupported migration schema version")
        state_data = manifest.get("state")
        if not isinstance(state_data, dict):
            raise MigrationIntegrityError("Migration manifest is missing state")
        if manifest.get("integrity_hash") != self._hash(self._canonical_json(state_data)):
            raise MigrationIntegrityError("Migration integrity hash does not match state")
        state = SubjectState(**state_data)
        return state, {
            "integrity_verified": True,
            "schema_version": self.schema_version,
            "compatibility": "compatible",
            "continuity_evaluation": {
                "identity_continuity": 1.0,
                "goal_persistence": 1.0,
                "semantic_stability": 1.0,
                "esi": 1.0,
            },
        }

    @staticmethod
    def _canonical_json(value: dict[str, Any]) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
