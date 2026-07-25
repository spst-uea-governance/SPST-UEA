import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any

from spst_runtime.process_transport import (
    PROCESS_PROVIDER_REQUEST_SCHEMA,
    DurableProcessProviderStore,
    ProcessTransportError,
    build_process_provider_response,
)


def _payload() -> dict[str, Any]:
    try:
        value = json.loads(sys.stdin.read())
    except json.JSONDecodeError as error:
        raise ProcessTransportError("process_transport_payload_invalid") from error
    if not isinstance(value, dict) or value.get("schema") != PROCESS_PROVIDER_REQUEST_SCHEMA:
        raise ProcessTransportError("process_transport_payload_invalid")
    return value


def _write_marker(path: str | None, state: str, result: dict[str, Any] | None) -> None:
    if path is None:
        return
    marker = Path(path).expanduser().resolve()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "schema": "spst-process-provider-commit-marker-v1",
                "state": state,
                "worker_pid": os.getpid(),
                "result_sha256": (
                    build_process_provider_response(
                        provider_instance_sha256="0" * 64,
                        status="marker",
                        result=result,
                    )["result_sha256"]
                    if result is not None
                    else None
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="No-network process-isolated provider simulator worker."
    )
    parser.add_argument("command", choices=("infer", "reconcile", "status"))
    parser.add_argument("--provider-db", required=True)
    parser.add_argument("--expected-instance-sha256", required=True)
    parser.add_argument("--response-delay-ms", type=int, default=0)
    parser.add_argument("--commit-marker")
    parser.add_argument("--failure-mode", choices=("before_commit_exit",))
    args = parser.parse_args(argv)

    try:
        store = DurableProcessProviderStore(args.provider_db)
        instance = store.identity()
        if instance != args.expected_instance_sha256:
            raise ProcessTransportError("process_provider_instance_mismatch")
        if args.command == "status":
            result: dict[str, Any] | None = store.status()
            status = "completed"
        elif args.command == "infer":
            request = _payload()
            prompt = request.get("prompt")
            context = request.get("context")
            if not isinstance(prompt, str) or not isinstance(context, dict):
                raise ProcessTransportError("process_transport_infer_payload_invalid")
            if args.failure_mode == "before_commit_exit":
                _write_marker(args.commit_marker, "before_commit_exit", None)
                os._exit(92)
            result = store.infer(prompt, context)
            _write_marker(args.commit_marker, "provider_committed", result)
            if args.response_delay_ms < 0:
                raise ProcessTransportError("process_transport_response_delay_invalid")
            if args.response_delay_ms:
                time.sleep(args.response_delay_ms / 1000)
            status = "completed"
        else:
            request = _payload()
            idempotency_key = request.get("idempotency_key")
            binding_sha256 = request.get("provider_request_binding_sha256")
            if not isinstance(idempotency_key, str) or not isinstance(
                binding_sha256, str
            ):
                raise ProcessTransportError(
                    "process_transport_reconciliation_payload_invalid"
                )
            result = store.reconcile(idempotency_key, binding_sha256)
            status = "completed" if result is not None else "not_found"
        response = build_process_provider_response(
            provider_instance_sha256=instance,
            status=status,
            result=result,
        )
    except (OSError, sqlite3.Error, ProcessTransportError, ValueError) as error:
        print(
            json.dumps(
                {
                    "schema": "spst-process-provider-worker-error-v1",
                    "status": "rejected",
                    "reason": str(error),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    print(json.dumps(response, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
