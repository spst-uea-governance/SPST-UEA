import platform
import tempfile
import time
from pathlib import Path
from typing import Any

from spst_runtime import __version__
from spst_runtime.events.event import Event
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator


def run_benchmark(dispatches: int = 10) -> dict[str, Any]:
    """Run a deterministic, API-key-free lifecycle benchmark in isolated storage."""
    if dispatches < 1:
        raise ValueError("dispatches must be positive")
    with tempfile.TemporaryDirectory(prefix="spst_benchmark_") as directory:
        orchestrator = RuntimeOrchestrator(db_path=str(Path(directory) / "benchmark.db"))
        state = orchestrator.create_subject("benchmark", role="verifier")
        started = time.perf_counter()
        authorized = []
        for index in range(dispatches):
            state = orchestrator.dispatch(
                state,
                Event(type="benchmark", payload={"prompt": f"deterministic benchmark turn {index}"}),
            )
            authorized.append(state.metadata.get("governance", {}).get("authorized", False))
        elapsed = time.perf_counter() - started
    return {
        "specification_version": "RFC-0003/RFC-0005",
        "runtime_version": __version__,
        "results": {
            "dispatches": dispatches,
            "elapsed_seconds": round(elapsed, 6),
            "dispatches_per_second": round(dispatches / elapsed, 3) if elapsed else 0.0,
            "all_authorized": all(authorized),
            "last_trace": state.metadata.get("last_trace", []),
        },
        "known_limitations": [
            "SQLite writes are serialized per local database path for deterministic safety.",
            "The benchmark uses the API-key-free Codex-mediated local adapter.",
        ],
        "reproducibility": {
            "requires_api_key": False,
            "provider": "codex-mediated-local",
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
    }
