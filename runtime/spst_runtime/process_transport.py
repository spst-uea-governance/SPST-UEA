import hashlib
import hmac
import json
import os
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time
from typing import Any

from spst_runtime.interfaces.model_adapter import ModelAdapter
from spst_runtime.provider_observation import (
    build_provider_observation,
    build_provider_request_binding,
    verify_provider_observation,
)
from spst_runtime.provider_transport import (
    TRANSPORT_CONTEXT_KEY,
    build_provider_transport_receipt,
    validate_provider_transport_request,
    verify_provider_transport_receipt,
)
from spst_runtime.recovery_authority import (
    RECOVERY_AUTHORITY_GRANT_SCHEMA,
    RECOVERY_AUTHORITY_TRUST_SCHEMA,
    SUPERVISOR_ATTESTATION_SCHEMA,
    validate_recovery_authority_trust_anchor,
    verify_recovery_authority_grant,
)


PROCESS_PROVIDER_REQUEST_SCHEMA = "spst-process-provider-request-v1"
PROCESS_PROVIDER_RESPONSE_SCHEMA = "spst-process-provider-response-v1"
PROCESS_PROVIDER_STATUS_SCHEMA = "spst-process-provider-status-v1"
PROCESS_PROVIDER_AUTHENTICATION_SCHEMA = (
    "spst-process-provider-store-authentication-v1"
)
PROCESS_RECOVERY_LEASE_SCHEMA = "spst-process-recovery-lease-v1"
PROCESS_RECOVERY_SUPERVISOR_AUTHORITY_SCHEMA = (
    "spst-process-recovery-supervisor-authority-v1"
)
PROCESS_PROVIDER_NAME = "spst-local-process-provider"
PROCESS_PROVIDER_MODEL_VERSION = "process-transport-v2"
PROCESS_PROVIDER_OBSERVATION_SOURCE = "local_process_provider_echo"
PROCESS_TRANSPORT_PROTOCOL = "subprocess-stdio-sqlite-hmac-lease-v2"
SIGNED_PROCESS_TRANSPORT_PROTOCOL = "subprocess-stdio-sqlite-hmac-lease-pki-v3"
STATEFUL_SIGNED_PROCESS_TRANSPORT_PROTOCOL = (
    "subprocess-stdio-sqlite-hmac-lease-pki-state-v4"
)
MINIMUM_RECOVERY_LEASE_TTL_MS = 100
MAXIMUM_RECOVERY_LEASE_TTL_MS = 600_000


class ProcessTransportError(ValueError):
    """Reject an invalid or unavailable local process transport boundary."""


class DurableProcessProviderStore:
    """A no-network SQLite provider simulator shared by independent processes."""

    def __init__(
        self,
        path: str | Path,
        *,
        authentication_key_file: str | Path | None = None,
        recovery_authority_trust_anchor: dict[str, Any] | None = None,
        recovery_authority_state: dict[str, Any] | None = None,
        recovery_authority_rollback_anchor: dict[str, Any] | None = None,
        read_only: bool = False,
    ):
        self.path = Path(path).expanduser().resolve()
        self.authentication_key_file = (
            Path(authentication_key_file).expanduser().resolve()
            if authentication_key_file is not None
            else Path(f"{self.path}.provider_key")
        )
        self.read_only = read_only
        trust_reason = (
            validate_recovery_authority_trust_anchor(
                recovery_authority_trust_anchor
            )
            if recovery_authority_trust_anchor is not None
            else None
        )
        if trust_reason:
            raise ProcessTransportError(trust_reason)
        self.recovery_authority_trust_anchor = (
            deepcopy(recovery_authority_trust_anchor)
            if recovery_authority_trust_anchor is not None
            else None
        )
        self.recovery_authority_trust_anchor_sha256 = (
            str(recovery_authority_trust_anchor["trust_anchor_sha256"])
            if recovery_authority_trust_anchor is not None
            else None
        )
        self.recovery_authority_state = (
            deepcopy(recovery_authority_state)
            if recovery_authority_state is not None
            else None
        )
        self.recovery_authority_rollback_anchor = (
            deepcopy(recovery_authority_rollback_anchor)
            if recovery_authority_rollback_anchor is not None
            else None
        )
        self._authentication_secret, self.authentication_key_source = (
            self._load_authentication_secret()
        )
        self.authentication_key_id = (
            hashlib.sha256(self._authentication_secret).hexdigest()
            if self._authentication_secret
            else ""
        )
        if not read_only:
            self._initialize()
        elif self.path.is_file():
            self._verify_database_trust_configuration()

    def identity(self) -> str:
        value, reason = self._identity_status()
        if reason or value is None:
            raise ProcessTransportError(
                reason or "process_provider_instance_identity_invalid"
            )
        return value

    def _identity_status(self) -> tuple[str | None, str | None]:
        with closing(self._connect()) as connection:
            rows = dict(
                connection.execute(
                    """
                    SELECT key, value FROM provider_meta
                    WHERE key IN (
                        'provider_instance_sha256',
                        'authentication_key_id',
                        'authentication_key_generation',
                        'provider_instance_authentication_tag',
                        'recovery_authority_trust_anchor_sha256'
                    )
                    """
                ).fetchall()
            )
        value = str(rows.get("provider_instance_sha256") or "")
        key_id = str(rows.get("authentication_key_id") or "")
        try:
            generation = int(rows.get("authentication_key_generation") or 0)
        except (TypeError, ValueError):
            generation = 0
        tag = str(rows.get("provider_instance_authentication_tag") or "")
        stored_trust_anchor_sha256 = rows.get(
            "recovery_authority_trust_anchor_sha256"
        )
        stored_trust_anchor_sha256 = (
            str(stored_trust_anchor_sha256)
            if stored_trust_anchor_sha256 is not None
            else None
        )
        if not self._authentication_secret:
            return None, "process_provider_authentication_key_unavailable"
        if (
            not _is_sha256(value)
            or key_id != self.authentication_key_id
            or generation < 1
            or stored_trust_anchor_sha256
            != self.recovery_authority_trust_anchor_sha256
            or not self._verify_authentication_tag(
                self._instance_authentication_payload(
                    value,
                    key_id=key_id,
                    generation=generation,
                    recovery_authority_trust_anchor_sha256=(
                        stored_trust_anchor_sha256
                    ),
                ),
                tag,
            )
        ):
            return None, "process_provider_instance_authentication_invalid"
        return value, None

    def infer(self, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
        transport_request = _mapping(context.get(TRANSPORT_CONTEXT_KEY))
        transport_valid, transport_reason = validate_provider_transport_request(
            transport_request
        )
        if not transport_valid:
            raise ProcessTransportError(
                transport_reason or "process_provider_transport_request_invalid"
            )
        provider_binding = build_provider_request_binding(prompt, context)
        idempotency_key = str(transport_request["idempotency_key"])
        text = json.dumps(
            {
                "answer": (
                    1
                    if isinstance(
                        context.get("evidence_context_intervention"), dict
                    )
                    else 0
                )
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        response_id = f"process-{transport_request['idempotency_key_sha256'][:24]}"
        observation = build_provider_observation(
            provider_binding,
            provider_name=PROCESS_PROVIDER_NAME,
            model_version=PROCESS_PROVIDER_MODEL_VERSION,
            response_id=response_id,
            response_status="completed",
            output_text=text,
            observation_source=PROCESS_PROVIDER_OBSERVATION_SOURCE,
            acknowledged_request_binding_sha256=provider_binding[
                "request_binding_sha256"
            ],
        )
        receipt = build_provider_transport_receipt(
            transport_request,
            provider_binding,
            provider_name=PROCESS_PROVIDER_NAME,
            model_version=PROCESS_PROVIDER_MODEL_VERSION,
            response_id=response_id,
            response_status="completed",
            output_text=text,
            acknowledged_idempotency_key=idempotency_key,
        )
        result = {
            "provider": PROCESS_PROVIDER_NAME,
            "model_version": PROCESS_PROVIDER_MODEL_VERSION,
            "available": True,
            "text": text,
            "provider_observation": observation,
            "provider_transport_receipt": receipt,
            "provider_request_binding": provider_binding,
        }
        result_json = _canonical_json(result)
        result_sha256 = _sha256_text(result_json)
        transport_json = _canonical_json(transport_request)

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT idempotency_key, provider_request_binding_sha256,
                       transport_request_json, result_json, result_sha256,
                       execution_count, request_count, creator_pid,
                       authentication_key_generation, authentication_tag
                FROM provider_results WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is None:
                generation = self._authentication_generation(connection)
                authentication_tag = self._authentication_tag(
                    self._result_authentication_payload(
                        idempotency_key=idempotency_key,
                        provider_request_binding_sha256=provider_binding[
                            "request_binding_sha256"
                        ],
                        transport_request_json=transport_json,
                        result_json=result_json,
                        result_sha256=result_sha256,
                        execution_count=1,
                        request_count=1,
                        creator_pid=os.getpid(),
                        generation=generation,
                    )
                )
                connection.execute(
                    """
                    INSERT INTO provider_results(
                        idempotency_key,
                        provider_request_binding_sha256,
                        transport_request_json,
                        result_json,
                        result_sha256,
                        execution_count,
                        request_count,
                        creator_pid,
                        authentication_key_generation,
                        authentication_tag
                    ) VALUES(?, ?, ?, ?, ?, 1, 1, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        provider_binding["request_binding_sha256"],
                        transport_json,
                        result_json,
                        result_sha256,
                        os.getpid(),
                        generation,
                        authentication_tag,
                    ),
                )
                connection.commit()
                return result

            existing, reason = self._validate_result_row(row)
            if reason or existing is None:
                connection.rollback()
                raise ProcessTransportError(
                    reason or "process_provider_result_integrity_invalid"
                )
            if (
                str(row[1]) != provider_binding["request_binding_sha256"]
                or str(row[2]) != transport_json
            ):
                connection.rollback()
                raise ProcessTransportError(
                    "process_provider_idempotency_key_reuse_mismatch"
                )
            updated_request_count = int(row[6]) + 1
            updated_tag = self._authentication_tag(
                self._result_authentication_payload(
                    idempotency_key=str(row[0]),
                    provider_request_binding_sha256=str(row[1]),
                    transport_request_json=str(row[2]),
                    result_json=str(row[3]),
                    result_sha256=str(row[4]),
                    execution_count=int(row[5]),
                    request_count=updated_request_count,
                    creator_pid=int(row[7]),
                    generation=int(row[8]),
                )
            )
            connection.execute(
                """
                UPDATE provider_results
                SET request_count = ?, authentication_tag = ?
                WHERE idempotency_key = ?
                """,
                (updated_request_count, updated_tag, idempotency_key),
            )
            connection.commit()
            return existing

    def reconcile(
        self,
        idempotency_key: str,
        provider_request_binding_sha256: str,
    ) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT idempotency_key, provider_request_binding_sha256,
                       transport_request_json, result_json, result_sha256,
                       execution_count, request_count, creator_pid,
                       authentication_key_generation, authentication_tag
                FROM provider_results WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is None:
                generation = self._authentication_generation(connection)
                worker_pid = os.getpid()
                authentication_tag = self._authentication_tag(
                    self._reconciliation_authentication_payload(
                        idempotency_key=idempotency_key,
                        provider_request_binding_sha256=(
                            provider_request_binding_sha256
                        ),
                        result_sha256=None,
                        found=False,
                        worker_pid=worker_pid,
                        generation=generation,
                    )
                )
                connection.execute(
                    """
                    INSERT INTO provider_reconciliations(
                        idempotency_key,
                        provider_request_binding_sha256,
                        result_sha256,
                        found,
                        worker_pid,
                        authentication_key_generation,
                        authentication_tag
                    ) VALUES(?, ?, NULL, 0, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        provider_request_binding_sha256,
                        worker_pid,
                        generation,
                        authentication_tag,
                    ),
                )
                connection.commit()
                return None
            if str(row[1]) != provider_request_binding_sha256:
                connection.rollback()
                raise ProcessTransportError(
                    "process_provider_reconciliation_request_mismatch"
                )
            result, reason = self._validate_result_row(row)
            if reason or result is None:
                connection.rollback()
                raise ProcessTransportError(
                    reason or "process_provider_result_integrity_invalid"
                )
            generation = self._authentication_generation(connection)
            worker_pid = os.getpid()
            authentication_tag = self._authentication_tag(
                self._reconciliation_authentication_payload(
                    idempotency_key=idempotency_key,
                    provider_request_binding_sha256=(
                        provider_request_binding_sha256
                    ),
                    result_sha256=str(row[4]),
                    found=True,
                    worker_pid=worker_pid,
                    generation=generation,
                )
            )
            connection.execute(
                """
                INSERT INTO provider_reconciliations(
                    idempotency_key,
                    provider_request_binding_sha256,
                    result_sha256,
                    found,
                    worker_pid,
                    authentication_key_generation,
                    authentication_tag
                ) VALUES(?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    provider_request_binding_sha256,
                    str(row[4]),
                    worker_pid,
                    generation,
                    authentication_tag,
                ),
            )
            connection.commit()
            return result

    def status(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT idempotency_key, provider_request_binding_sha256,
                       transport_request_json, result_json, result_sha256,
                       execution_count, request_count, creator_pid,
                       authentication_key_generation, authentication_tag
                FROM provider_results ORDER BY idempotency_key
                """
            ).fetchall()
            reconciliations = connection.execute(
                """
                SELECT idempotency_key, provider_request_binding_sha256,
                       result_sha256, found, worker_pid,
                       authentication_key_generation, authentication_tag
                FROM provider_reconciliations ORDER BY sequence
                """
            ).fetchall()
            leases = connection.execute(
                """
                SELECT resource_id, owner_id, generation, lease_token_sha256,
                       acquired_at_ms, expires_at_ms, status,
                       previous_owner_id, adoption_count,
                       authentication_key_generation, authentication_tag
                FROM provider_recovery_leases ORDER BY resource_id
                """
            ).fetchall()
        identity, identity_reason = self._identity_status()
        reasons = [self._validate_result_row(row)[1] for row in rows]
        reasons.extend(
            self._validate_reconciliation_row(row)[1]
            for row in reconciliations
        )
        reasons.extend(self._validate_lease_row(row)[1] for row in leases)
        if identity_reason:
            reasons.append(identity_reason)
        return {
            "schema": PROCESS_PROVIDER_STATUS_SCHEMA,
            "provider_instance_sha256": identity,
            "record_count": len(rows),
            "total_execution_count": sum(int(row[5]) for row in rows),
            "total_infer_request_count": sum(int(row[6]) for row in rows),
            "reconciliation_query_count": len(reconciliations),
            "reconciliation_found_count": sum(
                1 for row in reconciliations if int(row[3]) == 1
            ),
            "creator_pids": sorted({int(row[7]) for row in rows}),
            "reconciliation_pids": sorted(
                {int(row[4]) for row in reconciliations}
            ),
            "recovery_lease_count": len(leases),
            "active_recovery_lease_count": sum(
                1 for row in leases if str(row[6]) == "acquired"
            ),
            "orphan_adoption_count": sum(int(row[8]) for row in leases),
            "integrity_verified": not any(reasons),
            "reason": next((reason for reason in reasons if reason), None),
            "provider_store_authenticity_verified": not any(reasons),
            "authentication_schema": PROCESS_PROVIDER_AUTHENTICATION_SCHEMA,
            "authentication_key_id": (
                self.authentication_key_id
                if self._authentication_secret
                else None
            ),
            "authentication_key_source": self.authentication_key_source,
            "recovery_authority_trust_schema": (
                RECOVERY_AUTHORITY_TRUST_SCHEMA
                if self.recovery_authority_trust_anchor is not None
                else None
            ),
            "recovery_authority_trust_anchor_sha256": (
                self.recovery_authority_trust_anchor_sha256
            ),
            "recovery_authority_trust_verified": (
                self.recovery_authority_trust_anchor is not None
                and identity_reason is None
            ),
            "recovery_authority_state_configured": (
                self.recovery_authority_state is not None
            ),
            "recovery_authority_state_sha256": (
                self.recovery_authority_state.get("state_sha256")
                if self.recovery_authority_state is not None
                else None
            ),
            "recovery_authority_rollback_anchor_sha256": (
                self.recovery_authority_rollback_anchor.get("anchor_sha256")
                if self.recovery_authority_rollback_anchor is not None
                else None
            ),
            "trusted_time_source_verified": False,
            "hardware_key_custody_verified": False,
            "full_rollback_resistance_verified": False,
            "provider_identity_authenticated": False,
            "exactly_once_execution_proven": False,
        }

    def _validate_result_row(
        self, row: sqlite3.Row | tuple[Any, ...]
    ) -> tuple[dict[str, Any] | None, str | None]:
        try:
            idempotency_key = str(row[0])
            binding_sha256 = str(row[1])
            transport_request = json.loads(str(row[2]))
            result = json.loads(str(row[3]))
            result_sha256 = str(row[4])
            execution_count = int(row[5])
            request_count = int(row[6])
            creator_pid = int(row[7])
            generation = int(row[8])
            authentication_tag = str(row[9])
        except (IndexError, TypeError, ValueError, json.JSONDecodeError):
            return None, "process_provider_result_record_invalid"
        if (
            not isinstance(result, dict)
            or not isinstance(transport_request, dict)
            or not _is_sha256(binding_sha256)
            or _sha256_text(_canonical_json(result)) != result_sha256
            or execution_count != 1
            or request_count < 1
        ):
            return None, "process_provider_result_digest_mismatch"
        if not self._verify_authentication_tag(
            self._result_authentication_payload(
                idempotency_key=idempotency_key,
                provider_request_binding_sha256=binding_sha256,
                transport_request_json=str(row[2]),
                result_json=str(row[3]),
                result_sha256=result_sha256,
                execution_count=execution_count,
                request_count=request_count,
                creator_pid=creator_pid,
                generation=generation,
            ),
            authentication_tag,
        ):
            return None, "process_provider_result_authentication_failed"
        valid_request, request_reason = validate_provider_transport_request(
            transport_request
        )
        if not valid_request:
            return None, request_reason
        if transport_request.get("idempotency_key") != idempotency_key:
            return None, "process_provider_transport_key_mismatch"
        provider_binding = _mapping(result.get("provider_request_binding"))
        if provider_binding.get("request_binding_sha256") != binding_sha256:
            return None, "process_provider_request_binding_mismatch"
        text = str(result.get("text") or "")
        observation_valid, observation_reason = verify_provider_observation(
            result.get("provider_observation"),
            provider_binding,
            output_text=text,
            provider_name=PROCESS_PROVIDER_NAME,
            model_version=PROCESS_PROVIDER_MODEL_VERSION,
        )
        if not observation_valid:
            return None, observation_reason
        receipt_valid, receipt_reason = verify_provider_transport_receipt(
            result.get("provider_transport_receipt"),
            transport_request,
            provider_binding,
            output_text=text,
            provider_name=PROCESS_PROVIDER_NAME,
            model_version=PROCESS_PROVIDER_MODEL_VERSION,
        )
        if not receipt_valid:
            return None, receipt_reason
        return result, None

    def acquire_recovery_lease(
        self,
        resource_id: str,
        owner_id: str,
        *,
        ttl_ms: int,
        adoption_authority: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.read_only:
            raise PermissionError("process_recovery_lease_store_read_only")
        self._validate_lease_inputs(resource_id, owner_id, ttl_ms)
        now_ms = time.time_ns() // 1_000_000
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT resource_id, owner_id, generation, lease_token_sha256,
                       acquired_at_ms, expires_at_ms, status,
                       previous_owner_id, adoption_count,
                       authentication_key_generation, authentication_tag
                FROM provider_recovery_leases WHERE resource_id = ?
                """,
                (resource_id,),
            ).fetchone()
            previous_owner_id: str | None = None
            adoption_count = 0
            generation = 1
            adopted = False
            expired_generation: int | None = None
            if row is not None:
                lease, reason = self._validate_lease_row(row)
                if reason or lease is None:
                    connection.rollback()
                    raise ProcessTransportError(
                        reason or "process_recovery_lease_integrity_invalid"
                    )
                previous_owner_id = str(lease["owner_id"])
                adoption_count = int(lease["adoption_count"])
                generation = int(lease["generation"]) + 1
                if lease["status"] == "acquired":
                    if now_ms < int(lease["expires_at_ms"]):
                        connection.rollback()
                        raise ProcessTransportError("process_recovery_lease_active")
                    expired_generation = int(lease["generation"])
                    adoption_count += 1
                    adopted = True
            if self.recovery_authority_trust_anchor_sha256 is not None:
                if self.recovery_authority_trust_anchor is None:
                    connection.rollback()
                    raise ProcessTransportError(
                        "process_recovery_authority_trust_unavailable"
                    )
                _, authority_reason = verify_recovery_authority_grant(
                    adoption_authority,
                    self.recovery_authority_trust_anchor,
                    expected={
                        "provider_instance_sha256": self.identity(),
                        "lease_resource_id": resource_id,
                        "lease_owner_id": owner_id,
                        "previous_owner_id": (
                            previous_owner_id if adopted else None
                        ),
                        "expired_generation": (
                            expired_generation if adopted else None
                        ),
                    },
                    authority_state=self.recovery_authority_state,
                    rollback_anchor=self.recovery_authority_rollback_anchor,
                )
                if authority_reason:
                    connection.rollback()
                    raise ProcessTransportError(authority_reason)
            elif adopted:
                authority_valid, authority_reason = (
                    verify_recovery_supervisor_authority(
                        adoption_authority,
                        resource_id=resource_id,
                        provider_instance_sha256=self.identity(),
                        previous_owner_id=str(previous_owner_id),
                        expired_generation=int(expired_generation or 0),
                    )
                )
                if not authority_valid:
                    connection.rollback()
                    raise ProcessTransportError(
                        authority_reason
                        or "process_recovery_orphan_adoption_authority_required"
                    )
            lease_token = secrets.token_hex(32)
            lease_token_sha256 = _sha256_text(lease_token)
            expires_at_ms = now_ms + ttl_ms
            auth_generation = self._authentication_generation(connection)
            payload = self._lease_authentication_payload(
                resource_id=resource_id,
                owner_id=owner_id,
                generation=generation,
                lease_token_sha256=lease_token_sha256,
                acquired_at_ms=now_ms,
                expires_at_ms=expires_at_ms,
                status="acquired",
                previous_owner_id=previous_owner_id,
                adoption_count=adoption_count,
                authentication_key_generation=auth_generation,
            )
            connection.execute(
                """
                INSERT INTO provider_recovery_leases(
                    resource_id, owner_id, generation, lease_token_sha256,
                    acquired_at_ms, expires_at_ms, status,
                    previous_owner_id, adoption_count,
                    authentication_key_generation, authentication_tag
                ) VALUES(?, ?, ?, ?, ?, ?, 'acquired', ?, ?, ?, ?)
                ON CONFLICT(resource_id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    generation = excluded.generation,
                    lease_token_sha256 = excluded.lease_token_sha256,
                    acquired_at_ms = excluded.acquired_at_ms,
                    expires_at_ms = excluded.expires_at_ms,
                    status = excluded.status,
                    previous_owner_id = excluded.previous_owner_id,
                    adoption_count = excluded.adoption_count,
                    authentication_key_generation = excluded.authentication_key_generation,
                    authentication_tag = excluded.authentication_tag
                """,
                (
                    resource_id,
                    owner_id,
                    generation,
                    lease_token_sha256,
                    now_ms,
                    expires_at_ms,
                    previous_owner_id,
                    adoption_count,
                    auth_generation,
                    self._authentication_tag(payload),
                ),
            )
            connection.commit()
        return {
            **payload,
            "schema": PROCESS_RECOVERY_LEASE_SCHEMA,
            "lease_token": lease_token,
            "adopted_orphan": adopted,
        }

    def renew_recovery_lease(
        self,
        resource_id: str,
        owner_id: str,
        *,
        generation: int,
        lease_token: str,
        ttl_ms: int,
    ) -> dict[str, Any]:
        self._validate_lease_inputs(resource_id, owner_id, ttl_ms)
        return self._transition_recovery_lease(
            resource_id,
            owner_id,
            generation=generation,
            lease_token=lease_token,
            ttl_ms=ttl_ms,
            release=False,
        )

    def release_recovery_lease(
        self,
        resource_id: str,
        owner_id: str,
        *,
        generation: int,
        lease_token: str,
    ) -> dict[str, Any]:
        return self._transition_recovery_lease(
            resource_id,
            owner_id,
            generation=generation,
            lease_token=lease_token,
            ttl_ms=None,
            release=True,
        )

    def recovery_lease_status(self, resource_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT resource_id, owner_id, generation, lease_token_sha256,
                       acquired_at_ms, expires_at_ms, status,
                       previous_owner_id, adoption_count,
                       authentication_key_generation, authentication_tag
                FROM provider_recovery_leases WHERE resource_id = ?
                """,
                (resource_id,),
            ).fetchone()
        if row is None:
            return None
        lease, reason = self._validate_lease_row(row)
        if reason or lease is None:
            raise ProcessTransportError(
                reason or "process_recovery_lease_integrity_invalid"
            )
        return {"schema": PROCESS_RECOVERY_LEASE_SCHEMA, **lease}

    def _transition_recovery_lease(
        self,
        resource_id: str,
        owner_id: str,
        *,
        generation: int,
        lease_token: str,
        ttl_ms: int | None,
        release: bool,
    ) -> dict[str, Any]:
        if self.read_only:
            raise PermissionError("process_recovery_lease_store_read_only")
        if not _is_sha256(resource_id) or not _safe_process_identifier(owner_id):
            raise ProcessTransportError("process_recovery_lease_identity_invalid")
        if not isinstance(generation, int) or generation < 1:
            raise ProcessTransportError("process_recovery_lease_generation_invalid")
        if not isinstance(lease_token, str) or len(lease_token) < 32:
            raise ProcessTransportError("process_recovery_lease_token_invalid")
        if not release and ttl_ms is not None:
            self._validate_lease_inputs(resource_id, owner_id, ttl_ms)
        now_ms = time.time_ns() // 1_000_000
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT resource_id, owner_id, generation, lease_token_sha256,
                       acquired_at_ms, expires_at_ms, status,
                       previous_owner_id, adoption_count,
                       authentication_key_generation, authentication_tag
                FROM provider_recovery_leases WHERE resource_id = ?
                """,
                (resource_id,),
            ).fetchone()
            lease, reason = self._validate_lease_row(row) if row is not None else (None, None)
            if reason or lease is None:
                connection.rollback()
                raise ProcessTransportError(
                    reason or "process_recovery_lease_not_found"
                )
            if (
                lease["status"] != "acquired"
                or lease["owner_id"] != owner_id
                or lease["generation"] != generation
                or not hmac.compare_digest(
                    str(lease["lease_token_sha256"]),
                    _sha256_text(lease_token),
                )
            ):
                connection.rollback()
                raise ProcessTransportError("process_recovery_lease_fencing_failed")
            if not release and now_ms >= int(lease["expires_at_ms"]):
                connection.rollback()
                raise ProcessTransportError("process_recovery_lease_expired")
            updated_status = "released" if release else "acquired"
            expires_at_ms = now_ms if release else now_ms + int(ttl_ms or 0)
            payload = self._lease_authentication_payload(
                resource_id=resource_id,
                owner_id=owner_id,
                generation=generation,
                lease_token_sha256=str(lease["lease_token_sha256"]),
                acquired_at_ms=int(lease["acquired_at_ms"]),
                expires_at_ms=expires_at_ms,
                status=updated_status,
                previous_owner_id=lease.get("previous_owner_id"),
                adoption_count=int(lease["adoption_count"]),
                authentication_key_generation=int(
                    lease["authentication_key_generation"]
                ),
            )
            connection.execute(
                """
                UPDATE provider_recovery_leases
                SET expires_at_ms = ?, status = ?, authentication_tag = ?
                WHERE resource_id = ?
                """,
                (
                    expires_at_ms,
                    updated_status,
                    self._authentication_tag(payload),
                    resource_id,
                ),
            )
            connection.commit()
        return {"schema": PROCESS_RECOVERY_LEASE_SCHEMA, **payload}

    @staticmethod
    def _validate_lease_inputs(resource_id: str, owner_id: str, ttl_ms: int) -> None:
        if not _is_sha256(resource_id) or not _safe_process_identifier(owner_id):
            raise ProcessTransportError("process_recovery_lease_identity_invalid")
        if (
            not isinstance(ttl_ms, int)
            or isinstance(ttl_ms, bool)
            or ttl_ms < MINIMUM_RECOVERY_LEASE_TTL_MS
            or ttl_ms > MAXIMUM_RECOVERY_LEASE_TTL_MS
        ):
            raise ProcessTransportError("process_recovery_lease_ttl_invalid")

    def _validate_reconciliation_row(
        self, row: sqlite3.Row | tuple[Any, ...]
    ) -> tuple[dict[str, Any] | None, str | None]:
        try:
            payload = self._reconciliation_authentication_payload(
                idempotency_key=str(row[0]),
                provider_request_binding_sha256=str(row[1]),
                result_sha256=str(row[2]) if row[2] is not None else None,
                found=bool(int(row[3])),
                worker_pid=int(row[4]),
                generation=int(row[5]),
            )
            tag = str(row[6])
        except (IndexError, TypeError, ValueError):
            return None, "process_provider_reconciliation_record_invalid"
        if (
            not _is_sha256(payload["provider_request_binding_sha256"])
            or (
                payload["result_sha256"] is not None
                and not _is_sha256(payload["result_sha256"])
            )
            or not self._verify_authentication_tag(payload, tag)
        ):
            return None, "process_provider_reconciliation_authentication_failed"
        return payload, None

    def _validate_lease_row(
        self, row: sqlite3.Row | tuple[Any, ...]
    ) -> tuple[dict[str, Any] | None, str | None]:
        try:
            payload = self._lease_authentication_payload(
                resource_id=str(row[0]),
                owner_id=str(row[1]),
                generation=int(row[2]),
                lease_token_sha256=str(row[3]),
                acquired_at_ms=int(row[4]),
                expires_at_ms=int(row[5]),
                status=str(row[6]),
                previous_owner_id=(str(row[7]) if row[7] is not None else None),
                adoption_count=int(row[8]),
                authentication_key_generation=int(row[9]),
            )
            tag = str(row[10])
        except (IndexError, TypeError, ValueError):
            return None, "process_recovery_lease_record_invalid"
        if (
            not _is_sha256(payload["resource_id"])
            or not _safe_process_identifier(payload["owner_id"])
            or not _is_sha256(payload["lease_token_sha256"])
            or payload["generation"] < 1
            or payload["acquired_at_ms"] > payload["expires_at_ms"]
            or payload["status"] not in {"acquired", "released"}
            or payload["adoption_count"] < 0
            or not self._verify_authentication_tag(payload, tag)
        ):
            return None, "process_recovery_lease_authentication_failed"
        return payload, None

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            if not self.path.is_file():
                raise FileNotFoundError(self.path)
            connection = sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro",
                timeout=30,
                uri=True,
            )
            connection.execute("PRAGMA query_only=ON")
        else:
            connection = sqlite3.connect(str(self.path), timeout=30)
            connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(self.path), timeout=30)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_results(
                    idempotency_key TEXT PRIMARY KEY,
                    provider_request_binding_sha256 TEXT NOT NULL,
                    transport_request_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    result_sha256 TEXT NOT NULL,
                    execution_count INTEGER NOT NULL,
                    request_count INTEGER NOT NULL,
                    creator_pid INTEGER NOT NULL,
                    authentication_key_generation INTEGER NOT NULL,
                    authentication_tag TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_reconciliations(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL,
                    provider_request_binding_sha256 TEXT NOT NULL,
                    result_sha256 TEXT,
                    found INTEGER NOT NULL,
                    worker_pid INTEGER NOT NULL,
                    authentication_key_generation INTEGER NOT NULL,
                    authentication_tag TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_recovery_leases(
                    resource_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    lease_token_sha256 TEXT NOT NULL,
                    acquired_at_ms INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    previous_owner_id TEXT,
                    adoption_count INTEGER NOT NULL,
                    authentication_key_generation INTEGER NOT NULL,
                    authentication_tag TEXT NOT NULL
                )
                """
            )
            existing_trust_row = connection.execute(
                """
                SELECT value FROM provider_meta
                WHERE key = 'recovery_authority_trust_anchor_sha256'
                """
            ).fetchone()
            existing_trust_sha256 = (
                str(existing_trust_row[0])
                if existing_trust_row is not None
                else None
            )
            if existing_trust_sha256 is not None:
                if self.recovery_authority_trust_anchor_sha256 is None:
                    self.recovery_authority_trust_anchor_sha256 = (
                        existing_trust_sha256
                    )
                elif self.recovery_authority_trust_anchor_sha256 != existing_trust_sha256:
                    raise ProcessTransportError(
                        "process_recovery_authority_trust_mismatch"
                    )
            elif self.recovery_authority_trust_anchor_sha256 is not None:
                connection.execute(
                    """
                    INSERT INTO provider_meta(key, value)
                    VALUES('recovery_authority_trust_anchor_sha256', ?)
                    """,
                    (self.recovery_authority_trust_anchor_sha256,),
                )
            self._ensure_column(
                connection,
                "provider_results",
                "authentication_key_generation",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "provider_results",
                "authentication_tag",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "provider_reconciliations",
                "authentication_key_generation",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "provider_reconciliations",
                "authentication_tag",
                "TEXT NOT NULL DEFAULT ''",
            )
            seed = _sha256_text(secrets.token_hex(32))
            connection.execute(
                """
                INSERT OR IGNORE INTO provider_meta(key, value)
                VALUES('provider_instance_sha256', ?)
                """,
                (seed,),
            )
            instance_row = connection.execute(
                """
                SELECT value FROM provider_meta
                WHERE key = 'provider_instance_sha256'
                """
            ).fetchone()
            instance = str(instance_row[0]) if instance_row is not None else ""
            existing_key_row = connection.execute(
                """
                SELECT value FROM provider_meta
                WHERE key = 'authentication_key_id'
                """
            ).fetchone()
            existing_key_id = (
                str(existing_key_row[0]) if existing_key_row is not None else None
            )
            if existing_key_id not in {None, self.authentication_key_id}:
                connection.commit()
                return
            generation_row = connection.execute(
                """
                SELECT value FROM provider_meta
                WHERE key = 'authentication_key_generation'
                """
            ).fetchone()
            generation = int(generation_row[0]) if generation_row is not None else 1
            meta_values = {
                "authentication_schema": PROCESS_PROVIDER_AUTHENTICATION_SCHEMA,
                "authentication_key_id": self.authentication_key_id,
                "authentication_key_generation": str(generation),
                "provider_instance_authentication_tag": self._authentication_tag(
                    self._instance_authentication_payload(
                        instance,
                        key_id=self.authentication_key_id,
                        generation=generation,
                        recovery_authority_trust_anchor_sha256=(
                            self.recovery_authority_trust_anchor_sha256
                        ),
                    )
                ),
            }
            for key, value in meta_values.items():
                connection.execute(
                    """
                    INSERT INTO provider_meta(key, value) VALUES(?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )
            connection.commit()

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    def rotate_authentication_key(
        self,
        new_key_file: str | Path,
    ) -> dict[str, Any]:
        if self.read_only:
            raise PermissionError("process_provider_store_read_only")
        current = self.status()
        if current.get("integrity_verified") is not True:
            raise ProcessTransportError(
                "process_provider_authentication_rotation_source_invalid"
            )
        target = Path(new_key_file).expanduser().resolve()
        if target == self.authentication_key_file or target.exists():
            raise ProcessTransportError(
                "process_provider_authentication_rotation_target_invalid"
            )
        new_secret = secrets.token_hex(32).encode("utf-8")
        self._write_authentication_key(target, new_secret)
        new_key_id = hashlib.sha256(new_secret).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            generation = self._authentication_generation(connection) + 1
            result_rows = connection.execute(
                """
                SELECT idempotency_key, provider_request_binding_sha256,
                       transport_request_json, result_json, result_sha256,
                       execution_count, request_count, creator_pid
                FROM provider_results ORDER BY idempotency_key
                """
            ).fetchall()
            for row in result_rows:
                payload = self._result_authentication_payload(
                    idempotency_key=str(row[0]),
                    provider_request_binding_sha256=str(row[1]),
                    transport_request_json=str(row[2]),
                    result_json=str(row[3]),
                    result_sha256=str(row[4]),
                    execution_count=int(row[5]),
                    request_count=int(row[6]),
                    creator_pid=int(row[7]),
                    generation=generation,
                )
                connection.execute(
                    """
                    UPDATE provider_results
                    SET authentication_key_generation = ?, authentication_tag = ?
                    WHERE idempotency_key = ?
                    """,
                    (
                        generation,
                        self._authentication_tag(payload, secret=new_secret),
                        str(row[0]),
                    ),
                )
            reconciliation_rows = connection.execute(
                """
                SELECT sequence, idempotency_key,
                       provider_request_binding_sha256, result_sha256,
                       found, worker_pid
                FROM provider_reconciliations ORDER BY sequence
                """
            ).fetchall()
            for row in reconciliation_rows:
                payload = self._reconciliation_authentication_payload(
                    idempotency_key=str(row[1]),
                    provider_request_binding_sha256=str(row[2]),
                    result_sha256=(str(row[3]) if row[3] is not None else None),
                    found=bool(int(row[4])),
                    worker_pid=int(row[5]),
                    generation=generation,
                )
                connection.execute(
                    """
                    UPDATE provider_reconciliations
                    SET authentication_key_generation = ?, authentication_tag = ?
                    WHERE sequence = ?
                    """,
                    (
                        generation,
                        self._authentication_tag(payload, secret=new_secret),
                        int(row[0]),
                    ),
                )
            lease_rows = connection.execute(
                """
                SELECT resource_id, owner_id, generation, lease_token_sha256,
                       acquired_at_ms, expires_at_ms, status,
                       previous_owner_id, adoption_count
                FROM provider_recovery_leases ORDER BY resource_id
                """
            ).fetchall()
            for row in lease_rows:
                payload = self._lease_authentication_payload(
                    resource_id=str(row[0]),
                    owner_id=str(row[1]),
                    generation=int(row[2]),
                    lease_token_sha256=str(row[3]),
                    acquired_at_ms=int(row[4]),
                    expires_at_ms=int(row[5]),
                    status=str(row[6]),
                    previous_owner_id=(
                        str(row[7]) if row[7] is not None else None
                    ),
                    adoption_count=int(row[8]),
                    authentication_key_generation=generation,
                )
                connection.execute(
                    """
                    UPDATE provider_recovery_leases
                    SET authentication_key_generation = ?, authentication_tag = ?
                    WHERE resource_id = ?
                    """,
                    (
                        generation,
                        self._authentication_tag(payload, secret=new_secret),
                        str(row[0]),
                    ),
                )
            instance = self.identity()
            meta_values = {
                "authentication_key_id": new_key_id,
                "authentication_key_generation": str(generation),
                "provider_instance_authentication_tag": self._authentication_tag(
                    self._instance_authentication_payload(
                        instance,
                        key_id=new_key_id,
                        generation=generation,
                        recovery_authority_trust_anchor_sha256=(
                            self.recovery_authority_trust_anchor_sha256
                        ),
                    ),
                    secret=new_secret,
                ),
            }
            for key, value in meta_values.items():
                connection.execute(
                    "UPDATE provider_meta SET value = ? WHERE key = ?",
                    (value, key),
                )
            connection.commit()
        previous_key_id = self.authentication_key_id
        self.authentication_key_file = target
        self._authentication_secret = new_secret
        self.authentication_key_id = new_key_id
        self.authentication_key_source = "rotated_key_file"
        return {
            "schema": PROCESS_PROVIDER_AUTHENTICATION_SCHEMA,
            "status": "rotated",
            "previous_key_id": previous_key_id,
            "authentication_key_id": new_key_id,
            "authentication_key_generation": generation,
            "provider_instance_sha256": self.identity(),
            "provider_store_authenticity_verified": (
                self.status().get("integrity_verified") is True
            ),
        }

    def _load_authentication_secret(self) -> tuple[bytes, str]:
        if self.authentication_key_file.is_file():
            secret_text = self.authentication_key_file.read_text(
                encoding="utf-8"
            ).strip()
            if len(secret_text) < 32:
                raise ProcessTransportError(
                    "process_provider_authentication_key_invalid"
                )
            return secret_text.encode("utf-8"), "external_key_file"
        if self.read_only:
            return b"", "authentication_key_file_missing"
        if self._database_authentication_key_id() is not None:
            raise ProcessTransportError(
                "process_provider_authentication_key_unavailable"
            )
        secret_bytes = secrets.token_hex(32).encode("utf-8")
        self._write_authentication_key(
            self.authentication_key_file,
            secret_bytes,
        )
        return secret_bytes, "local_key_file"

    def _database_authentication_key_id(self) -> str | None:
        if not self.path.is_file():
            return None
        try:
            with closing(
                sqlite3.connect(
                    f"{self.path.as_uri()}?mode=ro",
                    timeout=30,
                    uri=True,
                )
            ) as connection:
                table = connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name = 'provider_meta'
                    """
                ).fetchone()
                if table is None:
                    return None
                row = connection.execute(
                    """
                    SELECT value FROM provider_meta
                    WHERE key = 'authentication_key_id'
                    """
                ).fetchone()
        except sqlite3.DatabaseError:
            return None
        value = str(row[0]) if row is not None else ""
        return value if _is_sha256(value) else None

    def _verify_database_trust_configuration(self) -> None:
        try:
            with closing(
                sqlite3.connect(
                    f"{self.path.as_uri()}?mode=ro",
                    timeout=30,
                    uri=True,
                )
            ) as connection:
                row = connection.execute(
                    """
                    SELECT value FROM provider_meta
                    WHERE key = 'recovery_authority_trust_anchor_sha256'
                    """
                ).fetchone()
        except sqlite3.DatabaseError:
            return
        stored = str(row[0]) if row is not None else None
        if stored is not None and self.recovery_authority_trust_anchor_sha256 is None:
            self.recovery_authority_trust_anchor_sha256 = stored
        elif stored != self.recovery_authority_trust_anchor_sha256:
            raise ProcessTransportError(
                "process_recovery_authority_trust_mismatch"
            )

    @staticmethod
    def _write_authentication_key(path: Path, secret: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
        temporary.write_text(f"{secret.decode('utf-8')}\n", encoding="utf-8")
        temporary.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _instance_authentication_payload(
        provider_instance_sha256: str,
        *,
        key_id: str,
        generation: int,
        recovery_authority_trust_anchor_sha256: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "schema": PROCESS_PROVIDER_AUTHENTICATION_SCHEMA,
            "kind": "provider_instance",
            "provider_instance_sha256": provider_instance_sha256,
            "authentication_key_id": key_id,
            "authentication_key_generation": generation,
        }
        if recovery_authority_trust_anchor_sha256 is not None:
            payload["recovery_authority_trust_anchor_sha256"] = (
                recovery_authority_trust_anchor_sha256
            )
        return payload

    @staticmethod
    def _result_authentication_payload(
        *,
        idempotency_key: str,
        provider_request_binding_sha256: str,
        transport_request_json: str,
        result_json: str,
        result_sha256: str,
        execution_count: int,
        request_count: int,
        creator_pid: int,
        generation: int,
    ) -> dict[str, Any]:
        return {
            "schema": PROCESS_PROVIDER_AUTHENTICATION_SCHEMA,
            "kind": "provider_result",
            "idempotency_key": idempotency_key,
            "provider_request_binding_sha256": provider_request_binding_sha256,
            "transport_request_json": transport_request_json,
            "result_json": result_json,
            "result_sha256": result_sha256,
            "execution_count": execution_count,
            "request_count": request_count,
            "creator_pid": creator_pid,
            "authentication_key_generation": generation,
        }

    @staticmethod
    def _reconciliation_authentication_payload(
        *,
        idempotency_key: str,
        provider_request_binding_sha256: str,
        result_sha256: str | None,
        found: bool,
        worker_pid: int,
        generation: int,
    ) -> dict[str, Any]:
        return {
            "schema": PROCESS_PROVIDER_AUTHENTICATION_SCHEMA,
            "kind": "provider_reconciliation",
            "idempotency_key": idempotency_key,
            "provider_request_binding_sha256": provider_request_binding_sha256,
            "result_sha256": result_sha256,
            "found": found,
            "worker_pid": worker_pid,
            "authentication_key_generation": generation,
        }

    @staticmethod
    def _lease_authentication_payload(
        *,
        resource_id: str,
        owner_id: str,
        generation: int,
        lease_token_sha256: str,
        acquired_at_ms: int,
        expires_at_ms: int,
        status: str,
        previous_owner_id: str | None,
        adoption_count: int,
        authentication_key_generation: int,
    ) -> dict[str, Any]:
        return {
            "resource_id": resource_id,
            "owner_id": owner_id,
            "generation": generation,
            "lease_token_sha256": lease_token_sha256,
            "acquired_at_ms": acquired_at_ms,
            "expires_at_ms": expires_at_ms,
            "status": status,
            "previous_owner_id": previous_owner_id,
            "adoption_count": adoption_count,
            "authentication_key_generation": authentication_key_generation,
        }

    def _authentication_generation(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            """
            SELECT value FROM provider_meta
            WHERE key = 'authentication_key_generation'
            """
        ).fetchone()
        try:
            generation = int(row[0]) if row is not None else 0
        except (TypeError, ValueError):
            generation = 0
        if generation < 1:
            raise ProcessTransportError(
                "process_provider_authentication_generation_invalid"
            )
        return generation

    def _authentication_tag(
        self,
        payload: dict[str, Any],
        *,
        secret: bytes | None = None,
    ) -> str:
        signing_secret = secret if secret is not None else self._authentication_secret
        if not signing_secret:
            raise ProcessTransportError(
                "process_provider_authentication_key_unavailable"
            )
        return hmac.new(
            signing_secret,
            _canonical_json(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _verify_authentication_tag(
        self,
        payload: dict[str, Any],
        tag: str,
    ) -> bool:
        if not self._authentication_secret or not _is_sha256(tag):
            return False
        return hmac.compare_digest(self._authentication_tag(payload), tag)


class ProcessIsolatedTransportAdapter(ModelAdapter):
    """Invoke a durable no-network provider worker in a separate process."""

    def __init__(
        self,
        provider_database: str | Path,
        *,
        provider_authentication_key_file: str | Path | None = None,
        recovery_authority_trust_anchor: dict[str, Any] | None = None,
        first_response_delay_ms: int = 0,
        first_commit_marker: str | Path | None = None,
        first_failure_mode: str | None = None,
        worker_timeout_seconds: float = 60.0,
    ):
        if first_response_delay_ms < 0:
            raise ValueError("process_transport_response_delay_invalid")
        if first_failure_mode not in {None, "before_commit_exit"}:
            raise ValueError("process_transport_failure_mode_invalid")
        self.provider_database = Path(provider_database).expanduser().resolve()
        self.provider_authentication_key_file = (
            Path(provider_authentication_key_file).expanduser().resolve()
            if provider_authentication_key_file is not None
            else Path(f"{self.provider_database}.provider_key")
        )
        self.recovery_authority_trust_anchor = (
            deepcopy(recovery_authority_trust_anchor)
            if recovery_authority_trust_anchor is not None
            else None
        )
        self.first_response_delay_ms = first_response_delay_ms
        self.first_commit_marker = (
            Path(first_commit_marker).expanduser().resolve()
            if first_commit_marker is not None
            else None
        )
        self.first_failure_mode = first_failure_mode
        self.worker_timeout_seconds = worker_timeout_seconds
        self.infer_calls = 0
        store = DurableProcessProviderStore(
            self.provider_database,
            authentication_key_file=self.provider_authentication_key_file,
            recovery_authority_trust_anchor=(
                self.recovery_authority_trust_anchor
            ),
        )
        self.provider_instance_sha256 = store.identity()
        self.provider_authentication_key_id = store.authentication_key_id

    async def infer(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.infer_calls += 1
        source = context if isinstance(context, dict) else {}
        options: list[str] = []
        if self.infer_calls == 1:
            if self.first_response_delay_ms:
                options.extend(
                    ["--response-delay-ms", str(self.first_response_delay_ms)]
                )
            if self.first_commit_marker is not None:
                options.extend(["--commit-marker", str(self.first_commit_marker)])
            if self.first_failure_mode is not None:
                options.extend(["--failure-mode", self.first_failure_mode])
        envelope = self._run_worker(
            "infer",
            {
                "schema": PROCESS_PROVIDER_REQUEST_SCHEMA,
                "prompt": prompt,
                "context": source,
            },
            options=options,
        )
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise ProcessTransportError("process_provider_result_missing")
        return result

    def reconcile_transport(
        self,
        idempotency_key: str,
        provider_request_binding_sha256: str,
    ) -> dict[str, Any] | None:
        envelope = self._run_worker(
            "reconcile",
            {
                "schema": PROCESS_PROVIDER_REQUEST_SCHEMA,
                "idempotency_key": idempotency_key,
                "provider_request_binding_sha256": (
                    provider_request_binding_sha256
                ),
            },
        )
        if envelope.get("status") == "not_found":
            return None
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise ProcessTransportError("process_provider_reconciliation_result_missing")
        return result

    def health(self) -> dict[str, Any]:
        status = DurableProcessProviderStore(
            self.provider_database,
            authentication_key_file=self.provider_authentication_key_file,
            recovery_authority_trust_anchor=(
                self.recovery_authority_trust_anchor
            ),
            read_only=True,
        ).status()
        return {
            "ok": (
                status.get("integrity_verified") is True
                and status.get("provider_store_authenticity_verified") is True
            ),
            "provider": PROCESS_PROVIDER_NAME,
            "model_version": PROCESS_PROVIDER_MODEL_VERSION,
            "provider_instance_sha256": self.provider_instance_sha256,
            "provider_store_authentication_key_id": (
                self.provider_authentication_key_id
            ),
            "provider_store_authenticity_verified": status.get(
                "provider_store_authenticity_verified"
            )
            is True,
            "recovery_authority_trust_anchor_sha256": status.get(
                "recovery_authority_trust_anchor_sha256"
            ),
            "recovery_authority_trust_verified": status.get(
                "recovery_authority_trust_verified"
            )
            is True,
            "requires_api_key": False,
            "network_used": False,
        }

    def get_capabilities(self) -> dict[str, Any]:
        trust_anchor = self.recovery_authority_trust_anchor
        signed_authority = trust_anchor is not None
        stateful_authority = bool(
            trust_anchor is not None
            and trust_anchor.get("authority_state_required") is True
        )
        return {
            "interface": "ModelAdapter",
            "supports_structured_evaluation": True,
            "supports_provider_observation": True,
            "provider_observation_source": PROCESS_PROVIDER_OBSERVATION_SOURCE,
            "execution_environment": "local_process",
            "billing_class": "no_charge",
            "supports_transport_idempotency": True,
            "supports_transport_reconciliation": True,
            "supports_process_isolated_recovery": True,
            "supports_authenticated_provider_store": True,
            "provider_store_authentication_schema": (
                PROCESS_PROVIDER_AUTHENTICATION_SCHEMA
            ),
            "provider_store_authentication_key_id": (
                self.provider_authentication_key_id
            ),
            "supports_leased_recovery_supervisor": True,
            "recovery_lease_schema": PROCESS_RECOVERY_LEASE_SCHEMA,
            "recovery_supervisor_authority_schema": (
                PROCESS_RECOVERY_SUPERVISOR_AUTHORITY_SCHEMA
            ),
            "supports_signed_recovery_authority": signed_authority,
            "recovery_authority_grant_schema": (
                RECOVERY_AUTHORITY_GRANT_SCHEMA if signed_authority else None
            ),
            "recovery_authority_trust_schema": (
                RECOVERY_AUTHORITY_TRUST_SCHEMA if signed_authority else None
            ),
            "recovery_authority_trust_anchor": (
                deepcopy(self.recovery_authority_trust_anchor)
                if signed_authority
                else None
            ),
            "recovery_authority_trust_anchor_sha256": (
                self.recovery_authority_trust_anchor.get("trust_anchor_sha256")
                if self.recovery_authority_trust_anchor is not None
                else None
            ),
            "supervisor_attestation_schema": (
                SUPERVISOR_ATTESTATION_SCHEMA if signed_authority else None
            ),
            "process_transport_protocol": (
                STATEFUL_SIGNED_PROCESS_TRANSPORT_PROTOCOL
                if stateful_authority
                else (
                    SIGNED_PROCESS_TRANSPORT_PROTOCOL
                    if signed_authority
                    else PROCESS_TRANSPORT_PROTOCOL
                )
            ),
            "transport_instance_sha256": self.provider_instance_sha256,
        }

    def _run_worker(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        options: list[str] | None = None,
    ) -> dict[str, Any]:
        arguments = [
            sys.executable,
            "-m",
            "spst_runtime.process_transport_worker",
            command,
            "--provider-db",
            str(self.provider_database),
            "--expected-instance-sha256",
            self.provider_instance_sha256,
            "--provider-key-file",
            str(self.provider_authentication_key_file),
            *(options or []),
        ]
        environment = os.environ.copy()
        environment.pop("OPENAI_API_KEY", None)
        environment["SPST_PROCESS_TRANSPORT_NO_NETWORK"] = "1"
        try:
            completed = subprocess.run(
                arguments,
                input=_canonical_json(payload),
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
                timeout=self.worker_timeout_seconds,
                cwd=str(Path(__file__).resolve().parents[1]),
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProcessTransportError(
                f"process_transport_worker_unavailable:{type(error).__name__}"
            ) from None
        if completed.returncode != 0:
            raise ProcessTransportError(
                f"process_transport_worker_failed:{completed.returncode}"
            )
        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError:
            raise ProcessTransportError("process_transport_worker_output_invalid") from None
        if not isinstance(envelope, dict):
            raise ProcessTransportError("process_transport_worker_output_invalid")
        digest = envelope.get("response_sha256")
        unsigned = {
            key: value for key, value in envelope.items() if key != "response_sha256"
        }
        if (
            envelope.get("schema") != PROCESS_PROVIDER_RESPONSE_SCHEMA
            or not _is_sha256(digest)
            or _canonical_hash(unsigned) != digest
            or envelope.get("provider_instance_sha256")
            != self.provider_instance_sha256
        ):
            raise ProcessTransportError("process_transport_worker_response_invalid")
        return envelope


def build_process_provider_response(
    *,
    provider_instance_sha256: str,
    status: str,
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    unsigned = {
        "schema": PROCESS_PROVIDER_RESPONSE_SCHEMA,
        "status": status,
        "provider_instance_sha256": provider_instance_sha256,
        "worker_pid": os.getpid(),
        "result": result,
        "result_sha256": _canonical_hash(result) if result is not None else None,
    }
    return {**unsigned, "response_sha256": _canonical_hash(unsigned)}


def build_recovery_lease_resource_id(
    *,
    program_id: str,
    attempt_id: str,
    provider_instance_sha256: str,
) -> str:
    if (
        not _safe_process_identifier(program_id)
        or not _safe_process_identifier(attempt_id)
        or not _is_sha256(provider_instance_sha256)
    ):
        raise ProcessTransportError("process_recovery_lease_resource_invalid")
    return _canonical_hash(
        {
            "program_id": program_id,
            "attempt_id": attempt_id,
            "provider_instance_sha256": provider_instance_sha256,
        }
    )


def build_recovery_supervisor_authority(
    *,
    resource_id: str,
    provider_instance_sha256: str,
    previous_owner_id: str,
    expired_generation: int,
    operator_id: str,
) -> dict[str, Any]:
    if (
        not _is_sha256(resource_id)
        or not _is_sha256(provider_instance_sha256)
        or not _safe_process_identifier(previous_owner_id)
        or not _safe_process_identifier(operator_id)
        or not isinstance(expired_generation, int)
        or isinstance(expired_generation, bool)
        or expired_generation < 1
    ):
        raise ProcessTransportError(
            "process_recovery_supervisor_authority_invalid"
        )
    unsigned = {
        "schema": PROCESS_RECOVERY_SUPERVISOR_AUTHORITY_SCHEMA,
        "action": "adopt_expired_recovery_lease",
        "resource_id": resource_id,
        "provider_instance_sha256": provider_instance_sha256,
        "previous_owner_id": previous_owner_id,
        "expired_generation": expired_generation,
        "operator_id": operator_id,
        "operator_identity_verified": False,
    }
    return {**unsigned, "authority_sha256": _canonical_hash(unsigned)}


def verify_recovery_supervisor_authority(
    value: dict[str, Any] | None,
    *,
    resource_id: str,
    provider_instance_sha256: str,
    previous_owner_id: str,
    expired_generation: int,
) -> tuple[bool, str | None]:
    if not isinstance(value, dict):
        return False, "process_recovery_orphan_adoption_authority_required"
    authority_sha256 = value.get("authority_sha256")
    unsigned = {
        key: item for key, item in value.items() if key != "authority_sha256"
    }
    if (
        value.get("schema") != PROCESS_RECOVERY_SUPERVISOR_AUTHORITY_SCHEMA
        or value.get("action") != "adopt_expired_recovery_lease"
        or value.get("resource_id") != resource_id
        or value.get("provider_instance_sha256")
        != provider_instance_sha256
        or value.get("previous_owner_id") != previous_owner_id
        or value.get("expired_generation") != expired_generation
        or value.get("operator_identity_verified") is not False
        or not _safe_process_identifier(value.get("operator_id"))
        or not _is_sha256(authority_sha256)
        or _canonical_hash(unsigned) != authority_sha256
    ):
        return False, "process_recovery_supervisor_authority_mismatch"
    return True, None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _canonical_hash(value: Any) -> str:
    return _sha256_text(_canonical_json(value))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _safe_process_identifier(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 128:
        return False
    return all(character.isalnum() or character in "._:-" for character in value)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
