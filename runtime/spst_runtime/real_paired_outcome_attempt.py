import asyncio
from copy import deepcopy
import hashlib
import re
import secrets
from typing import Any, Callable

from spst_runtime.live_pairing import canonical_live_pair_hash
from spst_runtime.persistence.sqlite_repository import SQLiteRepository


REAL_PAIRED_OUTCOME_ATTEMPT_SCHEMA = "spst-real-paired-outcome-attempt-v1"
PROGRAM_ATTEMPT_PREFIX = "runtime:real_paired_outcome:attempt:"
ACTIVE_ATTEMPT_STATES = frozenset({"claimed", "running"})
TERMINAL_ATTEMPT_OUTCOMES = frozenset({"completed", "blocked", "failed"})

_identifier = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_sha256 = re.compile(r"^[0-9a-f]{64}$")


class RealPairedOutcomeAttemptLedger:
    """Persist a single fail-closed execution claim and its state transitions."""

    def __init__(
        self,
        repository: SQLiteRepository,
        *,
        nonce_factory: Callable[[], str] | None = None,
    ):
        self.repository = repository
        self.nonce_factory = nonce_factory or (lambda: secrets.token_hex(32))

    def get(self, program_id: str) -> dict[str, Any] | None:
        if not self._safe_identifier(program_id):
            return None
        value = asyncio.run(self.repository.load(self._key(program_id)))
        return value if isinstance(value, dict) else None

    def claim(
        self,
        registration: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, bool, str | None]:
        program_id = str(registration.get("id") or "")
        nonce = str(self.nonce_factory() or "")
        nonce_sha256 = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        attempt_seed = hashlib.sha256(
            f"{program_id}:{nonce_sha256}".encode("utf-8")
        ).hexdigest()
        attempt_id = f"RATT-{attempt_seed[:16]}"
        unsigned = {
            "schema": REAL_PAIRED_OUTCOME_ATTEMPT_SCHEMA,
            "kind": "execution_attempt",
            "attempt_id": attempt_id,
            "program_id": program_id,
            "program_sha256": registration.get("program_sha256"),
            "execution_manifest_sha256": self._mapping(
                registration.get("execution_plan")
            ).get("manifest_sha256"),
            "state": "claimed",
            "transition_index": 1,
            "adapter_invocations_may_have_started": False,
            "automatic_retry_permitted": False,
            "claim_nonce_sha256": nonce_sha256,
            "claim_attempt_sha256": None,
            "claim_record_hash": None,
            "running_attempt_sha256": None,
            "running_record_hash": None,
            "previous_attempt_sha256": None,
            "previous_record_hash": None,
            "terminal_outcome": None,
            "execution_sha256": None,
            "reason": None,
            "failure_type": None,
        }
        claim = {**unsigned, "attempt_sha256": canonical_live_pair_hash(unsigned)}
        inserted = asyncio.run(
            self.repository.save_if_absent(self._key(program_id), claim)
        )
        if inserted:
            return claim, True, None
        existing = self.get(program_id)
        reason = self.validation_reason(existing, registration)
        if reason is None:
            reason = self.provenance_reason(existing)
        return existing, False, reason

    def mark_running(
        self,
        registration: dict[str, Any],
        claim: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        reason = self.validation_reason(claim, registration) or self.provenance_reason(
            claim
        )
        if reason:
            return None, reason
        if claim.get("state") != "claimed":
            return None, "real_paired_outcome_attempt_not_claimed"
        claim_record_hash = self.repository.record_hash(claim)
        unsigned = {
            **{
                key: deepcopy(value)
                for key, value in claim.items()
                if key != "attempt_sha256"
            },
            "state": "running",
            "transition_index": 2,
            "adapter_invocations_may_have_started": True,
            "claim_attempt_sha256": claim["attempt_sha256"],
            "claim_record_hash": claim_record_hash,
            "previous_attempt_sha256": claim["attempt_sha256"],
            "previous_record_hash": claim_record_hash,
        }
        running = {**unsigned, "attempt_sha256": canonical_live_pair_hash(unsigned)}
        replaced = asyncio.run(
            self.repository.replace_if_record_hash(
                self._key(str(claim["program_id"])),
                claim_record_hash,
                running,
            )
        )
        if not replaced:
            return None, "real_paired_outcome_attempt_transition_conflict"
        return running, self.provenance_reason(running)

    def complete(
        self,
        registration: dict[str, Any],
        running: dict[str, Any],
        *,
        outcome: str,
        execution_sha256: str | None,
        reason: str | None,
        failure_type: str | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        current = self.get(str(registration.get("id") or ""))
        if isinstance(current, dict) and current.get("state") == "terminal":
            current_reason = self.validation_reason(
                current, registration
            ) or self.provenance_reason(current)
            if current_reason:
                return None, current_reason
            if (
                current.get("attempt_id") == running.get("attempt_id")
                and current.get("terminal_outcome") == outcome
                and current.get("execution_sha256") == execution_sha256
            ):
                return current, None
            return None, "real_paired_outcome_attempt_terminal_conflict"
        if current != running:
            return None, "real_paired_outcome_attempt_transition_conflict"
        current_reason = self.validation_reason(
            running, registration
        ) or self.provenance_reason(running)
        if current_reason:
            return None, current_reason
        if running.get("state") != "running":
            return None, "real_paired_outcome_attempt_not_running"
        if outcome not in TERMINAL_ATTEMPT_OUTCOMES:
            return None, "real_paired_outcome_attempt_outcome_invalid"
        if outcome == "failed":
            if execution_sha256 is not None or not self._safe_identifier(failure_type):
                return None, "real_paired_outcome_attempt_failure_invalid"
        elif not self._valid_sha256(execution_sha256) or failure_type is not None:
            return None, "real_paired_outcome_attempt_execution_binding_invalid"
        running_record_hash = self.repository.record_hash(running)
        unsigned = {
            **{
                key: deepcopy(value)
                for key, value in running.items()
                if key != "attempt_sha256"
            },
            "state": "terminal",
            "transition_index": 3,
            "running_attempt_sha256": running["attempt_sha256"],
            "running_record_hash": running_record_hash,
            "previous_attempt_sha256": running["attempt_sha256"],
            "previous_record_hash": running_record_hash,
            "terminal_outcome": outcome,
            "execution_sha256": execution_sha256,
            "reason": reason,
            "failure_type": failure_type,
        }
        terminal = {**unsigned, "attempt_sha256": canonical_live_pair_hash(unsigned)}
        replaced = asyncio.run(
            self.repository.replace_if_record_hash(
                self._key(str(running["program_id"])),
                running_record_hash,
                terminal,
            )
        )
        if not replaced:
            return None, "real_paired_outcome_attempt_transition_conflict"
        return terminal, self.provenance_reason(terminal)

    def validation_reason(
        self,
        value: dict[str, Any] | None,
        registration: dict[str, Any],
    ) -> str | None:
        if not isinstance(value, dict):
            return "real_paired_outcome_attempt_missing"
        if value.get("schema") != REAL_PAIRED_OUTCOME_ATTEMPT_SCHEMA:
            return "real_paired_outcome_attempt_schema_invalid"
        digest = value.get("attempt_sha256")
        unsigned = {key: item for key, item in value.items() if key != "attempt_sha256"}
        if not self._valid_sha256(digest) or canonical_live_pair_hash(unsigned) != digest:
            return "real_paired_outcome_attempt_digest_mismatch"
        if (
            not self._safe_identifier(value.get("attempt_id"))
            or value.get("program_id") != registration.get("id")
            or value.get("program_sha256") != registration.get("program_sha256")
            or value.get("execution_manifest_sha256")
            != self._mapping(registration.get("execution_plan")).get("manifest_sha256")
            or value.get("automatic_retry_permitted") is not False
            or not self._valid_sha256(value.get("claim_nonce_sha256"))
        ):
            return "real_paired_outcome_attempt_binding_mismatch"
        state = value.get("state")
        if state == "claimed":
            valid = (
                value.get("transition_index") == 1
                and value.get("adapter_invocations_may_have_started") is False
                and all(
                    value.get(field) is None
                    for field in (
                        "claim_attempt_sha256",
                        "claim_record_hash",
                        "running_attempt_sha256",
                        "running_record_hash",
                        "previous_attempt_sha256",
                        "previous_record_hash",
                        "terminal_outcome",
                        "execution_sha256",
                        "reason",
                        "failure_type",
                    )
                )
            )
        elif state == "running":
            valid = (
                value.get("transition_index") == 2
                and value.get("adapter_invocations_may_have_started") is True
                and self._valid_sha256(value.get("claim_attempt_sha256"))
                and self._valid_sha256(value.get("claim_record_hash"))
                and value.get("previous_attempt_sha256")
                == value.get("claim_attempt_sha256")
                and value.get("previous_record_hash") == value.get("claim_record_hash")
                and all(
                    value.get(field) is None
                    for field in (
                        "running_attempt_sha256",
                        "running_record_hash",
                        "terminal_outcome",
                        "execution_sha256",
                        "reason",
                        "failure_type",
                    )
                )
            )
        elif state == "terminal":
            outcome = value.get("terminal_outcome")
            valid = (
                value.get("transition_index") == 3
                and value.get("adapter_invocations_may_have_started") is True
                and self._valid_sha256(value.get("claim_attempt_sha256"))
                and self._valid_sha256(value.get("claim_record_hash"))
                and self._valid_sha256(value.get("running_attempt_sha256"))
                and self._valid_sha256(value.get("running_record_hash"))
                and value.get("previous_attempt_sha256")
                == value.get("running_attempt_sha256")
                and value.get("previous_record_hash") == value.get("running_record_hash")
                and outcome in TERMINAL_ATTEMPT_OUTCOMES
                and (
                    (
                        outcome == "failed"
                        and value.get("execution_sha256") is None
                        and self._safe_identifier(value.get("failure_type"))
                    )
                    or (
                        outcome in {"completed", "blocked"}
                        and self._valid_sha256(value.get("execution_sha256"))
                        and value.get("failure_type") is None
                    )
                )
                and (
                    value.get("reason") is None
                    or (
                        isinstance(value.get("reason"), str)
                        and 0 < len(str(value["reason"])) <= 256
                    )
                )
            )
        else:
            valid = False
        return None if valid else "real_paired_outcome_attempt_state_invalid"

    def provenance_reason(self, value: dict[str, Any] | None) -> str | None:
        if not isinstance(value, dict):
            return "real_paired_outcome_attempt_missing"
        entries = [
            entry
            for entry in self.repository.provenance_entries()
            if entry.get("record_key") == self._key(str(value.get("program_id") or ""))
        ]
        expected_count = {"claimed": 1, "running": 2, "terminal": 3}.get(
            str(value.get("state")), 0
        )
        if len(entries) != expected_count or not entries:
            return "real_paired_outcome_attempt_provenance_mismatch"
        if entries[-1].get("record_hash") != self.repository.record_hash(value):
            return "real_paired_outcome_attempt_provenance_mismatch"
        if expected_count >= 2 and entries[0].get("record_hash") != value.get(
            "claim_record_hash"
        ):
            return "real_paired_outcome_attempt_provenance_mismatch"
        if expected_count == 3 and entries[1].get("record_hash") != value.get(
            "running_record_hash"
        ):
            return "real_paired_outcome_attempt_provenance_mismatch"
        return None

    def provenance_projection(self, value: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {"verified": False, "transition_count": 0}
        entries = [
            entry
            for entry in self.repository.provenance_entries()
            if entry.get("record_key") == self._key(str(value.get("program_id") or ""))
        ]
        return {
            "verified": self.provenance_reason(value) is None,
            "transition_count": len(entries),
            "claim_sequence": entries[0]["sequence"] if entries else None,
            "running_sequence": entries[1]["sequence"] if len(entries) > 1 else None,
            "terminal_sequence": entries[2]["sequence"] if len(entries) > 2 else None,
        }

    @staticmethod
    def _key(program_id: str) -> str:
        return f"{PROGRAM_ATTEMPT_PREFIX}{program_id}"

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _safe_identifier(value: Any) -> str:
        candidate = str(value or "")
        return candidate if _identifier.fullmatch(candidate) else ""

    @staticmethod
    def _valid_sha256(value: Any) -> bool:
        return isinstance(value, str) and bool(_sha256.fullmatch(value))
