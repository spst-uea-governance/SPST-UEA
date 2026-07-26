import argparse
import json
from pathlib import Path
from typing import Any

from spst_runtime.context_review import (
    SEMANTIC_REVIEW_SCOPE,
    ContextSemanticReviewError,
    ContextSemanticReviewLedger,
)
from spst_runtime.evidence_context import (
    SUPPORTED_ARTIFACT_KINDS,
    EvidenceContextCompiler,
    EvidenceContextError,
)


def _default_session_path() -> str:
    return str(Path(__file__).resolve().parents[1] / "spst_chat_state.db")


def _default_memory_path() -> str:
    return str(Path(__file__).resolve().parents[1] / "spst_long_term_memory.db")


def _compiler(arguments: argparse.Namespace, *, read_only: bool = False) -> EvidenceContextCompiler:
    return EvidenceContextCompiler(
        repository_root=arguments.repository_root,
        session_path=arguments.session_db,
        memory_path=arguments.memory_db,
        read_only=read_only,
    )


def _compile_arguments(arguments: argparse.Namespace) -> dict[str, Any]:
    return {
        "producer_receipt_id": arguments.producer_receipt,
        "artifact_kind": arguments.kind,
        "title": arguments.title,
        "statement": arguments.statement,
        "tags": arguments.tag,
        "confidence": arguments.confidence,
        "ttl_seconds": arguments.ttl_seconds,
        "supersedes_artifact_sha256": arguments.supersedes,
    }


def _bounded_result(result: dict[str, Any]) -> dict[str, Any]:
    artifact = result["artifact"]
    record = result["memory_record"]
    return {
        "schema": result["schema"],
        "artifact": {
            "schema": artifact["schema"],
            "artifact_sha256": artifact["artifact_sha256"],
            "artifact_kind": artifact["artifact_kind"],
            "source_kind": artifact["source"]["kind"],
            "source_sha256": artifact["source"]["source_sha256"],
            "producer_receipt_id": artifact["producer"]["receipt_id"],
            "policy": artifact["policy"],
            "lifecycle": artifact["lifecycle"],
            "authority": artifact["authority"],
            "semantic_claim": artifact["semantic_claim"],
        },
        "memory_record": {
            "id": record["id"],
            "kind": record["kind"],
            "source": record["source"],
            "confidence": record["confidence"],
            "policy_version": record["policy_version"],
            "expires_at": record["expires_at"],
            "status": record["status"],
        },
        "verification": result["verification"],
        "memory_provenance": result["memory_provenance"],
    }


def _add_store_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--session-db", default=_default_session_path())
    parser.add_argument("--memory-db", default=_default_memory_path())


def _add_compile_arguments(parser: argparse.ArgumentParser) -> None:
    _add_store_arguments(parser)
    parser.add_argument("--producer-receipt", required=True)
    parser.add_argument("--kind", choices=sorted(SUPPORTED_ARTIFACT_KINDS), required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--statement", required=True)
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--confidence", type=float, default=0.9)
    parser.add_argument("--ttl-seconds", type=int, default=7_776_000)
    parser.add_argument("--supersedes")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile source-bound evidence into governed SPST context artifacts."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    file_parser = commands.add_parser("compile-file")
    _add_compile_arguments(file_parser)
    file_parser.add_argument("--source-file", required=True)

    commit_parser = commands.add_parser("compile-commit")
    _add_compile_arguments(commit_parser)
    commit_parser.add_argument("--revision", required=True)

    action_parser = commands.add_parser("compile-action")
    _add_compile_arguments(action_parser)
    action_parser.add_argument("--action-id", required=True)

    verify_parser = commands.add_parser("verify")
    _add_store_arguments(verify_parser)
    verify_parser.add_argument("--record-id", required=True)

    review_parser = commands.add_parser("review")
    _add_store_arguments(review_parser)
    review_parser.add_argument("--record-id", required=True)
    review_parser.add_argument("--reviewer-id", required=True)
    review_parser.add_argument("--decision", choices=("supported", "unsupported"), required=True)
    review_parser.add_argument("--artifact-sha256", required=True)
    review_parser.add_argument("--source-sha256", required=True)
    review_parser.add_argument("--review-note", required=True)

    intervention_parser = commands.add_parser("intervention")
    _add_store_arguments(intervention_parser)
    intervention_parser.add_argument("--artifact-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command in {"verify", "review", "intervention"}:
            compiler = _compiler(arguments, read_only=True)
            reviews = ContextSemanticReviewLedger(
                arguments.memory_db,
                read_only=arguments.command != "review",
            )
            if arguments.command == "intervention":
                record = reviews.find_artifact_record(arguments.artifact_sha256)
            else:
                matches = [
                    record
                    for record in compiler.memory.list_all()
                    if record.id == arguments.record_id
                ]
                if len(matches) != 1:
                    raise EvidenceContextError(
                        "artifact_record_missing"
                        if not matches
                        else "artifact_record_ambiguous"
                    )
                record = matches[0]
            structural = compiler.verifier.verify_record(record)
            if structural.get("verified") is not True:
                raise EvidenceContextError(str(structural.get("reason")))
            if arguments.command == "verify":
                output = {
                    **structural,
                    "semantic_support": reviews.verify_record(record, structural),
                }
            elif arguments.command == "review":
                output = reviews.review(
                    record,
                    structural,
                    {
                        "reviewer_id": arguments.reviewer_id,
                        "reviewer_kind": "human",
                        "review_scope": SEMANTIC_REVIEW_SCOPE,
                        "decision": arguments.decision,
                        "artifact_sha256": arguments.artifact_sha256,
                        "source_sha256": arguments.source_sha256,
                        "review_note": arguments.review_note,
                    },
                )
            else:
                output = reviews.build_intervention(record, structural)
        else:
            compiler = _compiler(arguments)
            common = _compile_arguments(arguments)
            if arguments.command == "compile-file":
                result = compiler.compile_repository_file(
                    **common,
                    relative_path=arguments.source_file,
                )
            elif arguments.command == "compile-commit":
                result = compiler.compile_git_commit(
                    **common,
                    revision=arguments.revision,
                )
            else:
                result = compiler.compile_action_result(
                    **common,
                    action_id=arguments.action_id,
                )
            output = _bounded_result(result)
    except (EvidenceContextError, ContextSemanticReviewError) as error:
        print(
            json.dumps(
                {"ok": False, "reason": str(error)},
                sort_keys=True,
                indent=2,
            )
        )
        return 2

    print(json.dumps(output, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
