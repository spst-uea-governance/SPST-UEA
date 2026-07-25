import asyncio
from copy import deepcopy
import hashlib
import inspect
import json
import re
from typing import Any

from spst_runtime.interfaces.model_adapter import ModelAdapter
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.provider_observation import (
    build_provider_request_binding,
    validate_provider_request_binding,
)


PROVIDER_TRANSPORT_REQUEST_SCHEMA = "spst-provider-transport-request-v1"
PROVIDER_TRANSPORT_RECEIPT_SCHEMA = "spst-provider-transport-receipt-v1"
PROVIDER_TRANSPORT_RECORD_SCHEMA = "spst-provider-transport-record-v1"
PROVIDER_TRANSPORT_QUERY_SCHEMA = "spst-provider-transport-query-v1"
PROVIDER_RECOVERY_AUTHORITY_SCHEMA = "spst-provider-recovery-authority-v1"
PROVIDER_RECOVERY_RECORD_SCHEMA = "spst-provider-recovery-record-v1"
TRANSPORT_RECORD_PREFIX = "runtime:real_paired_outcome:transport:"
TRANSPORT_QUERY_PREFIX = "runtime:real_paired_outcome:transport_query:"
RECOVERY_RECORD_PREFIX = "runtime:real_paired_outcome:recovery:"
TRANSPORT_CONTEXT_KEY = "provider_transport"

_identifier = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_sha256 = re.compile(r"^[0-9a-f]{64}$")


class ProviderTransportOutcomeUnknown(BaseException):
    """Stop evaluation when the provider may have accepted an unresolved call."""


class ProviderTransportRecoveryRequired(BaseException):
    """Stop evaluation rather than replaying a submitted transport call."""


class ProviderTransportError(ValueError):
    """Raised when transport evidence cannot be safely constructed."""


def build_provider_transport_request(
    registration: dict[str, Any],
    attempt: dict[str, Any],
    *,
    call_ordinal: int,
    logical_request_binding: dict[str, Any],
) -> dict[str, Any]:
    valid, reason = validate_provider_request_binding(logical_request_binding)
    if not valid:
        raise ProviderTransportError(reason or "provider_transport_logical_request_invalid")
    if not isinstance(call_ordinal, int) or isinstance(call_ordinal, bool) or call_ordinal < 1:
        raise ProviderTransportError("provider_transport_call_ordinal_invalid")
    seed = {
        "program_id": registration.get("id"),
        "program_sha256": registration.get("program_sha256"),
        "execution_manifest_sha256": _mapping(
            registration.get("execution_plan")
        ).get("manifest_sha256"),
        "attempt_id": attempt.get("attempt_id"),
        "attempt_running_sha256": attempt.get("attempt_sha256"),
        "call_ordinal": call_ordinal,
        "logical_request_binding_sha256": logical_request_binding.get(
            "request_binding_sha256"
        ),
    }
    if (
        not _safe_identifier(seed["program_id"])
        or not _safe_identifier(seed["attempt_id"])
        or any(
            not _valid_sha256(seed[field])
            for field in (
                "program_sha256",
                "execution_manifest_sha256",
                "attempt_running_sha256",
                "logical_request_binding_sha256",
            )
        )
    ):
        raise ProviderTransportError("provider_transport_request_binding_invalid")
    seed_sha256 = _canonical_hash(seed)
    idempotency_key = f"SPST-{seed_sha256}"
    unsigned = {
        "schema": PROVIDER_TRANSPORT_REQUEST_SCHEMA,
        **seed,
        "idempotency_key": idempotency_key,
        "idempotency_key_sha256": _sha256_text(idempotency_key),
        "retry_semantics": "provider_reconcile_before_resume",
        "automatic_retry_permitted": False,
    }
    return {**unsigned, "transport_request_sha256": _canonical_hash(unsigned)}


def validate_provider_transport_request(value: Any) -> tuple[bool, str | None]:
    if not isinstance(value, dict):
        return False, "provider_transport_request_missing"
    digest = value.get("transport_request_sha256")
    unsigned = {
        key: item for key, item in value.items() if key != "transport_request_sha256"
    }
    if (
        value.get("schema") != PROVIDER_TRANSPORT_REQUEST_SCHEMA
        or not _valid_sha256(digest)
        or _canonical_hash(unsigned) != digest
    ):
        return False, "provider_transport_request_digest_mismatch"
    if (
        not _safe_identifier(value.get("program_id"))
        or not _safe_identifier(value.get("attempt_id"))
        or not isinstance(value.get("call_ordinal"), int)
        or isinstance(value.get("call_ordinal"), bool)
        or int(value["call_ordinal"]) < 1
        or any(
            not _valid_sha256(value.get(field))
            for field in (
                "program_sha256",
                "execution_manifest_sha256",
                "attempt_running_sha256",
                "logical_request_binding_sha256",
                "idempotency_key_sha256",
            )
        )
        or not isinstance(value.get("idempotency_key"), str)
        or value.get("idempotency_key")
        != f"SPST-{_canonical_hash(_transport_seed(value))}"
        or _sha256_text(str(value["idempotency_key"]))
        != value.get("idempotency_key_sha256")
        or value.get("retry_semantics") != "provider_reconcile_before_resume"
        or value.get("automatic_retry_permitted") is not False
    ):
        return False, "provider_transport_request_binding_invalid"
    return True, None


def build_provider_transport_receipt(
    transport_request: dict[str, Any],
    provider_request_binding: dict[str, Any],
    *,
    provider_name: str,
    model_version: str,
    response_id: str,
    response_status: str,
    output_text: str,
    acknowledged_idempotency_key: str | None,
) -> dict[str, Any]:
    transport_valid, transport_reason = validate_provider_transport_request(
        transport_request
    )
    provider_valid, provider_reason = validate_provider_request_binding(
        provider_request_binding
    )
    if not transport_valid:
        raise ProviderTransportError(
            transport_reason or "provider_transport_request_invalid"
        )
    if not provider_valid:
        raise ProviderTransportError(provider_reason or "provider_request_binding_invalid")
    reasons: list[str] = []
    if not _safe_identifier(provider_name):
        reasons.append("provider_transport_provider_invalid")
    if not _safe_identifier(model_version):
        reasons.append("provider_transport_model_invalid")
    if not isinstance(response_id, str) or not response_id.strip():
        reasons.append("provider_transport_response_id_missing")
    if response_status != "completed":
        reasons.append("provider_transport_response_incomplete")
    if acknowledged_idempotency_key != transport_request["idempotency_key"]:
        reasons.append("provider_transport_idempotency_acknowledgement_mismatch")
    unsigned = {
        "schema": PROVIDER_TRANSPORT_RECEIPT_SCHEMA,
        "status": "observed" if not reasons else "unresolved",
        "reason": reasons[0] if reasons else None,
        "transport_request_sha256": transport_request["transport_request_sha256"],
        "idempotency_key_sha256": transport_request["idempotency_key_sha256"],
        "provider_request_binding_sha256": provider_request_binding[
            "request_binding_sha256"
        ],
        "provider": {
            "name": provider_name,
            "model_version": model_version,
            "identity_cryptographically_verified": False,
        },
        "response": {
            "response_id_sha256": _sha256_text(response_id.strip()),
            "status": response_status,
            "output_sha256": _sha256_text(output_text),
        },
        "transport": {
            "idempotency_key_acknowledged": not reasons
            or "provider_transport_idempotency_acknowledgement_mismatch"
            not in reasons,
            "result_retrievable_by_idempotency_key": True,
            "source_authenticated": False,
        },
        "claims": {
            "provider_idempotency_observed": not reasons,
            "provider_identity_authenticated": False,
            "exactly_once_execution_proven": False,
        },
    }
    return {**unsigned, "transport_receipt_sha256": _canonical_hash(unsigned)}


def verify_provider_transport_receipt(
    value: Any,
    transport_request: dict[str, Any],
    provider_request_binding: dict[str, Any],
    *,
    output_text: str,
    provider_name: str,
    model_version: str,
) -> tuple[bool, str | None]:
    if not isinstance(value, dict):
        return False, "provider_transport_receipt_missing"
    digest = value.get("transport_receipt_sha256")
    unsigned = {
        key: item for key, item in value.items() if key != "transport_receipt_sha256"
    }
    if (
        value.get("schema") != PROVIDER_TRANSPORT_RECEIPT_SCHEMA
        or not _valid_sha256(digest)
        or _canonical_hash(unsigned) != digest
    ):
        return False, "provider_transport_receipt_digest_mismatch"
    transport_valid, transport_reason = validate_provider_transport_request(
        transport_request
    )
    provider_valid, provider_reason = validate_provider_request_binding(
        provider_request_binding
    )
    if not transport_valid:
        return False, transport_reason
    if not provider_valid:
        return False, provider_reason
    provider = _mapping(value.get("provider"))
    response = _mapping(value.get("response"))
    transport = _mapping(value.get("transport"))
    claims = _mapping(value.get("claims"))
    if value.get("status") != "observed" or value.get("reason") is not None:
        return False, str(value.get("reason") or "provider_transport_receipt_unresolved")
    if (
        value.get("transport_request_sha256")
        != transport_request["transport_request_sha256"]
        or value.get("idempotency_key_sha256")
        != transport_request["idempotency_key_sha256"]
        or value.get("provider_request_binding_sha256")
        != provider_request_binding["request_binding_sha256"]
    ):
        return False, "provider_transport_receipt_request_mismatch"
    if (
        provider.get("name") != provider_name
        or provider.get("model_version") != model_version
        or provider.get("identity_cryptographically_verified") is not False
        or response.get("status") != "completed"
        or response.get("output_sha256") != _sha256_text(output_text)
        or not _valid_sha256(response.get("response_id_sha256"))
        or transport.get("idempotency_key_acknowledged") is not True
        or transport.get("result_retrievable_by_idempotency_key") is not True
        or transport.get("source_authenticated") is not False
        or claims.get("provider_idempotency_observed") is not True
        or claims.get("provider_identity_authenticated") is not False
        or claims.get("exactly_once_execution_proven") is not False
    ):
        return False, "provider_transport_receipt_boundary_invalid"
    return True, None


class ProviderTransportLedger:
    """Persist per-call intent, submission, receipt, and explicit recovery claims."""

    def __init__(
        self,
        repository: SQLiteRepository,
        registration: dict[str, Any],
        attempt: dict[str, Any],
    ):
        self.repository = repository
        self.registration = registration
        self.attempt = attempt
        self.attempt_running_sha256 = (
            attempt.get("attempt_sha256")
            if attempt.get("state") == "running"
            else attempt.get("running_attempt_sha256")
        )

    def prepare(
        self,
        logical_request_binding: dict[str, Any],
        provider_request_binding: dict[str, Any],
        *,
        call_ordinal: int,
        transport_request: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        unsigned = {
            "schema": PROVIDER_TRANSPORT_RECORD_SCHEMA,
            "program_id": self.registration["id"],
            "program_sha256": self.registration["program_sha256"],
            "attempt_id": self.attempt["attempt_id"],
            "attempt_running_sha256": self.attempt_running_sha256,
            "call_ordinal": call_ordinal,
            "state": "prepared",
            "transition_index": 1,
            "logical_request_binding_sha256": logical_request_binding[
                "request_binding_sha256"
            ],
            "provider_request_binding_sha256": provider_request_binding[
                "request_binding_sha256"
            ],
            "transport_request": deepcopy(transport_request),
            "prepared_record_hash": None,
            "submitted_record_hash": None,
            "transport_receipt": None,
        }
        record = {**unsigned, "record_sha256": _canonical_hash(unsigned)}
        key = self._record_key(call_ordinal)
        inserted = asyncio.run(self.repository.save_if_absent(key, record))
        current = record if inserted else asyncio.run(self.repository.load(key))
        reason = self.validation_reason(current)
        if reason:
            return None, reason
        if not self._same_request(current, record):
            return None, "provider_transport_idempotency_key_reuse_mismatch"
        return deepcopy(current), None

    def mark_submitted(
        self, record: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        current = asyncio.run(
            self.repository.load(self._record_key(int(record["call_ordinal"])))
        )
        reason = self.validation_reason(current)
        if reason or not isinstance(current, dict):
            return None, reason or "provider_transport_record_missing"
        if current.get("state") in {"submitted", "completed"}:
            return current, None
        if current.get("state") != "prepared":
            return None, "provider_transport_transition_invalid"
        prepared_hash = self.repository.record_hash(current)
        unsigned = {
            **{key: deepcopy(value) for key, value in current.items() if key != "record_sha256"},
            "state": "submitted",
            "transition_index": 2,
            "prepared_record_hash": prepared_hash,
        }
        submitted = {**unsigned, "record_sha256": _canonical_hash(unsigned)}
        replaced = asyncio.run(
            self.repository.replace_if_record_hash(
                self._record_key(int(record["call_ordinal"])),
                prepared_hash,
                submitted,
            )
        )
        if not replaced:
            return None, "provider_transport_transition_conflict"
        return submitted, self.validation_reason(submitted)

    def complete(
        self,
        record: dict[str, Any],
        receipt: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        ordinal = int(record["call_ordinal"])
        current = asyncio.run(self.repository.load(self._record_key(ordinal)))
        reason = self.validation_reason(current)
        if reason or not isinstance(current, dict):
            return None, reason or "provider_transport_record_missing"
        if current.get("state") == "completed":
            if current.get("transport_receipt") == receipt:
                return current, None
            return None, "provider_transport_terminal_conflict"
        if current.get("state") != "submitted":
            return None, "provider_transport_not_submitted"
        submitted_hash = self.repository.record_hash(current)
        unsigned = {
            **{key: deepcopy(value) for key, value in current.items() if key != "record_sha256"},
            "state": "completed",
            "transition_index": 3,
            "submitted_record_hash": submitted_hash,
            "transport_receipt": deepcopy(receipt),
        }
        completed = {**unsigned, "record_sha256": _canonical_hash(unsigned)}
        replaced = asyncio.run(
            self.repository.replace_if_record_hash(
                self._record_key(ordinal), submitted_hash, completed
            )
        )
        if not replaced:
            return None, "provider_transport_transition_conflict"
        return completed, self.validation_reason(completed)

    def get(self, call_ordinal: int) -> dict[str, Any] | None:
        value = asyncio.run(self.repository.load(self._record_key(call_ordinal)))
        return value if isinstance(value, dict) else None

    def records(self) -> list[dict[str, Any]]:
        values = asyncio.run(self.repository.load_prefix(self._record_prefix()))
        records = [value for value in values.values() if isinstance(value, dict)]
        return sorted(records, key=lambda item: int(item.get("call_ordinal") or 0))

    def summary(self, *, expected_calls: int) -> dict[str, Any]:
        records = self.records()
        reasons = [self.validation_reason(record) for record in records]
        queries = self._query_records()
        reasons.extend(self._query_reason(query) for query in queries)
        ordinals = [record.get("call_ordinal") for record in records]
        if ordinals != list(range(1, len(records) + 1)):
            reasons.append("provider_transport_call_sequence_invalid")
        states = {state: 0 for state in ("prepared", "submitted", "completed")}
        for record in records:
            state = str(record.get("state") or "")
            if state in states:
                states[state] += 1
        receipt_set = [
            _mapping(record.get("transport_receipt")).get("transport_receipt_sha256")
            for record in records
            if record.get("state") == "completed"
        ]
        valid = not any(reasons)
        complete = valid and len(records) == expected_calls and states["completed"] == expected_calls
        return {
            "schema": "spst-provider-transport-summary-v1",
            "expected_call_count": expected_calls,
            "record_count": len(records),
            "prepared_count": states["prepared"],
            "submitted_count": states["submitted"],
            "completed_count": states["completed"],
            "query_count": len(queries),
            "record_set_sha256": _canonical_hash(
                [record.get("record_sha256") for record in records]
            ),
            "receipt_set_sha256": _canonical_hash(receipt_set),
            "integrity_verified": valid,
            "receipt_set_complete": complete,
            "provider_idempotency_observed": complete,
            "provider_identity_authenticated": False,
            "exactly_once_execution_proven": False,
            "reason": next((reason for reason in reasons if reason), None),
        }

    def reconcile_existing(
        self,
        adapter: ModelAdapter,
        *,
        maximum_queries: int,
    ) -> tuple[dict[int, dict[str, Any]], str | None]:
        method = getattr(adapter, "reconcile_transport", None)
        if not callable(method):
            return {}, "provider_transport_reconciliation_capability_missing"
        cache: dict[int, dict[str, Any]] = {}
        for record in self.records():
            reason = self.validation_reason(record)
            if reason:
                return {}, reason
            if record.get("state") == "prepared":
                continue
            query, query_reason = self._reserve_query(record, maximum_queries)
            if query_reason or query is None:
                return {}, query_reason or "provider_transport_query_reservation_failed"
            request = _mapping(record.get("transport_request"))
            try:
                response = method(
                    str(request.get("idempotency_key") or ""),
                    str(record.get("provider_request_binding_sha256") or ""),
                )
                if inspect.isawaitable(response):
                    response = asyncio.run(_await_value(response))
            except Exception:
                query_terminal_reason = self._complete_query(
                    query,
                    observed=False,
                    receipt_sha256=None,
                    reason="provider_transport_reconciliation_failed",
                )
                return (
                    {},
                    query_terminal_reason
                    or "provider_transport_reconciliation_failed",
                )
            if not isinstance(response, dict):
                query_terminal_reason = self._complete_query(
                    query,
                    observed=False,
                    receipt_sha256=None,
                    reason="provider_transport_reconciliation_unresolved",
                )
                return {}, query_terminal_reason or "provider_transport_reconciliation_unresolved"
            result_reason = self._result_reason(record, response)
            if result_reason:
                query_terminal_reason = self._complete_query(
                    query,
                    observed=False,
                    receipt_sha256=None,
                    reason=result_reason,
                )
                return {}, query_terminal_reason or result_reason
            receipt = _mapping(response.get("provider_transport_receipt"))
            query_complete_reason = self._complete_query(
                query,
                observed=True,
                receipt_sha256=str(receipt.get("transport_receipt_sha256") or ""),
                reason=None,
            )
            if query_complete_reason:
                return {}, query_complete_reason
            if record.get("state") == "submitted":
                completed, complete_reason = self.complete(record, receipt)
                if complete_reason or completed is None:
                    return {}, complete_reason or "provider_transport_completion_failed"
            cache[int(record["call_ordinal"])] = deepcopy(response)
        return cache, None

    def claim_recovery(
        self,
        authority: dict[str, Any],
        *,
        expected_calls: int,
    ) -> tuple[dict[str, Any] | None, str | None]:
        authority_reason = validate_recovery_authority(
            authority,
            program_id=str(self.registration["id"]),
            attempt_id=str(self.attempt["attempt_id"]),
        )
        if authority_reason:
            return None, authority_reason
        existing = self._recovery_records()
        if existing and existing[-1].get("state") == "active":
            return None, "provider_recovery_already_active"
        cycle = len(existing) + 1
        summary = self.summary(expected_calls=expected_calls)
        unsigned = {
            "schema": PROVIDER_RECOVERY_RECORD_SCHEMA,
            "program_id": self.registration["id"],
            "program_sha256": self.registration["program_sha256"],
            "attempt_id": self.attempt["attempt_id"],
            "attempt_running_sha256": self.attempt_running_sha256,
            "cycle": cycle,
            "state": "active",
            "transition_index": 1,
            "authority": deepcopy(authority),
            "transport_record_set_sha256": summary["record_set_sha256"],
            "terminal_outcome": None,
            "execution_sha256": None,
            "active_record_hash": None,
            "automatic_retry_permitted": False,
        }
        record = {**unsigned, "recovery_record_sha256": _canonical_hash(unsigned)}
        inserted = asyncio.run(
            self.repository.save_if_absent(self._recovery_key(cycle), record)
        )
        if not inserted:
            return None, "provider_recovery_claim_conflict"
        return record, None

    def complete_recovery(
        self,
        recovery: dict[str, Any],
        *,
        outcome: str,
        execution_sha256: str | None,
        expected_calls: int,
    ) -> str | None:
        if outcome not in {"execution_completed", "transport_interrupted"}:
            return "provider_recovery_outcome_invalid"
        if outcome == "execution_completed" and not _valid_sha256(execution_sha256):
            return "provider_recovery_execution_binding_invalid"
        if outcome == "transport_interrupted" and execution_sha256 is not None:
            return "provider_recovery_execution_binding_invalid"
        current_hash = self.repository.record_hash(recovery)
        summary = self.summary(expected_calls=expected_calls)
        unsigned = {
            **{
                key: deepcopy(value)
                for key, value in recovery.items()
                if key != "recovery_record_sha256"
            },
            "state": "terminal",
            "transition_index": 2,
            "transport_record_set_sha256": summary["record_set_sha256"],
            "terminal_outcome": outcome,
            "execution_sha256": execution_sha256,
            "active_record_hash": current_hash,
        }
        terminal = {**unsigned, "recovery_record_sha256": _canonical_hash(unsigned)}
        replaced = asyncio.run(
            self.repository.replace_if_record_hash(
                self._recovery_key(int(recovery["cycle"])),
                current_hash,
                terminal,
            )
        )
        return None if replaced else "provider_recovery_transition_conflict"

    def recovery_projection(self) -> dict[str, Any]:
        records = self._recovery_records()
        latest = records[-1] if records else None
        reasons = [self._recovery_reason(record) for record in records]
        return {
            "cycle_count": len(records),
            "active": bool(latest and latest.get("state") == "active"),
            "latest_outcome": (
                latest.get("terminal_outcome") if isinstance(latest, dict) else None
            ),
            "operator_identity_verified": False,
            "verified": not any(reasons),
            "reason": next((reason for reason in reasons if reason), None),
        }

    def integrity_reason(self, *, expected_calls: int) -> str | None:
        summary = self.summary(expected_calls=expected_calls)
        if summary.get("integrity_verified") is not True:
            return str(summary.get("reason") or "provider_transport_integrity_invalid")
        return next(
            (
                reason
                for record in self._recovery_records()
                if (reason := self._recovery_reason(record)) is not None
            ),
            None,
        )

    def validation_reason(self, value: Any) -> str | None:
        if not isinstance(value, dict):
            return "provider_transport_record_missing"
        digest = value.get("record_sha256")
        unsigned = {key: item for key, item in value.items() if key != "record_sha256"}
        if (
            value.get("schema") != PROVIDER_TRANSPORT_RECORD_SCHEMA
            or not _valid_sha256(digest)
            or _canonical_hash(unsigned) != digest
        ):
            return "provider_transport_record_digest_mismatch"
        request = _mapping(value.get("transport_request"))
        request_valid, request_reason = validate_provider_transport_request(request)
        if not request_valid:
            return request_reason
        if (
            value.get("program_id") != self.registration.get("id")
            or value.get("program_sha256") != self.registration.get("program_sha256")
            or value.get("attempt_id") != self.attempt.get("attempt_id")
            or value.get("attempt_running_sha256") != self.attempt_running_sha256
            or value.get("call_ordinal") != request.get("call_ordinal")
            or value.get("logical_request_binding_sha256")
            != request.get("logical_request_binding_sha256")
            or not _valid_sha256(value.get("provider_request_binding_sha256"))
        ):
            return "provider_transport_record_binding_mismatch"
        state = value.get("state")
        index = value.get("transition_index")
        if state == "prepared":
            valid = index == 1 and all(
                value.get(field) is None
                for field in (
                    "prepared_record_hash",
                    "submitted_record_hash",
                    "transport_receipt",
                )
            )
        elif state == "submitted":
            valid = (
                index == 2
                and _valid_sha256(value.get("prepared_record_hash"))
                and value.get("submitted_record_hash") is None
                and value.get("transport_receipt") is None
            )
        elif state == "completed":
            receipt = _mapping(value.get("transport_receipt"))
            valid = (
                index == 3
                and _valid_sha256(value.get("prepared_record_hash"))
                and _valid_sha256(value.get("submitted_record_hash"))
                and _valid_sha256(receipt.get("transport_receipt_sha256"))
                and receipt.get("transport_request_sha256")
                == request.get("transport_request_sha256")
                and receipt.get("provider_request_binding_sha256")
                == value.get("provider_request_binding_sha256")
            )
        else:
            valid = False
        if not valid:
            return "provider_transport_record_state_invalid"
        entries = [
            item
            for item in self.repository.provenance_entries()
            if item.get("record_key") == self._record_key(int(value["call_ordinal"]))
        ]
        if len(entries) != index or entries[-1].get("record_hash") != self.repository.record_hash(value):
            return "provider_transport_record_provenance_mismatch"
        return None

    def _result_reason(
        self, record: dict[str, Any], result: dict[str, Any]
    ) -> str | None:
        request = _mapping(record.get("transport_request"))
        provider_binding = result.get("provider_request_binding")
        if not isinstance(provider_binding, dict):
            return "provider_transport_reconciliation_request_missing"
        if (
            provider_binding.get("request_binding_sha256")
            != record.get("provider_request_binding_sha256")
        ):
            return "provider_transport_reconciliation_request_mismatch"
        provider_name = str(result.get("provider") or "")
        model_version = str(result.get("model_version") or result.get("model") or "")
        valid, reason = verify_provider_transport_receipt(
            result.get("provider_transport_receipt"),
            request,
            provider_binding,
            output_text=str(result.get("text") or ""),
            provider_name=provider_name,
            model_version=model_version,
        )
        return None if valid else reason

    def _reserve_query(
        self, record: dict[str, Any], maximum_queries: int
    ) -> tuple[dict[str, Any] | None, str | None]:
        queries = self._query_records()
        if len(queries) >= maximum_queries:
            return None, "provider_transport_reconciliation_query_cap_exhausted"
        index = len(queries) + 1
        unsigned = {
            "schema": PROVIDER_TRANSPORT_QUERY_SCHEMA,
            "program_id": self.registration["id"],
            "attempt_id": self.attempt["attempt_id"],
            "query_index": index,
            "call_ordinal": record["call_ordinal"],
            "transport_record_sha256": record["record_sha256"],
            "idempotency_key_sha256": _mapping(
                record.get("transport_request")
            ).get("idempotency_key_sha256"),
            "state": "issued",
            "transition_index": 1,
            "provider_result_observed": False,
            "result_transport_receipt_sha256": None,
            "reason": None,
            "issued_record_hash": None,
            "query_is_inference": False,
        }
        query = {**unsigned, "query_sha256": _canonical_hash(unsigned)}
        inserted = asyncio.run(
            self.repository.save_if_absent(self._query_key(index), query)
        )
        if not inserted:
            return None, "provider_transport_reconciliation_query_conflict"
        return query, None

    def _complete_query(
        self,
        query: dict[str, Any],
        *,
        observed: bool,
        receipt_sha256: str | None,
        reason: str | None,
    ) -> str | None:
        if observed:
            if not _valid_sha256(receipt_sha256) or reason is not None:
                return "provider_transport_query_result_invalid"
        elif receipt_sha256 is not None or not _safe_identifier(reason):
            return "provider_transport_query_result_invalid"
        issued_hash = self.repository.record_hash(query)
        unsigned = {
            **{key: deepcopy(value) for key, value in query.items() if key != "query_sha256"},
            "state": "terminal",
            "transition_index": 2,
            "provider_result_observed": observed,
            "result_transport_receipt_sha256": receipt_sha256,
            "reason": reason,
            "issued_record_hash": issued_hash,
        }
        terminal = {**unsigned, "query_sha256": _canonical_hash(unsigned)}
        replaced = asyncio.run(
            self.repository.replace_if_record_hash(
                self._query_key(int(query["query_index"])),
                issued_hash,
                terminal,
            )
        )
        return None if replaced else "provider_transport_query_transition_conflict"

    def _query_records(self) -> list[dict[str, Any]]:
        values = asyncio.run(self.repository.load_prefix(self._query_prefix()))
        records = [value for value in values.values() if isinstance(value, dict)]
        return sorted(records, key=lambda item: int(item.get("query_index") or 0))

    def _query_reason(self, value: Any) -> str | None:
        if not isinstance(value, dict):
            return "provider_transport_query_missing"
        digest = value.get("query_sha256")
        unsigned = {key: item for key, item in value.items() if key != "query_sha256"}
        if (
            value.get("schema") != PROVIDER_TRANSPORT_QUERY_SCHEMA
            or not _valid_sha256(digest)
            or _canonical_hash(unsigned) != digest
            or value.get("program_id") != self.registration.get("id")
            or value.get("attempt_id") != self.attempt.get("attempt_id")
            or not isinstance(value.get("query_index"), int)
            or isinstance(value.get("query_index"), bool)
            or int(value["query_index"]) < 1
            or not isinstance(value.get("call_ordinal"), int)
            or isinstance(value.get("call_ordinal"), bool)
            or not _valid_sha256(value.get("transport_record_sha256"))
            or not _valid_sha256(value.get("idempotency_key_sha256"))
            or value.get("query_is_inference") is not False
        ):
            return "provider_transport_query_binding_invalid"
        state = value.get("state")
        if state == "issued":
            valid = (
                value.get("transition_index") == 1
                and value.get("provider_result_observed") is False
                and value.get("result_transport_receipt_sha256") is None
                and value.get("reason") is None
                and value.get("issued_record_hash") is None
            )
        elif state == "terminal":
            observed = value.get("provider_result_observed")
            valid = (
                value.get("transition_index") == 2
                and isinstance(observed, bool)
                and _valid_sha256(value.get("issued_record_hash"))
                and (
                    (
                        observed is True
                        and _valid_sha256(
                            value.get("result_transport_receipt_sha256")
                        )
                        and value.get("reason") is None
                    )
                    or (
                        observed is False
                        and value.get("result_transport_receipt_sha256") is None
                        and bool(_safe_identifier(value.get("reason")))
                    )
                )
            )
        else:
            valid = False
        if not valid:
            return "provider_transport_query_state_invalid"
        entries = [
            item
            for item in self.repository.provenance_entries()
            if item.get("record_key") == self._query_key(int(value["query_index"]))
        ]
        if (
            len(entries) != value.get("transition_index")
            or entries[-1].get("record_hash") != self.repository.record_hash(value)
        ):
            return "provider_transport_query_provenance_mismatch"
        return None

    def _recovery_records(self) -> list[dict[str, Any]]:
        values = asyncio.run(self.repository.load_prefix(self._recovery_prefix()))
        records = [value for value in values.values() if isinstance(value, dict)]
        return sorted(records, key=lambda item: int(item.get("cycle") or 0))

    def _recovery_reason(self, value: Any) -> str | None:
        if not isinstance(value, dict):
            return "provider_recovery_record_missing"
        digest = value.get("recovery_record_sha256")
        unsigned = {
            key: item for key, item in value.items() if key != "recovery_record_sha256"
        }
        authority = _mapping(value.get("authority"))
        authority_reason = validate_recovery_authority(
            authority,
            program_id=str(self.registration["id"]),
            attempt_id=str(self.attempt["attempt_id"]),
        )
        if (
            value.get("schema") != PROVIDER_RECOVERY_RECORD_SCHEMA
            or not _valid_sha256(digest)
            or _canonical_hash(unsigned) != digest
            or value.get("program_id") != self.registration.get("id")
            or value.get("program_sha256") != self.registration.get("program_sha256")
            or value.get("attempt_id") != self.attempt.get("attempt_id")
            or value.get("attempt_running_sha256") != self.attempt_running_sha256
            or not isinstance(value.get("cycle"), int)
            or isinstance(value.get("cycle"), bool)
            or int(value["cycle"]) < 1
            or not _valid_sha256(value.get("transport_record_set_sha256"))
            or value.get("automatic_retry_permitted") is not False
            or authority_reason is not None
        ):
            return authority_reason or "provider_recovery_record_binding_invalid"
        if value.get("state") == "active":
            valid = (
                value.get("transition_index") == 1
                and value.get("terminal_outcome") is None
                and value.get("execution_sha256") is None
                and value.get("active_record_hash") is None
            )
        elif value.get("state") == "terminal":
            outcome = value.get("terminal_outcome")
            valid = (
                value.get("transition_index") == 2
                and _valid_sha256(value.get("active_record_hash"))
                and outcome in {"execution_completed", "transport_interrupted"}
                and (
                    (
                        outcome == "execution_completed"
                        and _valid_sha256(value.get("execution_sha256"))
                    )
                    or (
                        outcome == "transport_interrupted"
                        and value.get("execution_sha256") is None
                    )
                )
            )
        else:
            valid = False
        if not valid:
            return "provider_recovery_record_state_invalid"
        entries = [
            item
            for item in self.repository.provenance_entries()
            if item.get("record_key") == self._recovery_key(int(value["cycle"]))
        ]
        if (
            len(entries) != value.get("transition_index")
            or entries[-1].get("record_hash") != self.repository.record_hash(value)
        ):
            return "provider_recovery_record_provenance_mismatch"
        return None

    @staticmethod
    def _same_request(left: Any, right: Any) -> bool:
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        fields = (
            "program_id",
            "program_sha256",
            "attempt_id",
            "attempt_running_sha256",
            "call_ordinal",
            "logical_request_binding_sha256",
            "provider_request_binding_sha256",
            "transport_request",
        )
        return all(left.get(field) == right.get(field) for field in fields)

    def _record_prefix(self) -> str:
        return f"{TRANSPORT_RECORD_PREFIX}{self.registration['id']}:{self.attempt['attempt_id']}:"

    def _record_key(self, ordinal: int) -> str:
        return f"{self._record_prefix()}{ordinal:04d}"

    def _query_prefix(self) -> str:
        return f"{TRANSPORT_QUERY_PREFIX}{self.registration['id']}:{self.attempt['attempt_id']}:"

    def _query_key(self, index: int) -> str:
        return f"{self._query_prefix()}{index:04d}"

    def _recovery_prefix(self) -> str:
        return f"{RECOVERY_RECORD_PREFIX}{self.registration['id']}:{self.attempt['attempt_id']}:"

    def _recovery_key(self, cycle: int) -> str:
        return f"{self._recovery_prefix()}{cycle:04d}"


class IdempotentTransportAdapter(ModelAdapter):
    """Add durable, provider-acknowledged idempotency around one adapter."""

    def __init__(
        self,
        delegate: ModelAdapter,
        ledger: ProviderTransportLedger,
        *,
        replay_results: dict[int, dict[str, Any]] | None = None,
    ):
        self.delegate = delegate
        self.ledger = ledger
        self.replay_results = replay_results or {}
        self.call_ordinal = 0
        self.pending_records: dict[int, dict[str, Any]] = {}

    def prepare_transport_context(
        self, prompt: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        self.call_ordinal += 1
        logical = build_provider_request_binding(prompt, context)
        request = build_provider_transport_request(
            self.ledger.registration,
            self.ledger.attempt,
            call_ordinal=self.call_ordinal,
            logical_request_binding=logical,
        )
        provider_context = deepcopy(context)
        provider_context[TRANSPORT_CONTEXT_KEY] = request
        provider_binding = build_provider_request_binding(prompt, provider_context)
        record, reason = self.ledger.prepare(
            logical,
            provider_binding,
            call_ordinal=self.call_ordinal,
            transport_request=request,
        )
        if reason or record is None:
            raise ProviderTransportRecoveryRequired(
                reason or "provider_transport_prepare_failed"
            )
        if record.get("state") == "submitted":
            raise ProviderTransportRecoveryRequired(
                "provider_transport_submitted_result_not_reconciled"
            )
        if record.get("state") == "prepared":
            submitted, submit_reason = self.ledger.mark_submitted(record)
            if submit_reason or submitted is None:
                raise ProviderTransportRecoveryRequired(
                    submit_reason or "provider_transport_submission_failed"
                )
            record = submitted
        self.pending_records[self.call_ordinal] = record
        return provider_context

    async def infer(
        self, prompt: str, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        source = context if isinstance(context, dict) else {}
        request = _mapping(source.get(TRANSPORT_CONTEXT_KEY))
        valid, reason = validate_provider_transport_request(request)
        if not valid:
            raise ProviderTransportRecoveryRequired(
                reason or "provider_transport_request_invalid"
            )
        ordinal = int(request["call_ordinal"])
        record = self.pending_records.get(ordinal)
        record_reason = self.ledger.validation_reason(record)
        if record_reason or record is None:
            raise ProviderTransportRecoveryRequired(
                record_reason or "provider_transport_record_missing"
            )
        if record.get("state") == "completed":
            replay = self.replay_results.get(ordinal)
            if replay is None:
                raise ProviderTransportRecoveryRequired(
                    "provider_transport_completed_result_not_reconciled"
                )
            return deepcopy(replay)
        submitted = record
        try:
            result = await self.delegate.infer(prompt, source)
        except Exception as error:
            raise ProviderTransportOutcomeUnknown(type(error).__name__) from None
        if not isinstance(result, dict):
            raise ProviderTransportOutcomeUnknown("provider_result_invalid")
        provider_binding = build_provider_request_binding(prompt, source)
        result = {**result, "provider_request_binding": provider_binding}
        result_reason = self.ledger._result_reason(submitted, result)
        if result_reason:
            raise ProviderTransportOutcomeUnknown(result_reason)
        return result

    def finalize_transport_result(
        self,
        prompt: str,
        context: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        request = _mapping(context.get(TRANSPORT_CONTEXT_KEY))
        ordinal = int(request.get("call_ordinal") or 0)
        record = self.pending_records.get(ordinal)
        if not isinstance(record, dict):
            raise ProviderTransportRecoveryRequired(
                "provider_transport_pending_record_missing"
            )
        if record.get("state") == "completed":
            return result
        completed, complete_reason = self.ledger.complete(
            record,
            _mapping(result.get("provider_transport_receipt")),
        )
        if complete_reason or completed is None:
            raise ProviderTransportOutcomeUnknown(
                complete_reason or "provider_transport_completion_failed"
            )
        self.pending_records[ordinal] = completed
        return result

    def health(self) -> dict[str, Any]:
        return self.delegate.health()

    def get_capabilities(self) -> dict[str, Any]:
        return self.delegate.get_capabilities()


def validate_recovery_authority(
    value: Any,
    *,
    program_id: str,
    attempt_id: str,
) -> str | None:
    if not isinstance(value, dict):
        return "provider_recovery_authority_missing"
    digest = value.get("authority_sha256")
    unsigned = {key: item for key, item in value.items() if key != "authority_sha256"}
    if (
        value.get("schema") != PROVIDER_RECOVERY_AUTHORITY_SCHEMA
        or not _valid_sha256(digest)
        or _canonical_hash(unsigned) != digest
    ):
        return "provider_recovery_authority_digest_mismatch"
    if (
        value.get("program_id") != program_id
        or value.get("attempt_id") != attempt_id
        or value.get("decision") != "reconcile_and_resume"
        or value.get("provider_reconciliation_authorized") is not True
        or value.get("new_adapter_invocations_authorized") is not True
        or value.get("automatic_retry_authorized") is not False
        or not _safe_identifier(value.get("operator_id"))
        or value.get("operator_identity_verified") is not False
    ):
        return "provider_recovery_authority_binding_mismatch"
    return None


def build_recovery_authority(
    *,
    program_id: str,
    attempt_id: str,
    operator_id: str,
) -> dict[str, Any]:
    unsigned = {
        "schema": PROVIDER_RECOVERY_AUTHORITY_SCHEMA,
        "program_id": program_id,
        "attempt_id": attempt_id,
        "decision": "reconcile_and_resume",
        "provider_reconciliation_authorized": True,
        "new_adapter_invocations_authorized": True,
        "automatic_retry_authorized": False,
        "operator_id": operator_id,
        "operator_identity_verified": False,
    }
    return {**unsigned, "authority_sha256": _canonical_hash(unsigned)}


def _transport_seed(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "program_id": value.get("program_id"),
        "program_sha256": value.get("program_sha256"),
        "execution_manifest_sha256": value.get("execution_manifest_sha256"),
        "attempt_id": value.get("attempt_id"),
        "attempt_running_sha256": value.get("attempt_running_sha256"),
        "call_ordinal": value.get("call_ordinal"),
        "logical_request_binding_sha256": value.get(
            "logical_request_binding_sha256"
        ),
    }


async def _await_value(value: Any) -> Any:
    return await value


def _canonical_hash(value: Any) -> str:
    return _sha256_text(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_sha256.fullmatch(value))


def _safe_identifier(value: Any) -> str:
    candidate = str(value or "")
    return candidate if _identifier.fullmatch(candidate) else ""


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
