import argparse
import json

from spst_runtime.memory.long_term_memory import LongTermMemoryStore


def clean_generated_files() -> dict:
    """Refuse to treat repository caches or source files as runtime-owned data."""
    return {
        "status": "skipped",
        "reason": "repository_source_cleanup_disabled",
        "removed_files": 0,
        "removed_dirs": 0,
    }


def run_maintenance(memory_path: str | None = None) -> dict:
    memory = LongTermMemoryStore(memory_path)
    expiration_result = memory.expire_stale()
    compact_result = memory.compact()
    clean_result = clean_generated_files()
    return {
        "memory_compaction": compact_result,
        "memory_expiration": expiration_result,
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
