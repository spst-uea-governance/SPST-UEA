"""Measure ARCH-18 evidence-plane latency without mutating live SPST databases."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


RUNTIME_DIR = Path(__file__).resolve().parents[1] / "runtime"
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

from spst_runtime.chat_bridge import (  # noqa: E402
    get_chat_status,
    run_chat_turn,
    verify_chat_receipt,
)
from spst_runtime.project_context import (  # noqa: E402
    load_json,
    load_verified_corpus,
    query_corpus,
    query_verified_corpus,
)
from spst_runtime.repository_identity import capture_repository_identity  # noqa: E402


QUERY = "ARCH-18 low latency repository evidence context cache performance"


def _measure(operation: Callable[[], Any], samples: int) -> dict[str, Any]:
    operation()
    durations: list[float] = []
    results: list[Any] = []
    for _ in range(samples):
        started = time.perf_counter()
        results.append(operation())
        durations.append(time.perf_counter() - started)
    ordered = sorted(durations)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95)))
    return {
        "samples_seconds": durations,
        "median_seconds": statistics.median(durations),
        "p95_seconds": ordered[p95_index],
        "last_result": results[-1],
    }


def _copy_database(source: Path, destination: Path) -> None:
    shutil.copy2(source, destination)
    key_source = source.with_name(f"{source.name}.provenance_key")
    if key_source.is_file():
        shutil.copy2(
            key_source,
            destination.with_name(f"{destination.name}.provenance_key"),
        )


def _measure_route(
    *,
    repository: Path,
    session: Path,
    memory: Path,
    samples: int,
) -> dict[str, Any]:
    durations: list[float] = []
    results: list[dict[str, Any]] = []
    for index in range(samples + 1):
        started = time.perf_counter()
        result = run_chat_turn(
            f"ARCH-18 isolated benchmark sample {index}",
            steps=1,
            profile="standard",
            session_path=str(session),
            memory_path=str(memory),
            repository_root=str(repository),
        )
        duration = time.perf_counter() - started
        if index:
            durations.append(duration)
            results.append(result)
    ordered = sorted(durations)
    return {
        "samples_seconds": durations,
        "median_seconds": statistics.median(durations),
        "p95_seconds": ordered[
            min(len(ordered) - 1, max(0, int(len(ordered) * 0.95)))
        ],
        "last_result": results[-1],
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--receipt-id", required=True)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.samples <= 30:
        raise ValueError("samples_out_of_range")
    return args


def main() -> int:
    args = _arguments()
    repository = args.repository.resolve(strict=True)
    runtime = args.runtime.resolve(strict=True)
    corpus_path = args.corpus.resolve(strict=True)
    session_source = runtime / "spst_chat_state.db"
    memory_source = runtime / "spst_long_term_memory.db"

    measurements = {
        "repository_identity": _measure(
            lambda: capture_repository_identity(repository),
            args.samples,
        ),
        "read_only_status": _measure(
            lambda: get_chat_status(repository_root=str(repository)),
            args.samples,
        ),
        "receipt_verification": _measure(
            lambda: verify_chat_receipt(
                args.receipt_id,
                repository_root=str(repository),
            ),
            args.samples,
        ),
        "project_context_query": _measure(
            lambda: query_corpus(load_json(corpus_path), QUERY),
            args.samples,
        ),
    }
    verified_corpus = load_verified_corpus(corpus_path)
    measurements["project_context_query_warm_verified"] = _measure(
        lambda: query_verified_corpus(verified_corpus, QUERY),
        args.samples,
    )

    with tempfile.TemporaryDirectory(prefix="spst-arch18-benchmark-") as temporary:
        temporary_root = Path(temporary)
        session = temporary_root / "session.db"
        memory = temporary_root / "memory.db"
        _copy_database(session_source, session)
        _copy_database(memory_source, memory)
        measurements["standard_route"] = _measure_route(
            repository=repository,
            session=session,
            memory=memory,
            samples=args.samples,
        )

    repository_head = measurements["repository_identity"]["last_result"]["head_revision"]
    compact: dict[str, Any] = {}
    for name, measurement in measurements.items():
        result = measurement.pop("last_result")
        compact[name] = {
            **measurement,
            "result_contract": {
                "verified": result.get("verified") if isinstance(result, dict) else None,
                "status": result.get("status") if isinstance(result, dict) else None,
                "identity_sha256": (
                    result.get("identity_sha256") if isinstance(result, dict) else None
                ),
                "receipt_verified": (
                    result.get("routing_receipt", {}).get("verification", {}).get("verified")
                    if isinstance(result, dict)
                    else None
                ),
            },
        }

    output = {
        "schema": "spst-arch18-low-latency-benchmark-v1",
        "repository_head": repository_head,
        "samples_per_operation": args.samples,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "measurements": compact,
        "claim_boundary": {
            "same_machine_only": True,
            "provider_latency_included": False,
            "human_latency_included": False,
            "quality_improvement_claimed": False,
        },
    }
    serialized = json.dumps(output, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8", newline="\n")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
