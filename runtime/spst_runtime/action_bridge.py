import argparse
import json
from pathlib import Path

from spst_runtime.action_manifest import ActionManifestLedger
from spst_runtime.verification_profiles import VERIFICATION_PROFILE_NAMES


def _default_session_path() -> str:
    return str(Path(__file__).resolve().parents[1] / "spst_chat_state.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bind governed local actions to a verified SPST routing receipt."
    )
    parser.add_argument("--session-db", default=_default_session_path())
    subparsers = parser.add_subparsers(dest="command", required=True)

    execute = subparsers.add_parser("execute-profile")
    execute.add_argument("--receipt-id", required=True)
    execute.add_argument("--profile", required=True, choices=VERIFICATION_PROFILE_NAMES)
    execute.add_argument("--repository-root", required=True)

    prepare = subparsers.add_parser("prepare-external")
    prepare.add_argument("--receipt-id", required=True)
    prepare.add_argument("--operation", required=True)
    prepare.add_argument("--tool-name", required=True)
    prepare.add_argument("--arguments-sha256", required=True)
    prepare.add_argument("--workspace-root", required=True)

    approve = subparsers.add_parser("approve")
    approve.add_argument("--action-id", required=True)
    approve.add_argument("--decision", choices=("approve", "reject"), required=True)
    approve.add_argument("--actor", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--action-id", required=True)

    receipt_status = subparsers.add_parser("receipt-status")
    receipt_status.add_argument("--receipt-id", required=True)

    args = parser.parse_args(argv)
    read_only = args.command in {"verify", "receipt-status"}
    ledger = ActionManifestLedger(args.session_db, read_only=read_only)

    try:
        if args.command == "execute-profile":
            result = ledger.run_fixed_profile(
                args.receipt_id,
                args.profile,
                repository_root=args.repository_root,
            )
        elif args.command == "prepare-external":
            result = ledger.prepare_external_action(
                args.receipt_id,
                operation=args.operation,
                tool_name=args.tool_name,
                arguments_sha256=args.arguments_sha256,
                workspace_root=args.workspace_root,
            )
        elif args.command == "approve":
            result = ledger.record_approval(
                args.action_id,
                approved=args.decision == "approve",
                actor=args.actor,
            )
        elif args.command == "verify":
            result = ledger.verify(args.action_id)
        else:
            result = ledger.summarize_receipt(args.receipt_id)
    except (PermissionError, ValueError) as error:
        result = {
            "schema": "spst-action-bridge-error-v1",
            "status": "rejected",
            "reason": str(error),
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
