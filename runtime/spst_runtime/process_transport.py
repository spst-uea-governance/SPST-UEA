import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
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


PROCESS_PROVIDER_REQUEST_SCHEMA = "spst-process-provider-request-v1"
PROCESS_PROVIDER_RESPONSE_SCHEMA = "spst-process-provider-response-v1"
PROCESS_PROVIDER_STATUS_SCHEMA = "spst-process-provider-status-v1"
PROCESS_PROVIDER_NAME = "spst-local-process-provider"
PROCESS_PROVIDER_MODEL_VERSION = "process-transport-v1"
PROCESS_PROVIDER_OBSERVATION_SOURCE = "local_process_provider_echo"
PROCESS_TRANSPORT_PROTOCOL = "subprocess-stdio-sqlite-v1"


class ProcessTransportError(ValueError):
    """Reject an invalid or unavailable local process transport boundary."""


class DurableProcessProviderStore:
    """A no-network SQLite provider simulator shared by independent processes."""

    def __init__(self, path: str | Path, *, read_only: bool = False):
        self.path = Path(path).expanduser().resolve()
        self.read_only = read_only
        if not read_only:
            self._initialize()

    def identity(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM provider_meta WHERE key = ?",
                ("provider_instance_sha256",),
            ).fetchone()
        value = str(row[0]) if row is not None else ""
        if not _is_sha256(value):
            raise ProcessTransportError("process_provider_instance_identity_invalid")
        return value

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

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT idempotency_key, provider_request_binding_sha256,
                       transport_request_json, result_json, result_sha256,
                       execution_count, request_count, creator_pid
                FROM provider_results WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is None:
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
                        creator_pid
                    ) VALUES(?, ?, ?, ?, ?, 1, 1, ?)
                    """,
                    (
                        idempotency_key,
                        provider_binding["request_binding_sha256"],
                        transport_json,
                        result_json,
                        result_sha256,
                        os.getpid(),
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
            connection.execute(
                """
                UPDATE provider_results
                SET request_count = request_count + 1
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            )
            connection.commit()
            return existing

    def reconcile(
        self,
        idempotency_key: str,
        provider_request_binding_sha256: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT idempotency_key, provider_request_binding_sha256,
                       transport_request_json, result_json, result_sha256,
                       execution_count, request_count, creator_pid
                FROM provider_results WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO provider_reconciliations(
                        idempotency_key,
                        provider_request_binding_sha256,
                        result_sha256,
                        found,
                        worker_pid
                    ) VALUES(?, ?, NULL, 0, ?)
                    """,
                    (
                        idempotency_key,
                        provider_request_binding_sha256,
                        os.getpid(),
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
            connection.execute(
                """
                INSERT INTO provider_reconciliations(
                    idempotency_key,
                    provider_request_binding_sha256,
                    result_sha256,
                    found,
                    worker_pid
                ) VALUES(?, ?, ?, 1, ?)
                """,
                (
                    idempotency_key,
                    provider_request_binding_sha256,
                    str(row[4]),
                    os.getpid(),
                ),
            )
            connection.commit()
            return result

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT idempotency_key, provider_request_binding_sha256,
                       transport_request_json, result_json, result_sha256,
                       execution_count, request_count, creator_pid
                FROM provider_results ORDER BY idempotency_key
                """
            ).fetchall()
            reconciliations = connection.execute(
                """
                SELECT found, worker_pid
                FROM provider_reconciliations ORDER BY sequence
                """
            ).fetchall()
        reasons = [self._validate_result_row(row)[1] for row in rows]
        return {
            "schema": PROCESS_PROVIDER_STATUS_SCHEMA,
            "provider_instance_sha256": self.identity(),
            "record_count": len(rows),
            "total_execution_count": sum(int(row[5]) for row in rows),
            "total_infer_request_count": sum(int(row[6]) for row in rows),
            "reconciliation_query_count": len(reconciliations),
            "reconciliation_found_count": sum(
                1 for row in reconciliations if int(row[0]) == 1
            ),
            "creator_pids": sorted({int(row[7]) for row in rows}),
            "reconciliation_pids": sorted(
                {int(row[1]) for row in reconciliations}
            ),
            "integrity_verified": not any(reasons),
            "reason": next((reason for reason in reasons if reason), None),
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
            int(row[7])
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
        with sqlite3.connect(str(self.path), timeout=30) as connection:
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
                    creator_pid INTEGER NOT NULL
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
                    worker_pid INTEGER NOT NULL
                )
                """
            )
            seed = _sha256_text(secrets.token_hex(32))
            connection.execute(
                """
                INSERT OR IGNORE INTO provider_meta(key, value)
                VALUES('provider_instance_sha256', ?)
                """,
                (seed,),
            )
            connection.commit()


class ProcessIsolatedTransportAdapter(ModelAdapter):
    """Invoke a durable no-network provider worker in a separate process."""

    def __init__(
        self,
        provider_database: str | Path,
        *,
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
        self.first_response_delay_ms = first_response_delay_ms
        self.first_commit_marker = (
            Path(first_commit_marker).expanduser().resolve()
            if first_commit_marker is not None
            else None
        )
        self.first_failure_mode = first_failure_mode
        self.worker_timeout_seconds = worker_timeout_seconds
        self.infer_calls = 0
        self.provider_instance_sha256 = DurableProcessProviderStore(
            self.provider_database
        ).identity()

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
            read_only=True,
        ).status()
        return {
            "ok": status.get("integrity_verified") is True,
            "provider": PROCESS_PROVIDER_NAME,
            "model_version": PROCESS_PROVIDER_MODEL_VERSION,
            "provider_instance_sha256": self.provider_instance_sha256,
            "requires_api_key": False,
            "network_used": False,
        }

    def get_capabilities(self) -> dict[str, Any]:
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
            "process_transport_protocol": PROCESS_TRANSPORT_PROTOCOL,
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


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
