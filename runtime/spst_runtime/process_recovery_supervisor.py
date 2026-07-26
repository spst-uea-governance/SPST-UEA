import argparse
import json
import os
from pathlib import Path
import sqlite3
from threading import Event, Thread
import time
from typing import Any

from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.process_transport import (
    DurableProcessProviderStore,
    ProcessIsolatedTransportAdapter,
    ProcessTransportError,
    build_recovery_lease_resource_id,
)
from spst_runtime.real_paired_outcome import RealPairedOutcomeProgram


class _LeaseHeartbeat:
    def __init__(
        self,
        store: DurableProcessProviderStore,
        lease: dict[str, Any],
        *,
        ttl_ms: int,
    ):
        self.store = store
        self.lease = lease
        self.ttl_ms = ttl_ms
        self.stop_event = Event()
        self.error: Exception | None = None
        self.thread = Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=max(2.0, self.ttl_ms / 1000))
        if self.thread.is_alive():
            raise ProcessTransportError(
                "process_recovery_supervisor_heartbeat_stop_failed"
            )
        if self.error is not None:
            raise ProcessTransportError(
                "process_recovery_supervisor_heartbeat_failed"
            ) from self.error

    def _run(self) -> None:
        interval_seconds = max(0.05, self.ttl_ms / 3000)
        while not self.stop_event.wait(interval_seconds):
            try:
                self.store.renew_recovery_lease(
                    str(self.lease["resource_id"]),
                    str(self.lease["owner_id"]),
                    generation=int(self.lease["generation"]),
                    lease_token=str(self.lease["lease_token"]),
                    ttl_ms=self.ttl_ms,
                )
            except Exception as error:
                self.error = error
                self.stop_event.set()
                return


def _json_file(path: str | None, *, required: bool) -> dict[str, Any] | None:
    if path is None:
        if required:
            raise ValueError("process_recovery_supervisor_input_required")
        return None
    try:
        value = json.loads(
            Path(path).expanduser().resolve().read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("process_recovery_supervisor_input_invalid") from error
    if not isinstance(value, dict):
        raise ValueError("process_recovery_supervisor_input_invalid")
    return value


def _marker(path: str | None, lease: dict[str, Any]) -> None:
    if path is None:
        return
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    visible = {key: value for key, value in lease.items() if key != "lease_token"}
    target.write_text(
        json.dumps(
            {
                "schema": "spst-process-recovery-supervisor-marker-v1",
                "state": "lease_acquired",
                "supervisor_pid": os.getpid(),
                "lease": visible,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Recover one process-isolated paired program under an authenticated "
            "durable fencing lease."
        )
    )
    parser.add_argument("command", choices=("recover", "status"))
    parser.add_argument("--database")
    parser.add_argument("--provider-db", required=True)
    parser.add_argument("--provider-key-file", required=True)
    parser.add_argument("--program-id")
    parser.add_argument("--intervention-file")
    parser.add_argument("--recovery-authority-file")
    parser.add_argument("--adoption-authority-file")
    parser.add_argument("--lease-owner")
    parser.add_argument("--lease-ttl-ms", type=int, default=1000)
    parser.add_argument("--lease-marker")
    parser.add_argument("--post-lease-delay-ms", type=int, default=0)
    parser.add_argument("--resource-id")
    args = parser.parse_args(argv)

    try:
        if args.post_lease_delay_ms < 0:
            raise ValueError("process_recovery_supervisor_delay_invalid")
        store = DurableProcessProviderStore(
            args.provider_db,
            authentication_key_file=args.provider_key_file,
            read_only=args.command == "status",
        )
        if args.command == "status":
            if args.resource_id is None:
                raise ValueError("process_recovery_supervisor_resource_required")
            response = {
                "schema": "spst-process-recovery-supervisor-result-v1",
                "command": "status",
                "supervisor_pid": os.getpid(),
                "provider": store.status(),
                "lease": store.recovery_lease_status(args.resource_id),
            }
        else:
            required_values = (
                args.database,
                args.program_id,
                args.intervention_file,
                args.recovery_authority_file,
                args.lease_owner,
            )
            if any(value is None for value in required_values):
                raise ValueError("process_recovery_supervisor_arguments_incomplete")
            repository = SQLiteRepository(str(args.database))
            corpus = OperationalEvaluationCorpus(repository)
            adapter = ProcessIsolatedTransportAdapter(
                args.provider_db,
                provider_authentication_key_file=args.provider_key_file,
            )
            program = RealPairedOutcomeProgram(repository, corpus, adapter)
            current = program.get(str(args.program_id))
            if current is None or current.get("status") != "recovery_required":
                raise ValueError("process_recovery_supervisor_program_not_recoverable")
            attempt = current.get("operational_hardening", {}).get("attempt")
            attempt_id = attempt.get("attempt_id") if isinstance(attempt, dict) else None
            if not isinstance(attempt_id, str):
                raise ValueError("process_recovery_supervisor_attempt_missing")
            resource_id = build_recovery_lease_resource_id(
                program_id=str(args.program_id),
                attempt_id=attempt_id,
                provider_instance_sha256=store.identity(),
            )
            lease = store.acquire_recovery_lease(
                resource_id,
                str(args.lease_owner),
                ttl_ms=args.lease_ttl_ms,
                adoption_authority=_json_file(
                    args.adoption_authority_file,
                    required=False,
                ),
            )
            _marker(args.lease_marker, lease)
            heartbeat = _LeaseHeartbeat(
                store,
                lease,
                ttl_ms=args.lease_ttl_ms,
            )
            heartbeat.start()
            if args.post_lease_delay_ms:
                time.sleep(args.post_lease_delay_ms / 1000)
            result = program.recover(
                str(args.program_id),
                context_intervention=_json_file(
                    args.intervention_file,
                    required=True,
                )
                or {},
                recovery_authority=_json_file(
                    args.recovery_authority_file,
                    required=True,
                )
                or {},
            )
            heartbeat.stop()
            released = store.release_recovery_lease(
                resource_id,
                str(args.lease_owner),
                generation=int(lease["generation"]),
                lease_token=str(lease["lease_token"]),
            )
            response = {
                "schema": "spst-process-recovery-supervisor-result-v1",
                "command": "recover",
                "supervisor_pid": os.getpid(),
                "lease": {
                    key: value for key, value in lease.items() if key != "lease_token"
                },
                "lease_release": released,
                "result": result,
            }
    except (
        OSError,
        sqlite3.Error,
        ProcessTransportError,
        ValueError,
    ) as error:
        print(
            json.dumps(
                {
                    "schema": "spst-process-recovery-supervisor-error-v1",
                    "status": "rejected",
                    "reason": str(error),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2

    print(json.dumps(response, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
