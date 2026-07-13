import argparse
import json
from pathlib import Path

from spst_runtime.adaptive_profile import RequestedProfile, select_execution_profile
from spst_runtime.cli import run_dispatch, run_loop
from spst_runtime.chat_session import ChatSessionStore, record_turn, summarize_session
from spst_runtime.routing_receipt import ROUTING_RECEIPT_SCHEMA, RoutingReceiptLedger


def run_chat_turn(
    prompt: str,
    steps: int = 3,
    event: str = "chat_turn",
    *,
    profile: RequestedProfile = "auto",
    session_path: str | None = None,
    memory_path: str | None = None,
) -> dict:
    """Run one Codex-mediated chat turn through SPST-UEA without an API key."""

    selected_profile = select_execution_profile(prompt, event=event, requested=profile)
    profile_payload = selected_profile.as_dict()
    loop = run_loop(steps)
    pipeline = run_dispatch(
        event,
        prompt=prompt,
        extra_payload={"execution_profile": profile_payload},
    )
    session = record_turn(
        prompt,
        event,
        pipeline,
        steps=steps,
        session_path=session_path,
        memory_path=memory_path,
        execution_profile=profile_payload,
    )

    return {
        "mode": "codex-chat-mediated",
        "requires_api_key": False,
        "input": {
            "prompt": prompt,
            "event": event,
            "steps": steps,
            "execution_profile": profile_payload,
        },
        "runtime": {
            "loop": loop,
            "pipeline": pipeline,
        },
        "execution_profile": profile_payload,
        "session": {
            "turn_count": session["turn_count"],
            "goals": session["goals"],
            "evaluation": session["evaluation"],
            "summary": session["summary"],
            "amplification": session["amplification"],
            "memory": session["memory"],
            "latest_audit": session["audit"][-1],
            "execution_profile": profile_payload,
        },
        "routing_receipt": session["routing_receipt"],
    }


def _empty_routing_status(turn_count: int) -> dict:
    return {
        "receipt_schema": ROUTING_RECEIPT_SCHEMA,
        "total_receipts": 0,
        "verified_receipts": 0,
        "verified_turns": 0,
        "profile_counts": {},
        "memory_action_counts": {},
        "failed_receipts": 0,
        "receipt_epoch_start_turn": None,
        "eligible_turns": 0,
        "legacy_unreceipted_turns": turn_count,
        "receipt_coverage": None,
        "latest_receipt_id": None,
        "latest_receipt_verified": None,
        "global_codex_task_coverage": None,
        "global_coverage_reason": "codex_task_denominator_unavailable",
        "task_quality_delta": None,
        "task_quality_reason": "paired_outcome_measurement_unavailable",
    }


def get_chat_status(session_path: str | None = None) -> dict:
    store = ChatSessionStore(session_path, read_only=True)
    database_exists = Path(store.path).expanduser().is_file()
    state = store.load()
    turn_count = int(state.get("turn_count", 0))
    routing = (
        RoutingReceiptLedger(store.path, read_only=True).summarize(turn_count)
        if database_exists
        else _empty_routing_status(turn_count)
    )
    provenance = (
        store.repository.verify_provenance()
        if database_exists
        else {
            "valid": False,
            "entries": 0,
            "reason": "database_missing",
            "key_source": "unavailable",
        }
    )
    return {
        "mode": state.get("mode"),
        "provider": state.get("provider"),
        "requires_api_key": False,
        "turn_count": turn_count,
        "evaluation": state.get("evaluation", {}),
        "summary": state.get("summary", summarize_session(state)),
        "amplification": state.get("amplification", {}),
        "memory": state.get("memory", {}),
        "latest_audit": (state.get("audit") or [None])[-1],
        "routing": routing,
        "persistence": {
            "access": "read_only_immutable",
            "source_unchanged": True,
            "state": "present" if database_exists else "missing",
            "provenance": provenance,
        },
    }


def verify_chat_receipt(receipt_id: str, session_path: str | None = None) -> dict:
    store = ChatSessionStore(session_path, read_only=True)
    if not Path(store.path).expanduser().is_file():
        return {"verified": False, "receipt_id": receipt_id, "reason": "receipt_missing"}
    verification = RoutingReceiptLedger(store.path, read_only=True).verify(receipt_id)
    if verification.get("verified"):
        from spst_runtime.action_manifest import ActionManifestLedger

        verification["actions"] = ActionManifestLedger(
            store.path, read_only=True
        ).summarize_receipt(receipt_id)
    return verification


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a Codex chat prompt through the SPST-UEA runtime boundary."
    )
    parser.add_argument("prompt", nargs="?", help="Prompt received from the Codex chat.")
    parser.add_argument("--steps", type=int, default=3)
    parser.add_argument("--event", default="chat_turn")
    parser.add_argument(
        "--profile",
        choices=("auto", "light", "standard", "strict"),
        default="auto",
        help="Select adaptive SPST execution intensity; risk floors cannot be downgraded.",
    )
    parser.add_argument(
        "--session-db",
        default=None,
        help="Override the local chat session database path.",
    )
    parser.add_argument(
        "--memory-db",
        default=None,
        help="Override the local long-term memory database path for routed turns.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Read persisted status without creating or changing database state.",
    )
    parser.add_argument(
        "--verify-receipt",
        default=None,
        metavar="RECEIPT_ID",
        help="Verify one persisted routing receipt through the read-only path.",
    )
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps(get_chat_status(args.session_db), indent=2, sort_keys=True))
        return 0

    if args.verify_receipt:
        print(
            json.dumps(
                verify_chat_receipt(args.verify_receipt, args.session_db),
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if not args.prompt:
        parser.error("prompt is required unless --status or --verify-receipt is used")

    print(
        json.dumps(
            run_chat_turn(
                args.prompt,
                args.steps,
                args.event,
                profile=args.profile,
                session_path=args.session_db,
                memory_path=args.memory_db,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
