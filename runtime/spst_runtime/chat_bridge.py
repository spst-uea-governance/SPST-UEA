import argparse
import json
import sqlite3
from pathlib import Path

from spst_runtime.adaptive_profile import RequestedProfile, select_execution_profile
from spst_runtime.cli import run_dispatch, run_loop
from spst_runtime.chat_session import ChatSessionStore, record_turn, summarize_session
from spst_runtime.context_mediation import ContextBudget, ContextMediator
from spst_runtime.context_review import (
    ContextSemanticReviewError,
    ContextSemanticReviewLedger,
)
from spst_runtime.evidence_context import (
    EvidenceContextError,
    EvidenceContextVerifier,
    reject_unverifiable_artifact_origins,
)
from spst_runtime.evaluation.practical_semantic_study import (
    PracticalSemanticStudyLedger,
)
from spst_runtime.memory.long_term_memory import LongTermMemoryStore
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.repository_identity import capture_repository_identity
from spst_runtime.routing_receipt import (
    ROUTING_RECEIPT_SCHEMA,
    RoutingReceiptLedger,
    empty_memory_origin_index,
)


def preview_context(
    query: str,
    *,
    session_path: str | None = None,
    memory_path: str | None = None,
    repository_root: str | None = None,
    repository_identity: dict | None = None,
    budget: ContextBudget | None = None,
) -> dict:
    """Read accumulated state without mutation and build one model-facing packet."""

    mediator = ContextMediator(budget)
    identity = repository_identity
    if identity is None and repository_root is not None:
        identity = capture_repository_identity(repository_root)
    session_store = ChatSessionStore(session_path, read_only=True)
    resolved_session_path = Path(session_store.path).expanduser()
    try:
        status = get_chat_status(session_path)
        origin_index = (
            RoutingReceiptLedger(str(resolved_session_path), read_only=True).memory_origin_index(
                identity
            )
            if resolved_session_path.is_file()
            else empty_memory_origin_index(
                current_repository_identity_sha256=(
                    identity.get("identity_sha256") if isinstance(identity, dict) else None
                )
            )
        )
    except (FileNotFoundError, PermissionError, sqlite3.DatabaseError) as error:
        origin_index = empty_memory_origin_index(
            f"session_store_unavailable:{type(error).__name__}",
            current_repository_identity_sha256=(
                identity.get("identity_sha256") if isinstance(identity, dict) else None
            ),
        )
        return mediator.unavailable(
            query,
            f"session_store_unavailable:{type(error).__name__}",
            repository_identity=identity,
            origin_index=origin_index,
        )
    default_memory_path = Path(__file__).resolve().parents[1] / "spst_long_term_memory.db"
    resolved_memory_path = Path(memory_path or default_memory_path).expanduser()
    if not resolved_memory_path.is_file():
        return mediator.unavailable(
            query,
            "memory_store_missing",
            repository_identity=identity,
            origin_index=origin_index,
        )
    try:
        store = LongTermMemoryStore(str(resolved_memory_path), read_only=True)
        candidates = store.search(
            query,
            limit=min(100, mediator.budget.max_items * 4),
            min_confidence=mediator.budget.minimum_confidence,
            min_relevance=mediator.budget.minimum_relevance,
            policy_version=mediator.budget.policy_version,
        )
        retrieval_health = store.retrieval_health(
            min_confidence=mediator.budget.minimum_confidence,
            policy_version=mediator.budget.policy_version,
        )
        memory_provenance = store.repository.verify_provenance()
    except (FileNotFoundError, PermissionError, sqlite3.DatabaseError) as error:
        return mediator.unavailable(
            query,
            f"memory_store_unavailable:{type(error).__name__}",
            repository_identity=identity,
            origin_index=origin_index,
        )

    if repository_root is None or identity is None:
        origin_index = reject_unverifiable_artifact_origins(
            origin_index,
            candidates,
            reason="artifact_repository_root_missing",
        )
    else:
        try:
            artifact_verifier = EvidenceContextVerifier(
                repository_root=repository_root,
                session_path=resolved_session_path,
                repository_identity=identity,
            )
            origin_index = artifact_verifier.augment_origin_index(
                origin_index,
                candidates,
            )
            origin_index = ContextSemanticReviewLedger(
                str(resolved_memory_path),
                read_only=True,
            ).apply_gate(origin_index, candidates)
        except (EvidenceContextError, ContextSemanticReviewError):
            origin_index = reject_unverifiable_artifact_origins(
                origin_index,
                candidates,
                reason="artifact_verifier_unavailable",
            )

    return mediator.build(
        query,
        candidates,
        retrieval_health=retrieval_health,
        routing=status.get("routing", {}),
        origin_index=origin_index,
        memory_provenance=memory_provenance,
        session_provenance=status.get("persistence", {}).get("provenance", {}),
        repository_identity=identity,
    )


def run_chat_turn(
    prompt: str,
    steps: int = 3,
    event: str = "chat_turn",
    *,
    profile: RequestedProfile = "auto",
    session_path: str | None = None,
    memory_path: str | None = None,
    repository_root: str | None = None,
) -> dict:
    """Run one Codex-mediated chat turn through SPST-UEA without an API key."""

    repository_identity = (
        capture_repository_identity(repository_root) if repository_root is not None else None
    )
    selected_profile = select_execution_profile(prompt, event=event, requested=profile)
    profile_payload = selected_profile.as_dict()
    if profile_payload.get("memory_mode") == "session_only":
        context_packet = ContextMediator().skipped(prompt)
    else:
        context_packet = preview_context(
            prompt,
            session_path=session_path,
            memory_path=memory_path,
            repository_root=repository_root,
            repository_identity=repository_identity,
            budget=ContextBudget(
                minimum_confidence=max(
                    0.7,
                    float(profile_payload.get("minimum_memory_confidence", 0.7)),
                )
            ),
        )
    loop = run_loop(steps)
    pipeline = run_dispatch(
        event,
        prompt=prompt,
        extra_payload={
            "execution_profile": profile_payload,
            "context_packet": context_packet,
        },
    )
    session = record_turn(
        prompt,
        event,
        pipeline,
        steps=steps,
        session_path=session_path,
        memory_path=memory_path,
        execution_profile=profile_payload,
        repository_identity=repository_identity,
        context_packet=context_packet,
    )

    return {
        "mode": "codex-chat-mediated",
        "requires_api_key": False,
        "input": {
            "prompt": prompt,
            "event": event,
            "steps": steps,
            "execution_profile": profile_payload,
            "repository_bound": repository_identity is not None,
        },
        "runtime": {
            "loop": loop,
            "pipeline": pipeline,
        },
        "execution_profile": profile_payload,
        "context_mediation": context_packet,
        "session": {
            "turn_count": session["turn_count"],
            "goals": session["goals"],
            "evaluation": session["evaluation"],
            "summary": session["summary"],
            "amplification": session["amplification"],
            "memory": session["memory"],
            "latest_audit": session["audit"][-1],
            "execution_profile": profile_payload,
            "context_mediation": session.get("context_mediation", {}),
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
        "receipt_schema_counts": {},
        "context_bound_receipts": 0,
        "repository_context_bound_receipts": 0,
        "context_unbound_receipts": 0,
        "repository_bound_receipts": 0,
        "failed_receipts": 0,
        "receipt_epoch_start_turn": None,
        "eligible_turns": 0,
        "legacy_unreceipted_turns": turn_count,
        "receipt_coverage": None,
        "latest_receipt_id": None,
        "latest_receipt_verified": None,
        "latest_repository_current_match": None,
        "global_codex_task_coverage": None,
        "global_coverage_reason": "codex_task_denominator_unavailable",
        "task_quality_delta": None,
        "task_quality_reason": "paired_outcome_measurement_unavailable",
    }


def get_chat_status(
    session_path: str | None = None, repository_root: str | None = None
) -> dict:
    store = ChatSessionStore(session_path, read_only=True)
    database_exists = Path(store.path).expanduser().is_file()
    state = store.load()
    turn_count = int(state.get("turn_count", 0))
    routing = (
        RoutingReceiptLedger(store.path, read_only=True).summarize(
            turn_count, repository_root=repository_root
        )
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
    semantic_study = _practical_semantic_study_status(
        store.path,
        repository_root=repository_root,
        database_exists=database_exists,
    )
    task_quality = semantic_study.get("task_quality", {})
    semantic_study_count = int(semantic_study.get("study_count", 0) or 0)
    semantic_available = task_quality.get("available") is True
    routing = {
        **routing,
        "task_quality_delta": (
            task_quality.get("paired_delta") if semantic_available else None
        ),
        "task_quality_reason": (
            None
            if semantic_available
            else semantic_study.get("reason")
            if semantic_study_count
            else routing.get("task_quality_reason")
        ),
        "task_quality_pair_count": task_quality.get("pair_count", 0),
        "task_quality_direction": task_quality.get("direction"),
        "task_quality_claim_eligible": task_quality.get("claim_eligible", False),
        "task_quality_study_id": semantic_study.get("selected_study_id"),
        "practical_semantic_study_reason": semantic_study.get("reason"),
    }
    return {
        "mode": state.get("mode"),
        "provider": state.get("provider"),
        "requires_api_key": False,
        "turn_count": turn_count,
        "evaluation": state.get("evaluation", {}),
        "summary": state.get("summary", summarize_session(state)),
        "amplification": state.get("amplification", {}),
        "memory": state.get("memory", {}),
        "context_mediation": state.get("context_mediation", {}),
        "latest_audit": (state.get("audit") or [None])[-1],
        "routing": routing,
        "practical_semantic_study": semantic_study,
        "persistence": {
            "access": "read_only_immutable",
            "source_unchanged": True,
            "state": "present" if database_exists else "missing",
            "provenance": provenance,
        },
    }


def _practical_semantic_study_status(
    database_path: str,
    *,
    repository_root: str | None,
    database_exists: bool,
) -> dict:
    if not database_exists:
        return {
            "schema": "spst-practical-semantic-study-status-v1",
            "study_count": 0,
            "completed_study_count": 0,
            "latest_study_id": None,
            "selected_study_id": None,
            "status": "unavailable",
            "reason": "practical_semantic_study_unavailable",
            "task_quality": {
                "available": False,
                "paired_delta": None,
                "pair_count": 0,
                "uncertainty": None,
                "direction": None,
                "claim_eligible": False,
                "generalization_beyond_registered_corpus": False,
                "automatic_promotion": False,
            },
            "latest": {},
            "read_only_projection": True,
        }
    try:
        repository = SQLiteRepository(database_path, read_only=True)
        return PracticalSemanticStudyLedger(
            repository,
            repository_root=repository_root,
        ).status()
    except (FileNotFoundError, PermissionError, sqlite3.DatabaseError, ValueError) as error:
        return {
            "schema": "spst-practical-semantic-study-status-v1",
            "status": "unavailable",
            "reason": f"practical_semantic_study_unavailable:{type(error).__name__}",
            "task_quality": {
                "available": False,
                "paired_delta": None,
                "pair_count": 0,
                "direction": None,
                "claim_eligible": False,
            },
            "read_only_projection": True,
        }


def verify_chat_receipt(
    receipt_id: str,
    session_path: str | None = None,
    repository_root: str | None = None,
) -> dict:
    store = ChatSessionStore(session_path, read_only=True)
    if not Path(store.path).expanduser().is_file():
        return {"verified": False, "receipt_id": receipt_id, "reason": "receipt_missing"}
    verification = RoutingReceiptLedger(store.path, read_only=True).verify(
        receipt_id, repository_root=repository_root
    )
    if verification.get("verified"):
        from spst_runtime.action_manifest import ActionManifestLedger
        from spst_runtime.execution_coverage import GovernedExecutionCoverageLedger

        verification["actions"] = ActionManifestLedger(
            store.path, read_only=True
        ).summarize_receipt(receipt_id)
        verification["execution_coverage"] = GovernedExecutionCoverageLedger(
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
        "--repository-root",
        default=None,
        help=(
            "Bind a routed turn to the exact Git HEAD and canonical worktree state; "
            "on read-only verification, compare that recorded identity with this repository."
        ),
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
    parser.add_argument(
        "--context-preview",
        default=None,
        metavar="QUERY",
        help="Build a read-only, bounded context packet without recording a chat turn.",
    )
    parser.add_argument("--context-max-items", type=int, default=5)
    parser.add_argument("--context-max-chars", type=int, default=4_000)
    args = parser.parse_args(argv)

    selected_read_only_modes = sum(
        [bool(args.status), bool(args.verify_receipt), args.context_preview is not None]
    )
    if selected_read_only_modes > 1:
        parser.error("--status, --verify-receipt, and --context-preview are mutually exclusive")

    if args.status:
        print(
            json.dumps(
                get_chat_status(args.session_db, args.repository_root),
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.verify_receipt:
        print(
            json.dumps(
                verify_chat_receipt(
                    args.verify_receipt,
                    args.session_db,
                    args.repository_root,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.context_preview is not None:
        print(
            json.dumps(
                preview_context(
                    args.context_preview,
                    session_path=args.session_db,
                    memory_path=args.memory_db,
                    repository_root=args.repository_root,
                    budget=ContextBudget(
                        max_items=args.context_max_items,
                        max_total_chars=args.context_max_chars,
                        max_item_chars=min(1_500, args.context_max_chars),
                    ),
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if not args.prompt:
        parser.error(
            "prompt is required unless --status, --verify-receipt, or --context-preview is used"
        )

    print(
        json.dumps(
            run_chat_turn(
                args.prompt,
                args.steps,
                args.event,
                profile=args.profile,
                session_path=args.session_db,
                memory_path=args.memory_db,
                repository_root=args.repository_root,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
