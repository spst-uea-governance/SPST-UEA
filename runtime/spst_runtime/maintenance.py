import argparse
import json
import shutil
from pathlib import Path

from spst_runtime.memory.long_term_memory import LongTermMemoryStore


RUNTIME_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = RUNTIME_DIR.parent


def clean_generated_files() -> dict:
    removed_files = 0
    removed_dirs = 0
    for path in PROJECT_DIR.rglob("*"):
        if not path.exists():
            continue
        if path.is_file() and (path.suffix == ".pyc" or ".pytest_cache" in path.parts):
            path.unlink()
            removed_files += 1
    for directory in sorted(PROJECT_DIR.rglob("__pycache__"), key=lambda item: len(item.parts), reverse=True):
        if directory.exists():
            shutil.rmtree(directory)
            removed_dirs += 1
    pytest_cache = RUNTIME_DIR / ".pytest_cache"
    if pytest_cache.exists():
        shutil.rmtree(pytest_cache)
        removed_dirs += 1
    return {"removed_files": removed_files, "removed_dirs": removed_dirs}


def run_maintenance(memory_path: str | None = None) -> dict:
    memory = LongTermMemoryStore(memory_path)
    compact_result = memory.compact()
    clean_result = clean_generated_files()
    return {
        "memory_compaction": compact_result,
        "generated_cleanup": clean_result,
        "memory_stats": memory.stats(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SPST-UEA local maintenance.")
    parser.parse_args()
    print(json.dumps(run_maintenance(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
