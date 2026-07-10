"""Run the reproducible local SPST-UEA benchmark from the repository root."""

import json
import sys
from pathlib import Path


RUNTIME_DIR = Path(__file__).resolve().parents[1] / "runtime"
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

from spst_runtime.benchmark import run_benchmark


if __name__ == "__main__":
    print(json.dumps(run_benchmark(), indent=2, sort_keys=True))
