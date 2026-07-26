import asyncio
import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.recovery_authority_state import (
    RECOVERY_AUTHORITY_ROLLBACK_ANCHOR_SCHEMA,
    RECOVERY_AUTHORITY_STATE_SCHEMA,
    default_recovery_key_custody_policy,
    validate_recovery_key_custody_policy,
    verify_recovery_authority_state,
)


RECOVERY_AUTHORITY_TRUST_SCHEMA = "spst-recovery-authority-trust-v1"
RECOVERY_OPERATOR_CERTIFICATE_SCHEMA = "spst-recovery-operator-certificate-v1"
RECOVERY_AUTHORITY_GRANT_SCHEMA = "spst-recovery-authority-grant-v1"
RECOVERY_AUTHORITY_CLAIMS_SCHEMA = "spst-recovery-authority-claims-v1"
SUPERVISOR_ATTESTATION_SCHEMA = "spst-recovery-supervisor-attestation-v1"
SUPERVISOR_ATTESTATION_PREFIX = "runtime:real_paired_outcome:supervisor_attestation:"
RECOVERY_AUTHORITY_ACTION = "supervise_program_recovery"
RECOVERY_AUTHORITY_ALGORITHM = "Ed25519"
SUPERVISOR_EVENTS = (
    "authority_accepted",
    "lease_acquired",
    "lease_renewed",
    "heartbeat_stopped",
    "recovery_returned",
    "lease_released",
)

_identifier = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_sha256 = re.compile(r"^[0-9a-f]{64}$")


class RecoveryAuthorityError(ValueError):
    """Reject invalid local recovery authority or supervisor evidence."""


def generate_recovery_authority_pki(
    root_private_key_file: str | Path,
    operator_private_key_file: str | Path,
    *,
    operator_id: str,
    valid_from_ms: int | None = None,
    valid_until_ms: int | None = None,
    authority_state_required: bool = False,
    minimum_authority_state_generation: int = 1,
    key_custody_policy: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not _safe_identifier(operator_id):
        raise RecoveryAuthorityError("recovery_operator_identity_invalid")
    now_ms = _now_ms() if valid_from_ms is None else valid_from_ms
    until_ms = now_ms + 86_400_000 if valid_until_ms is None else valid_until_ms
    if not _valid_time_window(now_ms, until_ms):
        raise RecoveryAuthorityError("recovery_operator_certificate_window_invalid")
    root_key = _create_private_key(root_private_key_file)
    operator_key = _create_private_key(operator_private_key_file)
    root_public = _public_bytes(root_key.public_key())
    operator_public = _public_bytes(operator_key.public_key())
    trust_unsigned: dict[str, Any] = {
        "schema": RECOVERY_AUTHORITY_TRUST_SCHEMA,
        "algorithm": RECOVERY_AUTHORITY_ALGORITHM,
        "root_key_id": _sha256_bytes(root_public),
        "root_public_key": _encode(root_public),
        "external_identity_verified": False,
    }
    if authority_state_required:
        policy = key_custody_policy or default_recovery_key_custody_policy()
        policy_reason = validate_recovery_key_custody_policy(policy)
        if policy_reason:
            raise RecoveryAuthorityError(policy_reason)
        if (
            not isinstance(minimum_authority_state_generation, int)
            or isinstance(minimum_authority_state_generation, bool)
            or minimum_authority_state_generation < 1
        ):
            raise RecoveryAuthorityError(
                "recovery_authority_minimum_state_generation_invalid"
            )
        trust_unsigned.update(
            {
                "authority_state_required": True,
                "authority_state_schema": RECOVERY_AUTHORITY_STATE_SCHEMA,
                "rollback_anchor_schema": RECOVERY_AUTHORITY_ROLLBACK_ANCHOR_SCHEMA,
                "minimum_authority_state_generation": (
                    minimum_authority_state_generation
                ),
                "key_custody_policy": deepcopy(policy),
            }
        )
    trust = {
        **trust_unsigned,
        "trust_anchor_sha256": _canonical_hash(trust_unsigned),
    }
    certificate_unsigned = {
        "schema": RECOVERY_OPERATOR_CERTIFICATE_SCHEMA,
        "algorithm": RECOVERY_AUTHORITY_ALGORITHM,
        "issuer_key_id": trust["root_key_id"],
        "serial": secrets.token_hex(16),
        "operator_id": operator_id,
        "operator_key_id": _sha256_bytes(operator_public),
        "operator_public_key": _encode(operator_public),
        "allowed_actions": [RECOVERY_AUTHORITY_ACTION],
        "valid_from_ms": now_ms,
        "valid_until_ms": until_ms,
        "operator_identity_external_verified": False,
    }
    certificate_digest = _canonical_hash(certificate_unsigned)
    certificate = {
        **certificate_unsigned,
        "certificate_sha256": certificate_digest,
        "issuer_signature": _encode(
            root_key.sign(_canonical_bytes(certificate_unsigned))
        ),
    }
    reason = validate_recovery_operator_certificate(
        certificate,
        trust,
        verification_time_ms=now_ms,
    )
    if reason:
        raise RecoveryAuthorityError(reason)
    return trust, certificate


def generate_supervisor_attestation_key(
    private_key_file: str | Path,
) -> dict[str, Any]:
    private_key = _create_private_key(private_key_file)
    public = _public_bytes(private_key.public_key())
    unsigned: dict[str, Any] = {
        "schema": "spst-recovery-supervisor-public-key-v1",
        "algorithm": RECOVERY_AUTHORITY_ALGORITHM,
        "key_id": _sha256_bytes(public),
        "public_key": _encode(public),
    }
    return {**unsigned, "public_key_sha256": _canonical_hash(unsigned)}


def issue_recovery_authority_grant(
    trust_anchor: dict[str, Any],
    operator_certificate: dict[str, Any],
    operator_private_key_file: str | Path,
    supervisor_public_key: dict[str, Any],
    *,
    program_id: str,
    program_sha256: str,
    attempt_id: str,
    provider_instance_sha256: str,
    lease_resource_id: str,
    lease_owner_id: str,
    transport_recovery_authority_sha256: str,
    context_intervention_sha256: str,
    previous_owner_id: str | None = None,
    expired_generation: int | None = None,
    issued_at_ms: int | None = None,
    expires_at_ms: int | None = None,
    nonce: str | None = None,
    authority_state: dict[str, Any] | None = None,
    rollback_anchor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issued = _now_ms() if issued_at_ms is None else issued_at_ms
    expires = issued + 300_000 if expires_at_ms is None else expires_at_ms
    certificate_reason = validate_recovery_operator_certificate(
        operator_certificate,
        trust_anchor,
        verification_time_ms=issued,
    )
    if certificate_reason:
        raise RecoveryAuthorityError(certificate_reason)
    state_required = trust_anchor.get("authority_state_required") is True
    state_projection: dict[str, Any] | None = None
    if state_required or authority_state is not None or rollback_anchor is not None:
        state_projection, state_reason = verify_recovery_authority_state(
            authority_state,
            trust_anchor,
            rollback_anchor=rollback_anchor,
            verification_time_ms=issued,
            revoked_operator_certificate_sha256=operator_certificate.get(
                "certificate_sha256"
            ),
        )
        if state_reason or state_projection is None:
            raise RecoveryAuthorityError(
                state_reason or "recovery_authority_state_invalid"
            )
    operator_key = _load_private_key(operator_private_key_file)
    if _sha256_bytes(_public_bytes(operator_key.public_key())) != operator_certificate.get(
        "operator_key_id"
    ):
        raise RecoveryAuthorityError("recovery_operator_private_key_mismatch")
    supervisor_reason = _supervisor_public_key_reason(supervisor_public_key)
    if supervisor_reason:
        raise RecoveryAuthorityError(supervisor_reason)
    if not _valid_time_window(issued, expires):
        raise RecoveryAuthorityError("recovery_authority_grant_window_invalid")
    if (previous_owner_id is None) is not (expired_generation is None):
        raise RecoveryAuthorityError("recovery_authority_adoption_claim_invalid")
    claims = {
        "schema": RECOVERY_AUTHORITY_CLAIMS_SCHEMA,
        "action": RECOVERY_AUTHORITY_ACTION,
        "program_id": program_id,
        "program_sha256": program_sha256,
        "attempt_id": attempt_id,
        "provider_instance_sha256": provider_instance_sha256,
        "lease_resource_id": lease_resource_id,
        "lease_owner_id": lease_owner_id,
        "transport_recovery_authority_sha256": (
            transport_recovery_authority_sha256
        ),
        "context_intervention_sha256": context_intervention_sha256,
        "previous_owner_id": previous_owner_id,
        "expired_generation": expired_generation,
        "operator_id": operator_certificate["operator_id"],
        "operator_certificate_sha256": operator_certificate["certificate_sha256"],
        "operator_identity_external_verified": False,
        "supervisor_attestation_key_id": supervisor_public_key["key_id"],
        "supervisor_attestation_public_key": supervisor_public_key["public_key"],
        "issued_at_ms": issued,
        "expires_at_ms": expires,
        "nonce": nonce or secrets.token_hex(16),
        "automatic_retry_authorized": False,
    }
    if state_projection is not None:
        anchor_projection = _mapping(state_projection.get("rollback_anchor"))
        claims.update(
            {
                "authority_state_sha256": state_projection["state_sha256"],
                "authority_state_generation": state_projection["generation"],
                "authority_time_floor_ms": state_projection[
                    "minimum_accepted_time_ms"
                ],
                "recovery_key_custody_evidence_sha256": state_projection[
                    "key_custody_evidence_sha256"
                ],
                "rollback_anchor_sha256": anchor_projection.get("anchor_sha256"),
            }
        )
    reason = _recovery_claims_reason(claims)
    if reason:
        raise RecoveryAuthorityError(reason)
    claims_sha256 = _canonical_hash(claims)
    unsigned: dict[str, Any] = {
        "schema": RECOVERY_AUTHORITY_GRANT_SCHEMA,
        "claims": claims,
        "claims_sha256": claims_sha256,
        "operator_certificate": deepcopy(operator_certificate),
        "operator_signature": _encode(operator_key.sign(_canonical_bytes(claims))),
    }
    if state_projection is not None:
        unsigned.update(
            {
                "authority_state": deepcopy(authority_state),
                "rollback_anchor": deepcopy(rollback_anchor),
            }
        )
    return {**unsigned, "grant_sha256": _canonical_hash(unsigned)}


def validate_recovery_authority_trust_anchor(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "recovery_authority_trust_anchor_missing"
    unsigned = {
        key: item for key, item in value.items() if key != "trust_anchor_sha256"
    }
    try:
        public = _decode_public_key(value.get("root_public_key"))
    except RecoveryAuthorityError as error:
        return str(error)
    state_required = value.get("authority_state_required") is True
    state_contract_invalid = state_required and (
        value.get("authority_state_schema") != RECOVERY_AUTHORITY_STATE_SCHEMA
        or value.get("rollback_anchor_schema")
        != RECOVERY_AUTHORITY_ROLLBACK_ANCHOR_SCHEMA
        or not isinstance(value.get("minimum_authority_state_generation"), int)
        or isinstance(value.get("minimum_authority_state_generation"), bool)
        or int(value["minimum_authority_state_generation"]) < 1
        or validate_recovery_key_custody_policy(value.get("key_custody_policy"))
        is not None
    )
    if (
        value.get("schema") != RECOVERY_AUTHORITY_TRUST_SCHEMA
        or value.get("algorithm") != RECOVERY_AUTHORITY_ALGORITHM
        or value.get("external_identity_verified") is not False
        or value.get("root_key_id") != _sha256_bytes(public)
        or not _is_sha256(value.get("trust_anchor_sha256"))
        or _canonical_hash(unsigned) != value.get("trust_anchor_sha256")
        or state_contract_invalid
    ):
        return "recovery_authority_trust_anchor_invalid"
    return None


def validate_recovery_operator_certificate(
    value: Any,
    trust_anchor: dict[str, Any],
    *,
    verification_time_ms: int | None = None,
) -> str | None:
    trust_reason = validate_recovery_authority_trust_anchor(trust_anchor)
    if trust_reason:
        return trust_reason
    if not isinstance(value, dict):
        return "recovery_operator_certificate_missing"
    unsigned = {
        key: item
        for key, item in value.items()
        if key not in {"certificate_sha256", "issuer_signature"}
    }
    try:
        operator_public = _decode_public_key(value.get("operator_public_key"))
        signature = _decode(value.get("issuer_signature"))
        root_public = Ed25519PublicKey.from_public_bytes(
            _decode_public_key(trust_anchor.get("root_public_key"))
        )
        root_public.verify(signature, _canonical_bytes(unsigned))
    except (RecoveryAuthorityError, InvalidSignature, ValueError):
        return "recovery_operator_certificate_signature_invalid"
    current = _now_ms() if verification_time_ms is None else verification_time_ms
    if (
        value.get("schema") != RECOVERY_OPERATOR_CERTIFICATE_SCHEMA
        or value.get("algorithm") != RECOVERY_AUTHORITY_ALGORITHM
        or value.get("issuer_key_id") != trust_anchor.get("root_key_id")
        or not _safe_identifier(value.get("serial"))
        or not _safe_identifier(value.get("operator_id"))
        or value.get("operator_key_id") != _sha256_bytes(operator_public)
        or value.get("allowed_actions") != [RECOVERY_AUTHORITY_ACTION]
        or value.get("operator_identity_external_verified") is not False
        or not _valid_time_window(
            value.get("valid_from_ms"), value.get("valid_until_ms")
        )
        or not int(value["valid_from_ms"]) <= current <= int(value["valid_until_ms"])
        or not _is_sha256(value.get("certificate_sha256"))
        or _canonical_hash(unsigned) != value.get("certificate_sha256")
    ):
        return "recovery_operator_certificate_invalid"
    return None


def verify_recovery_authority_grant(
    value: Any,
    trust_anchor: dict[str, Any],
    *,
    expected: dict[str, Any] | None = None,
    verification_time_ms: int | None = None,
    authority_state: dict[str, Any] | None = None,
    rollback_anchor: dict[str, Any] | None = None,
    allow_embedded_authority_state: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(value, dict):
        return None, "signed_recovery_authority_required"
    claims = value.get("claims")
    certificate = value.get("operator_certificate")
    if not isinstance(claims, dict) or not isinstance(certificate, dict):
        return None, "signed_recovery_authority_invalid"
    current = _now_ms() if verification_time_ms is None else verification_time_ms
    certificate_reason = validate_recovery_operator_certificate(
        certificate,
        trust_anchor,
        verification_time_ms=current,
    )
    if certificate_reason:
        return None, certificate_reason
    reason = _recovery_claims_reason(claims)
    if reason:
        return None, reason
    unsigned = {
        key: item for key, item in value.items() if key != "grant_sha256"
    }
    try:
        operator_public = Ed25519PublicKey.from_public_bytes(
            _decode_public_key(certificate.get("operator_public_key"))
        )
        operator_public.verify(
            _decode(value.get("operator_signature")),
            _canonical_bytes(claims),
        )
    except (RecoveryAuthorityError, InvalidSignature, ValueError):
        return None, "signed_recovery_authority_signature_invalid"
    if (
        value.get("schema") != RECOVERY_AUTHORITY_GRANT_SCHEMA
        or value.get("claims_sha256") != _canonical_hash(claims)
        or claims.get("operator_certificate_sha256")
        != certificate.get("certificate_sha256")
        or claims.get("operator_id") != certificate.get("operator_id")
        or not _is_sha256(value.get("grant_sha256"))
        or _canonical_hash(unsigned) != value.get("grant_sha256")
        or not int(claims["issued_at_ms"]) <= current <= int(claims["expires_at_ms"])
    ):
        return None, "signed_recovery_authority_binding_invalid"
    for key, expected_value in (expected or {}).items():
        if claims.get(key) != expected_value:
            return None, f"signed_recovery_authority_{key}_mismatch"
    state_required = trust_anchor.get("authority_state_required") is True
    embedded_state = value.get("authority_state")
    embedded_anchor = value.get("rollback_anchor")
    state_bindings_present = any(
        claims.get(field) is not None
        for field in (
            "authority_state_sha256",
            "authority_state_generation",
            "authority_time_floor_ms",
            "recovery_key_custody_evidence_sha256",
            "rollback_anchor_sha256",
        )
    )
    state_projection: dict[str, Any] | None = None
    if state_required or state_bindings_present:
        embedded_projection, embedded_reason = verify_recovery_authority_state(
            embedded_state,
            trust_anchor,
            rollback_anchor=_mapping_or_none(embedded_anchor),
            verification_time_ms=current,
            expected_state_sha256=claims.get("authority_state_sha256"),
            expected_anchor_sha256=claims.get("rollback_anchor_sha256"),
            revoked_operator_certificate_sha256=certificate.get(
                "certificate_sha256"
            ),
            revoked_grant_sha256=value.get("grant_sha256"),
        )
        if embedded_reason or embedded_projection is None:
            return None, embedded_reason or "recovery_authority_state_invalid"
        if (
            embedded_projection.get("generation")
            != claims.get("authority_state_generation")
            or embedded_projection.get("minimum_accepted_time_ms")
            != claims.get("authority_time_floor_ms")
            or embedded_projection.get("key_custody_evidence_sha256")
            != claims.get("recovery_key_custody_evidence_sha256")
        ):
            return None, "recovery_authority_state_binding_mismatch"
        if authority_state is None or rollback_anchor is None:
            if state_required and not allow_embedded_authority_state:
                return None, "recovery_authority_current_state_required"
            state_projection = embedded_projection
        else:
            state_projection, state_reason = verify_recovery_authority_state(
                authority_state,
                trust_anchor,
                rollback_anchor=rollback_anchor,
                verification_time_ms=current,
                minimum_generation=int(embedded_projection["generation"]),
                revoked_operator_certificate_sha256=certificate.get(
                    "certificate_sha256"
                ),
                revoked_grant_sha256=value.get("grant_sha256"),
            )
            if state_reason or state_projection is None:
                return None, state_reason or "recovery_authority_state_invalid"
    return (
        {
            "schema": RECOVERY_AUTHORITY_GRANT_SCHEMA,
            "grant_sha256": value["grant_sha256"],
            "claims": deepcopy(claims),
            "operator_certificate_verified": True,
            "operator_identity_external_verified": False,
            "supervisor_attestation_key_verified": False,
            "authority_state": deepcopy(state_projection),
            "revocation_checked": state_projection is not None,
            "trusted_time_source_verified": False,
            "full_rollback_resistance_verified": False,
            "verified": True,
        },
        None,
    )


class SupervisorAttestationLedger:
    def __init__(
        self,
        repository: SQLiteRepository,
        registration: dict[str, Any],
        *,
        authority_state: dict[str, Any] | None = None,
        rollback_anchor: dict[str, Any] | None = None,
    ):
        self.repository = repository
        self.registration = registration
        self.program_id = str(registration.get("id") or "")
        self.trust_anchor = _mapping(registration.get("provider")).get(
            "recovery_authority_trust_anchor"
        )
        self.authority_state = deepcopy(authority_state)
        self.rollback_anchor = deepcopy(rollback_anchor)

    def append(
        self,
        grant: dict[str, Any],
        supervisor_private_key_file: str | Path,
        *,
        event: str,
        details: dict[str, Any],
        observed_at_ms: int | None = None,
    ) -> dict[str, Any]:
        if self.repository.read_only:
            raise PermissionError("supervisor_attestation_store_read_only")
        if event not in SUPERVISOR_EVENTS or not isinstance(details, dict):
            raise RecoveryAuthorityError("supervisor_attestation_event_invalid")
        now_ms = _now_ms() if observed_at_ms is None else observed_at_ms
        grant_projection, grant_reason = verify_recovery_authority_grant(
            grant,
            _mapping(self.trust_anchor),
            expected=self._grant_expectations(),
            verification_time_ms=now_ms,
            authority_state=self.authority_state,
            rollback_anchor=self.rollback_anchor,
        )
        if grant_reason or grant_projection is None:
            raise RecoveryAuthorityError(
                grant_reason or "signed_recovery_authority_invalid"
            )
        private_key = _load_private_key(supervisor_private_key_file)
        key_id = _sha256_bytes(_public_bytes(private_key.public_key()))
        claims = _mapping(grant.get("claims"))
        if key_id != claims.get("supervisor_attestation_key_id"):
            raise RecoveryAuthorityError("supervisor_attestation_private_key_mismatch")
        with self.repository.locked():
            records = self._records()
            integrity_reason = self._records_reason(records)
            if integrity_reason:
                raise RecoveryAuthorityError(integrity_reason)
            grant_sha256 = str(grant["grant_sha256"])
            grant_records = [
                record
                for record in records
                if record.get("authority_grant_sha256") == grant_sha256
            ]
            if event == "authority_accepted":
                previous_state_generations = [
                    int(
                        _mapping(
                            _mapping(record.get("authority_grant")).get("claims")
                        ).get("authority_state_generation")
                        or 0
                    )
                    for record in records
                    if record.get("event") == "authority_accepted"
                ]
                if int(claims.get("authority_state_generation") or 0) < max(
                    previous_state_generations,
                    default=0,
                ):
                    raise RecoveryAuthorityError(
                        "supervisor_authority_state_generation_rollback"
                    )
                if grant_records:
                    raise RecoveryAuthorityError(
                        "supervisor_authority_grant_replay"
                    )
            else:
                if not grant_records or records[-1].get(
                    "authority_grant_sha256"
                ) != grant_sha256:
                    raise RecoveryAuthorityError(
                        "supervisor_attestation_session_not_active"
                    )
                expected_event = self._next_event(str(grant_records[-1]["event"]))
                if event != expected_event and not (
                    event == "lease_renewed"
                    and grant_records[-1].get("event") == "lease_renewed"
                ):
                    raise RecoveryAuthorityError(
                        "supervisor_attestation_transition_invalid"
                    )
            sequence = len(records) + 1
            previous = records[-1].get("attestation_sha256") if records else None
            unsigned = {
                "schema": SUPERVISOR_ATTESTATION_SCHEMA,
                "program_id": self.program_id,
                "program_sha256": self.registration.get("program_sha256"),
                "attempt_id": claims.get("attempt_id"),
                "provider_instance_sha256": claims.get(
                    "provider_instance_sha256"
                ),
                "lease_resource_id": claims.get("lease_resource_id"),
                "lease_owner_id": claims.get("lease_owner_id"),
                "authority_grant": deepcopy(grant),
                "authority_grant_sha256": grant_sha256,
                "supervisor_attestation_key_id": key_id,
                "sequence": sequence,
                "event": event,
                "observed_at_ms": now_ms,
                "supervisor_pid": os.getpid(),
                "process_identity_verified": False,
                "previous_attestation_sha256": previous,
                "details": deepcopy(details),
                "details_sha256": _canonical_hash(details),
            }
            attestation_sha256 = _canonical_hash(unsigned)
            signed = {**unsigned, "attestation_sha256": attestation_sha256}
            record = {
                **signed,
                "supervisor_signature": _encode(
                    private_key.sign(_canonical_bytes(signed))
                ),
            }
            inserted = asyncio.run(
                self.repository.save_if_absent(
                    self._record_key(sequence),
                    record,
                )
            )
            if not inserted:
                raise RecoveryAuthorityError("supervisor_attestation_append_conflict")
        return deepcopy(record)

    def summary(self) -> dict[str, Any]:
        records = self._records()
        reason = self._records_reason(records)
        if reason:
            return {
                "schema": SUPERVISOR_ATTESTATION_SCHEMA,
                "status": "invalid",
                "event_count": len(records),
                "integrity_verified": False,
                "lifecycle_complete": False,
                "reason": reason,
                "operator_identity_external_verified": False,
                "process_identity_verified": False,
            }
        if not records:
            return {
                "schema": SUPERVISOR_ATTESTATION_SCHEMA,
                "status": "not_started",
                "event_count": 0,
                "integrity_verified": True,
                "lifecycle_complete": False,
                "reason": None,
                "operator_identity_external_verified": False,
                "process_identity_verified": False,
            }
        latest_grant = str(records[-1]["authority_grant_sha256"])
        latest = [
            record
            for record in records
            if record.get("authority_grant_sha256") == latest_grant
        ]
        events = [str(record["event"]) for record in latest]
        lifecycle_complete = (
            len(events) >= len(SUPERVISOR_EVENTS)
            and events[0] == "authority_accepted"
            and events[1] == "lease_acquired"
            and "lease_renewed" in events
            and events[-3:] == [
                "heartbeat_stopped",
                "recovery_returned",
                "lease_released",
            ]
        )
        claims = _mapping(
            _mapping(latest[-1].get("authority_grant")).get("claims")
        )
        return {
            "schema": SUPERVISOR_ATTESTATION_SCHEMA,
            "status": "complete" if lifecycle_complete else "incomplete",
            "event_count": len(records),
            "session_event_count": len(latest),
            "events": events,
            "latest_attestation_sha256": records[-1]["attestation_sha256"],
            "latest_authority_grant_sha256": latest_grant,
            "operator_id": claims.get("operator_id"),
            "operator_certificate_verified": True,
            "operator_identity_external_verified": False,
            "supervisor_attestation_key_verified": True,
            "authority_state_sha256": claims.get("authority_state_sha256"),
            "authority_state_generation": claims.get(
                "authority_state_generation"
            ),
            "rollback_anchor_sha256": claims.get("rollback_anchor_sha256"),
            "revocation_checked": claims.get("authority_state_sha256") is not None,
            "trusted_time_source_verified": False,
            "hardware_key_custody_verified": False,
            "full_rollback_resistance_verified": False,
            "process_identity_verified": False,
            "program_bound": True,
            "integrity_verified": True,
            "lifecycle_complete": lifecycle_complete,
            "reason": None,
        }

    def _records(self) -> list[dict[str, Any]]:
        values = asyncio.run(
            self.repository.load_prefix(
                f"{SUPERVISOR_ATTESTATION_PREFIX}{self.program_id}:"
            )
        )
        return [value for value in values.values() if isinstance(value, dict)]

    def _records_reason(self, records: list[dict[str, Any]]) -> str | None:
        previous: str | None = None
        seen_grants: set[str] = set()
        active_grant: str | None = None
        active_event: str | None = None
        highest_state_generation = 0
        entries = self.repository.provenance_entries()
        for index, record in enumerate(records, start=1):
            unsigned = {
                key: item
                for key, item in record.items()
                if key not in {"attestation_sha256", "supervisor_signature"}
            }
            signed = {**unsigned, "attestation_sha256": record.get("attestation_sha256")}
            grant = _mapping(record.get("authority_grant"))
            claims = _mapping(grant.get("claims"))
            _, grant_reason = verify_recovery_authority_grant(
                grant,
                _mapping(self.trust_anchor),
                expected=self._grant_expectations(),
                verification_time_ms=int(record.get("observed_at_ms") or 0),
                allow_embedded_authority_state=True,
            )
            try:
                public = Ed25519PublicKey.from_public_bytes(
                    _decode_public_key(
                        claims.get("supervisor_attestation_public_key")
                    )
                )
                public.verify(
                    _decode(record.get("supervisor_signature")),
                    _canonical_bytes(signed),
                )
            except (RecoveryAuthorityError, InvalidSignature, ValueError):
                return "supervisor_attestation_signature_invalid"
            grant_sha256 = str(record.get("authority_grant_sha256") or "")
            event = str(record.get("event") or "")
            if (
                grant_reason
                or record.get("schema") != SUPERVISOR_ATTESTATION_SCHEMA
                or record.get("program_id") != self.program_id
                or record.get("program_sha256")
                != self.registration.get("program_sha256")
                or record.get("attempt_id") != claims.get("attempt_id")
                or record.get("provider_instance_sha256")
                != claims.get("provider_instance_sha256")
                or record.get("lease_resource_id")
                != claims.get("lease_resource_id")
                or record.get("lease_owner_id") != claims.get("lease_owner_id")
                or grant_sha256 != grant.get("grant_sha256")
                or record.get("supervisor_attestation_key_id")
                != claims.get("supervisor_attestation_key_id")
                or record.get("sequence") != index
                or event not in SUPERVISOR_EVENTS
                or record.get("process_identity_verified") is not False
                or record.get("previous_attestation_sha256") != previous
                or record.get("details_sha256")
                != _canonical_hash(record.get("details"))
                or record.get("attestation_sha256") != _canonical_hash(unsigned)
            ):
                return grant_reason or "supervisor_attestation_binding_invalid"
            if event == "authority_accepted":
                if grant_sha256 in seen_grants:
                    return "supervisor_authority_grant_replay"
                state_generation = int(
                    claims.get("authority_state_generation") or 0
                )
                if state_generation < highest_state_generation:
                    return "supervisor_authority_state_generation_rollback"
                highest_state_generation = max(
                    highest_state_generation,
                    state_generation,
                )
                seen_grants.add(grant_sha256)
                active_grant = grant_sha256
                active_event = event
            else:
                if active_grant != grant_sha256 or active_event is None:
                    return "supervisor_attestation_session_not_active"
                expected_event = self._next_event(active_event)
                if event != expected_event and not (
                    event == "lease_renewed" and active_event == "lease_renewed"
                ):
                    return "supervisor_attestation_transition_invalid"
                active_event = event
            key = self._record_key(index)
            provenance = [
                entry for entry in entries if entry.get("record_key") == key
            ]
            if (
                len(provenance) != 1
                or provenance[0].get("record_hash")
                != self.repository.record_hash(record)
            ):
                return "supervisor_attestation_provenance_mismatch"
            previous = str(record["attestation_sha256"])
        return None

    def _grant_expectations(self) -> dict[str, Any]:
        provider = _mapping(self.registration.get("provider"))
        return {
            "program_id": self.program_id,
            "program_sha256": self.registration.get("program_sha256"),
            "provider_instance_sha256": provider.get("transport_instance_sha256"),
        }

    def _record_key(self, sequence: int) -> str:
        return f"{SUPERVISOR_ATTESTATION_PREFIX}{self.program_id}:{sequence:06d}"

    @staticmethod
    def _next_event(event: str) -> str | None:
        try:
            return SUPERVISOR_EVENTS[SUPERVISOR_EVENTS.index(event) + 1]
        except (ValueError, IndexError):
            return None


def _recovery_claims_reason(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "recovery_authority_claims_missing"
    adoption_pair = (
        value.get("previous_owner_id"),
        value.get("expired_generation"),
    )
    adoption_valid = adoption_pair == (None, None) or (
        _safe_identifier(adoption_pair[0])
        and isinstance(adoption_pair[1], int)
        and not isinstance(adoption_pair[1], bool)
        and int(adoption_pair[1]) >= 1
    )
    state_fields = (
        "authority_state_sha256",
        "authority_state_generation",
        "authority_time_floor_ms",
        "recovery_key_custody_evidence_sha256",
        "rollback_anchor_sha256",
    )
    state_presence = [value.get(field) is not None for field in state_fields]
    state_binding_valid = not any(state_presence) or (
        all(state_presence)
        and all(
            _is_sha256(value.get(field))
            for field in (
                "authority_state_sha256",
                "recovery_key_custody_evidence_sha256",
                "rollback_anchor_sha256",
            )
        )
        and isinstance(value.get("authority_state_generation"), int)
        and not isinstance(value.get("authority_state_generation"), bool)
        and int(value["authority_state_generation"]) >= 1
        and isinstance(value.get("authority_time_floor_ms"), int)
        and not isinstance(value.get("authority_time_floor_ms"), bool)
        and int(value["authority_time_floor_ms"]) >= 0
    )
    if (
        value.get("schema") != RECOVERY_AUTHORITY_CLAIMS_SCHEMA
        or value.get("action") != RECOVERY_AUTHORITY_ACTION
        or not _safe_identifier(value.get("program_id"))
        or not _safe_identifier(value.get("attempt_id"))
        or not _safe_identifier(value.get("lease_owner_id"))
        or not _safe_identifier(value.get("operator_id"))
        or not _safe_identifier(value.get("nonce"))
        or any(
            not _is_sha256(value.get(field))
            for field in (
                "program_sha256",
                "provider_instance_sha256",
                "lease_resource_id",
                "transport_recovery_authority_sha256",
                "context_intervention_sha256",
                "operator_certificate_sha256",
                "supervisor_attestation_key_id",
            )
        )
        or value.get("operator_identity_external_verified") is not False
        or value.get("automatic_retry_authorized") is not False
        or not _valid_time_window(value.get("issued_at_ms"), value.get("expires_at_ms"))
        or not adoption_valid
        or not state_binding_valid
    ):
        return "recovery_authority_claims_invalid"
    try:
        supervisor_public = _decode_public_key(
            value.get("supervisor_attestation_public_key")
        )
    except RecoveryAuthorityError:
        return "recovery_authority_supervisor_key_invalid"
    if _sha256_bytes(supervisor_public) != value.get(
        "supervisor_attestation_key_id"
    ):
        return "recovery_authority_supervisor_key_invalid"
    return None


def _mapping_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _supervisor_public_key_reason(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "supervisor_public_key_missing"
    unsigned = {
        key: item for key, item in value.items() if key != "public_key_sha256"
    }
    try:
        public = _decode_public_key(value.get("public_key"))
    except RecoveryAuthorityError:
        return "supervisor_public_key_invalid"
    if (
        value.get("schema") != "spst-recovery-supervisor-public-key-v1"
        or value.get("algorithm") != RECOVERY_AUTHORITY_ALGORITHM
        or value.get("key_id") != _sha256_bytes(public)
        or value.get("public_key_sha256") != _canonical_hash(unsigned)
    ):
        return "supervisor_public_key_invalid"
    return None


def _create_private_key(path: str | Path) -> Ed25519PrivateKey:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    encoded = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    try:
        with target.open("xb") as stream:
            stream.write(encoded)
    except FileExistsError as error:
        raise RecoveryAuthorityError("recovery_private_key_already_exists") from error
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return key


def _load_private_key(path: str | Path) -> Ed25519PrivateKey:
    try:
        value = serialization.load_pem_private_key(
            Path(path).expanduser().resolve().read_bytes(),
            password=None,
        )
    except (OSError, ValueError, TypeError) as error:
        raise RecoveryAuthorityError("recovery_private_key_invalid") from error
    if not isinstance(value, Ed25519PrivateKey):
        raise RecoveryAuthorityError("recovery_private_key_type_invalid")
    return value


def _public_bytes(value: Ed25519PublicKey) -> bytes:
    return value.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def _decode_public_key(value: Any) -> bytes:
    decoded = _decode(value)
    if len(decoded) != 32:
        raise RecoveryAuthorityError("recovery_public_key_invalid")
    return decoded


def _decode(value: Any) -> bytes:
    if not isinstance(value, str):
        raise RecoveryAuthorityError("recovery_signature_encoding_invalid")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        raise RecoveryAuthorityError("recovery_signature_encoding_invalid") from error


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _canonical_hash(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_sha256.fullmatch(value))


def _safe_identifier(value: Any) -> str:
    candidate = str(value or "")
    return candidate if _identifier.fullmatch(candidate) else ""


def _valid_time_window(start: Any, end: Any) -> bool:
    return (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end
    )


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
