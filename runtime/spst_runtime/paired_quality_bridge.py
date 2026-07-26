import argparse
import json
from pathlib import Path
from typing import Any

from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.evaluation.paired_quality import PairedQualityEvidenceLedger
from spst_runtime.persistence.sqlite_repository import SQLiteRepository


def _default_evaluation_path() -> str:
    return str(Path(__file__).resolve().parents[1] / "spst_chat_state.db")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or append a human review to paired quality evidence."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("get", "status", "review"):
        command = commands.add_parser(name)
        command.add_argument("--evaluation-db", default=_default_evaluation_path())
        if name in {"get", "review"}:
            command.add_argument("--evaluation-id", required=True)
        if name == "review":
            command.add_argument("--reviewer-id", required=True)
            command.add_argument(
                "--decision",
                choices=("accepted", "rejected"),
                required=True,
            )
            command.add_argument("--scoring-artifact-digest", required=True)
            command.add_argument("--blind-review-surface-sha256")
            command.add_argument("--arm-mapping-not-accessed", action="store_true")
            command.add_argument(
                "--reviewer-independence-attested",
                action="store_true",
            )
    return parser


def _review_payload(
    arguments: argparse.Namespace,
    evaluation: dict[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "reviewer_id": arguments.reviewer_id,
        "reviewer_kind": "human",
        "review_scope": PairedQualityEvidenceLedger.REVIEW_SCOPE,
        "decision": arguments.decision,
        "scoring_artifact_digest": arguments.scoring_artifact_digest,
    }
    if evaluation and evaluation.get("provider_observation_required") is True:
        payload.update(
            {
                "blind_review_surface_sha256": arguments.blind_review_surface_sha256,
                "arm_mapping_not_accessed": arguments.arm_mapping_not_accessed,
                "reviewer_independence_attested": (
                    arguments.reviewer_independence_attested
                ),
            }
        )
    return payload


def _failed(output: dict[str, Any]) -> bool:
    operation = output.get("operation")
    return output.get("status") in {"blocked", "not_found"} or (
        isinstance(operation, dict) and operation.get("status") == "blocked"
    )


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    read_only = arguments.command != "review"
    repository = SQLiteRepository(arguments.evaluation_db, read_only=read_only)
    ledger = PairedQualityEvidenceLedger(
        repository,
        OperationalEvaluationCorpus(repository),
    )
    if arguments.command == "get":
        output = ledger.get(arguments.evaluation_id) or {
            "status": "not_found",
            "id": arguments.evaluation_id,
        }
    elif arguments.command == "status":
        output = {"coverage": ledger.coverage(), "history": ledger.history()}
    else:
        evaluation = ledger.get(arguments.evaluation_id)
        output = ledger.review(
            arguments.evaluation_id,
            _review_payload(arguments, evaluation),
        )
    print(json.dumps(output, sort_keys=True, indent=2))
    return 2 if _failed(output) else 0


if __name__ == "__main__":
    raise SystemExit(main())
