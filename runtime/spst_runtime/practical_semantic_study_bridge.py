from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from spst_runtime.evaluation.practical_semantic_study import (
    CORRECTION_POLICY,
    EXECUTION_AUTHORITY_SCHEMA,
    REVIEW_POLICY,
    PracticalSemanticStudyLedger,
)
from spst_runtime.evaluation.practical_semantic_runner import (
    PracticalSemanticRunnerError,
    PracticalSemanticStudyRunner,
    treatment_instructions,
)
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.providers.codex_cli_adapter import CodexCliAdapter
from spst_runtime.repository_identity import capture_repository_identity


def _default_database_path() -> str:
    return str(Path(__file__).resolve().parents[1] / "spst_chat_state.db")


def _default_task_pack_path() -> str:
    return str(Path(__file__).resolve().parent / "evaluation" / "novel_practical_tasks_v1.json")


def _default_treatment_context_path() -> str:
    return str(
        Path(__file__).resolve().parent
        / "evaluation"
        / "practical_semantic_treatment_context_v1.json"
    )


def _load_json(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"json_object_required:{path}")
    return value


def _write_json(value: object) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    encoding = getattr(sys.stdout, "encoding", None)
    if encoding is not None:
        try:
            rendered.encode(encoding, errors="strict")
        except (LookupError, UnicodeEncodeError):
            rendered = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Operate the receipt-bound 30-to-50 pair practical semantic study."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate-task-pack")
    validate.add_argument("--task-pack", default=_default_task_pack_path())

    preregister = commands.add_parser("preregister")
    preregister.add_argument("--evaluation-db", default=_default_database_path())
    preregister.add_argument("--repository-root", required=True)
    preregister.add_argument("--task-pack", default=_default_task_pack_path())
    preregister.add_argument(
        "--treatment-context",
        default=_default_treatment_context_path(),
    )
    preregister.add_argument("--generator-id", required=True)

    for name in ("authorize-execution", "record-pair", "review", "correct-review"):
        command = commands.add_parser(name)
        command.add_argument("--evaluation-db", default=_default_database_path())
        command.add_argument("--repository-root", required=True)
        command.add_argument("--study-id", required=True)
        command.add_argument("--payload", required=True)

    blind = commands.add_parser("blind-artifact")
    blind.add_argument("--evaluation-db", default=_default_database_path())
    blind.add_argument("--repository-root", required=True)
    blind.add_argument("--study-id", required=True)

    finalize = commands.add_parser("finalize")
    finalize.add_argument("--evaluation-db", default=_default_database_path())
    finalize.add_argument("--repository-root", required=True)
    finalize.add_argument("--study-id", required=True)

    for name in ("preflight", "execute"):
        command = commands.add_parser(name)
        command.add_argument("--evaluation-db", required=True)
        command.add_argument("--memory-db", required=True)
        command.add_argument("--repository-root", required=True)
        command.add_argument("--study-id", required=True)
        command.add_argument("--treatment-context", default=_default_treatment_context_path())
        command.add_argument("--model", default="gpt-5.6-sol")

    authority = commands.add_parser("authority-template")
    authority.add_argument("--evaluation-db", required=True)
    authority.add_argument("--repository-root", required=True)
    authority.add_argument("--study-id", required=True)
    authority.add_argument("--authority-label", required=True)

    status = commands.add_parser("status")
    status.add_argument("--evaluation-db", default=_default_database_path())
    status.add_argument("--repository-root")
    return parser


def _preregistration_payload(arguments: argparse.Namespace) -> dict[str, Any]:
    task_pack = _load_json(arguments.task_pack)
    tasks = task_pack.get("tasks")
    task_count = len(tasks) if isinstance(tasks, list) else 0
    raw_treatment_context = Path(arguments.treatment_context).read_bytes()
    treatment_value = json.loads(raw_treatment_context.decode("utf-8"))
    instructions = treatment_instructions(treatment_value)
    return {
        "task_pack": task_pack,
        "generator": {
            "generator_id": arguments.generator_id,
            "generator_kind": "model_adapter",
        },
        "intervention": {
            "schema": "spst-practical-semantic-intervention-v1",
            "context_sha256": hashlib.sha256(raw_treatment_context).hexdigest(),
            "model_instructions_sha256": hashlib.sha256(
                instructions.encode("utf-8")
            ).hexdigest(),
            "context_kind": "spst_process_guidance",
            "task_specific_answers_absent_attested": True,
        },
        "review_policy": REVIEW_POLICY,
        "correction_policy": CORRECTION_POLICY,
        "stop_rule": {
            "target_pair_count": task_count,
            "minimum_pair_count": 30,
            "maximum_pair_count": 50,
            "optional_stopping_permitted": False,
            "automatic_retry_permitted": False,
            "partial_execution_claim_eligible": False,
        },
        "repository_identity": capture_repository_identity(arguments.repository_root),
    }


def _authority_template(study: dict[str, Any], authority_label: str) -> dict[str, Any]:
    return {
        "schema": EXECUTION_AUTHORITY_SCHEMA,
        "decision": "authorized",
        "authority_label": authority_label,
        "study_id": study["id"],
        "study_sha256": study["study_sha256"],
        "repository_identity_sha256": study["repository_identity"]["identity_sha256"],
        "task_pack_sha256": study["task_pack"]["task_pack_sha256"],
        "intervention_sha256": study["intervention"]["context_sha256"],
        "external_provider_calls_authorized": True,
        "chatgpt_plan_usage_authorized": True,
        "paid_provider_calls_authorized": False,
        "maximum_adapter_invocations": 2 * study["stop_rule"]["target_pair_count"],
        "per_call_no_paid_guard_required": True,
        "optional_stopping_permitted": False,
        "automatic_retry_permitted": False,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "validate-task-pack":
        repository = SQLiteRepository(":memory:")
        ledger = PracticalSemanticStudyLedger(repository, nonce_factory=lambda: "validation")
        output = ledger.validate_task_pack(_load_json(arguments.task_pack))
    else:
        read_only = arguments.command in {
            "status",
            "blind-artifact",
            "authority-template",
            "preflight",
        }
        repository = SQLiteRepository(arguments.evaluation_db, read_only=read_only)
        ledger = PracticalSemanticStudyLedger(
            repository,
            repository_root=arguments.repository_root,
        )
        if arguments.command == "preregister":
            output = ledger.preregister(_preregistration_payload(arguments))
        elif arguments.command == "authority-template":
            study = ledger.get(arguments.study_id)
            output = (
                _authority_template(study, arguments.authority_label)
                if study is not None
                else {"status": "blocked", "reason": "practical_semantic_study_not_found"}
            )
        elif arguments.command == "authorize-execution":
            output = ledger.authorize_execution(
                arguments.study_id,
                _load_json(arguments.payload),
            )
        elif arguments.command == "record-pair":
            output = ledger.record_pair(arguments.study_id, _load_json(arguments.payload))
        elif arguments.command == "blind-artifact":
            output = ledger.blind_artifact(arguments.study_id)
        elif arguments.command == "review":
            output = ledger.submit_review(arguments.study_id, _load_json(arguments.payload))
        elif arguments.command == "correct-review":
            output = ledger.correct_review(arguments.study_id, _load_json(arguments.payload))
        elif arguments.command == "finalize":
            output = ledger.finalize(arguments.study_id)
        elif arguments.command in {"preflight", "execute"}:
            if Path(arguments.evaluation_db).resolve() == Path(_default_database_path()).resolve():
                output = {
                    "status": "blocked",
                    "reason": "practical_semantic_isolated_evaluation_db_required",
                }
            else:
                runner = PracticalSemanticStudyRunner(
                    ledger,
                    CodexCliAdapter(
                        model=arguments.model,
                        working_directory=arguments.repository_root,
                    ),
                    repository_root=arguments.repository_root,
                    session_path=arguments.evaluation_db,
                    memory_path=arguments.memory_db,
                    treatment_context_path=arguments.treatment_context,
                )
                try:
                    output = (
                        runner.preflight(arguments.study_id)
                        if arguments.command == "preflight"
                        else runner.execute_sync(arguments.study_id)
                    )
                except (PracticalSemanticRunnerError, OSError, ValueError) as error:
                    output = {"status": "blocked", "reason": str(error)}
        else:
            output = ledger.status()
    _write_json(output)
    return 2 if output.get("status") == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
