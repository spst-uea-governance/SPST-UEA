import argparse
import json
import os
from pathlib import Path
from typing import Any

from spst_runtime.evaluation.operational_corpus import OperationalEvaluationCorpus
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.process_transport import ProcessIsolatedTransportAdapter
from spst_runtime.real_paired_outcome import RealPairedOutcomeProgram


def _json_file(path: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("process_recovery_input_file_invalid") from error
    if not isinstance(value, dict):
        raise ValueError("process_recovery_input_file_invalid")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Execute or recover a paired program in a fresh client process."
    )
    parser.add_argument("command", choices=("execute", "recover", "status"))
    parser.add_argument("--database", required=True)
    parser.add_argument("--provider-db", required=True)
    parser.add_argument("--provider-key-file")
    parser.add_argument("--program-id", required=True)
    parser.add_argument("--intervention-file", required=True)
    parser.add_argument("--authority-file")
    parser.add_argument("--authority-trust-file")
    parser.add_argument("--commit-marker")
    parser.add_argument("--response-delay-ms", type=int, default=0)
    parser.add_argument("--failure-mode", choices=("before_commit_exit",))
    args = parser.parse_args(argv)

    try:
        repository = SQLiteRepository(args.database)
        corpus = OperationalEvaluationCorpus(repository)
        adapter = ProcessIsolatedTransportAdapter(
            args.provider_db,
            provider_authentication_key_file=args.provider_key_file,
            recovery_authority_trust_anchor=(
                _json_file(args.authority_trust_file)
                if args.authority_trust_file is not None
                else None
            ),
            first_response_delay_ms=args.response_delay_ms,
            first_commit_marker=args.commit_marker,
            first_failure_mode=args.failure_mode,
        )
        program = RealPairedOutcomeProgram(repository, corpus, adapter)
        intervention = _json_file(args.intervention_file)
        result: dict[str, Any] | None
        if args.command == "execute":
            result = program.execute(
                args.program_id,
                context_intervention=intervention,
            )
        elif args.command == "recover":
            if args.authority_file is None:
                raise ValueError("process_recovery_authority_file_required")
            result = program.recover(
                args.program_id,
                context_intervention=intervention,
                recovery_authority=_json_file(args.authority_file),
            )
        else:
            result = program.get(args.program_id)
            if result is None:
                raise ValueError("process_recovery_program_not_found")
        if result is None:
            raise ValueError("process_recovery_result_missing")
        response = {
            "schema": "spst-process-recovery-bridge-result-v1",
            "client_pid": os.getpid(),
            "command": args.command,
            "result": result,
        }
    except (OSError, ValueError) as error:
        print(
            json.dumps(
                {
                    "schema": "spst-process-recovery-bridge-error-v1",
                    "status": "rejected",
                    "reason": str(error),
                },
                sort_keys=True,
            )
        )
        return 2

    print(json.dumps(response, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
