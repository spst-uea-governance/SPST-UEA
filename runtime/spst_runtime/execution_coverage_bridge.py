import argparse
import json
from pathlib import Path

from spst_runtime.execution_coverage import (
    EXECUTION_PLAN_REQUEST_SCHEMA,
    GovernedExecutionCoverageLedger,
)


def _default_session_path() -> str:
    return str(Path(__file__).resolve().parents[1] / "spst_chat_state.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Register and verify a Receipt-bound declared action denominator."
    )
    parser.add_argument("--session-db", default=_default_session_path())
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register")
    register.add_argument("--receipt-id", required=True)
    register.add_argument("--plan-file", required=True)
    register.add_argument("--repository-root", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--plan-id", required=True)

    status = subparsers.add_parser("status")
    status.add_argument("--receipt-id", required=True)

    args = parser.parse_args(argv)
    read_only = args.command in {"verify", "status"}
    ledger = GovernedExecutionCoverageLedger(args.session_db, read_only=read_only)
    try:
        if args.command == "register":
            request = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
            if not isinstance(request, dict) or request.get("schema") != EXECUTION_PLAN_REQUEST_SCHEMA:
                raise ValueError("execution_plan_request_schema_mismatch")
            actions = request.get("actions")
            if not isinstance(actions, list) or any(not isinstance(item, dict) for item in actions):
                raise ValueError("execution_plan_request_actions_invalid")
            result = ledger.register_plan(
                args.receipt_id,
                actions,
                repository_root=args.repository_root,
            )
        elif args.command == "verify":
            result = ledger.summarize(args.plan_id)
        else:
            result = ledger.summarize_receipt(args.receipt_id)
    except (json.JSONDecodeError, OSError, PermissionError, ValueError) as error:
        result = {
            "schema": "spst-execution-coverage-bridge-error-v1",
            "status": "rejected",
            "reason": str(error),
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
