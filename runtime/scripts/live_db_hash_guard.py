from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


SCHEMA_VERSION = "live-db-hash-guard-v1"
PROTECTED_WORKSPACE_NAMES = (
    "spst_cockpit.db",
    "spst_cockpit.db.provenance_key",
    "spst_cockpit.db-wal",
    "spst_cockpit.db-shm",
    "spst_cockpit.db-journal",
)
PROTECTED_DB_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class GuardError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_state(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "sha256": sha256(path),
        "size": stat.st_size,
    }


def protected_workspace_paths(workspace: Path) -> list[Path]:
    return [workspace / name for name in PROTECTED_WORKSPACE_NAMES]


def prepare(database: Path, manifest: Path, workspace: Path) -> dict[str, Any]:
    database = database.resolve()
    manifest = manifest.resolve()
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise GuardError("workspace_not_found")
    if database.exists():
        raise GuardError("protected_database_already_exists")
    if manifest.exists():
        raise GuardError("manifest_already_exists")
    if any(path.exists() for path in protected_workspace_paths(workspace)):
        raise GuardError("protected_workspace_artifact_present_at_prepare")

    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute(
            "CREATE TABLE live_db_hash_guard (id INTEGER PRIMARY KEY, marker TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO live_db_hash_guard(id, marker) VALUES(1, ?)",
            (SCHEMA_VERSION,),
        )
        connection.commit()
    finally:
        connection.close()
    sidecars = [Path(f"{database}{suffix}") for suffix in PROTECTED_DB_SIDECAR_SUFFIXES]
    if any(path.exists() for path in sidecars):
        raise GuardError("protected_database_sidecar_created_during_prepare")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "database": file_state(database),
        "workspace": str(workspace),
        "protected_workspace_paths": [
            str(path) for path in protected_workspace_paths(workspace)
        ],
        "protected_database_sidecars": [str(path) for path in sidecars],
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return {"status": "prepared", **payload}


def verify(manifest: Path) -> tuple[dict[str, Any], bool]:
    manifest = manifest.resolve()
    if not manifest.is_file():
        raise GuardError("manifest_not_found")
    expected = json.loads(manifest.read_text(encoding="utf-8"))
    if expected.get("schema_version") != SCHEMA_VERSION:
        raise GuardError("manifest_schema_invalid")
    expected_database = expected.get("database")
    if not isinstance(expected_database, dict):
        raise GuardError("manifest_database_invalid")
    database_path = expected_database.get("path")
    expected_hash = expected_database.get("sha256")
    expected_size = expected_database.get("size")
    if not isinstance(database_path, str) or not isinstance(expected_hash, str):
        raise GuardError("manifest_database_invalid")
    if not isinstance(expected_size, int):
        raise GuardError("manifest_database_invalid")

    database = Path(database_path)
    reasons: list[str] = []
    actual_database: dict[str, Any] | None = None
    if not database.is_file():
        reasons.append("live_db_missing")
    else:
        actual_database = file_state(database)
        if actual_database["sha256"] != expected_hash:
            reasons.append("live_db_hash_changed")
        if actual_database["size"] != expected_size:
            reasons.append("live_db_size_changed")

    workspace_artifacts = [
        path
        for raw_path in expected.get("protected_workspace_paths", [])
        if isinstance(raw_path, str)
        for path in [Path(raw_path)]
        if path.exists()
    ]
    if workspace_artifacts:
        reasons.append("protected_workspace_artifact_created")
    database_sidecars = [
        path
        for raw_path in expected.get("protected_database_sidecars", [])
        if isinstance(raw_path, str)
        for path in [Path(raw_path)]
        if path.exists()
    ]
    if database_sidecars:
        reasons.append("protected_database_sidecar_created")

    passed = not reasons
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "passed" if passed else "failed",
        "reasons": reasons,
        "database": actual_database,
        "expected_database": expected_database,
        "workspace_artifacts": [str(path) for path in workspace_artifacts],
        "database_sidecars": [str(path) for path in database_sidecars],
    }
    return result, passed


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--db", type=Path, required=True)
    prepare_parser.add_argument("--manifest", type=Path, required=True)
    prepare_parser.add_argument("--workspace", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.db, args.manifest, args.workspace)
            passed = True
        else:
            result, passed = verify(args.manifest)
    except (GuardError, json.JSONDecodeError, OSError, sqlite3.Error) as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "failed",
            "reasons": [str(exc)],
        }
        passed = False
    print(json.dumps(result, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
