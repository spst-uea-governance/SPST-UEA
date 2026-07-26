import argparse
import json
from pathlib import Path

from spst_runtime.context_review import ContextSemanticReviewLedger
from spst_runtime.context_utility import ContextUtilityAttributionLedger
from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.evaluation.paired_quality import PairedQualityEvidenceLedger
from spst_runtime.evidence_context import EvidenceContextVerifier
from spst_runtime.persistence.sqlite_repository import SQLiteRepository


def _default_session_path() -> str:
    return str(Path(__file__).resolve().parents[1] / "spst_chat_state.db")


def _default_memory_path() -> str:
    return str(Path(__file__).resolve().parents[1] / "spst_long_term_memory.db")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Derive bounded context utility from reviewed paired evidence."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("attribute", "get", "status"):
        command = commands.add_parser(name)
        command.add_argument("--repository-root", required=True)
        command.add_argument("--session-db", default=_default_session_path())
        command.add_argument("--memory-db", default=_default_memory_path())
        command.add_argument("--evaluation-db", default=_default_session_path())
        if name == "attribute":
            command.add_argument("--evaluation-id", required=True)
            command.add_argument("--artifact-sha256", required=True)
        elif name == "get":
            command.add_argument("--attribution-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    read_only = arguments.command != "attribute"
    repository = SQLiteRepository(arguments.evaluation_db, read_only=read_only)
    corpus = OperationalEvaluationCorpus(repository)
    paired = PairedQualityEvidenceLedger(repository, corpus)
    semantic_reviews = ContextSemanticReviewLedger(
        arguments.memory_db,
        read_only=True,
    )
    verifier = EvidenceContextVerifier(
        repository_root=arguments.repository_root,
        session_path=arguments.session_db,
    )
    ledger = ContextUtilityAttributionLedger(
        repository,
        paired,
        semantic_reviews,
        verifier,
    )
    if arguments.command == "attribute":
        output = ledger.attribute(
            {
                "evaluation_id": arguments.evaluation_id,
                "artifact_sha256": arguments.artifact_sha256,
            }
        )
    elif arguments.command == "get":
        output = ledger.get(arguments.attribution_id) or {
            "status": "not_found",
            "id": arguments.attribution_id,
        }
    else:
        output = {"coverage": ledger.coverage(), "history": ledger.history()}
    print(json.dumps(output, sort_keys=True, indent=2))
    return 0 if output.get("status") != "blocked" else 2


if __name__ == "__main__":
    raise SystemExit(main())
